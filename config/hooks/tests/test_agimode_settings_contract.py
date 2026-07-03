#!/usr/bin/env python3
"""Settings-wiring + surfacing contract for agimode.

Mirrors test_fable_settings_contract.py: the surface hook is registered on
SessionStart AND UserPromptSubmit, its target exists on disk, the codex
background-enforcer carries the agimode wrapper pattern, and the surface hook
behaves correctly as a real subprocess (active surfaces, inactive/malformed
silent + fail-open).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]          # config/hooks
CONFIG_DIR = HOOKS_DIR.parent                            # config
SETTINGS = CONFIG_DIR / "settings.json"
SURFACE = HOOKS_DIR / "agimode-session-surface.py"

sys.path.insert(0, str(HOOKS_DIR))
import _session  # noqa: E402


def _commands_for_event(settings: dict, event: str) -> list[str]:
    return [
        h.get("command", "")
        for group in settings.get("hooks", {}).get(event, [])
        for h in group.get("hooks", [])
    ]


@pytest.fixture(scope="module")
def settings() -> dict:
    return json.loads(SETTINGS.read_text())


class TestSettingsContract:
    def test_surface_registered_on_sessionstart(self, settings):
        cmds = _commands_for_event(settings, "SessionStart")
        assert any("agimode-session-surface.py" in c for c in cmds)

    def test_surface_registered_on_userpromptsubmit(self, settings):
        cmds = _commands_for_event(settings, "UserPromptSubmit")
        assert any("agimode-session-surface.py" in c for c in cmds)

    def test_surface_target_exists(self):
        assert SURFACE.exists(), f"missing hook target: {SURFACE}"

    def test_enforcer_carries_agimode_wrapper(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "cbe", HOOKS_DIR / "codex-background-enforcer.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert any("run-agimode-codex" in p for p in mod.CODEX_WRAPPER_PATTERNS)


def _run_surface(cwd: str, event: str = "SessionStart") -> subprocess.CompletedProcess:
    payload = json.dumps({"cwd": cwd, "hook_event_name": event})
    return subprocess.run(
        [sys.executable, str(SURFACE)],
        input=payload, capture_output=True, text=True,
    )


class TestSurfaceSubprocess:
    def test_active_surfaces(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".claude").mkdir(parents=True)
        _session.write_agimode_state(str(proj), orchestrator="opus")
        res = _run_surface(str(proj))
        assert res.returncode == 0
        assert "AGIMODE ON" in res.stdout
        assert "orchestrator opus" in res.stdout

    def test_inactive_silent(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".claude").mkdir(parents=True)
        res = _run_surface(str(proj))
        assert res.returncode == 0
        assert res.stdout.strip() == ""

    def test_malformed_stdin_fail_open_silent(self):
        res = subprocess.run(
            [sys.executable, str(SURFACE)],
            input="not json", capture_output=True, text=True,
        )
        assert res.returncode == 0
        assert res.stdout.strip() == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
