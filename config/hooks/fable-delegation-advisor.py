#!/usr/bin/env python3
"""
PreToolUse hook — advisory orchestrator-mode delegation nudge (fable + agimode).

When Fable mode OR agimode is active (a valid ``.claude/fable-state.json`` /
``.claude/agimode-state.json`` resolves for the session's cwd) and the agent is
about to edit a SOURCE file directly, inject a non-blocking reminder that — as
orchestrator-only — substantive work belongs in a delegated lane, not the main
session's own hands. The lane named tracks the mode: fable → Opus subagent /
codex lane; agimode → the worktree-isolated codex fleet (``agimode_fleet.py``).

Doctrine difference encoded here: fable allows trivial direct edits (single
file, ~<=10 lines, no new logic — silence-biased heuristic below); agimode does
NOT — every source edit advises (only the carve-outs stay silent), because in
agimode ALL product-source execution is fleet-delegated. agimode takes
precedence when both states resolve (both-on is doctrine-forbidden anyway).

The reminder is emitted as ``hookSpecificOutput.additionalContext`` ONLY — with no
``permissionDecision`` — so it adds context without overriding any other guard.
This hook NEVER blocks; the agent decides whether to delegate.

Filename deliberately has NO ``-guard`` suffix: ``run-python-hook.sh`` fails open
for non-guard hooks, which is exactly what an advisory hook wants.

Hook event: PreToolUse
Matcher: Write|Edit|MultiEdit|NotebookEdit

Opt out: set ``FABLE_ADVISOR_SKIP=1``.

Canary signal: ``timed_hook`` logs a ``hook-metrics.jsonl`` line on EVERY
invocation, so line-presence proves nothing. ``metrics["decision"]`` (always set,
one of inactive / subagent / silent_carveout / silent_trivial / cooldown /
advised) is the only valid signal for the live canary.
``metrics["subagent_signal"]`` ("stdin-agent-keys" | "transcript" | "none",
always set) names WHICH detector attributed a subagent. Detection keys on the
presence of ``agent_id``/``agent_type`` in the hook stdin — empirically
captured: a subagent's PreToolUse stdin carries those keys, a main agent's does
not, and transcript_path is the PARENT session's jsonl in BOTH contexts (so a
sidechain-shaped transcript is only a secondary positive). The
CLAUDE_CODE_CHILD_SESSION env var is poisoned in some terminal-multiplexer-
launched MAIN sessions (run #6 misattributed a main-level edit) and is consulted
by NEITHER path.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Add hooks directory to path for shared imports.
sys.path.insert(0, str(Path(__file__).parent))
from _common import timed_hook
from _session import (
    get_fable_state,
    is_agimode_mode_active,
    is_fable_mode_active,
    resolve_agimode_state_path,
    resolve_fable_state_path,
)

# Tools whose payload is a direct file edit/write. MultiEdit is included
# deliberately: a substantive multi-hunk edit would otherwise bypass the advisor.
TARGET_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# Advisory cooldown: at most one advisory per this window, via an mtime stamp
# beside the resolved state file. Prevents an advisory flood in a long
# fable+autonomous arc. Per-mode stamps so fable and agimode cool down
# independently.
COOLDOWN_SECONDS = 600

STAMP_FILENAMES = {
    "fable": "fable-advisor.stamp",
    "agimode": "agimode-advisor.stamp",
}

# Trivial-edit threshold. The CANONICAL definition lives in
# config/rules/toolkit-fable-mode.md ("single file, ~<=10 changed lines, no new
# control flow or definitions"); this is its mechanical, DELIBERATELY
# SILENCE-BIASED encoding. False-negatives (an edit the heuristic calls trivial
# that a human would delegate) are accepted over nagging — the hook is advisory.
TRIVIAL_MAX_LINES = 10

# New-logic tokens. Any INCREASE in the count of these from old_string to
# new_string makes an edit non-trivial regardless of line count. Kept as literal
# substrings (not word-boundary regex) so e.g. "function " / "=>" in JS count too.
LOGIC_TOKENS = ("def ", "class ", "if ", "for ", "while ", "function ", "=>", "async ")


def _advisory_text(lane: str) -> str:
    """The fable advisory injected as additionalContext, naming the session lane."""
    return (
        "FABLE MODE (advisory): you are editing a source file directly. You are "
        "orchestrator-only — delegate substantive work to an Opus subagent "
        '(Agent tool, model="opus") or the codex lane (session default: '
        f"{lane}). Trivial edits (single file, ~<=10 lines, no new logic) are "
        "fine to do yourself. Advisory, never a block — you decide."
    )


def _agimode_advisory_text() -> str:
    """The agimode advisory: every product-source edit belongs to the fleet."""
    return (
        "AGIMODE (advisory): you are editing a source file directly. You are "
        "orchestrator-only — decompose this into file-disjoint spec packets and "
        "dispatch the worktree-isolated codex fleet (agimode_fleet.py) instead. "
        "Only coordination edits to planning/state artifacts (.claude/, tasks/, "
        "the job JSON, docs) are yours. Advisory, never a block — you decide."
    )


def _target_path(tool_name: str, tool_input: dict) -> str:
    """Extract the file path the tool will write, per tool shape."""
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path", "") or ""
    # Write / Edit / MultiEdit all key the single target on file_path.
    return tool_input.get("file_path", "") or ""


def _is_carveout(path: str) -> bool:
    """Paths the advisor stays silent on (planning/memory/docs, never code).

    Carve-outs: anything under a ``.claude/`` dir or a leading ``.claude/``;
    anything under ``tasks/``; plan/goal/memory artifacts by basename; and ANY
    Markdown file (docs are not the substantive code work Fable delegates).
    """
    norm = path.replace("\\", "/")
    if "/.claude/" in norm or norm.startswith(".claude/"):
        return True
    if norm.startswith("tasks/") or "/tasks/" in norm:
        return True
    name = norm.rsplit("/", 1)[-1]
    if name.endswith(".plan.md") or name.startswith(("PLAN_", "GOAL_")):
        return True
    if name == "MEMORIES.md":
        return True
    return name.endswith(".md")


def _changed_lines(old_text: str, new_text: str) -> int:
    """Silence-biased changed-line estimate: the larger of the two side counts."""
    return max(len(old_text.splitlines()), len(new_text.splitlines()))


def _adds_logic(old_text: str, new_text: str) -> bool:
    """True if new_text introduces more new-logic tokens than old_text.

    Any positive delta on any token counts as new logic — see LOGIC_TOKENS.
    """
    return any(
        new_text.count(tok) > old_text.count(tok) for tok in LOGIC_TOKENS
    )


def _edit_is_trivial(tool_input: dict) -> bool:
    """Single Edit: <=10 changed lines AND no new control-flow/def tokens."""
    old_text = tool_input.get("old_string", "") or ""
    new_text = tool_input.get("new_string", "") or ""
    if _adds_logic(old_text, new_text):
        return False
    return _changed_lines(old_text, new_text) <= TRIVIAL_MAX_LINES


def _multiedit_is_trivial(tool_input: dict) -> bool:
    """MultiEdit: sum changed lines across edits[]; same token rule; <=10 total."""
    edits = tool_input.get("edits", [])
    if not isinstance(edits, list):
        return False
    total = 0
    for edit in edits:
        if not isinstance(edit, dict):
            return False
        old_text = edit.get("old_string", "") or ""
        new_text = edit.get("new_string", "") or ""
        if _adds_logic(old_text, new_text):
            return False
        total += _changed_lines(old_text, new_text)
    return total <= TRIVIAL_MAX_LINES


def _is_trivial(tool_name: str, tool_input: dict) -> bool:
    """Whether a direct edit is trivial enough to skip the advisory.

    Write of a new OR existing source file is whole-file content — never
    trivial. NotebookEdit is never trivial. Edit / MultiEdit use the line +
    token heuristic above.
    """
    if tool_name == "Edit":
        return _edit_is_trivial(tool_input)
    if tool_name == "MultiEdit":
        return _multiedit_is_trivial(tool_input)
    # Write and NotebookEdit are never trivial.
    return False


def _cooldown_active_then_stamp(cwd: str, mode: str) -> bool:
    """Return True if within the cooldown window; else touch the stamp.

    The stamp lives next to the resolved mode-state file so the cooldown is
    shared by every hook invocation in the repo. A stamp whose mtime is within
    COOLDOWN_SECONDS suppresses the advisory; otherwise the stamp is (re)touched
    and the advisory proceeds. Any filesystem error fails OPEN (advise).
    """
    try:
        resolver = (
            resolve_agimode_state_path if mode == "agimode" else resolve_fable_state_path
        )
        state_path = Path(resolver(cwd))
        stamp = state_path.parent / STAMP_FILENAMES[mode]
        now = time.time()
        if stamp.exists() and (now - stamp.stat().st_mtime) < COOLDOWN_SECONDS:
            return True
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        return False
    return False


def _decide(tool_name: str, tool_input: dict, cwd: str, mode: str) -> tuple[str, str]:
    """Core decision. Returns (decision, lane) — lane only set when advised.

    Precedence: carve-out -> trivial (fable only) -> cooldown -> advised.
    agimode skips the trivial-edit silence: its doctrine sends EVERY product-
    source change to the fleet, so a small direct source edit still advises.
    """
    path = _target_path(tool_name, tool_input)
    if _is_carveout(path):
        return "silent_carveout", ""
    if mode == "fable" and _is_trivial(tool_name, tool_input):
        return "silent_trivial", ""

    lane = ""
    if mode == "fable":
        state = get_fable_state(cwd) or {}
        lane = state.get("lane", "opus")

    if _cooldown_active_then_stamp(cwd, mode):
        return "cooldown", ""
    return "advised", lane


# Sidechain-shaped transcript markers — SECONDARY positive only. Live probes
# show transcript_path is the PARENT session's uuid.jsonl in BOTH main and
# subagent contexts, so this shape has never been observed live; kept as a
# harmless extra positive in case a harness version emits it.
SUBAGENT_TRANSCRIPT_DIR = "/subagents/"
SUBAGENT_TRANSCRIPT_PREFIX = "agent-"


def _detect_subagent(input_data: dict) -> tuple[bool, str]:
    """Detect a subagent tool call from hook stdin — grounded live, twice falsified.

    Observed on this harness (2.1.x, probe 2026-06-12T15:05Z, both contexts
    minutes apart): a MAIN-agent Edit's PreToolUse stdin carries
    {cwd, effort, hook_event_name, permission_mode, session_id, tool_input,
    tool_name, tool_use_id, transcript_path}; a SUBAGENT Edit carries the SAME
    keys PLUS ``agent_id`` and ``agent_type``. The transcript_path is the
    PARENT session's UUID jsonl in BOTH cases (no /subagents/ component), so
    transcript-shape detection cannot discriminate (falsified hypothesis #2).
    CLAUDE_CODE_CHILD_SESSION=1 is present in main-session tool children too
    in certain wrapper-launched sessions (falsified hypothesis #1) — never use it.

    Precedence:
      1. ``agent_id`` or ``agent_type`` in stdin  => (True, "stdin-agent-keys")
      2. sidechain-shaped transcript_path         => (True, "transcript")
         (not observed live yet; kept as a harmless secondary positive)
      3. otherwise                                => (False, "none")
    """
    if "agent_id" in input_data or "agent_type" in input_data:
        return True, "stdin-agent-keys"
    transcript_path = str(input_data.get("transcript_path", "") or "")
    if transcript_path:
        norm = transcript_path.replace("\\", "/")
        name = norm.rsplit("/", 1)[-1]
        if SUBAGENT_TRANSCRIPT_DIR in norm or name.startswith(
            SUBAGENT_TRANSCRIPT_PREFIX
        ):
            return True, "transcript"
    return False, "none"


def main(metrics: dict) -> None:
    """Advisory logic. Always sets ``metrics['decision']``; exits 0."""
    metrics["decision"] = "inactive"
    metrics["subagent_signal"] = "none"

    try:
        input_data = json.load(sys.stdin)
        # Diagnosability: record which transcript this invocation belongs to and
        # the stdin field names (names only, never values) — this is how the
        # subagent-vs-main discriminator is grounded in real hook input.
        metrics["transcript_base"] = os.path.basename(
            str(input_data.get("transcript_path", ""))
        )
        metrics["stdin_keys"] = ",".join(sorted(input_data.keys()))
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # malformed stdin — fail open, stay inactive

    # Subagent guard — FIRST positive decision, before the skip/state checks,
    # so subagent attribution always wins. Subagent edits ARE the delegated
    # work the doctrine dispatches — never advise on them. Detection is
    # stdin-agent-keys primary, transcript shape secondary, env var never
    # consulted (see _detect_subagent for the two live falsifications).
    # Honest note: whether PreToolUse hooks fire inside subagents AT ALL is
    # non-deterministic on harness 2.1.175 (canary runs observed both zero
    # hook lines and full firing for identical subagent payloads); this guard
    # cannot make firing deterministic, but it makes the DECISION
    # deterministic ("subagent", silent) when hooks fire.
    is_subagent, signal = _detect_subagent(input_data)
    metrics["subagent_signal"] = signal
    if is_subagent:
        metrics["decision"] = "subagent"
        sys.exit(0)

    if os.environ.get("FABLE_ADVISOR_SKIP") == "1":
        sys.exit(0)

    tool_name = input_data.get("tool_name", "")
    if tool_name not in TARGET_TOOLS:
        sys.exit(0)

    cwd = input_data.get("cwd", "") or os.getcwd()
    # agimode takes precedence over fable (both-on is doctrine-forbidden);
    # neither active (or invalid state) — inactive.
    if is_agimode_mode_active(cwd):
        mode = "agimode"
    elif is_fable_mode_active(cwd):
        mode = "fable"
    else:
        sys.exit(0)
    metrics["mode"] = mode

    tool_input = input_data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}

    decision, lane = _decide(tool_name, tool_input, cwd, mode)
    metrics["decision"] = decision

    if decision != "advised":
        sys.exit(0)

    text = _agimode_advisory_text() if mode == "agimode" else _advisory_text(lane)
    print(json.dumps({  # noqa: T201 -- PreToolUse stdout is injected as advisory context
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": text,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    with timed_hook("fable-delegation-advisor") as metrics:
        try:
            main(metrics)
        except SystemExit:
            raise
        except Exception:
            # Fail open on any unexpected error: never block, never emit a
            # traceback to stdout (stderr is fine). Keep the decision as set.
            metrics.setdefault("decision", "inactive")
            sys.exit(0)
