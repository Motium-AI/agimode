#!/bin/bash
set -euo pipefail

# If the inherited cwd is gone or permission-blocked (removed/sandboxed worktree,
# or a lost macOS Full-Disk/Desktop grant), re-root to a safe dir so child shells
# don't cascade getcwd errors and hooks that read os.getcwd() don't raise OSError.
# Hooks receive the real cwd via stdin JSON, so this process cwd is irrelevant to hook logic.
if ! pwd -P >/dev/null 2>&1; then
    cd "${CLAUDE_PROJECT_DIR:-$HOME}" 2>/dev/null || cd "$HOME" 2>/dev/null || cd / 2>/dev/null || true
    warn="${TMPDIR:-/tmp}/.claude-cwd-eperm-warned"
    if [ ! -e "$warn" ] || [ -n "$(find "$warn" -mmin +60 2>/dev/null)" ]; then
        : > "$warn" 2>/dev/null || true
        echo "[run-python-hook] cwd was inaccessible (Operation not permitted); re-rooted to $(pwd). Likely a removed/sandboxed worktree or a lost macOS Full-Disk/Desktop grant (e.g. after an auto-update). Recovery: run 'cd ~' or restart the session; if scripts under ~/.claude also fail to exec, re-grant Full Disk Access (System Settings → Privacy & Security) or run the call with the Bash sandbox disabled." >&2
    fi
fi

resolve_python() {
    local override="${CLAUDE_HOOK_PYTHON:-}"

    if [ -n "$override" ]; then
        if [[ "$override" == */* ]]; then
            if [ -x "$override" ]; then
                printf '%s\n' "$override"
                return 0
            fi
        elif command -v "$override" >/dev/null 2>&1; then
            command -v "$override"
            return 0
        fi

        echo "[run-python-hook] CLAUDE_HOOK_PYTHON is set but not executable: $override" >&2
        return 1
    fi

    if [ "$(uname -s)" = "Darwin" ] && [ -x /usr/bin/python3 ]; then
        echo "/usr/bin/python3"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi

    if [ -x /usr/bin/python3 ]; then
        echo "/usr/bin/python3"
        return 0
    fi

    echo "[run-python-hook] No usable Python interpreter found. Install python3 or set CLAUDE_HOOK_PYTHON." >&2
    return 1
}

if [ "$#" -lt 1 ]; then
    echo "[run-python-hook] Usage: run-python-hook.sh <python-args...>" >&2
    exit 64
fi

PYTHON_BIN="$(resolve_python)"

# Fail-mode by hook TYPE (Workstream H): if the hook script target is missing,
# `python3 <missing.py>` exits 2 with a raw "No such file" for EVERY hook — a
# missing advisory then wedges a session exactly like a guard. Intercept the
# missing-target case before exec and degrade by type: guards fail closed
# legibly (exit 2 + named recovery), advisories fail open (warn + exit 0).
# Classification lives in _common.py (single source of truth, shared with
# install.sh --verify); the bash branch is a fallback for when python itself
# cannot run. A target that EXISTS but crashes is left to exec (python exits 1 =
# non-blocking in Claude Code, so it does not wedge).
hook_script=""
for arg in "$@"; do
    case "$arg" in
        *.py) hook_script="$arg"; break ;;
    esac
done
if [ -n "$hook_script" ] && [ ! -f "$hook_script" ]; then
    common="$(dirname -- "$0")/_common.py"
    rc=0
    if [ -f "$common" ]; then
        "$PYTHON_BIN" "$common" --hook-missing "$hook_script" || rc=$?
    else
        rc=99
    fi
    case "$rc" in
        0) exit 0 ;;
        2) exit 2 ;;
        *)
            # python/_common.py unavailable: classify by naming convention.
            base="$(basename -- "$hook_script")"
            case "$base" in
                *-guard.py | *_guard.py | deploy-enforcer.py)
                    echo "[run-python-hook] guard '$base' could not run: missing target $hook_script. Tool call blocked (fail-closed). Recovery: run scripts/install.sh --force." >&2
                    exit 2 ;;
                *)
                    echo "[run-python-hook] advisory '$base' is missing ($hook_script); continuing without it. Recovery: run scripts/install.sh --force." >&2
                    exit 0 ;;
            esac
            ;;
    esac
fi

exec "$PYTHON_BIN" "$@"
