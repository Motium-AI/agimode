#!/usr/bin/env python3
"""PreToolUse guard - block Fable main sessions from spawning Fable subagents.

Mechanism: PreToolUse stdin does not expose the parent model, so this hook reads
``transcript_path`` and inspects only the last ~4MB of JSONL. It walks lines in
reverse and uses the latest non-sidechain assistant record with a
``message.model`` field as the parent-model evidence.

Honest limits: this is a post-hoc read of the last flushed assistant record. A
mid-turn ``/model`` switch can lag one turn if the transcript has not flushed a
new assistant record yet. Subagent-context spawns are not gated in v1; if hook
stdin carries ``agent_id`` or ``agent_type``, the decision is ``subagent`` and
the tool call proceeds.

Decision vocabulary: ``inactive`` (malformed stdin or non-Agent tool),
``skipped`` (emergency env opt-out), ``subagent`` (subagent context),
``no_model_evidence`` (missing/unreadable transcript or no usable assistant
record), ``parent_not_fable`` (latest parent model is not Fable),
``blocked_inherit`` (Agent would inherit Fable), ``blocked_explicit`` (Agent
explicitly requested Fable), and ``allowed_override`` (explicit cheaper/non-Fable
model).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# Add hooks directory to path for shared imports.
sys.path.insert(0, str(Path(__file__).parent))
from _common import timed_hook

HOOK_NAME = "fable-subagent-model-guard"
TAIL_BYTES = 4 * 1024 * 1024
SKIP_ENV = "FABLE_SUBAGENT_GUARD_SKIP"


def _load_input(metrics: dict) -> dict | None:
    """Parse hook stdin. Malformed stdin is inactive and fail-open."""
    metrics["decision"] = "inactive"
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _latest_parent_model(transcript_path: str) -> str | None:
    """Read the transcript tail and return the latest main assistant model."""
    if not transcript_path:
        return None

    path = Path(transcript_path)
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES), os.SEEK_SET)
            chunk = fh.read()
    except OSError:
        return None

    for raw_line in reversed(chunk.decode("utf-8", errors="replace").splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != "assistant" or record.get("isSidechain") is not False:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if model:
            return str(model)
    return None


def _parent_is_fable(model: str) -> bool:
    """Transcript model predicate for full Fable model ids."""
    return model.strip().lower().startswith("claude-fable")


def _requested_model_is_fable(model: object) -> bool:
    """Agent model override predicate for aliases and full Fable model ids."""
    model_text = str(model).strip().lower()
    return model_text == "fable" or model_text.startswith("claude-fable")


def _block_message(kind: str) -> str:
    """Actionable stderr text for the two block modes."""
    if kind == "inherit":
        reason = "The sub-agent would INHERIT Fable from this main session."
    else:
        reason = "The sub-agent explicitly requests Fable."
    return (
        "BLOCKED: Fable is orchestrator-only; do not spawn a Fable sub-agent.\n"
        f"{reason}\n"
        'Re-spawn with an explicit cheaper model: model="opus" for '
        'judgment-heavy work, or model="sonnet" for mechanical/research work. '
        "Never use Fable for the sub-agent.\n"
        f"Emergency opt-out only: set {SKIP_ENV}=1."
    )


def _block(decision: str, message_kind: str, metrics: dict) -> NoReturn:
    """Set the blocking decision, print stderr, and exit with hook block code."""
    metrics["decision"] = decision
    print(_block_message(message_kind), file=sys.stderr)  # noqa: T201
    sys.exit(2)


def main(metrics: dict) -> None:
    """Guard logic. Always sets ``metrics['decision']`` before exiting."""
    input_data = _load_input(metrics)
    if input_data is None:
        sys.exit(0)

    if input_data.get("tool_name") != "Agent":
        sys.exit(0)

    if os.environ.get(SKIP_ENV) == "1":
        metrics["decision"] = "skipped"
        sys.exit(0)

    if "agent_id" in input_data or "agent_type" in input_data:
        metrics["decision"] = "subagent"
        sys.exit(0)

    parent_model = _latest_parent_model(str(input_data.get("transcript_path", "") or ""))
    if not parent_model:
        metrics["decision"] = "no_model_evidence"
        sys.exit(0)

    if not _parent_is_fable(parent_model):
        metrics["decision"] = "parent_not_fable"
        sys.exit(0)

    tool_input = input_data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}

    if "model" not in tool_input or not str(tool_input.get("model") or "").strip():
        _block("blocked_inherit", "inherit", metrics)

    if _requested_model_is_fable(tool_input.get("model")):
        _block("blocked_explicit", "explicit", metrics)

    metrics["decision"] = "allowed_override"
    sys.exit(0)


if __name__ == "__main__":
    with timed_hook(HOOK_NAME) as _metrics:
        try:
            main(_metrics)
        except SystemExit:
            raise
        except Exception:
            # Fail open on unexpected errors: this guard blocks only with clear
            # transcript evidence, never because its own machinery failed.
            _metrics.setdefault("decision", "inactive")
            sys.exit(0)
