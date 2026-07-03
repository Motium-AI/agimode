#!/usr/bin/env python3
"""
Mode-state management for agimode/fable Claude Code hooks.

A single-file, task-agnostic mode-state system:

- One state file per mode: agimode-state.json / fable-state.json
  (orchestrator-only toggles that persist until the mode is turned off)
- Atomic writes via tempfile+fsync+rename (see _common.atomic_write_json)
- Worktree-aware resolution: write-then-read from any subdir/worktree surface
  of the same project converges on ONE path

Exports the API surface consumed by the agimode/fable hooks
(agimode-session-surface, fable-subagent-model-guard, fable-delegation-advisor).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _common import atomic_write_json

# Fable mode state file (orchestrator-only toggle; persists until /fable off).
FABLE_STATE_FILENAME = "fable-state.json"

# Lanes accepted by Fable mode (default lane is "opus").
FABLE_LANES = ("opus", "codex", "codex-fast")

# agimode mode state file (orchestrator-only toggle; persists until /agimode off).
AGIMODE_STATE_FILENAME = "agimode-state.json"
# Orchestrator identities agimode accepts: Fable 5 if self-reported, else Opus 4.8.
AGIMODE_ORCHESTRATORS = ("fable", "opus")


# ============================================================================
# File Location
# ============================================================================


def _walk_up_for_claude_file(cwd: str, filename: str) -> Path | None:
    """Walk up from ``cwd`` looking for ``.claude/<filename>``.

    Works for the main repo AND worktrees with propagated state.
    Returns the first existing path found, or None.
    """
    current = Path(cwd).resolve()
    home = Path.home()
    for _ in range(20):
        if current == home:
            break
        candidate = current / ".claude" / filename
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _resolve_via_worktree_main_repo(cwd: str, filename: str) -> Path | None:
    """Resolve ``.claude/<filename>`` via the worktree's ``main_repo``.

    When CWD is a git worktree (e.g. ~/.claude/worktrees/worker-1), the
    normal walk-up may not find the main repo's state. Reads the
    worktree's ``worktree-agent-state.json`` (which stores ``main_repo``)
    and returns that repo's ``.claude/<filename>`` if it exists.
    """
    current = Path(cwd).resolve()
    home = Path.home()
    for _ in range(20):
        if current == home:
            break
        agent_state_file = current / ".claude" / "worktree-agent-state.json"
        if agent_state_file.exists():
            try:
                agent_state = json.loads(agent_state_file.read_text())
                main_repo = agent_state.get("main_repo")
                if main_repo:
                    main_state = Path(main_repo) / ".claude" / filename
                    if main_state.exists():
                        return main_state
            except (json.JSONDecodeError, OSError):
                pass
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _find_claude_file(cwd: str, filename: str) -> Path | None:
    """Find ``.claude/<filename>`` walking up the directory tree.

    Generic resolver shared by the mode-state lookups. Handles worktree
    context: when CWD is a git worktree (e.g., ~/.claude/worktrees/worker-1),
    the normal walk-up may not find the main repo's file. Falls back to
    reading the worktree's ``worktree-agent-state.json`` which stores the
    ``main_repo`` path.
    """
    if not cwd:
        return None
    found = _walk_up_for_claude_file(cwd, filename)
    if found is not None:
        return found
    return _resolve_via_worktree_main_repo(cwd, filename)


def _load_state(path: Path) -> dict | None:
    """Load and parse a state JSON file."""
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ============================================================================
# Project-root anchoring (for first-enable writes)
# ============================================================================


def _git_toplevel(cwd: str) -> Path | None:
    """Return the git repo root for ``cwd`` via ``git rev-parse``, or None.

    Fail-soft: any error (not a repo, git missing, timeout) yields None so the
    caller falls back to a cwd-anchored target.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    return Path(top).resolve()


