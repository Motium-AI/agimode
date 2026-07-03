#!/usr/bin/env bash
#
# run-agimode-codex.sh — agimode fleet worker: a single headless GPT-5.5 codex
# coding lane, scoped to ONE git worktree.
#
# Adapted from run-fable-codex.sh. Differences:
#   - artifact root is <workdir>/.claude/agimode (not .claude/fable)
#   - env prefix AGIMODE_CODEX_* (not FABLE_CODEX_*)
#   - the artifact stamp carries a PID suffix so two runs in the same worktree
#     within one second cannot collide (red-team finding)
#   - optional --workdir <dir> pins the git toplevel explicitly instead of
#     deriving it from the spec's directory (the fleet engine drops the spec
#     under <worktree>/.claude/, which the dirty-tree guard excludes)
#
# Order is load-bearing:
#   (1) preflight (all fail-closed, distinct exit codes)
#   (2) artifact dir + running status.json (only after preflight passes)
#   (3) EXIT trap set, then launch codex under `timeout`
#   (4) trap writes terminal status; capture working-tree diff vs base_commit
#
# Sandbox is PINNED to workspace-write on the CLI — never inherited from user
# config. No --ephemeral, no --skip-git-repo-check. codex NEVER commits — the
# fleet engine (coordinator) owns commit + integration.

set -euo pipefail

# --- Configurable via env -------------------------------------------------
CODEX_BIN="${AGIMODE_CODEX_BIN:-codex}"
MODEL="${AGIMODE_CODEX_MODEL:-gpt-5.5}"
REASONING_EFFORT="${AGIMODE_CODEX_REASONING_EFFORT:-xhigh}"
DEFAULT_TIMEOUT_SEC="${AGIMODE_CODEX_TIMEOUT_SEC:-1800}"
APPROVAL_POLICY="never"
SANDBOX="workspace-write"

# --- Exit codes (distinct, fail-closed) -----------------------------------
EX_USAGE=2          # bad args
EX_SPEC=3           # spec file missing / unreadable
EX_NO_CODEX=4       # codex binary not on PATH
EX_NO_LOGIN=5       # codex not authenticated
EX_NOT_GIT=6        # not a git repo
EX_DEFAULT_BRANCH=7 # current branch is the repo default branch
EX_DIRTY=8          # dirty tracked tree (excluding .claude/)

usage() {
    cat <<'EOF'
Usage:
  run-agimode-codex.sh [--fast] [--timeout-sec N] [--workdir DIR] [--print-command] <spec-file>

Options:
  --fast              Request priority service tier (-c service_tier="priority").
  --timeout-sec N     Wall-clock timeout in seconds (default: $AGIMODE_CODEX_TIMEOUT_SEC or 1800).
  --workdir DIR       Pin the git toplevel explicitly (default: derived from the spec's dir).
  --print-command     Print the composed codex argv and exit 0 without running.
  -h, --help          Show this help.

Env overrides:
  AGIMODE_CODEX_BIN, AGIMODE_CODEX_MODEL, AGIMODE_CODEX_REASONING_EFFORT,
  AGIMODE_CODEX_TIMEOUT_SEC
EOF
}

fast=false
print_mode=false
timeout_sec="$DEFAULT_TIMEOUT_SEC"
workdir_override=""
spec_arg=""

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --fast) fast=true; shift ;;
        --timeout-sec)
            if [[ "$#" -lt 2 ]]; then
                echo "[agimode-codex] --timeout-sec requires a value." >&2
                exit "$EX_USAGE"
            fi
            timeout_sec="$2"; shift 2 ;;
        --workdir)
            if [[ "$#" -lt 2 ]]; then
                echo "[agimode-codex] --workdir requires a value." >&2
                exit "$EX_USAGE"
            fi
            workdir_override="$2"; shift 2 ;;
        --print-command) print_mode=true; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        -*)
            echo "[agimode-codex] Unknown option: $1" >&2
            usage >&2
            exit "$EX_USAGE" ;;
        *)
            if [[ -n "$spec_arg" ]]; then
                echo "[agimode-codex] Only one spec file is supported per invocation." >&2
                usage >&2
                exit "$EX_USAGE"
            fi
            spec_arg="$1"; shift ;;
    esac
done

if [[ -n "${1:-}" && -z "$spec_arg" ]]; then
    spec_arg="$1"
