#!/usr/bin/env python3
"""agimode Phase-1 LIVE fleet-seam proof (the go/no-go gate).

Proves the single uncertain dependency end-to-end against REAL runtime data:
two real headless ``codex`` agents run in two isolated git worktrees from ONE
frozen base, each produces a real diff, the engine validates + commits + merges
both into a clean integration worktree, and a real passfail ORACLE runs on the
INTEGRATED tree (both sentinel files present with the exact content codex was
told to write, AND ``ruff`` clean on the merged tree).

This is NOT a unit test and uses NO mocks — it burns real codex calls. It is
the Phase-1 critical-path gate; Phase 4 extends it into the full multi-leg
``agimode_live_canary.py``.

Run:  python3 agimode_fleet_proof.py [--keep] [--timeout-sec N]
Exit: 0 = PROOF PASS, 1 = PROOF FAIL.
"""

# ruff: noqa: T201, C901 — CLI proof harness: prints ARE the output; main() is linear.

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent.parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import agimode_fleet as fleet  # noqa: E402
import worktree_manager as wt  # noqa: E402

ALPHA = "AGIMODE-PROOF-ALPHA-7Q2"
BETA = "AGIMODE-PROOF-BETA-7Q2"
ARC = "proof-arc"


def _spec(slice_id: str, rel_path: str, marker: str) -> str:
    return (
        f"# agimode fleet proof — slice {slice_id}\n\n"
        f"## Goal\nCreate exactly one new file at `{rel_path}` (relative to the repo "
        f"root) whose entire contents are this single line:\n\n```\n{marker}\n```\n\n"
        f"## Files in scope\n- `{rel_path}` (create it)\n\n"
        f"## Forbidden paths\n- Everything else. Do NOT touch `config/`, `.github/`, "
        f"or any file other than `{rel_path}`.\n\n"
        f"## Validation\nAfter creating the file, run `cat {rel_path}` and confirm it "
        f"contains exactly `{marker}`. Do not commit.\n"
    )


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def main() -> int:
    keep = "--keep" in sys.argv
    timeout_sec = 1800
    if "--timeout-sec" in sys.argv:
        timeout_sec = int(sys.argv[sys.argv.index("--timeout-sec") + 1])

    main_root = str(wt.get_main_repo_root(str(Path.cwd())))
    # Clean any prior manifest so memoization can't let the proof PASS without
    # actually running the 2 real codex agents it exists to prove.
    shutil.rmtree(Path(main_root) / ".claude" / "agimode" / ARC, ignore_errors=True)
    print(f"[proof] main repo: {main_root}")
    print(f"[proof] HEAD base: {_git(['rev-parse', 'HEAD'], main_root).stdout.strip()}")

    job = {
        "arc_id": ARC,
        "main_repo": main_root,
        "max_workers": 4,
        "timeout_sec": timeout_sec,
        "slices": [
            {"slice_id": "a", "spec": _spec("a", "agimode_proof/alpha.md", ALPHA),
             "files_in_scope": ["agimode_proof/alpha.md"],
             "forbidden_paths": ["config", ".github", "agimode_proof/beta.md"]},
            {"slice_id": "b", "spec": _spec("b", "agimode_proof/beta.md", BETA),
             "files_in_scope": ["agimode_proof/beta.md"],
             "forbidden_paths": ["config", ".github", "agimode_proof/alpha.md"]},
        ],
    }

    print("[proof] dispatching 2 real codex workers (this is live, may take minutes)...")
    manifest = fleet.dispatch(job)
    print("[proof] manifest:\n" + json.dumps(manifest, indent=2))

    failures: list[str] = []

    # --- leg: every slice ran REAL codex and produced an in-scope committed diff.
    # Reject "memoized" and a non-"ok" status — the proof must exercise LIVE codex,
    # never a cached prior run.
    for rec in manifest["slices"]:
        if rec["state"] not in ("committed", "integrated"):
            failures.append(f"slice {rec['slice_id']}: state={rec['state']} (expected a fresh real run) reason={rec.get('reason')}")
        if rec.get("status") != "ok":
            failures.append(f"slice {rec['slice_id']}: codex status={rec.get('status')} (expected ok — a real codex run)")

    integ = manifest["integration"]
    int_wt = integ["worktree"]

    # --- leg: integration succeeded ------------------------------------------
    if not integ["integrated"]:
        failures.append(f"integration not clean: merged={integ['merged']} conflicts={integ['conflicts']}")

    # --- ORACLE on the INTEGRATED tree (real runtime data, not a worker diff) -
    alpha_p = Path(int_wt) / "agimode_proof" / "alpha.md"
    beta_p = Path(int_wt) / "agimode_proof" / "beta.md"
    for label, p, marker in (("alpha", alpha_p, ALPHA), ("beta", beta_p, BETA)):
        if not p.exists():
            failures.append(f"oracle: {label} missing in integrated tree ({p})")
        elif marker not in p.read_text():
            failures.append(f"oracle: {label} present but missing marker {marker}")

    # CI-gate slice: ruff clean on the merged tree (no python changed → must pass).
    ruff = subprocess.run(
        ["ruff", "check", "--config", "config/references/ruff-strict.toml",
         "agimode_proof"],
        cwd=int_wt, capture_output=True, text=True,
    )
    # ruff exits 0 with "All checks passed" or when no python matched; non-zero = real lint error.
    if ruff.returncode != 0:
        failures.append(f"oracle: ruff not clean on merged tree: {ruff.stdout.strip()} {ruff.stderr.strip()}")

    # --- cleanup --------------------------------------------------------------
    if not keep:
        for rec in manifest["slices"]:
            wt.cleanup_worktree(rec["agent_id"], main_repo=main_root,
                                preserve_branch=bool(failures))
        wt.cleanup_worktree(f"{ARC}-int", main_repo=main_root, preserve_branch=bool(failures))
        # Best-effort: drop the throwaway proof dir if it leaked into main.
        subprocess.run(["git", "worktree", "prune"], cwd=main_root, check=False)
        shutil.rmtree(Path(main_root) / ".claude" / "agimode" / ARC, ignore_errors=True)

    print()
    if failures:
        print("PROOF FAIL — " + str(len(failures)) + " issue(s):")
        for f in failures:
            print(f"  - {f}")
        if keep:
            print(f"[proof] worktrees kept; integration tree at {int_wt}")
        return 1
    print("PROOF PASS — 2 real codex agents → 2 in-scope diffs → clean local "
          "integration → oracle GREEN on the merged tree.")
    if keep:
        print(f"[proof] integration tree kept at {int_wt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
