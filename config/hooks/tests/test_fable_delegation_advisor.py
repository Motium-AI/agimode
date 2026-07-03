#!/usr/bin/env python3
"""Decision-matrix tests for the Fable-mode delegation advisor (PreToolUse).

Mechanism over mocks (toolkit-testing.md): every test seeds a REAL
``.claude/fable-state.json`` and runs the real hook as a subprocess — nothing is
patched. The hook is purely advisory, so each case asserts BOTH:

  * stdout behavior (advisory present / absent), and
  * the ``decision`` field on the ``hook-metrics.jsonl`` line the run appended
    — the live canary's only valid signal, since ``timed_hook`` logs EVERY run.

Metrics isolation: the project dir is nested under a fresh HOME
(``<home>/proj``). ``conftest.run_hook`` sets ``HOME=cwd.parent`` for any cwd, so
``~/.claude/hook-metrics.jsonl`` resolves under ``<home>`` — one clean metrics
file per test. The fable-state walk-up stops at HOME, so state at
``<home>/proj/.claude/`` resolves while nothing leaks from the real ~/.claude.

Subagent-env isolation: this test runner may ITSELF be a subagent carrying
``CLAUDE_CODE_CHILD_SESSION=1`` (``run_hook`` copies ``os.environ`` into the
hook subprocess). ``run_tool`` strips that marker by default and only the
``TestSubagent`` rows set it back explicitly — to PROVE the hook ignores it.

Subagent detection (live-probed, two falsified hypotheses): subagent PreToolUse
stdin carries ``agent_id``/``agent_type`` keys — the PRIMARY signal. The
transcript_path is the PARENT session's uuid.jsonl in BOTH contexts (shape
detection kept only as a secondary positive), and some terminal-multiplexer-
launched MAIN sessions inherit CLAUDE_CODE_CHILD_SESSION=1 from the wrapper
(run #6 misattributed a
main-level edit) — so the env var is NEVER consulted. The TestSubagent matrix
covers the signal combinations including both regression rows.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from conftest import HOOKS_DIR, run_hook

ADVISOR = HOOKS_DIR / "fable-delegation-advisor.py"


def _fable_state(lane: str = "opus") -> dict:
    """A valid v1 fable-state.json dict for the given lane."""
    return {
        "schema_version": 1,
        "mode": "fable",
        "lane": lane,
        "started_at": "2026-06-12T00:00:00Z",
        "model_expected": "claude-fable-5[1m]",
    }


def _agimode_state(orchestrator: str = "fable") -> dict:
    """A valid v1 agimode-state.json dict for the given orchestrator."""
    return {
        "schema_version": 1,
        "mode": "agimode",
        "orchestrator": orchestrator,
        "started_at": "2026-06-12T00:00:00Z",
        "model_expected": "claude-fable-5[1m]",
        "fleet": {"max_workers": 4, "codex_model": "gpt-5.5", "codex_effort": "xhigh"},
    }


class _AdvisorCase(unittest.TestCase):
    """Base: a fresh HOME with a nested project dir + isolated metrics."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="fable-adv-home-")
        self.proj = Path(self.home) / "proj"
        self.claude = self.proj / ".claude"
        self.claude.mkdir(parents=True)
        self.metrics_path = Path(self.home) / ".claude" / "hook-metrics.jsonl"

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    # -- fable state -------------------------------------------------------
    def fable_on(self, lane: str = "opus", state: dict | None = None) -> None:
        payload = state if state is not None else _fable_state(lane)
        body = payload if isinstance(payload, str) else json.dumps(payload)
        (self.claude / "fable-state.json").write_text(body)

    # -- agimode state ------------------------------------------------------
    def agimode_on(
        self, orchestrator: str = "fable", state: dict | None = None
    ) -> None:
        payload = state if state is not None else _agimode_state(orchestrator)
        body = payload if isinstance(payload, str) else json.dumps(payload)
        (self.claude / "agimode-state.json").write_text(body)

    # -- run + read decision ----------------------------------------------
    def run_tool(
        self,
        tool_name: str,
        tool_input: dict,
        env_extra=None,
        transcript_path=None,
        stdin_extra=None,
    ):
        """Run the real hook subprocess with a controlled environment.

        run_hook sets HOME=cwd.parent and copies os.environ; this runner may
        itself be a subagent, so CLAUDE_CODE_CHILD_SESSION is stripped by
        default unless env_extra sets it explicitly (only the rows proving the
        env var is IGNORED do that). ``transcript_path`` and ``stdin_extra``
        (e.g. agent_id/agent_type) mirror official hook-input schema fields.
        """
        stdin = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": str(self.proj),
        }
        if transcript_path is not None:
            stdin["transcript_path"] = transcript_path
        if stdin_extra:
            stdin.update(stdin_extra)
        overrides = dict(env_extra or {})
        managed = set(overrides) | {"CLAUDE_CODE_CHILD_SESSION"}
        saved = {k: os.environ.get(k) for k in managed}
        os.environ.pop("CLAUDE_CODE_CHILD_SESSION", None)
        os.environ.update(overrides)
        try:
            return run_hook(ADVISOR, stdin, cwd=str(self.proj))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _big_function(self) -> str:
        """An unambiguously substantive edit body (new def + 40 lines)."""
        lines = ["def handler(request):"]
        lines += [f"    step_{i} = compute({i})" for i in range(40)]
        return "\n".join(lines)

    def last_decision(self) -> str | None:
        """The decision on the last advisor metrics line, or None."""
        if not self.metrics_path.exists():
            return None
        decision = None
        for line in self.metrics_path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("hook") == "fable-delegation-advisor":
                decision = entry.get("decision")
        return decision

    def metrics_lines(self) -> list:
        if not self.metrics_path.exists():
            return []
        out = []
        for line in self.metrics_path.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("hook") == "fable-delegation-advisor":
                out.append(entry)
        return out

    def last_metrics(self) -> dict:
        """The full last advisor metrics entry (e.g. for subagent_signal)."""
        lines = self.metrics_lines()
        return lines[-1] if lines else {}

    # -- shared assertions -------------------------------------------------
    def assert_advised(self, code, out, lane="opus"):
        self.assertEqual(code, 0)
        self.assertIn("additionalContext", out)
        self.assertIn("FABLE MODE (advisory)", out)
        self.assertIn(lane, out)
        # Advisory injects context only — never a permission decision.
        self.assertNotIn("permissionDecision", out)
        self.assertEqual(self.last_decision(), "advised")

    def assert_silent(self, code, out, expected_decision):
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "", f"expected silence, got: {out!r}")
        self.assertEqual(self.last_decision(), expected_decision)


