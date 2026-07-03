#!/usr/bin/env python3
"""
Shared utilities for Claude Code hooks.

Constants, logging, git utilities, TTL checks, and worktree detection.
For state/checkpoint operations, see _session.py.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

# TTL for autonomous mode state files (hours).
# State older than this is considered expired and cleaned up.
SESSION_TTL_HOURS = 8


# Files/patterns excluded from version tracking (dirty calculation)
# These don't represent code changes requiring re-deployment
VERSION_TRACKING_EXCLUSIONS = [
    ".",
    ":(exclude).claude",
    ":(exclude).claude/**",
    ":(exclude)**/.claude",
    ":(exclude)**/.claude/**",
    ":(exclude)*.lock",
    ":(exclude)package-lock.json",
    ":(exclude)yarn.lock",
    ":(exclude)pnpm-lock.yaml",
    ":(exclude)poetry.lock",
    ":(exclude)Pipfile.lock",
    ":(exclude)Cargo.lock",
    ":(exclude).gitmodules",
    ":(exclude)*.pyc",
    ":(exclude)__pycache__",
    ":(exclude)*/__pycache__",
    ":(exclude).env*",
    ":(exclude)*.log",
    ":(exclude).DS_Store",
    ":(exclude)*.swp",
    ":(exclude)*.swo",
    ":(exclude)*.orig",
    ":(exclude).idea",
    ":(exclude).idea/*",
    ":(exclude).vscode",
    ":(exclude).vscode/*",
]

# Debug log location - shared across all hooks
DEBUG_LOG = Path(tempfile.gettempdir()) / "claude-hooks-debug.log"


# Pattern matching browser automation tool usage in Bash signatures.
# Single source of truth imported by _stop_melt.py and verification-monitor.py.
AGENT_BROWSER_PATTERN = r"\bagent-browser\b"


# ============================================================================
# Filesystem Utilities
# ============================================================================


def safe_cwd() -> str:
    """Return the cwd, falling back when it is permission-blocked (EPERM).

    Used in place of a bare ``os.getcwd()`` default, which Python evaluates
    eagerly and would raise even when the caller already supplied a cwd.
    """
    try:
        return os.getcwd()
    except OSError:
        return os.environ.get("CLAUDE_PROJECT_DIR") or os.path.expanduser("~")


# ============================================================================
# Hook fail-mode classification (Workstream H)
# ============================================================================
#
# A broken hook (missing/unrunnable target) must degrade by TYPE, never silently
# wedge a session:
#   * GUARD hooks protect an invariant -> FAIL CLOSED, legibly: block (exit 2)
#     with a named-file recovery line, never a raw Python traceback.
#   * Everything else is ADVISORY -> FAIL OPEN: emit one stderr warning and
#     exit 0, so a missing/broken advisory never blocks a tool call.
#
# The primary signal is the naming convention (*-guard.py / *_guard.py): a new
# guard only has to be named correctly to fail closed. GUARD_HOOK_OVERRIDES is
# the escape hatch for guards that do NOT follow the convention; install.sh
# --verify validates that every override resolves to a real file so a guard is
# never silently downgraded by a typo. This set is the single source of truth
# shared by run-python-hook.sh (the runner) and install.sh --verify.
GUARD_HOOK_OVERRIDES = frozenset(
    {
        "deploy-enforcer.py",  # blocks unapproved deploys; must fail closed
    }
)


def is_guard_hook(name: str) -> bool:
    """True if a missing/broken hook must FAIL CLOSED (exit 2), else advisory.

    ``name`` may be a basename or a full path. Classification = the
    ``*-guard``/``*_guard`` naming convention UNION ``GUARD_HOOK_OVERRIDES``.
    """
    base = os.path.basename(str(name))
    if base in GUARD_HOOK_OVERRIDES:
        return True
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem.endswith(("-guard", "_guard"))


def hook_missing_message(script: str) -> tuple[str, int]:
    """Return ``(stderr_message, exit_code)`` for a missing hook target.

    Guard -> legible block (exit 2); advisory -> one warning (exit 0). Never a
    raw traceback. Consumed by run-python-hook.sh via the ``--hook-missing`` CLI.
    """
    base = os.path.basename(str(script))
    if is_guard_hook(base):
        return (
            f"[run-python-hook] guard '{base}' could not run: missing target "
            f"{script}. Tool call blocked (fail-closed). Recovery: run "
            f"scripts/install.sh --force to repair the toolkit install.",
            2,
        )
    return (
        f"[run-python-hook] advisory '{base}' is missing ({script}); continuing "
        f"without it. Recovery: run scripts/install.sh --force to repair.",
        0,
    )


# ============================================================================
# Git Utilities
# ============================================================================


def get_diff_hash(cwd: str = "") -> str:
    """Get hash of current git diff (excluding metadata files).

    Used to detect if THIS session made changes by comparing against
    the snapshot taken at session start.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD", "--", *VERSION_TRACKING_EXCLUSIONS],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        return hashlib.sha1(result.stdout.encode()).hexdigest()[:12]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def get_current_branch(cwd: str = "") -> str:
    """Get the current git branch name.

    Returns empty string if git is unavailable or in detached HEAD state.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5, cwd=cwd or None,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def get_code_version(cwd: str = "") -> str:
    """Get current code version (git HEAD + dirty indicator).

    Returns format:
    - "abc1234" - clean commit
    - "abc1234-dirty" - commit with uncommitted changes
    - "unknown" - not a git repo or error

    NOTE: The dirty indicator is boolean, NOT a hash. This ensures version
    stability during development - version only changes at commit boundaries.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        head_hash = head.stdout.strip()
        if not head_hash:
            return "unknown"

        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", *VERSION_TRACKING_EXCLUSIONS],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        if diff.stdout.strip():
            return f"{head_hash}-dirty"

        return head_hash
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


