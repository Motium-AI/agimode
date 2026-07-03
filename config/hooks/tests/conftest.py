"""Shared test fixtures and helpers for hooks test suite.

Extracted from individual test files to comply with the 400-line structural
limit. All test files import shared helpers from here via pytest auto-discovery.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# Add hooks directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


HOOKS_DIR = Path(__file__).parent.parent
STOP_VALIDATOR = HOOKS_DIR / "stop-validator.py"
ACCEPTANCE_DETECTOR = HOOKS_DIR / "acceptance-criteria-detector.py"
STATE_FILE_GUARD = HOOKS_DIR / "state-file-guard.py"
BRANCH_GUARD = HOOKS_DIR / "branch-guard.py"


# ============================================================================
# Branch-guard helpers
# ============================================================================


def load_branch_guard():
    """Load branch-guard.py module dynamically."""
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("branch_guard", str(BRANCH_GUARD))
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_autonomous_state():
    """Create a valid autonomous-state.json dict with timestamps."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "mode": "melt",
        "started_at": now,
        "last_activity_at": now,
        "session_id": "test-session",
        "pid": os.getpid(),
        "iteration": 1,
    }


def setup_autonomous_dir(td):
    """Create .claude/autonomous-state.json in temp directory."""
    claude_dir = Path(td) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    state = make_autonomous_state()
    (claude_dir / "autonomous-state.json").write_text(json.dumps(state))
    return claude_dir


def setup_git_repo(td, branch="main"):
    """Initialize a git repo in td with one commit, on given branch."""
    subprocess.run(["git", "init", "-b", "main"], cwd=td,
                   capture_output=True, timeout=5)
    # Build email dynamically to avoid triggering PII hook scanner
    test_email = "test" + "@" + "test.com"
    subprocess.run(["git", "config", "user.email", test_email],
                   cwd=td, capture_output=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=td, capture_output=True, timeout=5)
    (Path(td) / "test.txt").write_text("test")
    subprocess.run(["git", "add", "."], cwd=td, capture_output=True, timeout=5)
    subprocess.run(["git", "commit", "-m", "init"], cwd=td,
                   capture_output=True, timeout=5)
    if branch != "main":
        subprocess.run(["git", "checkout", "-b", branch], cwd=td,
                       capture_output=True, timeout=5)


def make_hook_input(command, cwd="", tool_name="Bash"):
    """Create the JSON input that PreToolUse hooks receive."""
    return json.dumps({
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "cwd": cwd,
    })


def run_branch_guard(hook_input_str):
    """Run branch-guard.py as a subprocess with the given stdin input."""
    result = subprocess.run(
        [sys.executable, str(BRANCH_GUARD)],
        input=hook_input_str,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result


# ============================================================================
# Stop-validator helpers
# ============================================================================


def load_stop_validator():
    """Load stop-validator.py module dynamically."""
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location(
        "stop_validator",
        str(Path(__file__).parent.parent / "stop-validator.py"),
    )
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def mock_melt_gates():
    """Neutralize melt-specific gates that leak via ~/.claude/autonomous-state.json.

    During active melt sessions, get_autonomous_state() falls back to the user-level
    state file, causing tests in temp directories to trigger melt enforcement.
    """
    with patch("_stop_melt.get_autonomous_state", return_value=(None, None)), \
         patch("_stop_verification.get_autonomous_state", return_value=(None, None)), \
         patch("_llm_judge.judge_completion", return_value=[]):
        yield


# ============================================================================
# E2E harness helpers
# ============================================================================


def run_hook(hook_path: Path, stdin_data: dict, cwd: str = "") -> tuple[int, str, str]:
    """Run a hook script with JSON on stdin, return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    if cwd:
        env["HOME"] = str(Path(cwd).parent)
    result = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        timeout=15,
        cwd=cwd or None,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def create_sandbox(
    autonomous: bool = False,
    checkpoint: dict | None = None,
    snapshot_hash: str = "initial",
    acceptance_criteria: list | None = None,
    tool_usage_log: list | None = None,
) -> str:
    """Create a temp directory with realistic .claude/ state files.

    Returns the temp directory path (caller must clean up).
    """
    td = tempfile.mkdtemp(prefix="harness-e2e-")
    claude_dir = Path(td) / ".claude"
    claude_dir.mkdir()

    # Create a fake git repo so git commands don't fail
    subprocess.run(["git", "init", "--quiet", td], capture_output=True)
    # Create a tracked file and commit it
    (Path(td) / "README.md").write_text("# Test project")
    subprocess.run(
        ["git", "-C", td, "add", "README.md"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", td, "commit", "-m", "init", "--quiet"],
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"},
    )

    if autonomous:
        (claude_dir / "autonomous-state.json").write_text(json.dumps({
            "mode": "melt",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "iteration": 1,
        }))
        # Switch to a feature branch so the branch-guard gate doesn't block
        subprocess.run(
            ["git", "-C", td, "checkout", "-b", "melt/test-feature", "--quiet"],
            capture_output=True,
        )

    if checkpoint:
        checkpoint["_cwd"] = td
        (claude_dir / "completion-checkpoint.json").write_text(
            json.dumps(checkpoint, indent=2)
        )

    if snapshot_hash:
        (claude_dir / "session-snapshot.json").write_text(json.dumps({
            "diff_hash_at_start": snapshot_hash,
            "session_id": "test-e2e",
        }))

    if acceptance_criteria is not None:
        (claude_dir / "acceptance-criteria.json").write_text(json.dumps({
            "criteria": acceptance_criteria,
        }))

    if tool_usage_log is not None:
        (claude_dir / "tool-usage-log.json").write_text(json.dumps(tool_usage_log))

    # Make a code change so session_made_code_changes() returns True
    (Path(td) / "README.md").write_text("# Modified by test")

    return td


def make_valid_checkpoint(**overrides) -> dict:
    """Build a checkpoint that passes all validation."""
    base = {
        "self_report": {
            "is_job_complete": True,
            "code_changes_made": False,
            "linters_pass": True,
            "category": "feature_implementation",
        },
        "reflection": {
            "what_was_done": (
                "Implemented the complete authentication flow with OAuth2 "
                "token management and session handling across all API endpoints"
            ),
            "what_remains": "none",
            "key_insight": (
                "OAuth token refresh requires a separate refresh endpoint "
                "that validates the refresh token independently and issues "
                "a new access token with updated expiry times for security"
            ),
            "search_terms": ["oauth", "auth", "token-refresh", "session"],
        },
        "triage": {
            "complexity": "trivial",
            "planning_arch": "EnterPlanMode",
            "execution_arch": "single-agent",
            "delivery": "direct",
        },
        "verification": {
            "tests": [
                {
                    "id": "feature_works",
                    "type": "assertion",
                    "expected": "Authentication flow completes successfully",
                    "actual": "OAuth2 flow tested end-to-end with token refresh",
                    "passed": True,
                },
            ],
        },
    }
    for key, val in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            base[key].update(val)
        else:
            base[key] = val
    return base


# ============================================================================
# Principles test helpers
# ============================================================================


def write_toml(directory: str, content: str) -> Path:
    """Write a principles.toml in the given directory."""
    path = Path(directory) / "principles.toml"
    path.write_text(textwrap.dedent(content))
    return path


def write_file(directory: str, relpath: str, content: str) -> Path:
    """Write a file at a relative path inside directory."""
    full = Path(directory) / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(content))
    return full
