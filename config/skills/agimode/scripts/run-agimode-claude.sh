#!/usr/bin/env bash
#
# run-agimode-claude.sh - agimode fleet worker: a single headless Claude coding
# lane, scoped to ONE git worktree.
#
# This is the Anthropic-executor twin of run-agimode-codex.sh. Differences:
#   - env prefix AGIMODE_CLAUDE_* (not AGIMODE_CODEX_*)
#   - claude runs from WORKDIR as cwd; there is no relied-on -C equivalent
#   - optional --fast is accepted for fleet compatibility but has no effect
#   - --model and --effort override env defaults for Claude worker selection
#   - worker-env isolation disables local toolkit hooks for the child process
#
# Order is load-bearing:
#   (1) preflight (all fail-closed, distinct exit codes)
#   (2) artifact dir + running status.json (only after preflight passes)
#   (3) EXIT trap set, then launch claude under `timeout`
#   (4) trap writes terminal status; capture working-tree diff vs base_commit
#
# Auth note: Claude CLI has no cheap "login status" probe equivalent here.
# We use `claude --version` only as the binary-presence preflight. Auth failures
# are treated honestly as runtime failures and recorded in status.json.
#
# Sandbox posture is PINNED to Claude acceptEdits plus an explicit tool allowlist.
# No --dangerously flags and no bypassPermissions. Claude NEVER commits - the
# fleet engine (coordinator) owns commit + integration.

set -euo pipefail

# --- Configurable via env -------------------------------------------------
CLAUDE_BIN="${AGIMODE_CLAUDE_BIN:-claude}"
MODEL="${AGIMODE_CLAUDE_MODEL:-claude-sonnet-5}"
EFFORT="${AGIMODE_CLAUDE_EFFORT:-high}"
DEFAULT_TIMEOUT_SEC="${AGIMODE_CLAUDE_TIMEOUT_SEC:-1800}"
MAX_TURNS="${AGIMODE_CLAUDE_MAX_TURNS:-80}"
ALLOWED_TOOLS="Read,Write,Edit,MultiEdit,Glob,Grep,Bash"

# --- Exit codes (distinct, fail-closed) -----------------------------------
EX_USAGE=2          # bad args
EX_SPEC=3           # spec file missing / unreadable
EX_NO_CLAUDE=4      # claude binary not on PATH / not runnable
EX_NO_LOGIN=5       # reserved: no cheap Claude auth preflight exists
EX_NOT_GIT=6        # not a git repo
EX_DEFAULT_BRANCH=7 # current branch is the repo default branch
EX_DIRTY=8          # dirty tracked tree (excluding .claude/)

: "$EX_NO_LOGIN" # Reserved distinct exit code; auth failures are runtime status.

usage() {
    cat <<'EOF'
Usage:
  run-agimode-claude.sh [--fast] [--timeout-sec N] [--workdir DIR] [--model MODEL] [--effort EFFORT] [--print-command] <spec-file>

Options:
  --fast              Accepted for fleet compatibility; no effect for Claude.
  --timeout-sec N     Wall-clock timeout in seconds (default: $AGIMODE_CLAUDE_TIMEOUT_SEC or 1800).
  --workdir DIR       Pin the git toplevel explicitly (default: derived from the spec's dir).
  --model MODEL       Claude model (default: $AGIMODE_CLAUDE_MODEL or claude-sonnet-5).
  --effort EFFORT     Claude effort (default: $AGIMODE_CLAUDE_EFFORT or high).
  --print-command     Print the composed claude argv and exit 0 without running.
  -h, --help          Show this help.

Env overrides:
  AGIMODE_CLAUDE_BIN, AGIMODE_CLAUDE_MODEL, AGIMODE_CLAUDE_EFFORT,
  AGIMODE_CLAUDE_TIMEOUT_SEC, AGIMODE_CLAUDE_MAX_TURNS
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
                echo "[agimode-claude] --timeout-sec requires a value." >&2
                exit "$EX_USAGE"
            fi
            timeout_sec="$2"; shift 2 ;;
        --workdir)
            if [[ "$#" -lt 2 ]]; then
                echo "[agimode-claude] --workdir requires a value." >&2
                exit "$EX_USAGE"
            fi
            workdir_override="$2"; shift 2 ;;
        --model)
            if [[ "$#" -lt 2 ]]; then
                echo "[agimode-claude] --model requires a value." >&2
                exit "$EX_USAGE"
            fi
            MODEL="$2"; shift 2 ;;
        --effort)
            if [[ "$#" -lt 2 ]]; then
                echo "[agimode-claude] --effort requires a value." >&2
                exit "$EX_USAGE"
            fi
            EFFORT="$2"; shift 2 ;;
        --print-command) print_mode=true; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        -*)
            echo "[agimode-claude] Unknown option: $1" >&2
            usage >&2
            exit "$EX_USAGE" ;;
        *)
            if [[ -n "$spec_arg" ]]; then
                echo "[agimode-claude] Only one spec file is supported per invocation." >&2
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
    echo "[agimode-claude] A spec file is required." >&2
    usage >&2
    exit "$EX_USAGE"
fi

if ! [[ "$timeout_sec" =~ ^[0-9]+$ ]] || [[ "$timeout_sec" -eq 0 ]]; then
    echo "[agimode-claude] --timeout-sec must be a positive integer (got: $timeout_sec)." >&2
    exit "$EX_USAGE"
