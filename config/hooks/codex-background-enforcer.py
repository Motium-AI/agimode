#!/usr/bin/env python3
"""
PreToolUse hook that blocks codex wrapper scripts unless run_in_background=true.

Hook event: PreToolUse
Matcher: Bash

Agent + Bash tool calls in the same message get serialized by the harness.
Codex wrappers must run in background so the Opus agents run in true parallel.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import log_debug

CODEX_WRAPPER_PATTERNS = [
    r"run-fix-codex\.sh",
    r"run-plan-redteam-codex\.sh",
    r"run-fable-codex\.sh",
    r"run-agimode-codex\.sh",
]


def main():
    stdin_data = sys.stdin.read()
    if not stdin_data.strip():
        sys.exit(0)

    try:
        input_data = json.loads(stdin_data)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = input_data.get("tool_input", {})
    command = tool_input.get("command", "")

    if not re.match(r"^bash\s+", command):
        sys.exit(0)
    if re.match(r"^bash\s+-[a-z]", command):
        sys.exit(0)

    is_codex_wrapper = any(
        re.search(pattern, command) for pattern in CODEX_WRAPPER_PATTERNS
    )
    if not is_codex_wrapper:
        sys.exit(0)

    run_in_background = tool_input.get("run_in_background", False)
    if run_in_background:
        log_debug(
            f"Codex wrapper running in background (correct): {command[:80]}",
            hook_name="codex-background-enforcer",
        )
        sys.exit(0)

    log_debug(
        f"BLOCKED: codex wrapper not in background: {command[:80]}",
        hook_name="codex-background-enforcer",
    )
    print(  # noqa: T201 -- PreToolUse stdout block message
        "BLOCKED: Codex wrapper must run in background for parallel execution.\n"
        "Use run_in_background=true when calling this script.\n"
        "Agent + Bash in the same message get serialized — background mode\n"
        "ensures the codex lane runs in parallel with Opus agents.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