# ============================================================================
# Logging
# ============================================================================


def log_debug(
    message: str,
    hook_name: str = "unknown",
    raw_input: str = "",
    parsed_data: dict | None = None,
    error: Exception | None = None,
) -> None:
    """Log diagnostic info for debugging hook issues."""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"Timestamp: {datetime.now().isoformat()}\n")
            f.write(f"Hook: {hook_name}\n")
            f.write(f"Message: {message}\n")
            if error:
                f.write(f"Error: {type(error).__name__}: {error}\n")
            if raw_input:
                f.write(
                    f"Raw stdin ({len(raw_input)} bytes): {raw_input[:500]!r}\n"
                )
            if parsed_data is not None:
                f.write(f"Parsed data: {json.dumps(parsed_data, indent=2)}\n")
            f.write(f"{'=' * 60}\n")
    except Exception as e:
        # Last-resort signal: write to stderr once so operators know logging is broken
        import sys as _sys
        with suppress(Exception):
            _sys.stderr.write(f"[claude-hooks] log_debug write failed: {e}\n")


# ============================================================================
# TTL & Session Utilities
# ============================================================================


def is_state_expired(state: dict, ttl_hours: int = SESSION_TTL_HOURS) -> bool:
    """Check if a state file has exceeded its TTL.

    Uses last_activity_at if present, falls back to started_at.
    Missing or malformed timestamps are treated as expired.
    """
    timestamp_str = state.get("last_activity_at") or state.get("started_at")
    if not timestamp_str:
        return True

    try:
        if timestamp_str.endswith("Z"):
            timestamp_str = timestamp_str[:-1] + "+00:00"
        ts = datetime.fromisoformat(timestamp_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - ts) > timedelta(hours=ttl_hours)
    except (ValueError, TypeError):
        return True


# ============================================================================
# Process Utilities
# ============================================================================


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running.

    Uses os.kill(pid, 0) which doesn't actually send a signal,
    just checks if the process exists.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we don't own it
    except OSError:
        return False


# ============================================================================
# Worktree Detection
# ============================================================================


def is_worktree(cwd: str = "") -> bool:
    """Check if the current directory is a git worktree (not the main repo)."""
    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        git_common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        return git_dir.stdout.strip() != git_common.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_worktree_info(cwd: str = "") -> dict | None:
    """Get information about the current worktree if in one.

    Returns:
        Dict with branch, agent_id, path, is_claude_worktree.
        None if not in a worktree.
    """
    if not is_worktree(cwd):
        return None
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )
        worktree_path = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd or None,
        )

        state_file = (
            Path(worktree_path.stdout.strip()) / ".claude" / "worktree-agent-state.json"
        )
        agent_id = None
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                agent_id = state.get("agent_id")
            except (OSError, json.JSONDecodeError):
                pass

        return {
            "branch": branch.stdout.strip(),
            "agent_id": agent_id,
            "path": worktree_path.stdout.strip(),
            "is_claude_worktree": agent_id is not None,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ============================================================================
# Hook Execution Metrics
# ============================================================================

HOOK_METRICS_PATH = Path.home() / ".claude" / "hook-metrics.jsonl"


def emit_hook_metric(
    hook_name: str, duration_ms: float, status: str = "ok", **extra,
) -> None:
    """Write a structured metric entry to hook-metrics.jsonl (append)."""
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hook": hook_name,
        "duration_ms": round(duration_ms, 1),
        "status": status,
    }
    entry.update(extra)
    try:
        HOOK_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HOOK_METRICS_PATH, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


# ============================================================================
# QMD Detection
# ============================================================================


def check_qmd_available(cwd: str = "") -> bool:
    """Check if QMD MCP server is configured in settings.

    Single source of truth — imported by read-docs-reminder.py and
    read-docs-trigger.py instead of duplicating the check.
    """
    mcp_paths = [
        Path(cwd) / ".mcp.json" if cwd else None,
        Path.home() / ".claude.json",
        Path.home() / ".claude" / "settings.json",
    ]
    for mcp_path in mcp_paths:
        if mcp_path is None or not mcp_path.exists():
            continue
        try:
            config = json.loads(mcp_path.read_text())
            if "qmd" in config.get("mcpServers", {}):
                return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def get_main_repo_path(cwd: str = "") -> str:
    """Resolve worktree path to the main repo checkout path.

    QMD indexes the main repo, not worktrees. When running in a worktree,
    this returns the main repo root so collection registration and searches
    target the correct directory.

    Returns cwd unchanged if not in a worktree.
    """
    if not is_worktree(cwd):
        return cwd
    try:
        # git-common-dir points to main repo's .git directory
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, cwd=cwd or None,
        )
        git_common = result.stdout.strip()
        if git_common and result.returncode == 0:
            # .git dir is typically <repo>/.git — parent is the repo root
            return str(Path(git_common).resolve().parent)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return cwd


