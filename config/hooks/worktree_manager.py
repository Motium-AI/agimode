#!/usr/bin/env python3
"""
Git Worktree Manager for Parallel Agent Isolation

Provides utilities for creating and managing git worktrees to isolate
parallel agent operations. Each agent gets its own worktree with a
dedicated branch, preventing git operation conflicts.

Usage:
    # Create worktree for an agent
    python3 worktree-manager.py create <agent-id>

    # Cleanup worktree after agent completes
    python3 worktree-manager.py cleanup <agent-id>

    # Merge agent's work back to main branch
    python3 worktree-manager.py merge <agent-id>

    # List all active agent worktrees
    python3 worktree-manager.py list

    # Get worktree path for an agent (returns path to stdout)
    python3 worktree-manager.py path <agent-id>

    # Check if current directory is a worktree
    python3 worktree-manager.py is-worktree

    # Show status with agent context
    python3 worktree-manager.py status

Exit codes:
    0 - Success
    1 - Error (message on stderr)
    2 - Conflict detected (for merge command)
"""

# ruff: noqa: T201, C901 — narration module (print to stdout is intentional); the two
# flagged functions are pre-existing complexity, not refactored in this bugfix.
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import contextlib

from _common import atomic_write_json

# Base directory for all Claude agent worktrees
# Configurable via CLAUDE_WORKTREE_BASE env var. Defaults to ~/.claude/worktrees
# (survives reboots, centralized, discoverable via `ls`)
WORKTREE_BASE = Path(
    os.environ.get("CLAUDE_WORKTREE_BASE", str(Path.home() / ".claude" / "worktrees"))
)

# Branch prefix for agent branches
BRANCH_PREFIX = "claude-agent"

# State file tracking active worktrees
STATE_FILE = Path.home() / ".claude" / "worktree-state.json"

# Lock file for atomic state operations (prevents TOCTOU races)
STATE_LOCK_PATH = Path.home() / ".claude" / ".worktree-state.lock"


def run_git(
    args: list[str], cwd: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def get_main_repo_root(cwd: str | None = None) -> Path:
    """Get the root of the main git repository (not worktree).

    Always returns an absolute path (git may return relative paths
    like '.git' when called from the main repo).
    """
    result = run_git(["rev-parse", "--git-common-dir"], cwd=cwd)
    git_common = Path(result.stdout.strip())

    # git may return relative paths — resolve against cwd
    if not git_common.is_absolute():
        base = Path(cwd) if cwd else Path.cwd()
        git_common = (base / git_common).resolve()

    # git-common-dir returns the path to .git (or .git/worktrees/xxx for worktrees)
    # We want the parent of .git for the main repo
    if git_common.name == ".git":
        return git_common.parent
    elif "worktrees" in git_common.parts:
        # This is a worktree, find the main repo
        # .git/worktrees/xxx -> .git -> parent
        return git_common.parent.parent.parent
    else:
        return git_common.parent


def is_worktree(cwd: str | None = None) -> bool:
    """Check if the current directory is a git worktree (not the main repo)."""
    try:
        result = run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd, check=False)
        if result.returncode != 0:
            return False

        # Check if this is the main worktree or a linked worktree
        git_dir = run_git(["rev-parse", "--git-dir"], cwd=cwd)
        git_common = run_git(["rev-parse", "--git-common-dir"], cwd=cwd)

        # If git-dir != git-common-dir, this is a linked worktree
        return git_dir.stdout.strip() != git_common.stdout.strip()
    except (RuntimeError, subprocess.TimeoutExpired):
        return False


def get_worktree_info(cwd: str | None = None) -> dict | None:
    """Get information about the current worktree if in one."""
    if not is_worktree(cwd):
        return None

    try:
        # Get the branch name
        branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        branch_name = branch.stdout.strip()

        # Extract agent ID from branch name if it matches our pattern
        agent_id = None
        if branch_name.startswith(f"{BRANCH_PREFIX}/"):
            agent_id = branch_name[len(f"{BRANCH_PREFIX}/"):]

        # Get worktree path
        worktree_path = run_git(["rev-parse", "--show-toplevel"], cwd=cwd)

        return {
            "branch": branch_name,
            "agent_id": agent_id,
            "path": worktree_path.stdout.strip(),
            "is_claude_worktree": agent_id is not None,
        }
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