# ===========================================================================
# Inactive paths
# ===========================================================================
class TestInactive(_AdvisorCase):

    def test_fable_off_edit_source_is_inactive(self):
        # No fable-state.json written at all.
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "a", "new_string": "b"},
        )
        self.assert_silent(code, out, "inactive")

    def test_skip_env_is_inactive(self):
        self.fable_on()
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "a", "new_string": "b"},
            env_extra={"FABLE_ADVISOR_SKIP": "1"},
        )
        self.assert_silent(code, out, "inactive")

    def test_wrong_tool_bash_is_inactive(self):
        self.fable_on()
        code, out, _err = self.run_tool("Bash", {"command": "rm -rf /tmp/x"})
        self.assert_silent(code, out, "inactive")

    def test_invalid_state_lane_turbo_is_inactive(self):
        # _session validation rejects lane "turbo" -> mode reads as inactive.
        self.fable_on(state=_fable_state(lane="turbo"))
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "a", "new_string": "b"},
        )
        self.assert_silent(code, out, "inactive")

    def test_malformed_stdin_fails_open(self):
        # Not JSON on stdin -> exit 0, no stdout garbage.
        env = os.environ.copy()
        env["HOME"] = self.home
        # Main-session row: strip the child marker this runner may carry.
        env.pop("CLAUDE_CODE_CHILD_SESSION", None)
        import subprocess
        import sys as _sys
        result = subprocess.run(
            [_sys.executable, str(ADVISOR)],
            input="this is not json{{{",
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(self.proj),
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
        # Even on malformed stdin, decision is logged as inactive.
        self.assertEqual(self.last_decision(), "inactive")


# ===========================================================================
# Advised paths
# ===========================================================================
class TestAdvised(_AdvisorCase):

    def test_edit_large_new_function_is_advised(self):
        self.fable_on(lane="opus")
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "",
                "new_string": self._big_function(),
            },
        )
        self.assert_advised(code, out, lane="opus")

    def test_advisory_names_state_lane(self):
        # Lane from state (codex) must surface verbatim in the advisory.
        self.fable_on(lane="codex")
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "",
                "new_string": self._big_function(),
            },
        )
        self.assert_advised(code, out, lane="codex")

    def test_write_new_source_file_is_advised(self):
        self.fable_on()
        code, out, _err = self.run_tool(
            "Write",
            {"file_path": "src/bar.py", "content": "print('tiny')\n"},
        )
        # Write is never trivial (whole-file content), even for a 1-liner.
        self.assert_advised(code, out)

    def test_multiedit_over_ten_lines_is_advised(self):
        self.fable_on()
        big = "\n".join(f"x{i} = {i}" for i in range(8))
        code, out, _err = self.run_tool(
            "MultiEdit",
            {
                "file_path": "src/foo.py",
                "edits": [
                    {"old_string": "", "new_string": big},
                    {"old_string": "", "new_string": big},
                ],
            },
        )
        self.assert_advised(code, out)

    def test_edit_adding_def_within_five_lines_is_advised(self):
        # Token rule beats line count: 3 lines, but a new `def ` appears.
        self.fable_on()
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "x = 1\ny = 2\nz = 3",
                "new_string": "def f():\n    return 1\nz = 3",
            },
        )
        self.assert_advised(code, out)

    def test_notebook_edit_is_advised(self):
        # NotebookEdit is never trivial; path comes from notebook_path.
        self.fable_on()
        code, out, _err = self.run_tool(
            "NotebookEdit",
            {"notebook_path": "analysis/explore.ipynb", "new_source": "x=1"},
        )
        self.assert_advised(code, out)


