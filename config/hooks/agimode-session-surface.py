#!/usr/bin/env python3
"""agimode surfacing hook (advisory, fail-open).

Fires on BOTH SessionStart and UserPromptSubmit (same script registered for
each). When agimode is active for the session's cwd, prints a single plain
reminder line — orchestrator-only doctrine, the fleet-execution lane, and the
honest "the hook cannot read the model" caveat (switch with
``/model claude-fable-5[1m]`` or run on Opus 4.8 xhigh; ``/agimode off`` to exit).

Surfacing policy:
- SessionStart: always print when agimode is active.
- UserPromptSubmit: throttled — print only when the per-state-dir stamp
  ``agimode-surface.stamp`` is missing or older than ``THROTTLE_SECONDS``.

Never blocks, never errors out loud: any exception exits 0 silently. No
``-guard`` suffix — advisory, fails open by design.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import timed_hook
from _session import get_agimode_state, resolve_agimode_state_path

# UserPromptSubmit throttle window (seconds). SessionStart always prints.
THROTTLE_SECONDS = 1800

STAMP_FILENAME = "agimode-surface.stamp"


def _surface_line(orchestrator: str) -> str:
    """Build the one-line advisory naming the active orchestrator."""
    return (
        f"AGIMODE ON (orchestrator {orchestrator}) — you orchestrate only: decompose "
        "into file-disjoint packets and dispatch the worktree-isolated gpt-5.5 codex "
        "fleet to execute; never edit source yourself. If not on claude-fable-5, run "
        "Opus 4.8 xhigh. /agimode off to exit."
    )


def _stamp_path(cwd: str) -> Path:
    """Path of the throttle stamp beside the resolved agimode-state file."""
    return Path(resolve_agimode_state_path(cwd)).parent / STAMP_FILENAME


def _throttle_allows_print(cwd: str) -> bool:
    """True if the UserPromptSubmit throttle window has elapsed (or stamp missing)."""
    stamp = _stamp_path(cwd)
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        return True
    return age >= THROTTLE_SECONDS


def _touch_stamp(cwd: str) -> None:
    """Refresh the throttle stamp's mtime (best-effort)."""
    stamp = _stamp_path(cwd)
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    except OSError:
        pass


def main(metrics: dict) -> None:
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        metrics["decision"] = "inactive"
        sys.exit(0)

    cwd = input_data.get("cwd", "")
    event = input_data.get("hook_event_name", "")

    state = get_agimode_state(cwd)
    if state is None:
        metrics["decision"] = "inactive"
        sys.exit(0)

    orchestrator = state.get("orchestrator", "opus")

    if event == "UserPromptSubmit" and not _throttle_allows_print(cwd):
        metrics["decision"] = "throttled"
        sys.exit(0)

    print(_surface_line(orchestrator))  # noqa: T201 — advisory plain-print to stdout
    _touch_stamp(cwd)
    metrics["decision"] = "surfaced"
    sys.exit(0)


if __name__ == "__main__":
    with timed_hook("agimode-session-surface") as _metrics:
        try:
            main(_metrics)
        except SystemExit:
            raise
        except Exception:
            _metrics.setdefault("decision", "inactive")
            sys.exit(0)
