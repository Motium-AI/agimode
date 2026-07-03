---
name: agimode
description: Toggle and operate agimode — run the main session as an orchestrator-only agent (Fable 5 if available, else Opus 4.8 xhigh) that delegates execution to a fleet of parallel, worktree-isolated headless gpt-5.5 codex agents instead of editing source itself. Use when asked about "/agimode", "agimode", "agi mode", "codex fleet", "agimode on/off/status".
short-description: Toggle agimode — orchestrator delegates execution to a gpt-5.5 codex fleet
---

# agimode

agimode runs the main session as an **orchestrator** — Fable 5 if you self-report it, else **Opus 4.8 at xhigh effort** — that **delegates execution to headless `codex` agents (gpt-5.5, xhigh)** instead of writing code itself. It is **an orchestrator-only delegation mode with codex as the executor**: you reason, decompose, dispatch, integrate, and review; the codex fleet writes the code. It is a thin delegation mode — orthogonal to autonomous mode, with no loop, gate, or special delivery flow of its own.

The single canonical doctrine + honest-enforcement caveats live in `~/.claude/rules/toolkit-agimode.md`. This skill cites that rule; it does not redefine it.

## Triggers

- `/agimode`
- "agimode" / "agi mode" / "codex fleet"
- "agimode on" / "agimode off" / "agimode status"

## State operations — always through the resolver, never bare paths

State lives in `.claude/agimode-state.json`. **All reads and writes go through the `_session.py` helpers** via `python3 -c` one-liners (installed at `$HOME/.claude/hooks`). The resolver does the walk-up + worktree `main_repo` fallback that makes on/off/status correct from any worktree or subdirectory. **Never** use `rm`, `cat >`, or a heredoc on the state file.

Schema written: `{"schema_version":1,"mode":"agimode","orchestrator":"fable|opus","started_at":ISO,"model_expected":...,"fleet":{"max_workers":4,"executor":"codex","codex_model":"gpt-5.5","codex_effort":"xhigh","claude_model":"claude-sonnet-5"}}`. (The codex-spend cap is the job-level `max_codex_calls` the orchestrator passes to `agimode_fleet.dispatch`.) Invalid/missing `orchestrator`, unknown `schema_version`, or malformed JSON is treated as **inactive**, never defaulted.

### `/agimode on`

Self-report your model: if you are `claude-fable-5[1m]`, pass `orchestrator='fable'`; otherwise `orchestrator='opus'` (and run at xhigh effort).

```bash
python3 -c "import sys; sys.path.insert(0,'$HOME/.claude/hooks'); import _session; print(_session.write_agimode_state('.', 'opus'))"
```

Users WITHOUT an OpenAI/ChatGPT subscription pass a fleet override with `executor: "claude"`; workers then run headless Claude via `run-agimode-claude.sh` (`claude-sonnet-5` default, `claude-opus-4-8` for judgment-heavy slices) with `acceptEdits` and a tool allowlist, never `bypassPermissions`.

```bash
python3 -c "import sys; sys.path.insert(0,'$HOME/.claude/hooks'); import _session; print(_session.write_agimode_state('.', 'opus', fleet={'max_workers':4,'executor':'claude','claude_model':'claude-sonnet-5'}))"
```

Print:

> agimode armed (orchestrator: <orchestrator>). Execution now goes to gpt-5.5 codex — give me the task and I'll decompose it and dispatch the codex fleet; I orchestrate and review, I don't write the code myself. If you are not on claude-fable-5, run Opus 4.8 xhigh.

### `/agimode status`

```bash
python3 -c "import sys; sys.path.insert(0,'$HOME/.claude/hooks'); import _session; print(_session.get_agimode_state('.')); print(_session.resolve_agimode_state_path('.'))"
```

Report: orchestrator, `started_at`, and the resolved state path. Remind the user state is **per-repo**.

### `/agimode off`

```bash
python3 -c "import sys; sys.path.insert(0,'$HOME/.claude/hooks'); import _session; print(_session.clear_agimode_state('.'))"
```

Clears agimode-state; the session returns to writing code directly.

## Scout phase (research fan-out)

Before decomposing a non-trivial task, fan out parallel READ-ONLY scout subagents (Agent tool, `Explore` type), one per suspected slice area. Each scout returns a compact context pack: relevant files with line ranges, interfaces, invariants, existing tests, and gotchas.

Read the packs, not the codebase wholesale. Orchestrator tokens are the scarcest currency. Scout packs feed each packet's **Context pointers** field.

## Dispatching execution to the codex fleet

When agimode is active, coding work goes to the fleet, never your own edits:

