# agimode

**An orchestrator-only operating mode for Claude Code: the frontier model plans and reviews; a fleet of worktree-isolated headless coding agents does the writing.**

Your main Claude Code session is an expensive frontier model (Claude Opus, or Fable). agimode stops it from editing source directly. Instead it becomes an **orchestrator**: it decomposes your task into file-disjoint work packets and dispatches a fleet of parallel, git-worktree-isolated coding agents that write the actual code. The engine validates every worker's diff against its declared file-scope, commits only in-scope changes, retries a failed slice once, caps spend with a budget, optionally runs an oracle command against the integrated tree, and merges everything **locally** into a clean worktree — never pushing. You review the result and drive normal git flow.

It's a thin delegation mode, toggled with a single command: `/agimode on`.

---

## Why

Frontier orchestrator tokens are the scarcest thing in an agentic session. Spending them on mechanical edits — while a long file scrolls through context and the plan drifts — is the expensive way to work. agimode keeps the frontier model on the two things it's uniquely good at (decomposition and review) and pushes the writing out to cheaper, parallel, sandboxed workers whose output is mechanically checked before it's trusted.

Two executor backends ship:

- **codex** — OpenAI `gpt-5.5` at `xhigh` reasoning (the default).
- **claude** — Anthropic `claude-opus-4-8` / `sonnet-5`, for users **without** an OpenAI/ChatGPT subscription.

You only need one of them installed.

---

## How it works

```
  You: a task
   │
   ▼
┌─────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR  (Claude Opus / Fable — plans, never writes)   │
│  decompose → N file-disjoint spec packets                    │
└─────────────────────────────────────────────────────────────┘
   │  one dispatch call
   ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│  worker A     │   │  worker B     │   │  worker C     │   … parallel
│  worktree A   │   │  worktree B   │   │  worktree C   │     headless
│  scope: a/**  │   │  scope: b/**  │   │  scope: c/**  │     codex|claude
└───────────────┘   └───────────────┘   └───────────────┘
   │                    │                    │
   ▼                    ▼                    ▼
   validate diff ⟶ every changed path must be inside the packet's
                    files_in_scope and outside every other packet's
                    (out-of-scope edit ⟶ REJECTED, never committed;
                     rejected/failed slice ⟶ ONE auto-retry)
   │
   ▼
   integrate LOCALLY into a clean worktree (git merge --no-ff)
   │
   ▼
   optional oracle command runs on the integrated tree
   │
   ▼
   manifest back to you → review → your normal git flow (you push, not the fleet)
```

The pipeline in words: **decompose → dispatch fleet → validate scope → integrate → oracle.**

