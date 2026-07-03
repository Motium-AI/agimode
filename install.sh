#!/usr/bin/env bash
#
# install.sh — standalone installer for the agimode Claude Code plugin.
#
# Usage:
#   ./install.sh              install (idempotent; safe to re-run)
#   ./install.sh --force      re-run install even if already installed
#   ./install.sh --verify     check that installed hook targets resolve
#   ./install.sh --uninstall  remove what this installer added
#
# Two install strategies, auto-detected:
#   1. "fresh"  — ~/.claude has no hooks/skills/rules yet: symlink whole dirs
#                 (config/hooks -> ~/.claude/hooks, etc). Fastest, cleanest.
#   2. "merge"  — ~/.claude already has hooks/skills/rules (other Claude Code
#                 tooling in use): copy only the agimode files in, and
#                 JSON-deep-merge the four agimode hook blocks from
#                 config/settings.json into ~/.claude/settings.json without
#                 clobbering the user's existing hooks.
#
# POSIX-ish bash, fail-closed on unexpected errors, idempotent on re-run.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd -P)"
REPO_HOOKS="$SCRIPT_DIR/config/hooks"
REPO_SKILLS="$SCRIPT_DIR/config/skills"
REPO_RULES="$SCRIPT_DIR/config/rules"
REPO_SETTINGS="$SCRIPT_DIR/config/settings.json"

CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
TARGET_HOOKS="$CLAUDE_HOME/hooks"
TARGET_SKILLS="$CLAUDE_HOME/skills"
TARGET_RULES="$CLAUDE_HOME/rules"
TARGET_SETTINGS="$CLAUDE_HOME/settings.json"

AGIMODE_HOOK_FILES=(
    "_common.py"
    "_session.py"
    "worktree_manager.py"
    "agimode_fleet.py"
    "agimode-session-surface.py"
    "codex-background-enforcer.py"
    "fable-subagent-model-guard.py"
    "fable-delegation-advisor.py"
    "run-python-hook.sh"
)

MARKER_FILE="$CLAUDE_HOME/.agimode-install-marker"

log()  { printf '[install] %s\n' "$1"; }
warn() { printf '[install] WARNING: %s\n' "$1" >&2; }
err()  { printf '[install] ERROR: %s\n' "$1" >&2; }

usage() {
    cat <<'EOF'
Usage: install.sh [--force] [--verify] [--uninstall]

  (no flags)   Install agimode, idempotently.
  --force      Re-run install even if the marker says it's already installed.
  --verify     Check that installed hook targets resolve; exit 1 if not.
  --uninstall  Remove files/symlinks/settings blocks this installer added.
EOF
}

# ---------------------------------------------------------------------------
# Strategy detection
# ---------------------------------------------------------------------------

is_fresh_target() {
    [ ! -e "$TARGET_HOOKS" ] && [ ! -e "$TARGET_SKILLS" ] && [ ! -e "$TARGET_RULES" ]
}

# ---------------------------------------------------------------------------
# Fresh install: whole-directory symlinks
# ---------------------------------------------------------------------------

install_fresh() {
    log "no existing ~/.claude hooks/skills/rules found — installing via whole-directory symlinks"
    mkdir -p "$CLAUDE_HOME"

    ln -sfn "$REPO_HOOKS" "$TARGET_HOOKS"
    log "linked $TARGET_HOOKS -> $REPO_HOOKS"

    ln -sfn "$REPO_SKILLS" "$TARGET_SKILLS"
    log "linked $TARGET_SKILLS -> $REPO_SKILLS"

    ln -sfn "$REPO_RULES" "$TARGET_RULES"
    log "linked $TARGET_RULES -> $REPO_RULES"

    if [ -e "$TARGET_SETTINGS" ] && [ ! -L "$TARGET_SETTINGS" ]; then
        warn "$TARGET_SETTINGS already exists; merging agimode hook blocks into it instead of overwriting"
        merge_settings
    else
        cp "$REPO_SETTINGS" "$TARGET_SETTINGS"
        log "wrote $TARGET_SETTINGS"
    fi

    echo "fresh" > "$MARKER_FILE"
}