1. **Decompose** the task into N **file-disjoint** spec packets (the engine's diff-path validator rejects any worker that strays out of its `files_in_scope` or into another's). Each packet is self-contained. Match the spec-packet template:
   - **Goal** — one sentence naming the deliverable.
   - **Files in scope** — explicit allowlist of paths the packet may touch.
   - **Forbidden paths** — other packets' files, shared config, and anything out of scope.
   - **Acceptance + validation commands** — this repo's canonical gate, named exactly: `python3 -m pytest config/hooks/tests -q` AND `ruff check --config config/references/ruff-strict.toml` on every touched Python file (CI re-runs both, pytest across 3.11 and 3.13 — a passing subset is a false green).
   - **Constraints** — invariants, style, and what not to refactor.
   - **Context pointers** — file:line references from the scout packs; do not make codex re-discover the codebase.
2. **Dispatch via the engine in ONE background call** (`run_in_background=true`; the engine owns concurrency via `subprocess.Popen`):
   ```bash
   bash -lc 'python3 $HOME/.claude/hooks/agimode_fleet.py .claude/agimode-job-<arc>.json'  # run_in_background=true
   ```
   The job JSON carries `arc_id`, `slices[]` (each with `slice_id`, `spec`, `files_in_scope`, `forbidden_paths`, and optional `no_retry`), `max_workers`, optional `executor` (`codex` default | `claude`), optional `claude_model`, optional `claude_effort`, `max_codex_calls`, optional `oracle_command`, and optional `oracle_timeout_sec`. Keep it under `.claude/` — that is the coordination carve-out the delegation advisor (and the engine's diff exclusion) already honors. A slice already integrated this run (same base + spec) is memoized — not re-dispatched. A rejected or worker-failed slice is automatically retried once per dispatch (fresh `-r1` worktree, failure feedback appended) unless it sets `no_retry: true` or the worker-launch budget is exhausted; infra `error` states are not auto-retried, and cumulative paid launches for either executor across re-dispatches stay bounded by `max_codex_calls`. Overall dispatch success is the CLI exit code (integrated AND oracle passed) — `integration.integrated` alone ignores the oracle verdict. A retried slice's record points at the `-r1` attempt; the preserved first-attempt worktree/branch (in `first_attempt`/`retry_of`) needs explicit cleanup too.
3. **Integrate**: the engine validates each diff, commits in-scope changes, and merges all workers into a CLEAN integration worktree (local only, never pushing). Read the returned manifest and review the result.

The codex fleet never pushes — the engine integrates locally; only you (the orchestrator) commit and push, via normal git flow when the user asks.

## Delegated review (post-integration, pre-PR)

After the engine integrates, fan out reviewers over the INTEGRATION DIFF before any PR is opened: a codex read-only review lane and/or an Opus subagent. Read findings and adjudicate; do not read a large diff yourself. Require every finding to cite file:line with a quoted hunk from the integration diff, and spot-verify those citations against the diff before adjudicating — reviewer prose without verifiable evidence is a proxy, not proof.

For large diffs (>~500 lines), also delegate per-slice diff summarization. Accepted substantive fixes go back to the fleet as repair slices, never hand-edited by the orchestrator.

## Handback protocol

1. Run the canonical gate inside the integration worktree.
2. Merge the integration branch `claude-agent/<arc_id>-int` into the delivery feature branch; never use `merge_worktree` because it pushes to origin.
3. Push and open the PR via normal git flow when the user asks. The orchestrator commits and pushes; the fleet never does.
4. Clean up worker worktrees AND the integration worktree/branch (`<arc_id>-int`) via `worktree_manager.cleanup_worktree`. The 8h TTL gc is the backstop, not the plan.
5. Record dispatch stats (slices, retries, codex calls) for the arc report.

## Honest enforcement (defer to the rule)

Hook stdin/env expose no session model, but the parent transcript records assistant `message.model`; `fable-subagent-model-guard.py` reads the last flushed main-agent record and blocks Fable→Fable Agent spawns (fail-open if the transcript is missing/unreadable, one-turn lag possible after a mid-turn `/model` switch, main-agent spawns only in v1). Mode-arming and slice-disjointness remain self-reports/judgments, not gates. The no-source-edit invariant is advisory (the per-session surface nudge + a per-edit advisory from `fable-delegation-advisor.py`, which under agimode advises on ANY product-source edit — no trivial-edit allowance, throttled to at most one advisory per 10-minute cooldown window) + canary-proven, not blocked. See `~/.claude/rules/toolkit-agimode.md` for the full, plainly-stated limits.