def _worktree_main_repo_dir(cwd: str) -> Path | None:
    """Return the worktree's ``main_repo`` directory for ``cwd``, or None.

    Mirrors ``_resolve_via_worktree_main_repo`` but returns the main repo's
    ``.claude`` dir REGARDLESS of whether the file already exists — so a
    first-enable from a worktree surface anchors its write at the same main
    repo the reads resolve through.
    """
    current = Path(cwd).resolve()
    home = Path.home()
    for _ in range(20):
        if current == home:
            break
        agent_state_file = current / ".claude" / "worktree-agent-state.json"
        if agent_state_file.exists():
            try:
                agent_state = json.loads(agent_state_file.read_text())
                main_repo = agent_state.get("main_repo")
                if main_repo:
                    return Path(main_repo).resolve() / ".claude"
            except (json.JSONDecodeError, OSError):
                pass
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _nearest_claude_dir_ancestor(cwd: str) -> Path | None:
    """Return the nearest ancestor of ``cwd`` that already has a ``.claude/`` dir.

    Non-git anchor for first-enable writes: an existing ``.claude/`` marks a
    project root the read walk-up will reach. Stops at $HOME (exclusive) so a
    user-level ``~/.claude`` never captures project state.
    """
    current = Path(cwd).resolve()
    home = Path.home()
    for _ in range(20):
        if current == home:
            break
        if (current / ".claude").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ----------------------------------------------------------------------------
# Mode-state core (parameterized) — fable and agimode are thin wrappers over
# this. Generalized so the strict-validation + worktree-convergence logic lives
# in ONE place. Fable's public functions keep identical names AND behavior.
# ----------------------------------------------------------------------------

def _resolve_mode_state_path(cwd: str, filename: str) -> str:
    """Return where ``.claude/<filename>`` lives or would live for ``cwd``.

    The SINGLE resolver used by both hooks (read) and a mode skill (write/remove),
    so ``on|off`` behaves identically from worktrees and subdirectories. The
    invariant: write-then-read from ANY subdir/worktree surface of the same
    project converges on ONE path.

    Resolution order: (1) an existing ``.claude/<filename>`` via walk-up (or the
    worktree ``main_repo`` fallback); (2) first-enable — anchor the write target
    at the PROJECT-ROOT ``.claude/`` (worktree main_repo dir → git toplevel →
    nearest ``.claude/`` ancestor → ``<cwd>/.claude/``). Always returns a string.
    """
    existing = _find_claude_file(cwd, filename)
    if existing is not None:
        return str(existing)

    worktree_dir = _worktree_main_repo_dir(cwd)
    if worktree_dir is not None:
        claude_dir = worktree_dir
    else:
        base = _git_toplevel(cwd) if cwd else None
        if base is None and cwd:
            base = _nearest_claude_dir_ancestor(cwd)
        if base is None:
            base = Path(cwd).resolve() if cwd else Path.cwd()
        claude_dir = base / ".claude"
    return str(claude_dir / filename)


def _get_mode_state(cwd: str, filename: str, tag: str, validate) -> dict | None:
    """Return the validated mode-state dict for ``cwd``, or None.

    ``validate(state) -> (ok, diag)`` is the per-mode strict check. Malformed
    JSON or a failed validation returns None and prints ONE stderr diagnostic
    (prefixed ``[tag]``) — never raises, never defaults. Missing file → None.
    """
    path = _find_claude_file(cwd, filename)
    if path is None:
        return None

    state = _load_state(path)
    if state is None:
        print(  # noqa: T201 — single stderr diagnostic for malformed state
            f"[{tag}] ignoring malformed {filename} at {path}", file=sys.stderr,
        )
        return None

    ok, diag = validate(state)
    if not ok:
        print(  # noqa: T201 — single stderr diagnostic for invalid state
            f"[{tag}] ignoring invalid {filename} at {path} ({diag})",
            file=sys.stderr,
        )
        return None

    return state


def _write_mode_state(cwd: str, filename: str, payload: dict) -> str:
    """Write ``payload`` to the resolved mode-state path; return the path."""
    path = Path(_resolve_mode_state_path(cwd, filename))
    atomic_write_json(path, payload)
    return str(path)


def _clear_mode_state(cwd: str, filename: str) -> bool:
    """Remove the resolved mode-state file if present; return whether removed."""
    path = _find_claude_file(cwd, filename)
    if path is None or not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


# --- Fable mode (thin wrappers; names + behavior unchanged) ------------------

def resolve_fable_state_path(cwd: str) -> str:
    """Return where fable-state.json lives or would live for ``cwd``."""
    return _resolve_mode_state_path(cwd, FABLE_STATE_FILENAME)


