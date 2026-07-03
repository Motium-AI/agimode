#!/usr/bin/env python3
"""Tests for the agimode fleet engine (agimode_fleet.py).

Real temp git repos, NO mocks (per toolkit-testing: the boundary here is git,
exercised against real git; the boundary that IS expensive/non-deterministic —
real codex — is proven by the live canary, not unit tests). Covers the
diff-path validator (incl. the untracked-new-dir regression), commit_worker,
and local integration (clean / conflict / identity-free / memoized).
"""

import json
import os
import subprocess
import sys
import time

import agimode_fleet as fleet
import pytest
import worktree_manager as wt


def _git(args, cwd, **kw):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=kw.get("check", True))


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    # No global identity assumed — pin per-commit, like the engine does.
    _git(["-c", "user.name=t", "-c", "user.email=t@t",
          "commit", "--allow-empty", "-m", "base"], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


# --------------------------------------------------------------------------
# diff-path validator (the blocker regression lives here)
# --------------------------------------------------------------------------

class TestChangedPaths:
    def test_new_untracked_file_in_new_dir_lists_individually(self, tmp_path):
        """Regression: a brand-new untracked dir must NOT collapse to 'dir/'."""
        _init_repo(tmp_path)
        (tmp_path / "agimode_proof").mkdir()
        (tmp_path / "agimode_proof" / "alpha.md").write_text("X\n")
        assert fleet._changed_paths(str(tmp_path)) == ["agimode_proof/alpha.md"]

    def test_claude_paths_excluded(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "spec.md").write_text("s")
        (tmp_path / "real.txt").write_text("r")
        assert fleet._changed_paths(str(tmp_path)) == ["real.txt"]

    def test_rename_captures_old_path_no_scope_bypass(self, tmp_path):
        """A staged rename must surface BOTH source and destination so a worker
        can't move an out-of-scope tracked file INTO scope undetected."""
        _init_repo(tmp_path)
        (tmp_path / "secret.txt").write_text("S\n")
        _git(["add", "secret.txt"], tmp_path)
        _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "secret"], tmp_path)
        (tmp_path / "agimode_proof").mkdir()
        _git(["mv", "secret.txt", "agimode_proof/renamed.md"], tmp_path)  # staged rename
        changed = fleet._changed_paths(str(tmp_path))
        assert "secret.txt" in changed  # OLD path captured (the fix)
        # in-scope is only the destination → the out-of-scope source is rejected
        ok, reason, _ = fleet.validate_diff(
            str(tmp_path), ["agimode_proof/renamed.md"], [], [])
        assert not ok and "secret.txt" in reason


class TestPathInScope:
    def test_exact_and_dir_prefix(self):
        assert fleet._path_in_scope("a/b.md", ["a/b.md"]) is True
        assert fleet._path_in_scope("a/b.md", ["a"]) is True
        assert fleet._path_in_scope("a/b.md", ["a/"]) is True

    def test_no_sibling_prefix_false_positive(self):
        # 'config' must NOT match 'configuration/x'
        assert fleet._path_in_scope("configuration/x", ["config"]) is False
        assert fleet._path_in_scope("agimode_proof_x/a", ["agimode_proof"]) is False


class TestValidateDiff:
    def _repo_with(self, tmp_path, files):
        _init_repo(tmp_path)
        for rel, content in files.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return str(tmp_path)

    def test_in_scope_accepted(self, tmp_path):
        wtp = self._repo_with(tmp_path, {"agimode_proof/alpha.md": "X"})
        ok, _reason, ch = fleet.validate_diff(wtp, ["agimode_proof/alpha.md"], [], [])
        assert ok and ch == ["agimode_proof/alpha.md"]

    def test_out_of_scope_rejected(self, tmp_path):
        wtp = self._repo_with(tmp_path, {"stray.txt": "x"})
        ok, reason, _ = fleet.validate_diff(wtp, ["agimode_proof/alpha.md"], [], [])
        assert not ok and "OUT-OF-SCOPE" in reason

    def test_forbidden_rejected(self, tmp_path):
        wtp = self._repo_with(tmp_path, {"config/x.py": "x"})
        ok, reason, _ = fleet.validate_diff(wtp, ["config/x.py"], ["config"], [])
        assert not ok and "FORBIDDEN" in reason

    def test_cross_slice_rejected(self, tmp_path):
        wtp = self._repo_with(tmp_path, {"b.md": "x"})
        ok, reason, _ = fleet.validate_diff(wtp, ["a.md"], [], ["b.md"])
        assert not ok and "ANOTHER slice" in reason

    def test_empty_diff_rejected_unless_noop(self, tmp_path):
        wtp = self._repo_with(tmp_path, {})
        ok, reason, _ = fleet.validate_diff(wtp, ["a.md"], [], [])
        assert not ok and "empty diff" in reason
        ok2, _, _ = fleet.validate_diff(wtp, ["a.md"], [], [], allow_noop=True)
        assert ok2


