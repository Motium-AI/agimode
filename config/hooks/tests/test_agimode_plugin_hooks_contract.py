#!/usr/bin/env python3
"""Plugin-native hooks contract for the native ``/plugin install`` path.

The manual install.sh path symlinks ``config/hooks`` into ``~/.claude/hooks`` and
uses ``config/settings.json`` (whose command strings are ``$HOME``-anchored). A
user who runs ONLY ``/plugin marketplace add`` + ``/plugin install`` never gets
files into ``~/.claude/hooks``, so those ``$HOME`` paths would dangle.

This file asserts the plugin-native wiring that fixes that:
- ``.claude-plugin/plugin.json`` ``hooks`` points at the plugin-native hooks file
  (NOT at ``settings.json``), and ``skills`` points at the skills dir with a
  discoverable ``SKILL.md``.
- The plugin hooks file carries the SAME five hook registrations as
  ``settings.json`` — surface (SessionStart + UserPromptSubmit), model-guard
  (PreToolUse Agent), delegation-advisor (PreToolUse Write/Edit/MultiEdit/
  NotebookEdit), background-enforcer (PreToolUse Bash).
- Every plugin hook command uses ``${CLAUDE_PLUGIN_ROOT}`` and NEVER ``$HOME`` —
  the whole point of the plugin path.
- Every referenced hook target actually exists on disk.
"""

import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]          # config/hooks
CONFIG_DIR = HOOKS_DIR.parent                            # config
REPO_ROOT = CONFIG_DIR.parent                            # repo root
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(PLUGIN_MANIFEST.read_text())


@pytest.fixture(scope="module")
def plugin_hooks(manifest) -> dict:
    hooks_ref = manifest["hooks"]
    assert isinstance(hooks_ref, str), "hooks field should be a relative path string"
    hooks_path = (REPO_ROOT / hooks_ref).resolve()
    assert hooks_path.exists(), f"plugin hooks file missing: {hooks_path}"
    return json.loads(hooks_path.read_text())


def _commands_for_event(hooks_doc: dict, event: str) -> list[str]:
    return [
        h.get("command", "")
        for group in hooks_doc.get("hooks", {}).get(event, [])
        for h in group.get("hooks", [])
    ]


def _matcher_commands(hooks_doc: dict, event: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for group in hooks_doc.get("hooks", {}).get(event, []):
        matcher = group.get("matcher", "")
        out.setdefault(matcher, [])
        for h in group.get("hooks", []):
            out[matcher].append(h.get("command", ""))
    return out


class TestManifestWiring:
    def test_hooks_points_at_plugin_native_file(self, manifest):
        # Must NOT point at settings.json (the $HOME-anchored manual-install file).
        assert manifest["hooks"] != "./config/settings.json"
        assert "settings.json" not in manifest["hooks"]

    def test_skills_points_at_dir_with_skill_md(self, manifest):
        skills_ref = manifest["skills"]
        skills_dir = (REPO_ROOT / skills_ref).resolve()
        assert skills_dir.is_dir(), f"skills dir missing: {skills_dir}"
        skill_mds = list(skills_dir.rglob("SKILL.md"))
        assert skill_mds, f"no SKILL.md discoverable under {skills_dir}"


class TestPluginHooksRegistrations:
    def test_surface_on_sessionstart(self, plugin_hooks):
        cmds = _commands_for_event(plugin_hooks, "SessionStart")
        assert any("agimode-session-surface.py" in c for c in cmds)

    def test_surface_on_userpromptsubmit(self, plugin_hooks):
        cmds = _commands_for_event(plugin_hooks, "UserPromptSubmit")
        assert any("agimode-session-surface.py" in c for c in cmds)

    def test_model_guard_on_agent(self, plugin_hooks):
        by_matcher = _matcher_commands(plugin_hooks, "PreToolUse")
        assert "Agent" in by_matcher
        assert any("fable-subagent-model-guard.py" in c for c in by_matcher["Agent"])

    @pytest.mark.parametrize("matcher", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
    def test_delegation_advisor_on_edit_tools(self, plugin_hooks, matcher):
        by_matcher = _matcher_commands(plugin_hooks, "PreToolUse")
        assert matcher in by_matcher
        assert any("fable-delegation-advisor.py" in c for c in by_matcher[matcher])

    def test_background_enforcer_on_bash(self, plugin_hooks):
        by_matcher = _matcher_commands(plugin_hooks, "PreToolUse")
        assert "Bash" in by_matcher
        assert any("codex-background-enforcer.py" in c for c in by_matcher["Bash"])


class TestPluginRootPaths:
    def _all_commands(self, plugin_hooks) -> list[str]:
        cmds: list[str] = []
        for event_groups in plugin_hooks.get("hooks", {}).values():
            for group in event_groups:
                for h in group.get("hooks", []):
                    cmds.append(h.get("command", ""))
        return cmds

    def test_every_command_uses_plugin_root(self, plugin_hooks):
        cmds = self._all_commands(plugin_hooks)
        assert cmds, "no hook commands found"
        for c in cmds:
            assert "${CLAUDE_PLUGIN_ROOT}" in c, f"command not plugin-root-anchored: {c}"

    def test_no_command_uses_home(self, plugin_hooks):
        for c in self._all_commands(plugin_hooks):
            assert "$HOME" not in c, f"plugin command must not use $HOME: {c}"

    def test_referenced_targets_exist(self, plugin_hooks):
        # Each command references run-python-hook.sh + a <hook>.py under config/hooks.
        for c in self._all_commands(plugin_hooks):
            for token in c.split():
                token = token.strip('"')
                if token.endswith((".py", ".sh")):
                    rel = token.replace("${CLAUDE_PLUGIN_ROOT}/", "")
                    target = REPO_ROOT / rel
                    assert target.exists(), f"missing hook target: {target}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