fi

if [[ -z "$spec_arg" ]]; then
    echo "[agimode-codex] A spec file is required." >&2
    usage >&2
    exit "$EX_USAGE"
fi

if ! [[ "$timeout_sec" =~ ^[0-9]+$ ]]; then
    echo "[agimode-codex] --timeout-sec must be a positive integer (got: $timeout_sec)." >&2
    exit "$EX_USAGE"
fi

# ==========================================================================
# (1) PREFLIGHT — all fail-closed, distinct exit codes, one-line errors.
# ==========================================================================

if [[ ! -f "$spec_arg" || ! -r "$spec_arg" ]]; then
    echo "[agimode-codex] Spec file not found or unreadable: $spec_arg" >&2
    exit "$EX_SPEC"
fi
SPEC_ABS="$(cd "$(dirname "$spec_arg")" && pwd)/$(basename "$spec_arg")"
spec_filename="$(basename "$spec_arg")"

if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
    echo "[agimode-codex] codex binary not found on PATH: $CODEX_BIN" >&2
    exit "$EX_NO_CODEX"
fi

if ! "$CODEX_BIN" login status >/dev/null 2>&1; then
    echo "[agimode-codex] codex is not authenticated. Run: codex login" >&2
    exit "$EX_NO_LOGIN"
fi

# git repo — resolve toplevel from --workdir if given, else from the spec's dir.
if [[ -n "$workdir_override" ]]; then
    resolve_dir="$workdir_override"
else
    resolve_dir="$(dirname "$SPEC_ABS")"
fi
if ! WORKDIR="$(git -C "$resolve_dir" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "[agimode-codex] Not inside a git repository: $resolve_dir" >&2
    exit "$EX_NOT_GIT"
fi

# Compose the codex argv. Sandbox PINNED on the CLI — never inherited.
build_codex_cmd() {
    cmd=(
        "$CODEX_BIN"
        exec
        --model "$MODEL"
        -c "model_reasoning_effort=\"$REASONING_EFFORT\""
        -c "approval_policy=\"$APPROVAL_POLICY\""
        --sandbox "$SANDBOX"
        -C "$WORKDIR"
        --output-last-message "$1"
    )
    if [[ "$fast" == true ]]; then
        cmd+=(-c "service_tier=\"priority\"")
    fi
    local prompt
    read -r -d '' prompt <<EOF || true
Read $SPEC_ABS and execute the work packet exactly. Stay within the spec's files-in-scope, never touch forbidden paths, run the named validation command, and do not commit.
EOF
    cmd+=("$prompt")
}

if [[ "$print_mode" == true ]]; then
    build_codex_cmd "<ART>/codex-summary.md"
    for part in "${cmd[@]}"; do printf '%q ' "$part"; done
    printf '\n'
    exit 0
fi

resolve_default_branch() {
    local ref
    if ref="$(git -C "$WORKDIR" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null)"; then
        echo "${ref##*/}"; return 0
    fi
    if git -C "$WORKDIR" show-ref --verify --quiet refs/remotes/origin/main; then echo "main"; return 0; fi
    if git -C "$WORKDIR" show-ref --verify --quiet refs/remotes/origin/master; then echo "master"; return 0; fi
    if git -C "$WORKDIR" show-ref --verify --quiet refs/heads/main; then echo "main"; return 0; fi
    if git -C "$WORKDIR" show-ref --verify --quiet refs/heads/master; then echo "master"; return 0; fi
    echo "main"
}

current_branch="$(git -C "$WORKDIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")"
default_branch="$(resolve_default_branch)"
if [[ "$current_branch" == "$default_branch" ]]; then
    echo "[agimode-codex] Refusing to run on the default branch ('$current_branch'). Use a worktree branch." >&2
    exit "$EX_DEFAULT_BRANCH"
fi

# Dirty-tree check that EXCLUDES paths under .claude/ — our own spec/artifacts
# must never trip our own guard.
if [[ -n "$(git -C "$WORKDIR" status --porcelain -- . ':(exclude).claude' 2>/dev/null)" ]]; then
    echo "[agimode-codex] Refusing to run: working tree has uncommitted changes (outside .claude/)." >&2
    exit "$EX_DIRTY"
fi