def _validate_fable(state: dict) -> tuple[bool, str]:
    ok = (
        state.get("schema_version") == 1
        and state.get("mode") == "fable"
        and state.get("lane") in FABLE_LANES
    )
    diag = (
        f"schema_version={state.get('schema_version')!r} "
        f"mode={state.get('mode')!r} lane={state.get('lane')!r}"
    )
    return ok, diag


def get_fable_state(cwd: str) -> dict | None:
    """Return the validated fable-state dict for ``cwd``, or None (strict)."""
    return _get_mode_state(cwd, FABLE_STATE_FILENAME, "fable", _validate_fable)


def is_fable_mode_active(cwd: str) -> bool:
    """Return True if a valid fable-state.json resolves for ``cwd``."""
    return get_fable_state(cwd) is not None


def write_fable_state(
    cwd: str,
    lane: str = "opus",
    model_expected: str = "claude-fable-5[1m]",
) -> str:
    """Write fable-state.json for ``cwd`` and return the path written."""
    if lane not in FABLE_LANES:
        raise ValueError(
            f"invalid fable lane {lane!r}; expected one of {FABLE_LANES}"
        )
    return _write_mode_state(cwd, FABLE_STATE_FILENAME, {
        "schema_version": 1,
        "mode": "fable",
        "lane": lane,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_expected": model_expected,
    })


def clear_fable_state(cwd: str) -> bool:
    """Remove the resolved fable-state.json if present."""
    return _clear_mode_state(cwd, FABLE_STATE_FILENAME)


# --- agimode (orchestrator + fleet; same strict-validation contract) ---------

def resolve_agimode_state_path(cwd: str) -> str:
    """Return where agimode-state.json lives or would live for ``cwd``."""
    return _resolve_mode_state_path(cwd, AGIMODE_STATE_FILENAME)


def _validate_agimode(state: dict) -> tuple[bool, str]:
    ok = (
        state.get("schema_version") == 1
        and state.get("mode") == "agimode"
        and state.get("orchestrator") in AGIMODE_ORCHESTRATORS
    )
    diag = (
        f"schema_version={state.get('schema_version')!r} "
        f"mode={state.get('mode')!r} orchestrator={state.get('orchestrator')!r}"
    )
    return ok, diag


def get_agimode_state(cwd: str) -> dict | None:
    """Return the validated agimode-state dict for ``cwd``, or None (strict)."""
    return _get_mode_state(cwd, AGIMODE_STATE_FILENAME, "agimode", _validate_agimode)


def is_agimode_mode_active(cwd: str) -> bool:
    """Return True if a valid agimode-state.json resolves for ``cwd``."""
    return get_agimode_state(cwd) is not None


def write_agimode_state(
    cwd: str,
    orchestrator: str = "opus",
    model_expected: str = "",
    fleet: dict | None = None,
) -> str:
    """Write agimode-state.json for ``cwd`` and return the path written.

    Validates ``orchestrator`` against ``AGIMODE_ORCHESTRATORS`` (raises
    ValueError otherwise). Carries the fleet config block. ``schema_version``
    stays at 1 for additive fleet keys because validators are permissive, so
    additions remain backward/forward compatible.
    """
    if orchestrator not in AGIMODE_ORCHESTRATORS:
        raise ValueError(
            f"invalid agimode orchestrator {orchestrator!r}; "
            f"expected one of {AGIMODE_ORCHESTRATORS}"
        )
    if fleet is None:
        # The codex-spend cap is the JOB-level max_codex_calls the orchestrator
        # passes to agimode_fleet.dispatch (cumulative per arc) — not a separate,
        # unconsumed state field. One budget name, one place.
        fleet = {
            "max_workers": 4,
            "executor": "codex",
            "codex_model": "gpt-5.5",
            "codex_effort": "xhigh",
            "claude_model": "claude-sonnet-5",
        }
    return _write_mode_state(cwd, AGIMODE_STATE_FILENAME, {
        # Additive fleet keys keep schema_version 1: validators are permissive,
        # so additions remain backward/forward compatible.
        "schema_version": 1,
        "mode": "agimode",
        "orchestrator": orchestrator,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model_expected": model_expected,
        "fleet": fleet,
    })


def clear_agimode_state(cwd: str) -> bool:
    """Remove the resolved agimode-state.json if present."""
    return _clear_mode_state(cwd, AGIMODE_STATE_FILENAME)
