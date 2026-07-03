#!/usr/bin/env python3
"""agimode fleet engine — fan out N headless codex workers across isolated git
worktrees from ONE frozen base, validate each diff against its declared scope,
commit in-scope changes, integrate locally into a CLEAN integration worktree,
and report a manifest.

Design contract (from the agimode plan + dual-lane red-team):

  * Owns concurrency via ``subprocess.Popen`` — NOT Claude Bash
    ``run_in_background``. The orchestrator launches THIS module once (one Bash
    call the codex-background-enforcer can see); the module forks N codex
    workers itself.
  * ONE frozen base commit for the whole batch. All worktrees branch from it;
    integration happens only AFTER every worker terminates (no base drift).
  * Deterministic diff-path validator before commit: every changed path must be
    in the slice's ``files_in_scope`` and none in ``forbidden_paths`` or another
    slice's scope. A worker that strays is rejected, never committed.
  * Integration is LOCAL ONLY — a clean integration worktree, explicit
    ``git merge --no-ff``, conflict => abort + preserve branch + record. Never
    ``merge_worktree`` (it pushes to origin and merges into main's checkout).
  * Memoization: a slice whose ``spec_sha`` already reached ``integrated`` in the
    arc manifest is NOT re-dispatched (codex is paid + non-deterministic).
  * Per-job executor selection: ``executor=codex`` (default) or ``executor=claude``.
  * Per-arc budget: stop dispatching new paid workers once the budget
    (``max_codex_calls``) is hit; the field name is unchanged for compatibility.
  * Bounded auto-repair: a rejected/failed slice gets one automatic retry, still
    inside the same per-arc paid-worker budget, with failure feedback appended.

This is the Phase-1 critical-path engine. It is import-light (reuses
``worktree_manager`` for lock-safe worktree lifecycle) and never commits to or
pushes the main branch.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import worktree_manager as wt

WRAPPER = (
    Path(__file__).parent.parent
    / "skills" / "agimode" / "scripts" / "run-agimode-codex.sh"
)
CLAUDE_WRAPPER = (
    Path(__file__).parent.parent
    / "skills" / "agimode" / "scripts" / "run-agimode-claude.sh"
)

GIT_IDENTITY = ["-c", "user.name=agimode-fleet", "-c", "user.email=agimode-fleet@local"]


# ---------------------------------------------------------------------------
# git helpers (the engine's own; worktree_manager owns lifecycle, not content)
# ---------------------------------------------------------------------------

def _git(args, cwd, check=True):
    res = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} (cwd={cwd}) failed: {res.stderr.strip()}")
    return res


def _spec_sha(spec_text: str) -> str:
    return hashlib.sha256(spec_text.encode("utf-8")).hexdigest()[:16]


def _changed_paths(worktree: str) -> list[str]:
    """Paths codex changed in a worktree (uncommitted), excluding .claude/.

    Parses ``git status --porcelain`` so untracked (``??``), modified, added,
    and renamed entries are all captured.
    """
    # --untracked-files=all so a brand-new untracked directory lists each file
    # individually (e.g. agimode_proof/alpha.md) instead of collapsing to the
    # bare dir (agimode_proof/), which would defeat per-file scope validation.
    res = _git(["status", "--porcelain", "--untracked-files=all", "-z"], cwd=worktree)
    out = res.stdout
    paths: list[str] = []
    # -z output: records separated by NUL; rename records carry two NUL fields.
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        if not rec:
            i += 1
            continue
        status = rec[:2]
        path = rec[3:]
        if status[0] == "R" or status[1] == "R":
            # rename: the NEXT field is the OLD path. Validate BOTH source and
            # destination — a worker must not rename/move an OUT-OF-SCOPE tracked
            # file INTO an in-scope destination and slip past scope validation.
            old = fields[i + 1] if i + 1 < len(fields) else ""
            if old and not old.startswith(".claude/"):
                paths.append(old)
            i += 2
        else:
            i += 1
        if path:
            paths.append(path)
    # Exclude our own spec/artifacts under .claude/.
    return [p for p in paths if not p.startswith(".claude/")]


def _path_in_scope(path: str, scope: list[str]) -> bool:
    """A path is in scope if it equals or sits under any scope entry.

    Scope entries are treated as path prefixes (a file or a directory). This is
    a deterministic Level-3 path check — intentionally not a glob DSL.
    """
    for s in scope:
        s = s.rstrip("/")
        if path == s or path.startswith(s + "/"):
            return True
    return False


def validate_diff(
    worktree: str,
    files_in_scope: list[str],
    forbidden_paths: list[str],
    other_scopes: list[str],
    allow_noop: bool = False,
) -> tuple[bool, str, list[str]]:
    """Return (ok, reason, changed_paths). Rejects out-of-scope / forbidden edits."""
    changed = _changed_paths(worktree)
    if not changed and not allow_noop:
        return False, "empty diff (worker produced no in-scope change)", changed
    for p in changed:
        if _path_in_scope(p, forbidden_paths):
            return False, f"changed a FORBIDDEN path: {p}", changed
        if _path_in_scope(p, other_scopes):
            return False, f"changed ANOTHER slice's path: {p}", changed
        if not _path_in_scope(p, files_in_scope):
            return False, f"changed an OUT-OF-SCOPE path: {p}", changed
    return True, "all changes in scope", changed


def commit_worker(worktree: str, changed_paths: list[str], message: str) -> str:
    """Commit exactly the in-scope changed paths onto the worktree's branch.

    Uses an explicit ``git add <paths>`` (never ``-A``) so nothing outside the
    validated scope is committed, with a pinned identity so it works without
    global git config.
    """
    if changed_paths:
        _git(["add", "--", *changed_paths], cwd=worktree)
    _git([*GIT_IDENTITY, "commit", "-m", message], cwd=worktree)
    return _git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()


# ---------------------------------------------------------------------------
# worker dispatch (Popen-owned concurrency)
# ---------------------------------------------------------------------------

def _status_path(worktree: str) -> Path | None:
    """The single status.json under <worktree>/.claude/agimode/*/ (one run/worktree)."""
    base = Path(worktree) / ".claude" / "agimode"
    if not base.exists():
        return None
    candidates = sorted(base.glob("*/status.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _read_status(worktree: str) -> dict | None:
    sp = _status_path(worktree)
    if sp is None:
        return None
    try:
        return json.loads(sp.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def dispatch(job: dict) -> dict:  # noqa: C901 — cohesive Popen pool + drain + integrate
    """Run the fleet for one frontier and return the manifest.

    job = {
      "arc_id": str,
      "main_repo": str (defaults cwd),
      "max_workers": int (default 4),
      "max_codex_calls": int|None (paid worker launches for either executor),
      "timeout_sec": int (default 1800),
      "oracle_command": str|None,
      "oracle_timeout_sec": int (default 600),
      "executor": "codex"|"claude" (default "codex"),
      "claude_model": str (default "claude-sonnet-5"),
      "claude_effort": str (default "high"),
      "slices": [
        {"slice_id": str, "spec": "<full codex work-packet text>",
         "files_in_scope": [..], "forbidden_paths": [..], "allow_noop": bool,
         "no_retry": bool}
      ],
    }
    """
    arc_id = job["arc_id"]
    main_repo = job.get("main_repo") or os.getcwd()
    main_root = str(wt.get_main_repo_root(main_repo))
    max_workers = int(job.get("max_workers", 4))
    max_codex_calls = job.get("max_codex_calls")
    timeout_sec = int(job.get("timeout_sec", 1800))
    oracle_command = job.get("oracle_command")
    oracle_timeout_sec = int(job.get("oracle_timeout_sec", 600))
    executor = job.get("executor", "codex")
    if executor not in ("codex", "claude"):
        raise ValueError(f"invalid executor {executor!r}; expected 'codex' or 'claude'")
    claude_model = job.get("claude_model", "claude-sonnet-5")
    claude_effort = job.get("claude_effort", "high")
    slices = job["slices"]

    # ONE frozen base for the whole batch.
    base = _git(["rev-parse", "HEAD"], cwd=main_root).stdout.strip()

    manifest_dir = Path(main_root) / ".claude" / "agimode" / arc_id
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.json"
    prior = {}
    prior_calls = 0
    prior_base = None
    if manifest_path.exists():
        try:
            prior_manifest = json.loads(manifest_path.read_text())
            prior = {s["slice_id"]: s for s in prior_manifest.get("slices", [])}
            prior_calls = int(prior_manifest.get("codex_calls", 0))
            prior_base = prior_manifest.get("base_commit")
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            prior = {}
            prior_calls = 0
            prior_base = None

    # Reject duplicate slice_ids up front — live worker state is keyed by slice_id,
    # so a duplicate would overwrite the first worker's Popen/log and orphan a paid run.
    seen_ids: set[str] = set()
    for s in slices:
        if s["slice_id"] in seen_ids:
            raise ValueError(f"duplicate slice_id {s['slice_id']!r} — must be unique within a job")
        seen_ids.add(s["slice_id"])

    # Build per-slice scope map for cross-slice validation.
    scope_of = {s["slice_id"]: s.get("files_in_scope", []) for s in slices}
    slice_by_id = {s["slice_id"]: s for s in slices}

    records: list[dict] = []
    # Per-ARC budget: seed from prior dispatches' spend so max_codex_calls bounds
    # the whole arc (cumulative), not just one invocation.
    codex_calls = prior_calls
    for s in slices:
        sid = s["slice_id"]
        sha = _spec_sha(s["spec"])
        agent_id = f"{arc_id}-{sid}"
        rec = {
            "slice_id": sid, "spec_sha": sha, "agent_id": agent_id,
            "branch": f"{wt.BRANCH_PREFIX}/{agent_id}",
            "state": "pending", "worktree": None, "status": None,
            "validated": None, "reason": None, "commit_sha": None,
        }
        # Memoization: skip a slice whose paid worker already ran THIS arc against the
        # SAME base + spec. Accept integrated / memoized / committed (committed = a
        # prior crash AFTER the worker committed but before integration — its branch
        # exists, so re-merge it, never re-bill). Require prior_base == base so a
        # result built against a since-moved HEAD is NOT silently reused. Executor is
        # intentionally NOT part of this key: base + spec_sha memoizes the slice for
        # the arc, even if a prior run used the other executor.
        p = prior.get(sid)
        if (p and p.get("spec_sha") == sha and prior_base == base
                and p.get("state") in ("integrated", "memoized", "committed")):
            rec.update(p)
            rec["state"] = "memoized"
            records.append(rec)
            continue
        records.append(rec)

    to_run = [r for r in records if r["state"] == "pending"]

    # Create worktrees from the frozen base, then launch workers. create_worktree
    # branches from current HEAD; HEAD is stable across this loop, so every
    # base_commit must equal `base` (asserted per worker).
    live: dict[str, subprocess.Popen] = {}
    fhs: dict[str, object] = {}            # open log handles, closed on reap
    launched_at: dict[str, float] = {}     # monotonic launch time per worker
    reap_grace = 60  # seconds past the wrapper's own timeout before engine reaps

    def _launch(rec, spec_text: str | None = None, retry_meta: dict | None = None):
        nonlocal codex_calls
        sid = rec["slice_id"]
        # A single slice's setup failure must NOT bubble and orphan the paid
        # workers already running — record the error and continue.
        try:
            s = slice_by_id[sid]
            if retry_meta:
                rec.update(retry_meta)
                rec.update({
                    "agent_id": retry_meta["agent_id"],
                    "branch": f"{wt.BRANCH_PREFIX}/{retry_meta['agent_id']}",
                    "worktree": None, "status": None, "validated": None,
                    "reason": None, "commit_sha": None,
                })
                rec.pop("changed_paths", None)
            path = wt.create_worktree(rec["agent_id"], main_repo=main_root)
            rec["worktree"] = str(path)
            wt_state = json.loads((path / ".claude" / "worktree-agent-state.json").read_text())
            if wt_state.get("base_commit") != base:
                rec["state"] = "error"
                rec["reason"] = f"base drift: {wt_state.get('base_commit')} != {base}"
                return
            # Drop the spec under .claude/ (excluded from the dirty-tree guard).
            spec_path = path / ".claude" / f"agimode-spec-{sid}.md"
            spec_path.write_text(spec_text if spec_text is not None else s["spec"])
            log_path = path / ".claude" / "agimode-worker.log"
            wrapper = CLAUDE_WRAPPER if executor == "claude" else WRAPPER
            cmd = [
                "bash", str(wrapper),
                "--timeout-sec", str(timeout_sec),
                "--workdir", str(path),
            ]
            if executor == "claude":
                cmd.extend(["--model", str(claude_model), "--effort", str(claude_effort)])
            cmd.append(str(spec_path))
            lf = open(log_path, "w")  # noqa: SIM115 — handle outlives the fn; closed in _reap/finally
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=main_root)
            live[sid] = proc
            fhs[sid] = lf
            launched_at[sid] = time.monotonic()
            codex_calls += 1
            rec["state"] = "running"
        except Exception as exc:
            rec["state"] = "error"
            rec["reason"] = f"launch failed: {exc}"

    def _reap(sid):
        rec = next(r for r in records if r["slice_id"] == sid)
        st = _read_status(rec["worktree"]) if rec.get("worktree") else None
        rec["status"] = st.get("status") if st else "no_status"
        rec["state"] = "ran"
        fh = fhs.pop(sid, None)
        if fh:
            with contextlib.suppress(OSError):
                fh.close()
        live.pop(sid, None)
        launched_at.pop(sid, None)
        return rec

    def _drain(run_queue):  # noqa: C901 — same cohesive Popen drain loop as dispatch
        queue = list(run_queue)

        def _fill():
            while queue and len(live) < max_workers:
                if max_codex_calls is not None and codex_calls >= max_codex_calls:
                    break
                rec, spec_text, retry_meta = queue.pop(0)
                _launch(rec, spec_text, retry_meta)

        try:
            _fill()
            while live:
                time.sleep(2)
                for sid, proc in list(live.items()):
                    if proc.poll() is not None:
                        _reap(sid)
                    elif time.monotonic() - launched_at.get(sid, 0.0) > timeout_sec + reap_grace:
                        # Wrapper wedged past its own timeout — engine-side reap so
                        # the loop makes progress instead of hanging forever.
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        rec = _reap(sid)
                        if rec["status"] in (None, "no_status", "running"):
                            rec["status"] = "timed_out"
                _fill()
        finally:
            # On ANY unwind, never leave a paid worker subprocess orphaned.
            for sid, proc in list(live.items()):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                fh = fhs.pop(sid, None)
                if fh:
                    with contextlib.suppress(OSError):
                        fh.close()
            live.clear()

    def _validate_and_commit():
        for rec in records:
            if rec["state"] != "ran":
                continue
            if rec["status"] != "ok":
                rec["state"] = "worker_failed"
                rec["reason"] = f"codex status={rec['status']}"
                continue
            s = slice_by_id[rec["slice_id"]]
            others = [p for k, v in scope_of.items() if k != rec["slice_id"] for p in v]
            ok, reason, changed = validate_diff(
                rec["worktree"], s.get("files_in_scope", []),
                s.get("forbidden_paths", []), others, s.get("allow_noop", False),
            )
            rec["validated"] = ok
            rec["reason"] = reason
            rec["changed_paths"] = changed
            if not ok:
                rec["state"] = "rejected"
                continue
            if not changed:
                # validated no-op (allow_noop=True): nothing to commit or integrate.
                # commit_worker would crash on an empty `git commit` without --allow-empty.
                rec["state"] = "noop"
                continue
            rec["commit_sha"] = commit_worker(
                rec["worktree"], changed, f"agimode slice {rec['slice_id']}"
            )
            rec["state"] = "committed"

    def _retry_spec(s: dict, rec: dict) -> str:
        reason = rec.get("reason") or f"codex status={rec.get('status')}"
        changed = rec.get("changed_paths") or []
        changed_text = "\n".join(f"- {p}" for p in changed) or "- (none recorded)"
        return (
            s["spec"].rstrip()
            + "\n\n## RETRY FEEDBACK (attempt 2 of 2 — final)\n"
            + f"First attempt failed: {reason}\n\n"
            + "Offending changed_paths:\n"
            + f"{changed_text}\n\n"
            + f"Files in scope remain: {json.dumps(s.get('files_in_scope', []))}\n"
            + f"Forbidden paths remain: {json.dumps(s.get('forbidden_paths', []))}\n"
            + "Stay strictly within files-in-scope; never touch forbidden paths, "
            + "out-of-scope paths, or other slices' paths.\n"
        )

    def _retry_queue():
        queue = []
        for rec in records:
            s = slice_by_id[rec["slice_id"]]
            if rec["state"] not in ("rejected", "worker_failed"):
                continue
            if s.get("no_retry") or rec.get("retried"):
                continue
            first_attempt = {
                "status": rec["state"],
                "codex_status": rec.get("status"),
                "reason": rec.get("reason"),
                "changed_paths": rec.get("changed_paths", []),
                "worktree": rec.get("worktree"),
                "branch": rec.get("branch"),
            }
            retry_agent_id = f"{arc_id}-{rec['slice_id']}-r1"
            retry_meta = {
                "agent_id": retry_agent_id,
                "retried": True,
                "retry_of": rec["agent_id"],
                "first_attempt": first_attempt,
            }
            queue.append((rec, _retry_spec(s, rec), retry_meta))
        return queue

    _drain((r, None, None) for r in to_run)
    _validate_and_commit()
    _drain(_retry_queue())
    _validate_and_commit()

    integration: dict = {
        "worktree": None, "branch": None, "merged": [], "conflicts": [],
        "integrated": False, "unfinished": [r["slice_id"] for r in records],
    }

    def _persist() -> dict:
        out = {
            "arc_id": arc_id, "base_commit": base, "executor": executor,
            "codex_calls": codex_calls,
            "slices": records, "integration": integration,
        }
        manifest_path.write_text(json.dumps(out, indent=2))
        return out

    # Persist NOW — after the paid codex spend + commits, BEFORE _integrate — so a
    # crash in integration never loses the codex_calls record or the committed slice
    # state (which would re-bill paid codex on the next dispatch). Re-persist in the
    # finally so the post-integration state is always durable too.
    _persist()
    try:
        integration = _integrate(arc_id, main_root, base, records)
        if integration.get("integrated") and oracle_command:
            integration["oracle"] = _run_oracle(
                oracle_command, oracle_timeout_sec, integration["worktree"], manifest_dir
            )
    finally:
        out = _persist()
    return out