# ==========================================================================
# (2) Artifact dir + running status.json — only AFTER preflight passes.
# ==========================================================================

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
utc_stamp() { date -u +%Y%m%dT%H%M%SZ; }

slug="$(printf '%s' "$spec_filename" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
[[ -z "$slug" ]] && slug="spec"

# PID suffix guarantees uniqueness for same-second + same-slug runs.
ART="$WORKDIR/.claude/agimode/$(utc_stamp)-$slug-$$"
mkdir -p "$ART"
cp "$SPEC_ABS" "$ART/spec.md"

base_commit="$(git -C "$WORKDIR" rev-parse HEAD)"
started_at="$(utc_now)"
SUMMARY="$ART/codex-summary.md"
LOG="$ART/codex.log"
STATUS="$ART/status.json"
DIFF="$ART/diff.patch"

write_status() {
    local st="$1" ec="${2:-}" ended="${3:-}"
    local tmp="$STATUS.tmp.$$"
    {
        printf '{\n'
        printf '  "status": "%s",\n' "$st"
        printf '  "fast": %s,\n' "$fast"
        printf '  "base_commit": "%s",\n' "$base_commit"
        printf '  "artifact_dir": "%s",\n' "$ART"
        printf '  "started_at": "%s"' "$started_at"
        if [[ -n "$ec" ]]; then printf ',\n  "exit_code": %s' "$ec"; fi
        if [[ -n "$ended" ]]; then printf ',\n  "ended_at": "%s"' "$ended"; fi
        printf '\n}\n'
    } > "$tmp"
    mv -f "$tmp" "$STATUS"
}

write_status "running"

# Echo the artifact dir as the FIRST stdout line so the fleet engine can find
# the status.json deterministically (status path = $ART/status.json).
echo "AGIMODE_ART=$ART"

# ==========================================================================
# (3)/(4) EXIT trap (set BEFORE launch) writes terminal status; then launch.
# ==========================================================================

codex_rc=""
child_pid=""

on_exit() {
    local rc="$?"
    [[ -n "$codex_rc" ]] && rc="$codex_rc"
    local st
    case "$rc" in
        0)   st="ok" ;;
        124) st="timed_out" ;;
        130) st="killed" ;;
        143) st="killed" ;;
        137) st="killed" ;;
        *)   st="failed" ;;
    esac
    write_status "$st" "$rc" "$(utc_now)"
}

on_signal() {
    local sig="$1"
    [[ -n "$child_pid" ]] && kill -TERM "$child_pid" 2>/dev/null || true
    codex_rc="$sig"
    exit "$sig"
}

trap on_exit EXIT
trap 'on_signal 143' TERM
trap 'on_signal 130' INT

build_codex_cmd "$SUMMARY"

echo "[agimode-codex] launching codex (model=$MODEL, fast=$fast, timeout=${timeout_sec}s) -> $ART" >&2

TIMEOUT_BIN="timeout"
command -v timeout >/dev/null 2>&1 || TIMEOUT_BIN="gtimeout"

set +e
"$TIMEOUT_BIN" "$timeout_sec" "${cmd[@]}" > "$LOG" 2>&1 &
child_pid="$!"
wait "$child_pid"
codex_rc="$?"
child_pid=""
set -e

[[ -s "$LOG" ]] && cat "$LOG" >&2 || true

# Capture the WORKING-TREE diff vs base (NOT base..HEAD — codex never commits).
# Register untracked files with --intent-to-add (excluding .claude/ artifacts),
# diff, then unmark only those exact paths. Fully reversible.
capture_diff() {
    local ita_list="$ART/.ita-paths"
    git -C "$WORKDIR" ls-files --others --exclude-standard -z -- . ':(exclude).claude' \
        > "$ita_list" 2>/dev/null || : > "$ita_list"
    if [[ -s "$ita_list" ]]; then
        xargs -0 git -C "$WORKDIR" add -N -- < "$ita_list" 2>/dev/null || true
    fi
    git -C "$WORKDIR" diff --binary "$base_commit" -- . ':(exclude).claude' > "$DIFF" 2>/dev/null || true
    if [[ -s "$ita_list" ]]; then
        xargs -0 git -C "$WORKDIR" reset -q -- < "$ita_list" 2>/dev/null || true
    fi
    rm -f "$ita_list"
}
capture_diff

exit "$codex_rc"