- **One frozen base.** Every worker branches from a single base commit; integration happens only after all workers terminate, so there's no base drift mid-batch.
- **Deterministic scope validation.** Before any commit, the engine parses `git status` for each worktree and rejects the worker if any changed path falls outside its `files_in_scope` (or inside another slice's scope, or in `forbidden_paths`). Renames are checked on both source and destination.
- **Memoization + budget.** A slice whose spec already reached `integrated` this arc isn't re-dispatched. A per-arc budget (`max_codex_calls`) caps cumulative worker spend across retries.
- **Local-only integration.** Workers never push. The engine merges into a clean integration worktree; you own the PR.

---

## Requirements

- **Claude Code** (this is a plugin for it).
- **git** (worktrees are the isolation primitive).
- **At least one executor** — you need only one:
  - `codex` CLI, authenticated against an OpenAI/ChatGPT account (`codex login`), **or**
  - `claude` CLI (Claude Code's own binary).

---

## Install

### Plugin (recommended)

```
/plugin marketplace add Motium-AI/agimode
/plugin install agimode
```

### Manual (fallback)

```bash
git clone https://github.com/Motium-AI/agimode
cd agimode
./install.sh
```

---

## Quickstart

Turn it on, then just give it a task:

```
/agimode on
```

> agimode armed. Execution now goes to the coding fleet — give me the task and I'll
> decompose it and dispatch the workers; I orchestrate and review, I don't write the
> code myself.

Then, for example:

```
Add a --json flag to the report command and cover it with a test.
```

The orchestrator decomposes that into file-disjoint packets and dispatches the fleet. When it finishes you get a manifest of what each worker changed, integrated locally onto a clean worktree, ready to review.

**Check state / turn off:**

```
/agimode status
/agimode off
```

### Choosing the executor

The default executor is **codex**. To run the fleet on the **claude** executor instead — the option for users without an OpenAI subscription — set the executor in the job (or when arming the mode):

```jsonc
// .claude/agimode-job-<arc>.json
{
  "arc_id": "add-json-flag",
  "executor": "claude",          // "codex" (default) | "claude"
  "max_workers": 4,
  "max_codex_calls": 12,
  "oracle_command": "pytest -q",
  "slices": [
    {
      "slice_id": "cli",
      "spec": "Add a --json flag to report; …",
      "files_in_scope": ["src/cli/report.py"],
      "forbidden_paths": ["tests/**"]
    },
    {
      "slice_id": "test",
      "spec": "Cover the --json flag …",
      "files_in_scope": ["tests/test_report.py"],
      "forbidden_paths": ["src/**"]
    }
  ]
}
```

State is **per-repo**, so `/agimode on` in one repo doesn't arm another.

---

## How agimode differs from other multi-agent runners

Tools like claude-squad, uzi, gwq, and container-use give you parallel agents or sandboxes. agimode is opinionated in five specific ways:

1. **Orchestrator-only is a hard mode, not a per-task habit.** While it's on, the frontier model delegates *all* source edits — it doesn't dip in and out of writing code.
2. **Per-slice file-scope diff validation.** Workers physically cannot land edits outside their packet; an out-of-scope diff is rejected before commit, not caught in review.
3. **Mixed executor fleet.** codex (`gpt-5.5`) and Claude (`opus-4-8` / `sonnet-5`) workers under one interface — pick by what you have installed.
4. **One opinionated pipeline.** Budget caps, one-shot auto-retry, memoization, and local-only integration are wired together, not left as parts to assemble.
5. **Honest enforcement, documented.** The README and the rule doc state exactly what's mechanically gated, what's advisory, and what's doctrine — no overclaiming.

---

## Honest enforcement

Borrowing the doctrine's own candor, here's what's actually guaranteed versus what rests on the orchestrator following the rules:

- **Per-slice file-scope is mechanically enforced.** `validate_diff` rejects any worker that touches a path outside its `files_in_scope` or inside another slice's scope. This is a deterministic path check, not a judgment call — a straying worker is never committed.
- **The Fable→Fable spawn gate is mechanical.** Claude Code hook stdin doesn't expose the live session model, but the parent transcript records each assistant `message.model`; a guard reads the last flushed main-agent record and blocks a Fable orchestrator from spawning Fable subagents (exit 2). It fails **open** if the transcript is missing or unreadable, and can lag a mid-turn `/model` switch by one turn.
- **The orchestrator-only invariant is advisory + canary-proven, not a hard block.** A per-session surface reminder and a per-edit advisory nudge fire when the orchestrator is about to edit source directly; a live canary fails if the coordinator edited source during a run. Nothing hard-blocks a determined direct edit — the value rests on doctrine.
- **Decomposition correctness is judgment, not a gate.** Scope is enforced; whether your split is *sensible* is not. A wrong-but-scope-valid split surfaces as a wasted dispatch or a merge conflict, not a blocked action.

We'd rather tell you where the guardrails end than imply they're everywhere.

---

## Safety

Workers run **headless with a pinned, least-privilege posture** — it is never inherited from your user config:

- **codex** workers run under the `workspace-write` sandbox with `approval_policy=never`, confined to their own git worktree. They never commit; the engine owns commit and integration.
- **claude** workers run with `acceptEdits` and an explicit tool allowlist — **never** `bypassPermissions`.

Because each worker is isolated in its own worktree branched from a frozen base, and integration is local-only, a misbehaving worker can at worst produce a rejected or conflicting diff — it cannot touch your main branch or push anything.

---

## License

MIT © 2026 Motium-AI. See [LICENSE](./LICENSE).