# ============================================================================
# State Management (with file locking)
# ============================================================================



# atomic_write_json is imported from _common (canonical implementation)


def load_state() -> dict:
    """Load worktree state from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return {"worktrees": {}}


def save_state(state: dict) -> None:
    """Save worktree state to disk with file locking.

    Uses fcntl.flock to prevent TOCTOU races when multiple agents
    create/cleanup worktrees concurrently.
    """
    STATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_LOCK_PATH, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            atomic_write_json(STATE_FILE, state)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _locked_state_update(update_fn) -> dict:
    """Load state, apply update function, save — all under lock.

    Prevents the load → modify → save TOCTOU race by holding an
    exclusive lock for the entire read-modify-write cycle.

    Args:
        update_fn: Callable that receives state dict and mutates it in place.

    Returns:
        The updated state dict.
    """
    STATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_LOCK_PATH, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = load_state()
            update_fn(state)
            atomic_write_json(STATE_FILE, state)
            return state
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# ============================================================================
# Autonomous State Propagation
# ============================================================================


def _propagate_autonomous_state(
    main_repo_root: Path, worktree_path: Path, agent_id: str,
) -> None:
    """Propagate autonomous-state.json into worktree for enforcement hooks.

    Enforcement hooks (branch-guard, deploy-enforcer, state-file-guard,
    stop-validator) check is_autonomous_mode_active(cwd) which calls
    _find_state_path(cwd). Without propagation, the walk-up from a
    worktree path never reaches the main repo's state file.

    Copies the main repo's autonomous-state.json into the worktree's
    .claude/ directory with coordinator=false so enforcement hooks work
    correctly in worktree context.
    """
    main_state_file = main_repo_root / ".claude" / "autonomous-state.json"
    if not main_state_file.exists():
        return

    try:
        state = json.loads(main_state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return

    # Mark as non-coordinator so deploy-enforcer blocks direct deploys
    state["coordinator"] = False
    state["worktree_agent_id"] = agent_id
    state["main_repo"] = str(main_repo_root)

    worktree_state_path = worktree_path / ".claude" / "autonomous-state.json"
    with contextlib.suppress(OSError):
        atomic_write_json(worktree_state_path, state)


# ============================================================================
# Core Operations
# ============================================================================


def create_worktree(agent_id: str, main_repo: str | None = None) -> Path:
    """
    Create a new git worktree for an agent.

    Args:
        agent_id: Unique identifier for the agent
        main_repo: Path to main repo (defaults to cwd)

    Returns:
        Path to the created worktree

    Raises:
        RuntimeError: If git fails
    """
    if main_repo is None:
        main_repo = os.getcwd()

    # Ensure we're in the main repo, not a worktree
    main_repo_root = get_main_repo_root(main_repo)

    # Submodule SUPERPROJECTS coexist fine with worktrees. The git-worktree BUGS
    # caveat is about mutating the SAME submodule across multiple worktrees — which
    # agent/fleet workers never do: they only edit superproject files. `git worktree
    # add` does NOT recurse into submodules (it's excluded from `submodule.recurse`),
    # so the submodule working trees stay uninitialized — invisible to `git status`,
    # never touched. (Verified against a real submodule repo: worktree add + a
    # superproject commit + cleanup all run clean, and the submodule stays empty even
    # with submodule.recurse=true set. The old hard-raise here blocked the fleet in
    # any submodule repo.)

    branch_name = f"{BRANCH_PREFIX}/{agent_id}"
    worktree_path = WORKTREE_BASE / agent_id

    # Clean up any existing worktree with same ID
    if worktree_path.exists():
        cleanup_worktree(agent_id, main_repo=str(main_repo_root))

    # Ensure worktree base exists
    WORKTREE_BASE.mkdir(parents=True, exist_ok=True)

    # Get current HEAD commit
    head = run_git(["rev-parse", "HEAD"], cwd=str(main_repo_root))
    head_commit = head.stdout.strip()

    # Create branch from current HEAD
    # Delete if exists (from failed previous run)
    run_git(["branch", "-D", branch_name], cwd=str(main_repo_root), check=False)
    run_git(["branch", branch_name, head_commit], cwd=str(main_repo_root))

    # Create worktree with --lock to prevent pruning during active use.
    run_git(
        [
            "worktree", "add",
            "--lock",
            str(worktree_path),
            branch_name,
        ],
        cwd=str(main_repo_root),
    )

    # Enable per-worktree config isolation (prevents config poisoning)
    run_git(
        ["config", "extensions.worktreeConfig", "true"],
        cwd=str(worktree_path),
        check=False,
    )
    # Disable auto-gc in worktree (run gc once after merge-back instead)
    run_git(
        ["config", "--worktree", "gc.auto", "0"],
        cwd=str(worktree_path),
        check=False,
    )

    # Create .claude directory in worktree for checkpoint isolation
    worktree_claude_dir = worktree_path / ".claude"
    worktree_claude_dir.mkdir(parents=True, exist_ok=True)

    # Create agent-specific state file
    agent_state = {
        "agent_id": agent_id,
        "created_at": datetime.now(timezone.utc).isoformat() + "Z",
        "main_repo": str(main_repo_root),
        "branch": branch_name,
        "base_commit": head_commit,
    }
    (worktree_claude_dir / "worktree-agent-state.json").write_text(
        json.dumps(agent_state, indent=2)
    )

    # Propagate autonomous-state.json so enforcement hooks work in worktree
    _propagate_autonomous_state(main_repo_root, worktree_path, agent_id)

    # Update global state (under lock to prevent TOCTOU race)
    def _add_worktree(state: dict) -> None:
        state["worktrees"][agent_id] = {
            "path": str(worktree_path),
            "branch": branch_name,
            "main_repo": str(main_repo_root),
            "base_commit": head_commit,
            "created_at": agent_state["created_at"],
        }

    _locked_state_update(_add_worktree)

    return worktree_path


def cleanup_worktree(
    agent_id: str,
    main_repo: str | None = None,
    preserve_branch: bool = False,
) -> bool:
    """
    Remove a worktree and optionally its branch.

    Args:
        agent_id: Unique identifier for the agent
        main_repo: Path to main repo (defaults to finding it from state)
        preserve_branch: If True, keep the branch (e.g., after merge conflict)

    Returns:
        True if cleanup succeeded
    """
    state = load_state()
    worktree_info = state.get("worktrees", {}).get(agent_id)

    if worktree_info:
        main_repo = worktree_info.get("main_repo", main_repo)
        worktree_path = Path(worktree_info.get("path", WORKTREE_BASE / agent_id))
        branch_name = worktree_info.get("branch", f"{BRANCH_PREFIX}/{agent_id}")
    else:
        worktree_path = WORKTREE_BASE / agent_id
        branch_name = f"{BRANCH_PREFIX}/{agent_id}"
        if main_repo is None:
            main_repo = os.getcwd()
        main_repo = str(get_main_repo_root(main_repo))

    # Unlock worktree before removal (created with --lock)
    run_git(
        ["worktree", "unlock", str(worktree_path)],
        cwd=main_repo,
        check=False,
    )

    # Remove worktree
    if worktree_path.exists():
        with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired):
            run_git(
                ["worktree", "remove", "--force", str(worktree_path)],
                cwd=main_repo,
                check=False,
            )

        # Force remove if git worktree remove failed
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

    # Delete branch unless preserving (e.g., for manual conflict resolution)
    if not preserve_branch:
        with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired):
            run_git(["branch", "-D", branch_name], cwd=main_repo, check=False)

    # Update state (under lock)
    def _remove_worktree(s: dict) -> None:
        s.get("worktrees", {}).pop(agent_id, None)

    _locked_state_update(_remove_worktree)

    return True


def merge_worktree(agent_id: str, main_repo: str | None = None) -> tuple[bool, str]:
    """
    Merge agent's work back to the main branch.

    Uses fast-forward merge if possible, otherwise regular merge.
    If conflict detected, aborts merge but preserves branch (pushed to
    remote as safety net). The branch is NOT deleted on conflict to
    prevent permanent data loss.

    Args:
        agent_id: Unique identifier for the agent
        main_repo: Path to main repo

    Returns:
        (success, message) tuple
    """
    state = load_state()
    worktree_info = state.get("worktrees", {}).get(agent_id)

    if not worktree_info:
        return False, f"No worktree found for agent {agent_id}"

    main_repo = worktree_info.get("main_repo", main_repo)
    if main_repo is None:
        main_repo = os.getcwd()
    main_repo = str(get_main_repo_root(main_repo))

    branch_name = worktree_info.get("branch", f"{BRANCH_PREFIX}/{agent_id}")

    # Get current branch in main repo
    current = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=main_repo)
    current_branch = current.stdout.strip()

    # Check for uncommitted changes in main repo
    status = run_git(["status", "--porcelain"], cwd=main_repo)
    if status.stdout.strip():
        return False, "Main repo has uncommitted changes. Commit or stash first."

    # Safety net: push worker branch to remote before merge attempt
    push_result = run_git(
        ["push", "origin", branch_name], cwd=main_repo, check=False,
    )
    branch_pushed = push_result.returncode == 0

    # Try fast-forward merge first
    result = run_git(["merge", "--ff-only", branch_name], cwd=main_repo, check=False)
    if result.returncode == 0:
        return True, f"Fast-forward merged {branch_name} into {current_branch}"

    # Try regular merge
    result = run_git(["merge", branch_name, "--no-edit"], cwd=main_repo, check=False)
    if result.returncode == 0:
        return True, f"Merged {branch_name} into {current_branch}"

    # Conflict detected - abort but preserve branch for manual resolution
    run_git(["merge", "--abort"], cwd=main_repo, check=False)

    safety_note = ""
    if branch_pushed:
        safety_note = f" Branch pushed to origin/{branch_name} as safety net."
    else:
        safety_note = " WARNING: Could not push branch to remote — resolve locally."

    return (
        False,
        f"Merge conflict between {branch_name} and {current_branch}. "
        f"Merge aborted, branch preserved.{safety_note}",
    )


def list_worktrees() -> list[dict]:
    """List all active agent worktrees."""
    state = load_state()
    worktrees = []

    for agent_id, info in state.get("worktrees", {}).items():
        worktree_path = Path(info.get("path", ""))
        exists = worktree_path.exists()

        worktrees.append(
            {
                "agent_id": agent_id,
                "path": str(worktree_path),
                "branch": info.get("branch"),
                "main_repo": info.get("main_repo"),
                "created_at": info.get("created_at"),
                "exists": exists,
            }
        )

    return worktrees


def get_worktree_path(agent_id: str) -> Path | None:
    """Get the worktree path for an agent."""
    state = load_state()
    info = state.get("worktrees", {}).get(agent_id)
    if info:
        path = Path(info.get("path", ""))
        if path.exists():
            return path
    return None


def status_worktrees() -> list[dict]:
    """Get enriched worktree status with agent context.

    Reads per-worktree agent state files for additional context
    (task description, issue numbers) beyond what the global state tracks.
    """
    state = load_state()
    results = []

    for agent_id, info in state.get("worktrees", {}).items():
        worktree_path = Path(info.get("path", ""))
        exists = worktree_path.exists()

        entry = {
            "agent_id": agent_id,
            "path": str(worktree_path),
            "branch": info.get("branch", ""),
            "main_repo": info.get("main_repo", ""),
            "created_at": info.get("created_at", ""),
            "exists": exists,
            "has_autonomous_state": False,
            "mode": None,
        }

        if exists:
            # Check for autonomous state (propagated from main repo)
            auto_state_file = worktree_path / ".claude" / "autonomous-state.json"
            if auto_state_file.exists():
                try:
                    auto_state = json.loads(auto_state_file.read_text())
                    entry["has_autonomous_state"] = True
                    entry["mode"] = auto_state.get("mode")
                except (OSError, json.JSONDecodeError):
                    pass

            # Check for uncommitted changes
            try:
                diff = run_git(
                    ["status", "--porcelain"], cwd=str(worktree_path), check=False,
                )
                entry["dirty"] = bool(diff.stdout.strip())
            except (RuntimeError, subprocess.TimeoutExpired):
                entry["dirty"] = None

            # Count commits ahead of base
            base_commit = info.get("base_commit", "")
            if base_commit:
                try:
                    log = run_git(
                        ["rev-list", "--count", f"{base_commit}..HEAD"],
                        cwd=str(worktree_path),
                        check=False,
                    )
                    entry["commits_ahead"] = int(log.stdout.strip()) if log.stdout.strip() else 0
                except (RuntimeError, subprocess.TimeoutExpired, ValueError):
                    entry["commits_ahead"] = None

        results.append(entry)

    return results


def gc_worktrees(ttl_hours: int = 8, dry_run: bool = False) -> list[str]:
    """
    Garbage collect stale worktrees older than TTL.

    Coordinator crash recovery: Cleans up orphaned worktrees that exceed TTL.

    Cleans up:
    1. State file entries older than TTL
    2. Orphaned directories in WORKTREE_BASE not tracked in state
    3. Git worktree metadata (via git worktree prune)

    Args:
        ttl_hours: Hours before worktree is considered stale (default: 8, matches SESSION_TTL)
        dry_run: If True, only report what would be cleaned

    Returns:
        List of cleaned up agent IDs and orphan paths
    """
    from datetime import timedelta

    cleaned = []
    state = load_state()
    now = datetime.now(timezone.utc)

    def _is_expired(timestamp_str: str) -> bool:
        """Check if timestamp is older than TTL."""
        if not timestamp_str:
            return True
        try:
            ts_str = timestamp_str
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (now - ts) > timedelta(hours=ttl_hours)
        except (ValueError, TypeError):
            return True

    # 1. Clean stale entries from state file
    stale_agents = []
    for agent_id, info in state.get("worktrees", {}).items():
        if _is_expired(info.get("created_at", "")):
            stale_agents.append((agent_id, info.get("main_repo")))

    for agent_id, main_repo in stale_agents:
        if not dry_run:
            cleanup_worktree(agent_id, main_repo)
        cleaned.append(agent_id)

    # 2. Clean orphaned directories not in state file
    if WORKTREE_BASE.exists():
        current_agents = set(state.get("worktrees", {}).keys())
        for entry in WORKTREE_BASE.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in current_agents:
                continue  # Tracked in state, skip (already handled above if stale)

            # Check worktree's own state file for TTL
            agent_state_file = entry / ".claude" / "worktree-agent-state.json"
            should_clean = True

            if agent_state_file.exists():
                try:
                    agent_state = json.loads(agent_state_file.read_text())
                    if not _is_expired(agent_state.get("created_at", "")):
                        should_clean = False
                except (OSError, json.JSONDecodeError):
                    pass  # Corrupted file - clean it

            if should_clean:
                if not dry_run:
                    shutil.rmtree(entry, ignore_errors=True)
                cleaned.append(f"orphan:{entry.name}")

    # 3. Prune git worktree metadata for known repos
    main_repos = {
        info["main_repo"]
        for info in state.get("worktrees", {}).values()
        if info.get("main_repo")
    }

    for repo in main_repos:
        if Path(repo).exists():
            with contextlib.suppress(RuntimeError, subprocess.TimeoutExpired):
                run_git(["worktree", "prune"], cwd=repo, check=False)

    return cleaned


def main():
    if len(sys.argv) < 2:
        print("Usage: worktree-manager.py <command> [args]", file=sys.stderr)
        print(
            "Commands: create, cleanup, merge, list, path, is-worktree, status, gc",
            file=sys.stderr,
        )
        sys.exit(1)

    command = sys.argv[1]

    try:
        if command == "create":
            if len(sys.argv) < 3:
                print("Usage: worktree-manager.py create <agent-id>", file=sys.stderr)
                sys.exit(1)
            agent_id = sys.argv[2]
            main_repo = sys.argv[3] if len(sys.argv) > 3 else None
            path = create_worktree(agent_id, main_repo)
            print(f"Created worktree at: {path}")
            print(str(path))  # Machine-readable output on last line

        elif command == "cleanup":
            if len(sys.argv) < 3:
                print("Usage: worktree-manager.py cleanup <agent-id>", file=sys.stderr)
                sys.exit(1)
            agent_id = sys.argv[2]
            main_repo = sys.argv[3] if len(sys.argv) > 3 else None
            preserve = "--preserve-branch" in sys.argv
            cleanup_worktree(agent_id, main_repo, preserve_branch=preserve)
            msg = f"Cleaned up worktree for agent: {agent_id}"
            if preserve:
                msg += " (branch preserved)"
            print(msg)

        elif command == "merge":
            if len(sys.argv) < 3:
                print("Usage: worktree-manager.py merge <agent-id>", file=sys.stderr)
                sys.exit(1)
            agent_id = sys.argv[2]
            main_repo = sys.argv[3] if len(sys.argv) > 3 else None
            success, message = merge_worktree(agent_id, main_repo)
            print(message)
            sys.exit(0 if success else 2)

        elif command == "list":
            worktrees = list_worktrees()
            if not worktrees:
                print("No active worktrees")
            else:
                for wt in worktrees:
                    wt_status = "active" if wt["exists"] else "missing"
                    print(f"  {wt['agent_id']}: {wt['path']} ({wt_status})")

        elif command == "path":
            if len(sys.argv) < 3:
                print("Usage: worktree-manager.py path <agent-id>", file=sys.stderr)
                sys.exit(1)
            agent_id = sys.argv[2]
            path = get_worktree_path(agent_id)
            if path:
                print(str(path))
            else:
                print(f"No worktree found for agent: {agent_id}", file=sys.stderr)
                sys.exit(1)

        elif command == "is-worktree":
            cwd = sys.argv[2] if len(sys.argv) > 2 else None
            if is_worktree(cwd):
                info = get_worktree_info(cwd)
                print(json.dumps(info, indent=2))
                sys.exit(0)
            else:
                print("Not a worktree")
                sys.exit(1)

        elif command == "status":
            entries = status_worktrees()
            if not entries:
                print("No active worktrees")
            else:
                # Header
                print(f"{'Agent':<16} {'Branch':<30} {'Commits':<8} {'Dirty':<6} {'Mode':<10} {'Status'}")
                print("-" * 90)
                for e in entries:
                    wt_status = "active" if e["exists"] else "MISSING"
                    commits = str(e.get("commits_ahead", "?"))
                    dirty = "yes" if e.get("dirty") else "no" if e.get("dirty") is False else "?"
                    mode = e.get("mode") or "-"
                    enforcement = "OK" if e.get("has_autonomous_state") else "NO ENFORCEMENT"
                    print(f"  {e['agent_id']:<14} {e['branch']:<30} {commits:<8} {dirty:<6} {mode:<10} {wt_status} {enforcement}")

        elif command == "gc":
            # gc command: optional ttl_hours arg, optional --dry-run flag
            ttl = 8  # default
            dry_run = "--dry-run" in sys.argv
            for arg in sys.argv[2:]:
                if arg.isdigit():
                    ttl = int(arg)
            cleaned = gc_worktrees(ttl_hours=ttl, dry_run=dry_run)
            if cleaned:
                prefix = "Would clean" if dry_run else "Cleaned"
                print(f"{prefix} {len(cleaned)} stale worktree(s):")
                for item in cleaned:
                    print(f"  - {item}")
            else:
                print("No stale worktrees found")

        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