# ---------------------------------------------------------------------------
# Merge install: copy individual agimode files, deep-merge settings.json
# ---------------------------------------------------------------------------

install_merge() {
    log "existing ~/.claude hooks/skills/rules found — merging agimode files in without touching the rest"
    mkdir -p "$TARGET_HOOKS" "$TARGET_SKILLS" "$TARGET_RULES"

    for f in "${AGIMODE_HOOK_FILES[@]}"; do
        if [ -f "$REPO_HOOKS/$f" ]; then
            ln -sfn "$REPO_HOOKS/$f" "$TARGET_HOOKS/$f"
        fi
    done
    log "linked ${#AGIMODE_HOOK_FILES[@]} agimode hook file(s) into $TARGET_HOOKS"

    ln -sfn "$REPO_SKILLS/agimode" "$TARGET_SKILLS/agimode"
    log "linked $TARGET_SKILLS/agimode -> $REPO_SKILLS/agimode"

    if [ -f "$REPO_RULES/toolkit-agimode.md" ]; then
        ln -sfn "$REPO_RULES/toolkit-agimode.md" "$TARGET_RULES/toolkit-agimode.md"
        log "linked $TARGET_RULES/toolkit-agimode.md -> $REPO_RULES/toolkit-agimode.md"
    fi

    merge_settings

    echo "merge" > "$MARKER_FILE"
}

merge_settings() {
    if [ ! -f "$TARGET_SETTINGS" ]; then
        mkdir -p "$(dirname -- "$TARGET_SETTINGS")"
        printf '{}\n' > "$TARGET_SETTINGS"
    fi

    python3 - "$REPO_SETTINGS" "$TARGET_SETTINGS" <<'PYEOF'
import json
import sys
from pathlib import Path

src_path, dst_path = sys.argv[1], sys.argv[2]
src = json.loads(Path(src_path).read_text())
dst = json.loads(Path(dst_path).read_text() or "{}")

dst.setdefault("hooks", {})
added = 0
for event, groups in src.get("hooks", {}).items():
    existing = dst["hooks"].setdefault(event, [])
    for group in groups:
        if group not in existing:
            existing.append(group)
            added += 1

tmp = Path(dst_path).with_suffix(".json.tmp")
tmp.write_text(json.dumps(dst, indent=2) + "\n")
tmp.replace(dst_path)
print(f"[install] merged {added} new hook block(s) into {dst_path}")
PYEOF
}

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