# ===========================================================================
# Silent trivial
# ===========================================================================
class TestSilentTrivial(_AdvisorCase):

    def test_three_line_typo_fix_is_trivial(self):
        self.fable_on()
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "teh quick\nbrown fox\njumps",
                "new_string": "the quick\nbrown fox\njumps",
            },
        )
        self.assert_silent(code, out, "silent_trivial")

    def test_multiedit_two_small_edits_is_trivial(self):
        # Two edits totaling <=10 lines, no new tokens.
        self.fable_on()
        code, out, _err = self.run_tool(
            "MultiEdit",
            {
                "file_path": "src/foo.py",
                "edits": [
                    {"old_string": "a = 1", "new_string": "a = 2"},
                    {"old_string": "b = 3\nc = 4", "new_string": "b = 5\nc = 6"},
                ],
            },
        )
        self.assert_silent(code, out, "silent_trivial")


# ===========================================================================
# Silent carve-outs
# ===========================================================================
class TestSilentCarveout(_AdvisorCase):

    def _edit(self, path: str):
        return self.run_tool(
            "Edit",
            {"file_path": path, "old_string": "", "new_string": "x"},
        )

    def _write(self, path: str):
        return self.run_tool(
            "Write",
            {"file_path": path, "content": "anything\n"},
        )

    def test_write_tasks_todo_is_carveout(self):
        self.fable_on()
        code, out, _err = self._write("tasks/todo.md")
        self.assert_silent(code, out, "silent_carveout")

    def test_write_plan_file_is_carveout(self):
        self.fable_on()
        code, out, _err = self._write("PLAN_fable.md")
        self.assert_silent(code, out, "silent_carveout")

    def test_edit_readme_is_carveout(self):
        self.fable_on()
        code, out, _err = self._edit("README.md")
        self.assert_silent(code, out, "silent_carveout")

    def test_write_dot_claude_json_is_carveout(self):
        self.fable_on()
        code, out, _err = self._write(".claude/foo.json")
        self.assert_silent(code, out, "silent_carveout")

    def test_goal_file_is_carveout(self):
        self.fable_on()
        code, out, _err = self._write("GOAL_x.md")
        self.assert_silent(code, out, "silent_carveout")

    def test_memories_md_is_carveout(self):
        self.fable_on()
        code, out, _err = self._edit("src/MEMORIES.md")
        self.assert_silent(code, out, "silent_carveout")