class TestCommitWorker:
    def test_commits_only_in_scope_with_pinned_identity(self, tmp_path):
        """Works without global git identity; stages exactly the given paths."""
        _init_repo(tmp_path)
        (tmp_path / "agimode_proof").mkdir()
        (tmp_path / "agimode_proof" / "alpha.md").write_text("X\n")
        sha = fleet.commit_worker(str(tmp_path), ["agimode_proof/alpha.md"], "slice a")
        assert len(sha) == 40
        # tree clean after commit (the one path was committed)
        assert _git(["status", "--porcelain"], tmp_path).stdout.strip() == ""
        # the file is in the committed tree
        ls = _git(["ls-files"], tmp_path).stdout
        assert "agimode_proof/alpha.md" in ls


# --------------------------------------------------------------------------
# local integration (real worktrees)
# --------------------------------------------------------------------------

@pytest.fixture
def wt_sandbox(tmp_path, monkeypatch):
    """Redirect worktree_manager's global state under tmp_path."""
    monkeypatch.setattr(wt, "WORKTREE_BASE", tmp_path / "wts")
    monkeypatch.setattr(wt, "STATE_FILE", tmp_path / "worktree-state.json")
    monkeypatch.setattr(wt, "STATE_LOCK_PATH", tmp_path / ".worktree-state.lock")


def _worker_branch(repo, base, agent_id, rel, content):
    """Create a committed worker branch off base adding one file."""
    branch = f"{wt.BRANCH_PREFIX}/{agent_id}"
    _git(["branch", branch, base], repo)
    wtdir = repo.parent / f"wk-{agent_id}"
    _git(["worktree", "add", str(wtdir), branch], repo)
    (wtdir / rel).parent.mkdir(parents=True, exist_ok=True)
    (wtdir / rel).write_text(content)
    sha = fleet.commit_worker(str(wtdir), [rel], f"slice {agent_id}")
    _git(["worktree", "remove", "--force", str(wtdir)], repo)
    return branch, sha


def _install_stub_wrapper(tmp_path, monkeypatch):
    stub = tmp_path / "stub-wrapper.sh"
    stub.write_text("""#!/usr/bin/env bash
set -euo pipefail

workdir=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "--workdir" ]]; then
    workdir="$arg"
  fi
  prev="$arg"
done
spec="${@: -1}"

marker() {
  local key="$1"
  awk -v key="$key" 'index($0, key "=") == 1 {print substr($0, length(key) + 2); exit}' "$spec"
}

touch_rel() {
  local rel="$1"
  if [[ -n "$rel" ]]; then
    mkdir -p "$(dirname "$workdir/$rel")"
    printf 'stub\\n' > "$workdir/$rel"
  fi
}

status="$(marker "STUB:status")"
if [[ -z "$status" ]]; then
  status="ok"
fi

if grep -q "RETRY FEEDBACK" "$spec"; then
  retry_status="$(marker "STUB:retry_status")"
  if [[ -n "$retry_status" ]]; then
    status="$retry_status"
  fi
  retry_out="$(marker "STUB:retry_out_of_scope")"
  retry_touch="$(marker "STUB:retry_touch")"
  if [[ -n "$retry_out" ]]; then
    touch_rel "$retry_out"
  else
    touch_rel "$retry_touch"
  fi
else
  out_of_scope="$(marker "STUB:out_of_scope")"
  touch_path="$(marker "STUB:touch")"
  if [[ -n "$out_of_scope" ]]; then
    touch_rel "$out_of_scope"
  else
    touch_rel "$touch_path"
  fi
fi

mkdir -p "$workdir/.claude/agimode/stub"
printf '{"status":"%s"}\\n' "$status" > "$workdir/.claude/agimode/stub/status.json"
""")
    stub.chmod(0o755)
    monkeypatch.setattr(fleet, "WRAPPER", stub)
    return stub