def _run_oracle(command: str, timeout_sec: int, worktree: str, manifest_dir: Path) -> dict:
    log_path = manifest_dir / "oracle.log"
    try:
        proc = subprocess.Popen(
            ["bash", "-c", command],
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        pgid = proc.pid
        with contextlib.suppress(OSError):
            pgid = os.getpgid(proc.pid)
        output, _ = proc.communicate(timeout=timeout_sec)
        log_path.write_text(output or "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
        try:
            output, _ = proc.communicate(timeout=5)
            output = output or ""
        except subprocess.TimeoutExpired as drain_exc:
            output = drain_exc.stdout or output
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
        except Exception:
            pass
        log_path.write_text(
            f"oracle timed out after {timeout_sec}s\n{output}"
        )
        exit_code = None
    except Exception as exc:
        log_path.write_text(f"oracle failed to execute: {exc}\n")
        exit_code = None
    return {
        "command": command,
        "exit_code": exit_code,
        "passed": exit_code == 0,
        "log_path": str(log_path),
    }


def _integrate(arc_id: str, main_root: str, base: str, records: list[dict]) -> dict:
    """Merge committed worker branches into a CLEAN integration worktree.

    Local merges only (no origin push). Conflict => abort, preserve the branch,
    record it. Memoized slices are already in the integration history of a prior
    run; we re-merge their branch (skip-if-ancestor makes that a no-op).
    """
    mergeable = [r for r in records if r["state"] in ("committed", "memoized") and r.get("branch")]
    int_agent = f"{arc_id}-int"
    int_path = str(wt.create_worktree(int_agent, main_repo=main_root))
    # Pin the integration worktree to the FROZEN base — create_worktree branches
    # from current HEAD, which may have drifted since the workers were created off
    # `base`. The oracle must run on the same base the workers were validated against.
    _git(["reset", "--hard", base], cwd=int_path)
    result = {"worktree": int_path, "branch": f"{wt.BRANCH_PREFIX}/{int_agent}",
              "merged": [], "conflicts": [], "integrated": False}
    for r in mergeable:
        br = r["branch"]
        # A memoized slice's branch may have been deleted by a prior successful
        # cleanup. If the ref is gone, fall back to its recorded commit_sha for
        # the ancestry check, and never attempt to merge a missing ref.
        ref = br
        if _git(["rev-parse", "--verify", "--quiet", br],
                cwd=int_path, check=False).returncode != 0:
            ref = r.get("commit_sha")
            if not ref:
                # No branch, no recorded commit — assume already integrated.
                result["merged"].append(r["slice_id"])
                continue
        # Skip if already an ancestor (idempotent re-integration).
        anc = _git(["merge-base", "--is-ancestor", ref, "HEAD"], cwd=int_path, check=False)
        if anc.returncode == 0:
            result["merged"].append(r["slice_id"])
            continue
        # --no-ff forces a merge commit, which needs an author identity; pin it
        # so integration works without global git config (matches commit_worker).
        m = _git([*GIT_IDENTITY, "merge", "--no-ff", "--no-edit", ref],
                 cwd=int_path, check=False)
        if m.returncode == 0:
            result["merged"].append(r["slice_id"])
            if r["state"] == "committed":
                r["state"] = "integrated"
        else:
            _git(["merge", "--abort"], cwd=int_path, check=False)
            result["conflicts"].append(r["slice_id"])
    # Honest completion: integrated only when EVERY slice reached a done state —
    # no rejected / worker_failed / error / pending / conflicted slice. committed
    # was promoted to integrated above; memoized + validated-noop also count done.
    # Prevents exit 0 / integrated:True from overstating a partial frontier (e.g.
    # a budget cutoff or an out-of-scope rejection).
    unfinished = [r["slice_id"] for r in records
                  if r["state"] not in ("integrated", "memoized", "noop")]
    result["unfinished"] = unfinished
    result["integrated"] = not unfinished and not result["conflicts"]
    return result


def main():
    """CLI: read a job JSON (path arg or stdin), run the fleet, print the manifest."""
    if len(sys.argv) > 1 and sys.argv[1] not in ("-", "--stdin"):
        job = json.loads(Path(sys.argv[1]).read_text())
    else:
        job = json.loads(sys.stdin.read())
    out = dispatch(job)
    print(json.dumps(out, indent=2))  # noqa: T201 — CLI manifest output
    # Exit 0 only when integration and the optional oracle both pass.
    sys.exit(_exit_code(out))


def _exit_code(out: dict) -> int:
    integration = out.get("integration", {})
    if not integration.get("integrated"):
        return 1
    oracle = integration.get("oracle")
    if oracle is not None and not oracle.get("passed"):
        return 1
    return 0


if __name__ == "__main__":
    main()