# ===========================================================================
# Cooldown
# ===========================================================================
class TestCooldown(_AdvisorCase):

    def test_second_advised_eligible_call_is_cooldown(self):
        self.fable_on()
        payload = {
            "file_path": "src/foo.py",
            "old_string": "",
            "new_string": self._big_function(),
        }
        # First advised-eligible call -> advised (touches the stamp).
        _c1, out1, _e1 = self.run_tool("Edit", payload)
        self.assertIn("additionalContext", out1)

        # Second back-to-back -> within cooldown window -> no advisory.
        code2, out2, _e2 = self.run_tool("Edit", payload)
        self.assertEqual(code2, 0)
        self.assertEqual(out2.strip(), "", f"expected cooldown silence: {out2!r}")

        decisions = [m.get("decision") for m in self.metrics_lines()]
        self.assertEqual(decisions[-2:], ["advised", "cooldown"])


# ===========================================================================
# Subagent detection (stdin agent_id/agent_type keys — grounded live)
# ===========================================================================
class TestSubagent(_AdvisorCase):
    """Subagent tool calls never get the advisory.

    Grounded live (probe 2026-06-12T15:05Z): a subagent Edit's hook stdin
    carries ``agent_id`` + ``agent_type``; a main-agent Edit does not. The
    transcript_path is the PARENT session jsonl in both cases, and
    CLAUDE_CODE_CHILD_SESSION=1 appears in main-session tool children too
    (certain wrapper-launched) — both earlier detector keys were falsified live.
    """

    def test_agent_id_substantive_edit_is_subagent(self):
        # Subagent edits ARE the delegated work — never advise on them.
        self.fable_on()
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "",
                "new_string": self._big_function(),
            },
            stdin_extra={"agent_id": "a0deadbeef", "agent_type": "general-purpose"},
        )
        self.assert_silent(code, out, "subagent")
        self.assertEqual(
            self.last_metrics().get("subagent_signal"), "stdin-agent-keys"
        )

    def test_agent_type_alone_fable_off_still_subagent(self):
        # Detector precedes the state check: attribution stays "subagent"
        # even with no fable-state.json (cheaper, unambiguous in metrics).
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "",
                "new_string": self._big_function(),
            },
            stdin_extra={"agent_type": "Explore"},
        )
        self.assert_silent(code, out, "subagent")
        self.assertEqual(
            self.last_metrics().get("subagent_signal"), "stdin-agent-keys"
        )

    def test_env_var_alone_is_not_subagent(self):
        # REGRESSION (wrapper env poisoning, oracle run #6): the child-session
        # env var is present in MAIN-session tool children too — it must be
        # ignored. A substantive main edit with the var set still advises.
        self.fable_on()
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "",
                "new_string": self._big_function(),
            },
            env_extra={"CLAUDE_CODE_CHILD_SESSION": "1"},
        )
        self.assert_advised(code, out)
        self.assertEqual(self.last_metrics().get("subagent_signal"), "none")

    def test_run6_regression_poisoned_env_main_transcript_reaches_advised(self):
        # THE run-6 regression row, full shape: wrapper-launched MAIN session —
        # poisoned env var set, main-shaped (parent uuid.jsonl) transcript, NO
        # agent keys. The substantive edit must reach the normal flow and
        # advise; misattributing it as subagent is exactly the run-6 failure.
        self.fable_on(lane="opus")
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "",
                "new_string": self._big_function(),
            },
            env_extra={"CLAUDE_CODE_CHILD_SESSION": "1"},
            transcript_path=(
                f"{self.home}/.claude/projects/-Users-test-proj/"
                "4f0c95e1-aaaa-bbbb-cccc-0123456789ab.jsonl"
            ),
        )
        self.assert_advised(code, out, lane="opus")
        self.assertEqual(self.last_metrics().get("subagent_signal"), "none")

    def test_sidechain_transcript_path_is_subagent(self):
        # Secondary positive signal (not yet observed live; kept harmless).
        self.fable_on()
        code, out, _err = self.run_tool(
            "Edit",
            {
                "file_path": "src/foo.py",
                "old_string": "",
                "new_string": self._big_function(),
            },
            transcript_path="/x/.claude/projects/p/sess/subagents/agent-ab12.jsonl",
        )
        self.assert_silent(code, out, "subagent")
        self.assertEqual(self.last_metrics().get("subagent_signal"), "transcript")