@contextmanager
def timed_hook(hook_name: str):
    """Context manager that times hook execution and emits metrics.

    Usage at script entry point:
        if __name__ == "__main__":
            with timed_hook("my-hook"):
                main()

    Handles SystemExit from sys.exit() gracefully.
    """
    metrics = {}
    start = time.monotonic()
    status = "ok"
    try:
        yield metrics
    except SystemExit as e:
        status = "ok" if e.code in (0, None) else "blocked"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        emit_hook_metric(hook_name, duration_ms, status, **metrics)


# ============================================================================
# Tool Log Utilities
# ============================================================================


def load_tool_log(cwd: str) -> list[dict]:
    """Load tool-usage-log.json entries."""
    log_path = Path(cwd) / ".claude" / "tool-usage-log.json"
    if not log_path.exists():
        return []
    try:
        entries = json.loads(log_path.read_text())
        return entries if isinstance(entries, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def load_bash_signatures(cwd: str) -> list[str]:
    """Load Bash command signatures from tool-usage-log.json."""
    return [e.get("sig", "") for e in load_tool_log(cwd) if e.get("tool") == "Bash"]


# PR-review skill family. A `gh pr create` this session with none of these
# invoked is what the pr-review-reminder hook + Stop nudge key off of.
PR_REVIEW_SKILL_PREFIX = "pr-review-"  # pr-review-clopex / -codex / -opus


def pr_created_in_session(cwd: str) -> bool:
    """True if a ``gh pr create`` command ran this session (per tool log)."""
    return any("gh pr create" in sig for sig in load_bash_signatures(cwd))


def pr_review_skill_invoked(cwd: str) -> bool:
    """True if any ``pr-review-*`` skill was invoked this session (per tool log)."""
    return any(
        e.get("tool") == "Skill"
        and e.get("sig", "").lower().startswith(PR_REVIEW_SKILL_PREFIX)
        for e in load_tool_log(cwd)
    )


def load_mcp_signatures(cwd: str) -> list[str]:
    """Load MCP tool call signatures from tool-usage-log.json.

    MCP tool names follow the pattern ``mcp__<server>__<tool>``.
    Returns the tool name (not the sig) since MCP calls don't have
    meaningful bash-style signatures.
    """
    return [
        e.get("tool", "")
        for e in load_tool_log(cwd)
        if e.get("tool", "").startswith("mcp__")
    ]


# Patterns matching Xcode MCP tool usage (Apple MCP + XcodeBuildMCP).
# Used by _stop_melt.py to detect iOS verification.
XCODE_MCP_BUILD_PATTERNS = re.compile(
    r"mcp__.*(?:BuildProject|build_sim|build_device|test_sim|test_device"
    r"|RunAllTests|RunSomeTests)"
)
XCODE_MCP_VERIFY_PATTERNS = re.compile(
    r"mcp__.*(?:screenshot|snapshot_ui|RenderPreview|XcodeListNavigatorIssues)"
)


# ============================================================================
# Atomic File I/O
# ============================================================================


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically using write-temp-fsync-rename pattern.

    Guarantees: the file at ``path`` is either the old content or the
    new content, never a partial write.  Uses F_FULLFSYNC on macOS
    for true durability.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            # macOS fsync() doesn't flush disk write cache; F_FULLFSYNC does
            if hasattr(fcntl, "F_FULLFSYNC"):
                fcntl.fcntl(f.fileno(), fcntl.F_FULLFSYNC)
            else:
                os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


# ============================================================================
# Git Diff Utilities
# ============================================================================


def get_changed_files(cwd: str, *, exclude_metadata: bool = False) -> list[str]:
    """Get files changed relative to HEAD (both unstaged and staged).

    Args:
        cwd: Working directory for git commands.
        exclude_metadata: If True, exclude VERSION_TRACKING_EXCLUSIONS paths.
    """
    try:
        cmd = ["git", "diff", "--name-only", "HEAD"]
        if exclude_metadata:
            cmd += ["--", *VERSION_TRACKING_EXCLUSIONS]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, cwd=cwd,
        )
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


# ============================================================================
# CLI: hook fail-mode helper for run-python-hook.sh
# ============================================================================


def _main(argv: list[str]) -> int:
    import sys

    if len(argv) >= 3 and argv[1] == "--hook-missing":
        message, code = hook_missing_message(argv[2])
        sys.stderr.write(message + "\n")
        return code
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