def _install_recording_stub_wrapper(tmp_path, monkeypatch, attr, argv_log):
    stub = tmp_path / f"stub-{attr.lower()}.sh"
    monkeypatch.setenv("STUB_ARGV_LOG", str(argv_log))
    stub.write_text("""#!/usr/bin/env bash
set -euo pipefail

python3 - "$STUB_ARGV_LOG" "$@" <<'PY'
import json
import sys

with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[2:]) + "\\n")
PY

workdir=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "--workdir" ]]; then
    workdir="$arg"
  fi
  prev="$arg"
done
spec="${@: -1}"

marker() {
  local key="$1"
  awk -v key="$key" 'index($0, key "=") == 1 {print substr($0, length(key) + 2); exit}' "$spec"
}

touch_rel() {
  local rel="$1"
  if [[ -n "$rel" ]]; then
    mkdir -p "$(dirname "$workdir/$rel")"
    printf 'stub\\n' > "$workdir/$rel"
  fi
}

status="$(marker "STUB:status")"
if [[ -z "$status" ]]; then
  status="ok"
fi

if grep -q "RETRY FEEDBACK" "$spec"; then
  retry_status="$(marker "STUB:retry_status")"
  if [[ -n "$retry_status" ]]; then
    status="$retry_status"
  fi
  retry_out="$(marker "STUB:retry_out_of_scope")"
  retry_touch="$(marker "STUB:retry_touch")"
  if [[ -n "$retry_out" ]]; then
    touch_rel "$retry_out"
  else
    touch_rel "$retry_touch"
  fi
else
  out_of_scope="$(marker "STUB:out_of_scope")"
  touch_path="$(marker "STUB:touch")"
  if [[ -n "$out_of_scope" ]]; then
    touch_rel "$out_of_scope"
  else
    touch_rel "$touch_path"
  fi
fi

mkdir -p "$workdir/.claude/agimode/stub"
printf '{"status":"%s"}\\n' "$status" > "$workdir/.claude/agimode/stub/status.json"
""")
    stub.chmod(0o755)
    monkeypatch.setattr(fleet, attr, stub)
    return stub


def _install_failing_wrapper(tmp_path, monkeypatch, attr):
    stub = tmp_path / f"fail-{attr.lower()}.sh"
    stub.write_text("""#!/usr/bin/env bash
exit 99
""")
    stub.chmod(0o755)
    monkeypatch.setattr(fleet, attr, stub)
    return stub


def _argv_records(argv_log):
    if not argv_log.exists():
        return []
    return [json.loads(line) for line in argv_log.read_text().splitlines()]


def _arg_value(argv, flag):
    idx = argv.index(flag)
    return argv[idx + 1]


def _slice(slice_id, spec, files_in_scope, **kw):
    out = {
        "slice_id": slice_id,
        "spec": spec,
        "files_in_scope": files_in_scope,
        "forbidden_paths": [],
    }
    out.update(kw)
    return out


def _dispatch(repo, arc_id, slices, **kw):
    job = {
        "arc_id": arc_id,
        "main_repo": str(repo),
        "max_workers": 2,
        "timeout_sec": 15,
        "slices": slices,
    }
    job.update(kw)
    return fleet.dispatch(job)


def _manifest(repo, arc_id):
    path = repo / ".claude" / "agimode" / arc_id / "manifest.json"
    return json.loads(path.read_text())