fi

if ! [[ "$MAX_TURNS" =~ ^[0-9]+$ ]] || [[ "$MAX_TURNS" -eq 0 ]]; then
    echo "[agimode-claude] AGIMODE_CLAUDE_MAX_TURNS must be a positive integer (got: $MAX_TURNS)." >&2
    exit "$EX_USAGE"
fi

# ==========================================================================
# (1) PREFLIGHT - all fail-closed, distinct exit codes, one-line errors.
# ==========================================================================

if [[ ! -f "$spec_arg" || ! -r "$spec_arg" ]]; then
    echo "[agimode-claude] Spec file not found or unreadable: $spec_arg" >&2
    exit "$EX_SPEC"
fi
SPEC_ABS="$(cd "$(dirname "$spec_arg")" && pwd)/$(basename "$spec_arg")"
spec_filename="$(basename "$spec_arg")"

build_prompt() {
    prompt="Read $SPEC_ABS and execute the work packet exactly. Stay within the spec's files-in-scope, never touch forbidden paths, run the named validation command, and do not commit."
}

build_claude_cmd() {
    cmd=(
        "$CLAUDE_BIN"
        -p
        --model "$MODEL"
        --effort "$EFFORT"
        --permission-mode acceptEdits
        --allowedTools "$ALLOWED_TOOLS"
        --no-session-persistence
    )
    # NB: the claude CLI has no --max-turns flag; the worker is bounded by the
    # wall-clock `timeout` below (and the engine's reap grace). MAX_TURNS is kept
    # only as recorded metadata in status.json.
}

if [[ "$print_mode" == true ]]; then
    build_claude_cmd
    for part in "${cmd[@]}"; do printf '%q ' "$part"; done
    printf '\n'
    exit 0
fi

if ! "$CLAUDE_BIN" --version >/dev/null 2>&1; then
    echo "[agimode-claude] claude binary not found or not runnable: $CLAUDE_BIN" >&2
    exit "$EX_NO_CLAUDE"
fi

# git repo - resolve toplevel from --workdir if given, else from the spec's dir.
if [[ -n "$workdir_override" ]]; then
    resolve_dir="$workdir_override"
else
    resolve_dir="$(dirname "$SPEC_ABS")"
fi
if ! WORKDIR="$(git -C "$resolve_dir" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "[agimode-claude] Not inside a git repository: $resolve_dir" >&2
    exit "$EX_NOT_GIT"
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
    echo "[agimode-claude] Refusing to run on the default branch ('$current_branch'). Use a worktree branch." >&2
    exit "$EX_DEFAULT_BRANCH"
fi

# Dirty-tree check that EXCLUDES paths under .claude/ - our own spec/artifacts
# must never trip our own guard.
if [[ -n "$(git -C "$WORKDIR" status --porcelain -- . ':(exclude).claude' 2>/dev/null)" ]]; then
    echo "[agimode-claude] Refusing to run: working tree has uncommitted changes (outside .claude/)." >&2
    exit "$EX_DIRTY"
fi

# ==========================================================================
# (2) Artifact dir + running status.json - only AFTER preflight passes.
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
LOG="$ART/claude.log"
STATUS="$ART/status.json"
DIFF="$ART/diff.patch"

write_status() {
    local st="$1" ec="${2:-}" ended="${3:-}"
    local tmp="$STATUS.tmp.$$"
    {
        printf '{\n'
        printf '  "status": "%s",\n' "$st"
        printf '  "fast": %s,\n' "$fast"
        printf '  "model": "%s",\n' "$MODEL"
        printf '  "effort": "%s",\n' "$EFFORT"
        printf '  "max_turns": %s,\n' "$MAX_TURNS"
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

claude_rc=""
child_pid=""

# shellcheck disable=SC2329  # Invoked by EXIT trap.
on_exit() {
    local rc="$?"
    [[ -n "$claude_rc" ]] && rc="$claude_rc"
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

# shellcheck disable=SC2329  # Invoked by TERM/INT traps.
on_signal() {
    local sig="$1"
    [[ -n "$child_pid" ]] && kill -TERM "$child_pid" 2>/dev/null || true
    claude_rc="$sig"
    exit "$sig"
}

trap on_exit EXIT
trap 'on_signal 143' TERM
trap 'on_signal 130' INT

build_prompt
build_claude_cmd

echo "[agimode-claude] launching claude (model=$MODEL, effort=$EFFORT, fast=$fast, timeout=${timeout_sec}s) -> $ART" >&2

TIMEOUT_BIN="timeout"
command -v timeout >/dev/null 2>&1 || TIMEOUT_BIN="gtimeout"

set +e
(
    cd "$WORKDIR" || exit 1
    export FABLE_ADVISOR_SKIP=1
    export FABLE_SUBAGENT_GUARD_SKIP=1
    exec "$TIMEOUT_BIN" "$timeout_sec" "${cmd[@]}" \
        > >(tee "$LOG" >&2) \
        2>&1 \
        <<< "$prompt"
) &
child_pid="$!"
wait "$child_pid"
claude_rc="$?"
child_pid=""
set -e

# Capture the WORKING-TREE diff vs base (NOT base..HEAD - claude never commits).
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

exit "$claude_rc"
