#!/usr/bin/env python3
"""Tests for the agimode state API in _session.py.

Mirrors test_fable_state.py: the resolver (walk-up + worktree main_repo
fallback), strict schema validation, write/clear round-trips, and isolation
between agimode and fable state.

NOTE: the hermetic fixture pins HOME to tmp_path, and the resolver's walk-up
STOPS at HOME — so tests operate on a ``proj`` SUBDIR under tmp_path (cwd must
be below HOME for the walk-up to find the state), exactly as the fable tests do.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from _session import (
    AGIMODE_STATE_FILENAME,
    clear_agimode_state,
    get_agimode_state,
    get_fable_state,
    is_agimode_mode_active,
    is_fable_mode_active,
    write_agimode_state,
    write_fable_state,
)


@pytest.fixture(autouse=True)
def _hermetic_tmp(tmp_path, monkeypatch):
    """Sandbox every test under tmp_path (pins tempfile.tempdir + HOME)."""
    sandbox = Path(str(tmp_path)).resolve()
    monkeypatch.setattr(tempfile, "tempdir", str(sandbox))
    monkeypatch.setenv("HOME", str(sandbox))


@pytest.fixture
def proj(tmp_path):
    """A project dir BELOW HOME (so the walk-up can resolve its state)."""
    p = tmp_path / "proj"
    (p / ".claude").mkdir(parents=True)
    return p


def _write_claude_file(base: Path, filename: str, data) -> Path:
    claude_dir = base / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = claude_dir / filename
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"],
                   capture_output=True, text=True, check=True)


class TestAgimodeWriteGetRoundTrip:
    def test_default_orchestrator_and_fleet(self, proj):
        write_agimode_state(str(proj))
        st = get_agimode_state(str(proj))
        assert st is not None
        assert st["schema_version"] == 1
        assert st["mode"] == "agimode"
        assert st["orchestrator"] == "opus"
        assert st["fleet"]["executor"] == "codex"
        assert st["fleet"]["codex_model"] == "gpt-5.5"
        assert st["fleet"]["codex_effort"] == "xhigh"
        assert st["fleet"]["claude_model"] == "claude-sonnet-5"
        assert st["fleet"]["max_workers"] == 4
        assert is_agimode_mode_active(str(proj)) is True

    def test_explicit_orchestrators(self, proj):
        for orch in ("fable", "opus"):
            write_agimode_state(str(proj), orchestrator=orch)
            assert get_agimode_state(str(proj))["orchestrator"] == orch

    def test_custom_fleet_block(self, proj):
        fleet = {"max_workers": 2, "codex_model": "gpt-5.5",
                 "codex_effort": "xhigh", "per_arc_budget": 6}
        write_agimode_state(str(proj), fleet=fleet)
        st = get_agimode_state(str(proj))
        assert st["fleet"]["max_workers"] == 2
        assert st["fleet"]["per_arc_budget"] == 6

    def test_custom_claude_fleet_block_written_verbatim(self, proj):
        fleet = {
            "max_workers": 3,
            "executor": "claude",
            "claude_model": "claude-opus-4-8",
            "claude_effort": "xhigh",
            "max_codex_calls": 5,
        }
        write_agimode_state(str(proj), fleet=fleet)
        st = get_agimode_state(str(proj))
        assert st["fleet"] == fleet


class TestAgimodeWriteValidation:
    def test_invalid_orchestrator_rejected_and_persists_nothing(self, proj):
        with pytest.raises(ValueError):
            write_agimode_state(str(proj), orchestrator="gpt")
        assert get_agimode_state(str(proj)) is None


class TestAgimodeStrictValidation:
    """Every invalid-field permutation reads as inactive (None), never defaulted."""

    @pytest.mark.parametrize("bad", [
        {"schema_version": 2, "mode": "agimode", "orchestrator": "opus"},
        {"schema_version": 1, "mode": "fable", "orchestrator": "opus"},
        {"schema_version": 1, "mode": "agimode", "orchestrator": "gpt"},
        {"schema_version": 1, "mode": "agimode"},  # missing orchestrator
        {"mode": "agimode", "orchestrator": "opus"},  # missing schema_version
    ])
    def test_invalid_states_read_none(self, proj, bad):
        _write_claude_file(proj, AGIMODE_STATE_FILENAME, bad)
        assert get_agimode_state(str(proj)) is None
        assert is_agimode_mode_active(str(proj)) is False

    def test_malformed_json_reads_none(self, proj):
        _write_claude_file(proj, AGIMODE_STATE_FILENAME, "{not json")
        assert get_agimode_state(str(proj)) is None

    def test_missing_file_silent_none(self, proj):
        assert get_agimode_state(str(proj)) is None


class TestAgimodeWalkUpAndClear:
    def test_walk_up_from_subdir(self, proj):
        write_agimode_state(str(proj))
        sub = proj / "a" / "b"
        sub.mkdir(parents=True)
        assert is_agimode_mode_active(str(sub)) is True

    def test_first_enable_convergence_git_root(self, tmp_path):
        repo = tmp_path / "repo"
        _git_init(repo)
        sub = repo / "pkg" / "mod"
        sub.mkdir(parents=True)
        # First-enable from a subdir anchors at the repo root; read converges.
        write_agimode_state(str(sub))
        assert (repo / ".claude" / AGIMODE_STATE_FILENAME).exists()
        assert is_agimode_mode_active(str(sub)) is True
        assert is_agimode_mode_active(str(repo)) is True

    def test_clear_round_trip(self, proj):
        write_agimode_state(str(proj))
        assert clear_agimode_state(str(proj)) is True
        assert get_agimode_state(str(proj)) is None
        assert clear_agimode_state(str(proj)) is False  # nothing to remove


class TestAgimodeIsolation:
    """agimode-state must never read as fable, nor vice versa (real isolation)."""

    def test_agimode_does_not_read_as_fable(self, proj):
        write_agimode_state(str(proj), orchestrator="opus")
        assert is_agimode_mode_active(str(proj)) is True   # real positive
        assert get_fable_state(str(proj)) is None
        assert is_fable_mode_active(str(proj)) is False

    def test_fable_does_not_read_as_agimode(self, proj):
        write_fable_state(str(proj), lane="opus")
        assert is_fable_mode_active(str(proj)) is True      # real positive
        assert get_agimode_state(str(proj)) is None
        assert is_agimode_mode_active(str(proj)) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