class TestIntegrate:
    def test_disjoint_branches_merge_clean(self, tmp_path, wt_sandbox):
        repo = tmp_path / "repo"
        base = _init_repo(repo)
        b_a, sha_a = _worker_branch(repo, base, "arc-a", "alpha.md", "A\n")
        b_b, sha_b = _worker_branch(repo, base, "arc-b", "beta.md", "B\n")
        records = [
            {"slice_id": "a", "state": "committed", "branch": b_a, "commit_sha": sha_a},
            {"slice_id": "b", "state": "committed", "branch": b_b, "commit_sha": sha_b},
        ]
        res = fleet._integrate("arc", str(repo), base, records)
        assert res["integrated"] is True
        assert set(res["merged"]) == {"a", "b"} and res["conflicts"] == []
        int_wt = res["worktree"]
        assert (tmp_path.__class__(int_wt) / "alpha.md").exists()
        assert (tmp_path.__class__(int_wt) / "beta.md").exists()
        wt.cleanup_worktree("arc-int", main_repo=str(repo))

    def test_conflicting_branches_preserve_and_record(self, tmp_path, wt_sandbox):
        repo = tmp_path / "repo"
        base = _init_repo(repo)
        # both edit the SAME file differently → conflict on the second merge
        b_a, sha_a = _worker_branch(repo, base, "arc-a", "shared.md", "AAA\n")
        b_b, sha_b = _worker_branch(repo, base, "arc-b", "shared.md", "BBB\n")
        records = [
            {"slice_id": "a", "state": "committed", "branch": b_a, "commit_sha": sha_a},
            {"slice_id": "b", "state": "committed", "branch": b_b, "commit_sha": sha_b},
        ]
        res = fleet._integrate("arc", str(repo), base, records)
        assert res["integrated"] is False
        assert "a" in res["merged"] and "b" in res["conflicts"]
        # conflicting branch preserved (still resolvable)
        assert _git(["rev-parse", "--verify", "--quiet", b_b],
                    repo, check=False).returncode == 0
        wt.cleanup_worktree("arc-int", main_repo=str(repo))

    def test_integrated_false_when_a_slice_is_unfinished(self, tmp_path, wt_sandbox):
        """integrated must be False (not overstated) if any slice is rejected /
        failed / pending, even when another slice merged cleanly."""
        repo = tmp_path / "repo"
        base = _init_repo(repo)
        b_a, sha_a = _worker_branch(repo, base, "arc-a", "alpha.md", "A\n")
        records = [
            {"slice_id": "a", "state": "committed", "branch": b_a, "commit_sha": sha_a},
            {"slice_id": "b", "state": "rejected", "branch": None, "commit_sha": None},
        ]
        res = fleet._integrate("arc", str(repo), base, records)
        assert "a" in res["merged"]
        assert res["integrated"] is False          # overall NOT complete
        assert "b" in res["unfinished"]
        wt.cleanup_worktree("arc-int", main_repo=str(repo))

    def test_memoized_deleted_branch_via_commit_sha(self, tmp_path, wt_sandbox):
        repo = tmp_path / "repo"
        base = _init_repo(repo)
        b_a, sha_a = _worker_branch(repo, base, "arc-a", "alpha.md", "A\n")
        # delete the branch but the commit sha still resolves (dangling) — memoized.
        _git(["branch", "-D", b_a], repo)
        records = [{"slice_id": "a", "state": "memoized", "branch": b_a, "commit_sha": sha_a}]
        res = fleet._integrate("arc", str(repo), base, records)
        # merges via the recorded commit_sha rather than the (deleted) branch ref
        assert "a" in res["merged"] and res["conflicts"] == []
        wt.cleanup_worktree("arc-int", main_repo=str(repo))


# --------------------------------------------------------------------------
# dispatch with a stubbed paid worker boundary
# --------------------------------------------------------------------------