# ===========================================================================
# agimode paths (same advisor, stricter doctrine: no trivial-source silence)
# ===========================================================================
class TestAgimode(_AdvisorCase):

    def assert_agimode_advised(self, code, out):
        self.assertEqual(code, 0)
        self.assertIn("additionalContext", out)
        self.assertIn("AGIMODE (advisory)", out)
        self.assertIn("codex fleet", out)
        self.assertNotIn("permissionDecision", out)
        self.assertEqual(self.last_decision(), "advised")
        self.assertEqual(self.last_metrics().get("mode"), "agimode")

    def test_agimode_substantive_edit_is_advised(self):
        self.agimode_on()
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "",
             "new_string": self._big_function()},
        )
        self.assert_agimode_advised(code, out)

    def test_agimode_small_source_edit_still_advised(self):
        """A 3-line typo fix is silent_trivial under fable but ADVISED under
        agimode — agimode has no trivial-source-edit allowance."""
        self.agimode_on()
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py",
             "old_string": "x = 1\ny = 2\nz = 3",
             "new_string": "x = 1\ny = 2\nz = 4"},
        )
        self.assert_agimode_advised(code, out)

    def test_agimode_dot_claude_write_is_carveout(self):
        self.agimode_on()
        code, out, _err = self.run_tool(
            "Write",
            {"file_path": ".claude/agimode-job.json", "content": "{}"},
        )
        self.assert_silent(code, out, "silent_carveout")

    def test_agimode_markdown_edit_is_carveout(self):
        self.agimode_on()
        code, out, _err = self.run_tool(
            "Write",
            {"file_path": "docs/notes.md", "content": "# notes"},
        )
        self.assert_silent(code, out, "silent_carveout")

    def test_agimode_precedence_over_fable(self):
        """Both states resolving (doctrine-forbidden) → agimode advisory wins."""
        self.fable_on()
        self.agimode_on()
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "",
             "new_string": self._big_function()},
        )
        self.assert_agimode_advised(code, out)
        self.assertNotIn("FABLE MODE", out)

    def test_agimode_second_advised_call_is_cooldown(self):
        self.agimode_on()
        code1, out1, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "",
             "new_string": self._big_function()},
        )
        self.assert_agimode_advised(code1, out1)
        code2, out2, _err = self.run_tool(
            "Edit",
            {"file_path": "src/bar.py", "old_string": "",
             "new_string": self._big_function()},
        )
        self.assert_silent(code2, out2, "cooldown")

    def test_agimode_subagent_edit_is_subagent(self):
        self.agimode_on()
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "",
             "new_string": self._big_function()},
            stdin_extra={"agent_id": "a1", "agent_type": "general-purpose"},
        )
        self.assert_silent(code, out, "subagent")

    def test_agimode_invalid_orchestrator_is_inactive(self):
        self.agimode_on(state=_agimode_state(orchestrator="turbo"))
        code, out, _err = self.run_tool(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "",
             "new_string": self._big_function()},
        )
        self.assert_silent(code, out, "inactive")


if __name__ == "__main__":
    unittest.main()
