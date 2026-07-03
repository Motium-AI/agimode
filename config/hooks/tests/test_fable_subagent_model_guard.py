#!/usr/bin/env python3
"""Subprocess tests for the Fable subagent model guard.

The hook's authority comes from real transcript JSONL, so these tests write
real transcript files and run the hook as a subprocess through ``run_hook``.
Noise lines intentionally include malformed JSON, user records, and sidechain
assistant records to prove the tail-reader selects only the latest main
assistant model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import HOOKS_DIR, run_hook

HOOK = HOOKS_DIR / "fable-subagent-model-guard.py"
SETTINGS = HOOKS_DIR.parent / "settings.json"


def _assistant(model: str, *, sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"model": model},
        "sessionId": "test-session",
    }


def _user() -> dict:
    return {"type": "user", "isSidechain": False, "message": {"content": "hi"}}


def _write_transcript(path: Path, records: list[dict | str]) -> Path:
    lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n")
    return path


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def _stdin(proj: Path, transcript: Path | None, tool_input: dict | None = None) -> dict:
    payload = {
        "tool_name": "Agent",
        "tool_input": {"prompt": "do the work"} if tool_input is None else tool_input,
        "cwd": str(proj),
    }
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return payload


def _run_agent(
    proj: Path,
    transcript: Path | None,
    tool_input: dict | None = None,
    stdin_extra: dict | None = None,
) -> tuple[int, str, str]:
    payload = _stdin(proj, transcript, tool_input)
    if stdin_extra:
        payload.update(stdin_extra)
    return run_hook(HOOK, payload, cwd=str(proj))


def _last_decision(proj: Path) -> str | None:
    metrics = proj.parent / ".claude" / "hook-metrics.jsonl"
    decision = None
    if not metrics.exists():
        return None
    for line in metrics.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("hook") == "fable-subagent-model-guard":
            decision = entry.get("decision")
    return decision


def _blocking_transcript(tmp_path: Path) -> Path:
    return _write_transcript(
        tmp_path / "transcript.jsonl",
        [
            _user(),
            "{malformed json",
            _assistant("claude-opus-4-8", sidechain=True),
            _assistant("claude-fable-5"),
        ],
    )


@pytest.mark.parametrize("parent_model", ["claude-fable-5", " Claude-Fable-6 "])
def test_fable_parent_inherited_agent_model_blocks(tmp_path, parent_model):
    proj = _project(tmp_path)
    transcript = _write_transcript(
        tmp_path / "transcript.jsonl",
        [
            _user(),
            "{malformed json",
            _assistant("claude-opus-4-8", sidechain=True),
            _assistant(parent_model),
        ],
    )

    code, out, err = _run_agent(proj, transcript)

    assert code == 2
    assert out == ""
    assert "INHERIT Fable" in err
    assert 'model="opus"' in err
    assert 'model="sonnet"' in err
    assert "FABLE_SUBAGENT_GUARD_SKIP" in err
    assert _last_decision(proj) == "blocked_inherit"


def test_fable_parent_explicit_opus_is_allowed(tmp_path):
    proj = _project(tmp_path)
    transcript = _blocking_transcript(tmp_path)

    code, out, err = _run_agent(proj, transcript, {"prompt": "x", "model": "opus"})

    assert code == 0
    assert out == ""
    assert err == ""
    assert _last_decision(proj) == "allowed_override"


@pytest.mark.parametrize("model", ["fable", "claude-fable-5"])
def test_fable_parent_explicit_fable_blocks(tmp_path, model):
    proj = _project(tmp_path)
    transcript = _blocking_transcript(tmp_path)

    code, out, err = _run_agent(proj, transcript, {"prompt": "x", "model": model})

    assert code == 2
    assert out == ""
    assert "explicitly requests Fable" in err
    assert 'model="opus"' in err
    assert _last_decision(proj) == "blocked_explicit"


def test_opus_parent_inherited_agent_model_is_allowed(tmp_path):
    proj = _project(tmp_path)
    transcript = _write_transcript(
        tmp_path / "transcript.jsonl",
        [_assistant("claude-opus-4-8")],
    )

    code, out, err = _run_agent(proj, transcript)

    assert code == 0
    assert out == ""
    assert err == ""
    assert _last_decision(proj) == "parent_not_fable"


def test_latest_main_assistant_model_wins(tmp_path):
    proj = _project(tmp_path)
    older_fable = _write_transcript(
        tmp_path / "older-fable.jsonl",
        [_assistant("claude-fable-5"), _assistant("claude-opus-4-8")],
    )

    code, _out, _err = _run_agent(proj, older_fable)

    assert code == 0
    assert _last_decision(proj) == "parent_not_fable"

    latest_fable = _write_transcript(
        tmp_path / "latest-fable.jsonl",
        [_assistant("claude-opus-4-8"), _assistant("claude-fable-5")],
    )

    code, _out, err = _run_agent(proj, latest_fable)

    assert code == 2
    assert "INHERIT Fable" in err
    assert _last_decision(proj) == "blocked_inherit"


def test_sidechain_fable_records_do_not_count(tmp_path):
    proj = _project(tmp_path)
    transcript = _write_transcript(
        tmp_path / "transcript.jsonl",
        [
            _assistant("claude-opus-4-8"),
            _assistant("claude-fable-5", sidechain=True),
        ],
    )

    code, out, err = _run_agent(proj, transcript)

    assert code == 0
    assert out == ""
    assert err == ""
    assert _last_decision(proj) == "parent_not_fable"


def test_missing_unreadable_or_empty_transcript_fails_open(tmp_path):
    proj = _project(tmp_path)

    code, _out, _err = _run_agent(proj, None)
    assert code == 0
    assert _last_decision(proj) == "no_model_evidence"

    code, _out, _err = _run_agent(proj, tmp_path / "missing.jsonl")
    assert code == 0
    assert _last_decision(proj) == "no_model_evidence"

    no_assistant = _write_transcript(
        tmp_path / "no-assistant.jsonl",
        [_user(), "{malformed json", {"type": "tool_result"}],
    )
    code, _out, _err = _run_agent(proj, no_assistant)
    assert code == 0
    assert _last_decision(proj) == "no_model_evidence"


def test_non_agent_and_malformed_stdin_fail_open(tmp_path):
    proj = _project(tmp_path)
    transcript = _blocking_transcript(tmp_path)

    code, out, err = run_hook(
        HOOK,
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "cwd": str(proj),
            "transcript_path": str(transcript),
        },
        cwd=str(proj),
    )

    assert code == 0
    assert out == ""
    assert err == ""
    assert _last_decision(proj) == "inactive"

    env = os.environ.copy()
    env["HOME"] = str(proj.parent)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{not json",
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(proj),
        env=env,
    )

    assert proc.returncode == 0
    assert proc.stdout == ""


def test_skip_env_allows_otherwise_blocking_payload(tmp_path):
    proj = _project(tmp_path)
    transcript = _blocking_transcript(tmp_path)
    old = os.environ.get("FABLE_SUBAGENT_GUARD_SKIP")
    os.environ["FABLE_SUBAGENT_GUARD_SKIP"] = "1"
    try:
        code, out, err = _run_agent(proj, transcript)
    finally:
        if old is None:
            os.environ.pop("FABLE_SUBAGENT_GUARD_SKIP", None)
        else:
            os.environ["FABLE_SUBAGENT_GUARD_SKIP"] = old

    assert code == 0
    assert out == ""
    assert err == ""
    assert _last_decision(proj) == "skipped"


def test_subagent_context_allows_otherwise_blocking_payload(tmp_path):
    proj = _project(tmp_path)
    transcript = _blocking_transcript(tmp_path)

    code, out, err = _run_agent(proj, transcript, stdin_extra={"agent_id": "agent-1"})

    assert code == 0
    assert out == ""
    assert err == ""
    assert _last_decision(proj) == "subagent"


def test_large_transcript_tail_window_uses_latest_in_window_record(tmp_path):
    proj = _project(tmp_path)
    filler = json.dumps(_user()) + "\n"
    filler_count = (5 * 1024 * 1024 // len(filler)) + 100

    latest_fable = tmp_path / "large-latest-fable.jsonl"
    latest_fable.write_text(
        json.dumps(_assistant("claude-opus-4-8"))
        + "\n"
        + (filler * filler_count)
        + json.dumps(_assistant("claude-fable-5"))
        + "\n"
    )

    code, _out, err = _run_agent(proj, latest_fable)

    assert latest_fable.stat().st_size > 5 * 1024 * 1024
    assert code == 2
    assert "INHERIT Fable" in err
    assert _last_decision(proj) == "blocked_inherit"

    latest_opus = tmp_path / "large-latest-opus.jsonl"
    latest_opus.write_text(
        json.dumps(_assistant("claude-fable-5"))
        + "\n"
        + (filler * filler_count)
        + json.dumps(_assistant("claude-opus-4-8"))
        + "\n"
    )

    code, out, err = _run_agent(proj, latest_opus)

    assert latest_opus.stat().st_size > 5 * 1024 * 1024
    assert code == 0
    assert out == ""
    assert err == ""
    assert _last_decision(proj) == "parent_not_fable"


def test_settings_registers_agent_pretooluse_guard():
    settings = json.loads(SETTINGS.read_text())
    pretool = settings["hooks"]["PreToolUse"]
    blocks = [
        block
        for block in pretool
        if block.get("matcher") == "Agent"
        and any(
            "fable-subagent-model-guard.py" in hook.get("command", "")
            for hook in block.get("hooks", [])
        )
    ]

    assert len(blocks) == 1
    assert blocks[0]["hooks"] == [
        {
            "type": "command",
            "command": (
                "\"$HOME/.claude/hooks/run-python-hook.sh\" "
                "\"$HOME/.claude/hooks/fable-subagent-model-guard.py\""
            ),
            "timeout": 5,
        }
    ]