class TestDispatch:
    def test_default_executor_uses_codex_wrapper(self, tmp_path, monkeypatch, wt_sandbox):
        repo = tmp_path / "repo"
        _init_repo(repo)
        argv_log = tmp_path / "codex-argv.jsonl"
        _install_recording_stub_wrapper(tmp_path, monkeypatch, "WRAPPER", argv_log)

        out = _dispatch(
            repo,
            "arc-default-executor",
            [_slice("a", "STUB:touch=alpha.txt\n", ["alpha.txt"])],
            max_codex_calls=1,
        )

        records = _argv_records(argv_log)
        assert out["executor"] == "codex"
        assert out["integration"]["integrated"] is True
        assert len(records) == 1
        assert "--model" not in records[0]
        assert _arg_value(records[0], "--workdir")

    def test_claude_executor_passes_default_and_override_flags(
        self, tmp_path, monkeypatch, wt_sandbox
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        argv_log = tmp_path / "claude-argv.jsonl"
        _install_failing_wrapper(tmp_path, monkeypatch, "WRAPPER")
        _install_recording_stub_wrapper(tmp_path, monkeypatch, "CLAUDE_WRAPPER", argv_log)

        _dispatch(
            repo,
            "arc-claude-defaults",
            [_slice("a", "STUB:touch=alpha.txt\n", ["alpha.txt"])],
            executor="claude",
            max_codex_calls=1,
        )
        _dispatch(
            repo,
            "arc-claude-overrides",
            [_slice("a", "STUB:touch=beta.txt\n", ["beta.txt"])],
            executor="claude",
            claude_model="claude-opus-test",
            claude_effort="medium",
            max_codex_calls=1,
        )

        first, second = _argv_records(argv_log)
        assert _arg_value(first, "--model") == "claude-sonnet-5"
        assert _arg_value(first, "--effort") == "high"
        assert _arg_value(second, "--model") == "claude-opus-test"
        assert _arg_value(second, "--effort") == "medium"

    def test_claude_executor_integrates_and_persists_manifest(
        self, tmp_path, monkeypatch, wt_sandbox
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        argv_log = tmp_path / "claude-argv.jsonl"
        _install_failing_wrapper(tmp_path, monkeypatch, "WRAPPER")
        _install_recording_stub_wrapper(tmp_path, monkeypatch, "CLAUDE_WRAPPER", argv_log)

        out = _dispatch(
            repo,
            "arc-claude-integrates",
            [_slice("a", "STUB:touch=alpha.txt\n", ["alpha.txt"])],
            executor="claude",
            max_codex_calls=1,
        )

        assert out["integration"]["integrated"] is True
        assert out["slices"][0]["state"] == "integrated"
        assert out["executor"] == "claude"
        assert _manifest(repo, "arc-claude-integrates")["executor"] == "claude"
        assert len(_argv_records(argv_log)) == 1

    def test_invalid_executor_rejected_before_worktree_creation(
        self, tmp_path, wt_sandbox
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)

        with pytest.raises(ValueError, match="invalid executor"):
            _dispatch(
                repo,
                "arc-invalid-executor",
                [_slice("a", "STUB:touch=alpha.txt\n", ["alpha.txt"])],
                executor="bogus",
                max_codex_calls=1,
            )

        assert not wt.WORKTREE_BASE.exists() or list(wt.WORKTREE_BASE.iterdir()) == []

    def test_claude_retry_reuses_claude_wrapper_and_budget(
        self, tmp_path, monkeypatch, wt_sandbox
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        argv_log = tmp_path / "claude-retry-argv.jsonl"
        _install_failing_wrapper(tmp_path, monkeypatch, "WRAPPER")
        _install_recording_stub_wrapper(tmp_path, monkeypatch, "CLAUDE_WRAPPER", argv_log)

        out = _dispatch(
            repo,
            "arc-claude-retry",
            [_slice(
                "a",
                "STUB:status=failed\nSTUB:retry_status=ok\nSTUB:retry_touch=alpha.txt\n",
                ["alpha.txt"],
            )],
            executor="claude",
            max_codex_calls=2,
        )

        records = _argv_records(argv_log)
        assert out["integration"]["integrated"] is True
        assert out["codex_calls"] == 2
        assert len(records) == 2
        assert all(_arg_value(argv, "--model") == "claude-sonnet-5" for argv in records)
        assert out["slices"][0]["retried"] is True

    def test_rejected_slice_retries_once_and_integrates(self, tmp_path, monkeypatch, wt_sandbox):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _install_stub_wrapper(tmp_path, monkeypatch)

        out = _dispatch(
            repo,
            "arc-retry",
            [_slice(
                "a",
                "STUB:out_of_scope=stray.txt\nSTUB:retry_touch=alpha.txt\n",
                ["alpha.txt"],
            )],
            max_codex_calls=2,
        )

        rec = out["slices"][0]
        assert out["integration"]["integrated"] is True
        assert rec["state"] == "integrated"
        assert rec["retried"] is True
        assert rec["retry_of"] == "arc-retry-a"
        assert "stray.txt" in rec["first_attempt"]["reason"]
        assert out["codex_calls"] == 2

    def test_no_retry_slice_stays_failed(self, tmp_path, monkeypatch, wt_sandbox):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _install_stub_wrapper(tmp_path, monkeypatch)

        out = _dispatch(
            repo,
            "arc-no-retry",
            [_slice(
                "a",
                "STUB:out_of_scope=stray.txt\nSTUB:retry_touch=alpha.txt\n",
                ["alpha.txt"],
                no_retry=True,
            )],
            max_codex_calls=2,
        )

        rec = out["slices"][0]
        assert out["integration"]["integrated"] is False
        assert rec["state"] == "rejected"
        assert rec.get("retried") is None
        assert out["codex_calls"] == 1

    def test_budget_exhaustion_skips_retry(self, tmp_path, monkeypatch, wt_sandbox):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _install_stub_wrapper(tmp_path, monkeypatch)

        out = _dispatch(
            repo,
            "arc-budget",
            [_slice(
                "a",
                "STUB:out_of_scope=stray.txt\nSTUB:retry_touch=alpha.txt\n",
                ["alpha.txt"],
            )],
            max_codex_calls=1,
        )

        rec = out["slices"][0]
        assert out["integration"]["integrated"] is False
        assert rec["state"] == "rejected"
        assert rec["slice_id"] in out["integration"]["unfinished"]
        assert rec.get("retried") is None
        assert out["codex_calls"] == 1

    def test_retry_failure_is_final(self, tmp_path, monkeypatch, wt_sandbox):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _install_stub_wrapper(tmp_path, monkeypatch)

        out = _dispatch(
            repo,
            "arc-retry-fails",
            [_slice(
                "a",
                "STUB:out_of_scope=stray.txt\nSTUB:retry_out_of_scope=still-stray.txt\n",
                ["alpha.txt"],
            )],
            max_codex_calls=3,
        )

        rec = out["slices"][0]
        assert out["integration"]["integrated"] is False
        assert rec["state"] == "rejected"
        assert rec["retried"] is True
        assert rec["retry_of"] == "arc-retry-fails-a"
        assert "still-stray.txt" in rec["reason"]
        assert out["codex_calls"] == 2

    def test_oracle_command_passes_and_persists(self, tmp_path, monkeypatch, wt_sandbox):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _install_stub_wrapper(tmp_path, monkeypatch)

        out = _dispatch(
            repo,
            "arc-oracle-pass",
            [_slice("a", "STUB:touch=oracle.txt\n", ["oracle.txt"])],
            max_codex_calls=1,
            oracle_command="grep -n stub oracle.txt",
        )

        oracle = out["integration"]["oracle"]
        assert out["integration"]["integrated"] is True
        assert oracle["passed"] is True
        assert "stub" in (tmp_path.__class__(oracle["log_path"])).read_text()
        assert _manifest(repo, "arc-oracle-pass")["integration"]["oracle"] == oracle

    def test_oracle_command_failure_drives_exit_code(self, tmp_path, monkeypatch, wt_sandbox):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _install_stub_wrapper(tmp_path, monkeypatch)

        out = _dispatch(
            repo,
            "arc-oracle-fail",
            [_slice("a", "STUB:touch=oracle.txt\n", ["oracle.txt"])],
            max_codex_calls=1,
            oracle_command="printf 'nope\\n'; exit 1",
        )

        oracle = out["integration"]["oracle"]
        assert out["integration"]["integrated"] is True
        assert oracle["passed"] is False
        assert oracle["exit_code"] == 1
        assert fleet._exit_code(out) == 1
        assert fleet._exit_code({"integration": {"integrated": True}}) == 0
        assert fleet._exit_code({"integration": {"integrated": False}}) == 1
        assert fleet._exit_code({
            "integration": {"integrated": True, "oracle": {"passed": False}},
        }) == 1

    def test_oracle_timeout_kills_process_group_and_returns(
        self, tmp_path, monkeypatch, wt_sandbox
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _install_stub_wrapper(tmp_path, monkeypatch)

        pidfile = tmp_path / "oracle-child.pid"
        start = time.monotonic()
        out = _dispatch(
            repo,
            "arc-oracle-timeout",
            [_slice("a", "STUB:touch=oracle.txt\n", ["oracle.txt"])],
            max_codex_calls=1,
            oracle_command=f"sh -c 'sleep 300 & echo $! > {pidfile}; wait'",
            oracle_timeout_sec=2,
        )
        elapsed = time.monotonic() - start

        oracle = out["integration"]["oracle"]
        assert elapsed < 60
        assert oracle["passed"] is False
        assert oracle["exit_code"] is None
        assert (tmp_path.__class__(oracle["log_path"])).exists()
        # Prove the process-GROUP kill, not just the prompt return: the
        # backgrounded grandchild (the pipe-holder) must actually be dead.
        # SIGKILL delivery is async — poll briefly before declaring survival.
        child_pid = int(pidfile.read_text().strip())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            pytest.fail(f"oracle grandchild {child_pid} survived the process-group kill")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