verify() {
    local failures=0

    if [ ! -f "$TARGET_SETTINGS" ]; then
        err "missing $TARGET_SETTINGS"
        failures=$((failures + 1))
    else
        while IFS= read -r target; do
            if [ ! -e "$target" ]; then
                err "hook target does not resolve: $target"
                failures=$((failures + 1))
            fi
        done < <(python3 - "$TARGET_SETTINGS" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
seen = set()
for groups in data.get("hooks", {}).values():
    for group in groups:
        for hook in group.get("hooks", []):
            cmd = hook.get("command", "")
            for m in re.findall(r'"([^"]+\.(?:py|sh))"', cmd):
                expanded = m.replace("$HOME", str(Path.home()))
                if expanded not in seen:
                    seen.add(expanded)
                    print(expanded)
PYEOF
        )
    fi

    if [ -d "$TARGET_HOOKS" ]; then
        if ! PYTHONPATH="$TARGET_HOOKS" python3 -c "import _session" 2>/tmp/agimode-verify-import.err; then
            err "python3 -c 'import _session' failed from $TARGET_HOOKS:"
            sed 's/^/  /' /tmp/agimode-verify-import.err >&2
            failures=$((failures + 1))
        fi
        rm -f /tmp/agimode-verify-import.err
    else
        err "missing $TARGET_HOOKS"
        failures=$((failures + 1))
    fi

    for bin in codex claude; do
        if ! command -v "$bin" >/dev/null 2>&1; then
            warn "'$bin' CLI not found on PATH — the agimode fleet cannot dispatch to it until installed"
        fi
    done

    if [ "$failures" -eq 0 ]; then
        log "verify OK — hook targets resolve, _session imports cleanly"
        return 0
    fi
    err "verify FAILED — $failures problem(s) found"
    return 1
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

uninstall() {
    local mode=""
    [ -f "$MARKER_FILE" ] && mode="$(cat "$MARKER_FILE")"

    if [ "$mode" = "fresh" ]; then
        for link in "$TARGET_HOOKS" "$TARGET_SKILLS" "$TARGET_RULES"; do
            if [ -L "$link" ]; then
                rm -f "$link"
                log "removed symlink $link"
            fi
        done
    else
        for f in "${AGIMODE_HOOK_FILES[@]}"; do
            if [ -L "$TARGET_HOOKS/$f" ]; then
                rm -f "$TARGET_HOOKS/$f"
            fi
        done
        log "removed agimode hook file symlinks from $TARGET_HOOKS"

        if [ -L "$TARGET_SKILLS/agimode" ]; then
            rm -f "$TARGET_SKILLS/agimode"
            log "removed $TARGET_SKILLS/agimode"
        fi

        if [ -L "$TARGET_RULES/toolkit-agimode.md" ]; then
            rm -f "$TARGET_RULES/toolkit-agimode.md"
            log "removed $TARGET_RULES/toolkit-agimode.md"
        fi
    fi

    if [ -f "$TARGET_SETTINGS" ]; then
        python3 - "$REPO_SETTINGS" "$TARGET_SETTINGS" <<'PYEOF'
import json
import sys
from pathlib import Path

src_path, dst_path = sys.argv[1], sys.argv[2]
src = json.loads(Path(src_path).read_text())
dst = json.loads(Path(dst_path).read_text())

removed = 0
for event, groups in src.get("hooks", {}).items():
    existing = dst.get("hooks", {}).get(event, [])
    for group in list(groups):
        if group in existing:
            existing.remove(group)
            removed += 1

tmp = Path(dst_path).with_suffix(".json.tmp")
tmp.write_text(json.dumps(dst, indent=2) + "\n")
tmp.replace(dst_path)
print(f"[install] removed {removed} agimode hook block(s) from {dst_path}")
PYEOF
    fi

    rm -f "$MARKER_FILE"
    log "uninstall complete"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

check_executors() {
    local missing=()
    command -v codex >/dev/null 2>&1 || missing+=("codex")
    command -v claude >/dev/null 2>&1 || missing+=("claude")
    if [ "${#missing[@]}" -gt 0 ]; then
        warn "missing executor CLI(s): ${missing[*]} — agimode dispatches to these; install at least one before running '/agimode on'"
    fi
}

main() {
    local force=0
    local do_verify=0
    local do_uninstall=0

    for arg in "$@"; do
        case "$arg" in
            --force) force=1 ;;
            --verify) do_verify=1 ;;
            --uninstall) do_uninstall=1 ;;
            -h|--help) usage; exit 0 ;;
            *) err "unknown flag: $arg"; usage; exit 64 ;;
        esac
    done

    if [ "$do_uninstall" -eq 1 ]; then
        uninstall
        exit 0
    fi

    if [ "$do_verify" -eq 1 ]; then
        verify
        exit $?
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found on PATH — required by agimode hooks"
        exit 1
    fi

    if [ -f "$MARKER_FILE" ] && [ "$force" -ne 1 ]; then
        log "already installed (marker found at $MARKER_FILE); re-run with --force to reinstall"
    else
        if is_fresh_target; then
            install_fresh
        else
            install_merge
        fi
    fi

    check_executors

    log "verifying install..."
    if verify; then
        log ""
        log "agimode installed. Next steps:"
        log "  1. Restart Claude Code (hooks are loaded at session start)."
        log "  2. In a Claude Code session, run: /agimode on"
    else
        err "install completed but verify failed — see errors above"
        exit 1
    fi
}

main "$@"
