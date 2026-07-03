# agimode

agimode is a toggleable operating mode: the main session runs as an **orchestrator only** — Fable 5 if self-reported, else Opus 4.8 at xhigh effort — that delegates **execution** to a fleet of parallel, worktree-isolated headless coding agents (codex gpt-5.5 by default, or Claude Opus 4.8 / Sonnet 5 via the claude executor for users without OpenAI access) instead of editing source itself. It is **an orchestrator-only delegation mode with codex as the executor**. Toggle = the `/agimode` skill writing `.claude/agimode-state.json`. When the state file resolves active, you are orchestrator-only; when it does not, this rule is dormant.

## Orchestrator + codex-fleet doctrine

When active, you reason, decompose, dispatch, integrate, and review — **never implement source yourself**. Split each task into **file-disjoint** spec packets and dispatch the worktree-isolated codex fleet in ONE background call to `agimode_fleet.py` (it owns concurrency via `subprocess.Popen`); the engine validates each diff against its declared scope, commits in-scope changes, and integrates them locally into a clean worktree. You review the result and drive normal git flow. Full operating procedure (state ops, spec-packet shape, dispatch) lives in the `/agimode` skill — not here.

Doctrine: scout packs before decomposition; enriched spec packets with validation and context pointers; delegated review of the integration diff; explicit local handback from integration worktree to PR.

## The threshold (no direct source edits)

In agimode the orchestrator makes **no substantive source edits** — all execution is fleet-delegated. Trivial coordination edits to planning/memory/state artifacts (`.claude/`, including the job JSON — keep it under `.claude/` so the advisor carve-out covers it) are fine. Everything that changes product source goes to the fleet.

## Honest enforcement

Enforcement is narrow and explicit: some checks are mechanical, while the no-source-edit invariant is advisory + canary-proven. The real limits, stated plainly:

- Hook stdin/env expose **no session model**, but the parent transcript at `transcript_path` records assistant `message.model`; `fable-subagent-model-guard.py` reads the last flushed main-agent record and blocks (exit 2) Fable→Fable Agent spawns. It fails open if the transcript is missing/unreadable, can lag a mid-turn `/model` switch by one turn, gates only main-agent spawns in v1, and does not verify `/agimode on` (a missing hook TARGET fails closed per `-guard` semantics — `scripts/install.sh --force` repairs). On any other model, run Opus 4.8 xhigh.
- Per-slice **scope** IS mechanically enforced: `validate_diff` rejects any worker that touches a path outside its `files_in_scope` or inside another slice's scope (within a dispatch). What's NOT guarded is the orchestrator's *judgment* that the decomposition is correct — a semantically-wrong-but-scope-valid split surfaces as a wasted dispatch or a cross-dispatch merge conflict, not a blocked action.
- The **no-source-edit invariant** is advisory + canary-proven, never a hard block: `agimode-session-surface.py` carries the orchestrator-only reminder (fail-open, fired per session / prompt), `fable-delegation-advisor.py` injects a per-edit advisory the moment the orchestrator is about to Write/Edit product source (agimode has NO trivial-source-edit allowance — any source edit advises, throttled to one advisory per 10-minute cooldown window; coordination artifacts stay silent), and the live canary's `leg_no_orchestrator_edit` FAILS if the coordinator edited source during the arc.
- The fleet **re-bills codex on a retried dispatch** (paid + non-deterministic); per-slice memoization (same base + spec) bounds but does not eliminate this. The job-level `max_codex_calls` caps cumulative fleet spend.

Beyond those narrow checks, the mode's value rests on doctrine-following.

## Composition

- **Orthogonal to autonomous mode** — agimode neither enables nor disables autonomous enforcement; it is a thin delegation mode, exactly like Fable mode. Do not run agimode and Fable mode at once (both are orchestrator-only).
- The codex fleet never pushes — by design the engine integrates worker diffs locally into a clean worktree and pushes nothing; you (the coordinator) push via normal git flow when ready. This is a property of the engine's local-integration design, not a deploy-time gate (the old autonomous-mode push gate does not apply in this thin mode).

## Self-Correction Triggers

- **About to edit a source file directly while agimode is active** → stop; decompose and dispatch the fleet instead.
- **Decomposing into slices that share a file** → `validate_diff` rejects the overlap within a dispatch (and it would conflict at integration across dispatches); make the file-scope disjoint, or sequence them across dispatches.
- **Reaching for `merge_worktree` to integrate** → it pushes to origin and merges into main's checkout; agimode integrates locally into a clean worktree instead.

## Cross-References

- Toggle/dispatch operations: the `/agimode` skill (`config/skills/agimode/SKILL.md`)
