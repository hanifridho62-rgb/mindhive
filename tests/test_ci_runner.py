# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.runner` — the native CI runner (blueprint P1g; MEB CI-01).

Four gates in an ephemeral git worktree, stopping at the first red, and one
durable record of the verdict. The properties that carry the weight:

* **The ephemeral worktree never checks out the base.** `git worktree add
  --detach` and nothing afterwards names a base ref as a checkout target,
  because git refuses `fatal: 'master' is already checked out` and the whole
  merge path would die on the developer's own machine.
* **The child imports the worktree's copy, not the installed one.** A gate
  that imports the editable install is a gate that verified the wrong tree.
* **Cleanup is total.** `.pytest_cache` is usually absent, and a cleanup that
  raises on the common case leaves a worktree behind on every green run.
* **A red is recorded once, whichever gate stopped it.** A verdict nothing
  wrote down is a verdict the dashboard cannot show.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import chain, events, paths, verify
from mindhive.ci import runner as rn

GIT = "git"


# --- the world the runner runs in -----------------------------------------------------


class FakeRunner:
    """Records every child it was asked to run and answers from a script.

    *script* maps a matcher (a tuple prefix of argv) to `(rc, out, err)`; the
    LAST matching entry wins, so a test can override one step of a default.
    Anything unmatched is a silent success, which keeps a test about gate 3
    from having to spell out gates 1, 2 and 4.
    """

    def __init__(self, script=None, tree=None):
        self.calls = []
        self.script = list(script or [])
        self.tree = tree

    def __call__(self, argv, cwd=None, env=None, timeout_s=None):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "cwd": None if cwd is None else str(cwd),
                           "env": env, "timeout_s": timeout_s})
        answer = (0, "", "")
        for prefix, result in self.script:
            if self._matches(argv, prefix):
                answer = result
        if (argv[:4] == [GIT, "worktree", "add", "--detach"]
                and answer[0] == 0 and self.tree is not None):
            # What real git does, and what the gates depend on: the checkout
            # is THERE. Without it every `(wt / f).exists()` is false and gate
            # 1 silently reads as "this tree has no guard files".
            import shutil
            shutil.copytree(self.tree, argv[4],
                            ignore=shutil.ignore_patterns(".worktrees", ".git"))
        return answer

    @staticmethod
    def _matches(argv, prefix) -> bool:
        # Per TOKEN, never per substring: `("-m", "pg")` matched gate 1 on the
        # strength of `tests/test_pg_repo_gates.py`, so a test asserting the
        # pg gate had been skipped read as if it had run.
        if callable(prefix):
            return bool(prefix(argv))
        return all(str(p) in argv for p in prefix)

    def argvs(self, *needles):
        return [c["argv"] for c in self.calls
                if all(str(n) in c["argv"] for n in needles)]

    def ran(self, *needles) -> bool:
        return bool(self.argvs(*needles))

    def joined(self, *needles):
        return [c["argv"] for c in self.calls
                if all(str(n) in " ".join(c["argv"]) for n in needles)]


def _write(root: Path, rel: str, text: str = "") -> Path:
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def tree(tmp_path):
    """A directory shaped like a checkout: the five gate-1 files and a `src`.

    No git — the FakeRunner answers for git, and the tests that need a REAL
    repository build one with `real_repo`.
    """
    root = tmp_path / "checkout"
    for rel in rn.GATE1_FILES:
        _write(root, rel, "def test_ok():\n    assert True\n")
    _write(root, "src/mindhive/__init__.py", "")
    return root


@pytest.fixture
def runner(tree):
    fake = FakeRunner(tree=tree)
    return rn.NativeCIRunner(tree, project="test", runner=fake,
                             sleep=lambda _s: None), fake


def _git(root, *args, check=True):
    res = subprocess.run([GIT, *args], cwd=str(root), capture_output=True,
                         text=True)
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} -> {res.returncode}\n"
                             f"{res.stdout}{res.stderr}")
    return res


@pytest.fixture
def real_repo(tmp_path):
    """A real one-commit repository on `master`, with `master` CHECKED OUT.

    That is the developer's own machine, and it is the case KRITIK 2 is about:
    anything that tries to check `master` out again from the ephemeral worktree
    dies with `fatal: 'master' is already checked out`.
    """
    root = tmp_path / "real"
    root.mkdir()
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "ci@example.invalid")
    _git(root, "config", "user.name", "ci")
    _write(root, "src/mindhive/__init__.py", "VALUE = 'from-the-worktree'\n")
    for rel in rn.GATE1_FILES:
        _write(root, rel, "def test_ok():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "branch", "feature/one")
    return root


# --- the verdict ----------------------------------------------------------------------


def test_the_result_carries_the_whole_verdict_and_its_stamp():
    res = rn.CIRunResult(pr_id=7, branch="feature/one")
    assert res.verified_by == "python" == verify.VERIFIED_BY
    assert res.pr_id == 7 and res.branch == "feature/one"
    # Every field the dashboard and the event payload read, present on a bare
    # construction: a key that appears only sometimes is how a renderer starts
    # failing on the quiet case.
    for field in ("commit_sha", "ok", "exit_code", "duration_s", "counts",
                  "failed_tests", "report_text", "mode", "merge_state",
                  "cleanup_failed"):
        assert hasattr(res, field), field
    assert res.merge_state in rn.MERGE_STATES
    assert res.mode in rn.MODES
    assert res.cleanup_failed is False


def test_the_merge_states_are_the_four_the_plan_names():
    assert set(rn.MERGE_STATES) == {"merged", "pending_operator", "refused",
                                    "skipped"}


# --- the environment every gate child runs in (K14, I.2) ------------------------------


def test_the_gate_env_points_the_child_at_the_worktree_first(runner, tmp_path,
                                                             monkeypatch):
    run, _ = runner
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "operator"))
    wt = tmp_path / "wt"
    env = run._gate_env(wt)
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(wt / "src")
    # The operator's own entry is still theirs, merely outranked.
    assert str(tmp_path / "operator") in parts


def test_the_gate_env_refuses_to_write_bytecode(runner, tmp_path, monkeypatch):
    run, _ = runner
    monkeypatch.delenv(paths.NO_BYTECODE_ENV, raising=False)
    assert run._gate_env(tmp_path / "wt")[paths.NO_BYTECODE_ENV] == "1"


def test_the_operators_own_bytecode_choice_is_not_overruled(runner, tmp_path,
                                                            monkeypatch):
    run, _ = runner
    monkeypatch.setenv(paths.NO_BYTECODE_ENV, "")
    assert run._gate_env(tmp_path / "wt")[paths.NO_BYTECODE_ENV] == ""


def test_a_real_child_imports_the_worktrees_own_copy(tree):
    """The point of the whole env: a gate must verify the tree under test."""
    _write(tree, "src/mindhive/__init__.py", "MARKER = 'worktree-copy'\n")
    run = rn.NativeCIRunner(tree, project="test")
    rc, out, err = run._run(
        [sys.executable, "-c", "import mindhive; print(mindhive.__file__)"],
        cwd=tree, env=run._gate_env(tree))
    assert rc == 0, err
    assert str(tree.resolve()) in str(Path(out.strip()).resolve()), out


# --- the worktree (KRITIK 2) ----------------------------------------------------------


def test_the_worktree_is_added_detached_from_the_branch(runner):
    run, fake = runner
    wt = run._acquire_worktree(7, "feature/one")
    argv = fake.argvs("worktree", "add")[0]
    assert argv[:4] == [GIT, "worktree", "add", "--detach"]
    assert argv[4] == str(wt)
    assert argv[5] == "feature/one"
    assert wt.parent.name == rn.WORKTREE_DIRNAME
    assert wt.name.startswith("ci-7-")


def test_the_worktree_add_that_fails_is_not_a_silent_success(runner):
    run, fake = runner
    fake.script.append(((["worktree", "add"]), (128, "", "fatal: nope")))
    with pytest.raises(rn.CIRunnerError) as excinfo:
        run._acquire_worktree(7, "feature/one")
    assert "fatal: nope" in str(excinfo.value)


def test_the_ephemeral_worktree_never_checks_out_the_base(runner, tmp_path):
    """KRITIK 2, as a canary over EVERY child the whole run spawns."""
    run, fake = runner
    run.run_pr_gate(7, "feature/one", auto_merge=True)
    for call in fake.calls:
        argv = call["argv"]
        if argv[:2] == [GIT, "checkout"] or argv[:2] == [GIT, "switch"]:
            assert "master" not in argv, argv


# --- the four gates -------------------------------------------------------------------


def test_the_gates_run_in_the_order_the_blueprint_names(runner, monkeypatch):
    run, fake = runner
    monkeypatch.setenv(rn.PG_DSN_ENV, "postgresql://x/y")
    run.run_pr_gate(7, "feature/one")
    pytests = [a for a in fake.argvs("-m", "pytest")]
    assert len(pytests) >= 2
    # gate 1 names the guard files; gate 2 names none.
    assert any(str(rn.GATE1_FILES[0]) in " ".join(a) for a in pytests[:1])
    assert not any(f in " ".join(pytests[1]) for f in rn.GATE1_FILES)
    assert "-m" in pytests[2] and "pg" in " ".join(pytests[2])


def test_the_gates_stop_at_the_first_red(runner):
    run, fake = runner
    fake.script.append((([rn.GATE1_FILES[0]]), (1, "1 failed", "")))
    res = run.run_pr_gate(7, "feature/one")
    assert res.ok is False
    assert res.exit_code == 1
    # gate 2 is the bare suite run; it must never have started.
    assert not [a for a in fake.argvs("-m", "pytest")
                if not any(f in " ".join(a) for f in rn.GATE1_FILES)]
    assert "gate1" in res.report_text or "gate 1" in res.report_text


def test_a_missing_gate_file_is_noted_and_the_rest_still_run(runner, tree):
    """GAP 2: pytest answers exit 4 for a path that is not there."""
    run, fake = runner
    (tree / rn.GATE1_FILES[-1]).unlink()
    res = run.run_pr_gate(7, "feature/one")
    argv = fake.argvs("-m", "pytest")[0]
    assert rn.GATE1_FILES[-1] not in argv
    assert rn.GATE1_FILES[0] in argv
    assert rn.GATE1_FILES[-1] in res.report_text
    assert res.ok is True


def test_every_gate_file_missing_does_not_run_an_argument_less_pytest(runner,
                                                                      tree):
    run, fake = runner
    for rel in rn.GATE1_FILES:
        (tree / rel).unlink()
    res = run.run_pr_gate(7, "feature/one")
    # A `pytest -q` with no paths is the WHOLE suite — gate 1 collapsing into
    # gate 2 would double the longest run in the build.
    gate1 = [a for a in fake.argvs("-m", "pytest")
             if any(f in " ".join(a) for f in rn.GATE1_FILES)]
    assert gate1 == []
    assert res.ok is True


def test_the_pg_gate_is_skipped_without_a_dsn(runner, monkeypatch):
    run, fake = runner
    monkeypatch.delenv(rn.PG_DSN_ENV, raising=False)
    res = run.run_pr_gate(7, "feature/one")
    assert not fake.ran("-m", "pg")
    assert res.ok is True


def test_the_pg_gate_runs_the_parity_check_too(runner, monkeypatch):
    run, fake = runner
    monkeypatch.setenv(rn.PG_DSN_ENV, "postgresql://x/y")
    run.run_pr_gate(7, "feature/one")
    assert fake.ran("-m", "pg")
    assert fake.ran("pg", "parity")


def test_the_chain_gate_asks_the_ledger_and_needs_no_child(runner,
                                                           monkeypatch):
    run, fake = runner
    asked = {"n": 0}

    def fake_verify(project=None):
        asked["n"] += 1
        return {"ok": False, "reason": "broken_chain"}

    monkeypatch.setattr(rn.chain, "verify", fake_verify)
    res = run.run_pr_gate(7, "feature/one")
    assert asked["n"] == 1
    assert res.ok is False
    assert "broken_chain" in res.report_text


def test_a_store_nothing_ever_checkpointed_is_not_a_broken_chain(runner):
    """`chain.verify` answers not-ok for a store `hive project` has never
    sealed, which is every fresh machine. Reading that as a red integrity gate
    would fail the very first CI run anyone makes."""
    run, fake = runner
    verdict = chain.verify("test")
    assert verdict["ok"] is False and "no chain.json" in verdict["reason"]
    res = run.run_pr_gate(7, "feature/one")
    assert res.ok is True
    assert "unverifiable, not broken" in res.report_text


def test_an_unreadable_checkpoint_is_still_a_red_gate(runner, monkeypatch):
    """The canary for the line above: only the two named reasons are excused."""
    run, fake = runner
    monkeypatch.setattr(rn.chain, "verify",
                        lambda project=None: {"ok": False,
                                              "reason": "corrupt_event_log"})
    assert run.run_pr_gate(7, "feature/one").ok is False


def test_a_green_run_is_green_all_the_way_through(runner):
    run, fake = runner
    fake.script.append(((["-m", "pytest"]), (0, "12 passed in 1.00s", "")))
    res = run.run_pr_gate(7, "feature/one")
    assert res.ok is True and res.exit_code == 0
    assert res.counts.get("passed") == 12
    assert res.duration_s >= 0.0


# --- the one durable record -----------------------------------------------------------


def _verifications(project="test"):
    return [e for e in events.read_all(project) if e.type == "verification"]


def test_the_run_records_exactly_one_verification_scoped_ci(runner):
    run, fake = runner
    fake.script.append(((["-m", "pytest"]), (0, "12 passed in 1.00s", "")))
    res = run.run_pr_gate(7, "feature/one")
    rows = _verifications()
    assert len(rows) == 1
    p = rows[0].payload
    assert p["scope"] == "ci"
    assert p["pr_id"] == 7 and p["branch"] == "feature/one"
    assert p["commit_sha"] == res.commit_sha
    assert p["exit_code"] == 0
    assert p["verified_by"] == "python"


def test_a_red_at_the_first_gate_is_still_recorded_once(runner):
    """The dashboard's red card needs a record whichever gate stopped it."""
    run, fake = runner
    fake.script.append((([rn.GATE1_FILES[0]]), (1, "1 failed, 3 passed", "")))
    run.run_pr_gate(7, "feature/one")
    rows = _verifications()
    assert len(rows) == 1
    p = rows[0].payload
    assert p["exit_code"] == 1 and p["scope"] == "ci"
    # It names the gate that decided, and its argv names the five files rather
    # than the tree — a targeted run recorded as a tree verification is the
    # one thing `run_and_record(record=False)` exists to prevent.
    assert p["gate"] == rn.GATE_NAMES[0]
    assert any(rn.GATE1_FILES[0] in a for a in p["argv"])


def test_the_chain_gate_red_is_recorded_too(runner, monkeypatch):
    run, fake = runner
    monkeypatch.setattr(rn.chain, "verify",
                        lambda project=None: {"ok": False, "reason": "torn"})
    run.run_pr_gate(7, "feature/one")
    rows = _verifications()
    assert len(rows) == 1
    assert rows[0].payload["gate"] == rn.GATE_NAMES[3]
    assert rows[0].payload["exit_code"] != 0


def test_the_record_carries_the_mode_and_the_merge_state(runner):
    run, fake = runner
    run.run_pr_gate(7, "feature/one")
    p = _verifications()[0].payload
    assert p["mode"] in rn.MODES
    assert p["merge_state"] in rn.MERGE_STATES


def test_the_caller_cannot_forge_the_stamp(runner):
    """`verify._OWN_KEYS` owns these; a CI run must not be able to relabel a
    red as a green by naming the key itself."""
    run, fake = runner
    fake.script.append((([rn.GATE1_FILES[0]]), (1, "1 failed", "")))
    run.run_pr_gate(7, "feature/one")
    p = _verifications()[0].payload
    assert p["exit_code"] == 1
    assert p["verified_by"] == "python"


# --- cleanup (I.2; MEB verbatim mechanism) --------------------------------------------


def test_cleanup_is_quiet_when_the_cache_dirs_were_never_written(runner,
                                                                 tmp_path):
    """The MEB spelling calls `rmtree` on `.pytest_cache` unconditionally, and
    on this interpreter `rmtree` of a MISSING root calls the handler with
    `os.lstat` — so the handler would `chmod` a path that is not there and
    raise `FileNotFoundError` out of every green run."""
    run, fake = runner
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    assert not (wt / ".pytest_cache").exists()
    assert run._cleanup_worktree(wt) is False          # False == did not fail


def test_cleanup_strips_the_read_only_bit_before_removing(runner, tmp_path):
    run, fake = runner
    wt = tmp_path / "wt"
    pyc = wt / "pkg" / "__pycache__"
    pyc.mkdir(parents=True)
    locked = pyc / "mod.cpython-314.pyc"
    locked.write_bytes(b"x")
    os.chmod(locked, stat.S_IREAD)
    try:
        run._cleanup_worktree(wt)
    finally:
        if locked.exists():
            os.chmod(locked, stat.S_IWRITE)
    assert not pyc.exists()


def test_cleanup_retries_the_removal_three_times(runner, tmp_path):
    run, fake = runner
    wt = tmp_path / "wt"
    wt.mkdir()
    seen = {"n": 0}

    def flaky(argv):
        if "worktree" in argv and "remove" in argv:
            seen["n"] += 1
            return seen["n"] < 3
        return False

    fake.script.append((flaky, (1, "", "locked")))
    assert run._cleanup_worktree(wt) is False
    assert len(fake.argvs("worktree", "remove")) == rn.CLEANUP_ATTEMPTS


def test_a_removal_that_never_takes_prunes_and_says_so(runner, tmp_path):
    run, fake = runner
    wt = tmp_path / "wt"
    wt.mkdir()
    fake.script.append(((["worktree", "remove"]), (1, "", "locked")))
    assert run._cleanup_worktree(wt) is True          # True == cleanup failed
    assert len(fake.argvs("worktree", "remove")) == rn.CLEANUP_ATTEMPTS
    assert fake.ran("worktree", "prune")


def test_a_failed_cleanup_never_turns_a_green_run_red(runner, tmp_path):
    run, fake = runner
    fake.script.append(((["worktree", "remove"]), (1, "", "locked")))
    res = run.run_pr_gate(7, "feature/one")
    assert res.ok is True
    assert res.cleanup_failed is True


def test_keep_leaves_the_worktree_where_it_is(runner, tmp_path):
    run, fake = runner
    wt = tmp_path / "wt"
    (wt / "pkg" / "__pycache__").mkdir(parents=True)
    assert run._cleanup_worktree(wt, keep=True) is False
    assert (wt / "pkg" / "__pycache__").exists()
    assert not fake.ran("worktree", "remove")


def test_a_red_run_keeps_its_worktree_for_the_post_mortem(runner):
    run, fake = runner
    fake.script.append((([rn.GATE1_FILES[0]]), (1, "1 failed", "")))
    run.run_pr_gate(7, "feature/one")
    assert not fake.ran("worktree", "remove")


# --- mode and base ref (III.3) --------------------------------------------------------


def test_the_base_ref_prefers_the_remote_when_it_resolves(runner):
    run, fake = runner
    fake.script.append(((["rev-parse", "--verify", "origin/master"]),
                        (0, "abc123", "")))
    assert run._base_ref() == "origin/master"


def test_the_base_ref_falls_back_to_the_local_branch(runner):
    run, fake = runner
    fake.script.append(((["rev-parse", "--verify", "origin/master"]),
                        (128, "", "fatal: Needed a single revision")))
    assert run._base_ref() == "master"


def test_a_logged_out_gh_puts_the_run_in_offline_mode(runner):
    run, fake = runner
    fake.script.append(((["gh", "auth", "status"]), (1, "", "not logged in")))
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    assert res.mode == "offline"
    assert not fake.ran("gh", "pr", "merge")
    assert "offline" in res.report_text


# --- merge, github mode ---------------------------------------------------------------


def _merged(state="MERGED"):
    return json.dumps({"state": state, "mergedAt": "2026-09-21T00:00:00Z"})


def test_the_github_merge_approves_then_squashes(runner):
    run, fake = runner
    fake.script.append(((["-m", "pytest"]), (0, "12 passed", "")))
    fake.script.append(((["gh", "pr", "view"]), (0, _merged(), "")))
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    review = fake.argvs("gh", "pr", "review")[0]
    assert review[:5] == ["gh", "pr", "review", "7", "--approve"]
    assert review[5] == "-b"
    assert review[6] == "MindHive Native CI: All 12 tests verified green."
    merge = fake.argvs("gh", "pr", "merge")[0]
    assert merge == ["gh", "pr", "merge", "7", "--squash", "--delete-branch"]
    assert res.merge_state == "merged"


def test_a_non_zero_gh_exit_does_not_overrule_a_merged_pr(runner):
    """KTD2: `--delete-branch` failing on a protected branch is a note, not a
    verdict. The PR's own state is what says whether it merged."""
    run, fake = runner
    fake.script.append(((["gh", "pr", "merge"]),
                        (1, "", "failed to delete local branch")))
    fake.script.append(((["gh", "pr", "view"]), (0, _merged(), "")))
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    assert res.merge_state == "merged"
    assert "delete local branch" in res.report_text


def test_a_pr_that_did_not_merge_is_not_reported_as_merged(runner):
    run, fake = runner
    fake.script.append(((["gh", "pr", "view"]),
                        (0, json.dumps({"state": "OPEN", "mergedAt": None}),
                         "")))
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    assert res.merge_state == "refused"


def test_a_red_run_is_never_merged(runner):
    run, fake = runner
    fake.script.append((([rn.GATE1_FILES[0]]), (1, "1 failed", "")))
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    assert res.merge_state == "skipped"
    assert not fake.ran("gh", "pr", "merge")


def test_without_auto_merge_nothing_is_merged(runner):
    run, fake = runner
    res = run.run_pr_gate(7, "feature/one")
    assert res.merge_state == "skipped"
    assert not fake.ran("gh", "pr")


# --- merge, offline mode (KRITIK 2) ---------------------------------------------------


def _offline(fake, *, base_checked_out: bool):
    fake.script.append(((["gh", "auth", "status"]), (1, "", "not logged in")))
    listing = ("worktree /repo\nHEAD abc\nbranch refs/heads/master\n"
               if base_checked_out else "worktree /repo\nHEAD abc\ndetached\n")
    fake.script.append(((["worktree", "list", "--porcelain"]), (0, listing, "")))


def test_offline_with_the_base_free_fast_forwards_it_by_fetch(runner):
    run, fake = runner
    _offline(fake, base_checked_out=False)
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    fetch = fake.argvs("fetch")[0]
    assert fetch == [GIT, "fetch", ".", "feature/one:master"]
    assert res.merge_state == "merged"


def test_offline_with_the_base_checked_out_builds_an_integration_branch(runner):
    run, fake = runner
    _offline(fake, base_checked_out=True)
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    assert not fake.ran("fetch")
    assert fake.ran("checkout", "-B", "ci/integration-7")
    merge = fake.argvs("merge", "--ff-only")[0]
    assert merge == [GIT, "merge", "--ff-only", "feature/one"]
    assert res.merge_state == "pending_operator"
    assert "git switch master" in res.report_text
    assert "git merge --ff-only ci/integration-7" in res.report_text


def test_a_fetch_that_is_not_a_fast_forward_is_reported_not_swallowed(runner):
    run, fake = runner
    _offline(fake, base_checked_out=False)
    fake.script.append(((["fetch", "."]),
                        (1, "", "! [rejected] (non-fast-forward)")))
    res = run.run_pr_gate(7, "feature/one", auto_merge=True)
    assert res.merge_state == "refused"
    assert "non-fast-forward" in res.report_text


def test_the_real_repo_survives_the_base_being_checked_out(real_repo):
    """The whole of KRITIK 2, against real git rather than a script."""
    run = rn.NativeCIRunner(real_repo, project="test")
    listing = run._run([GIT, "worktree", "list", "--porcelain"], cwd=real_repo)
    assert "refs/heads/master" in listing[1]
    wt = run._acquire_worktree(7, "feature/one")
    try:
        assert wt.is_dir()
        head = run._run([GIT, "symbolic-ref", "-q", "HEAD"], cwd=wt)
        assert head[0] != 0, "the CI worktree must be on a detached HEAD"
        rc, out, err = run._run(
            [GIT, "checkout", "-B", "ci/integration-7", "master"], cwd=wt)
        assert rc == 0, err
        assert "fatal" not in (err or "").lower()
    finally:
        run._cleanup_worktree(wt)


# --- the failure bridge and the roadmap -----------------------------------------------


def test_a_red_run_files_an_incident(runner, monkeypatch):
    run, fake = runner
    seen = []
    monkeypatch.setattr(rn.hookcapture, "record_post_tool_use_failure",
                        lambda raw, **kw: seen.append(json.loads(raw)))
    fake.script.append((([rn.GATE1_FILES[0]]), (1, "1 failed", "")))
    run.run_pr_gate(7, "feature/one")
    assert len(seen) == 1
    assert seen[0]["tool_name"] == "hive ci"
    assert "7" in json.dumps(seen[0])


def test_a_green_run_files_nothing(runner, monkeypatch):
    run, fake = runner
    seen = []
    monkeypatch.setattr(rn.hookcapture, "record_post_tool_use_failure",
                        lambda raw, **kw: seen.append(raw))
    run.run_pr_gate(7, "feature/one")
    assert seen == []


def test_the_roadmap_is_retired_only_when_the_pr_really_merged(runner,
                                                                monkeypatch):
    run, fake = runner
    swept = []
    monkeypatch.setattr(rn.roadmap, "retire_landed",
                        lambda **kw: swept.append(kw) or [])
    fake.script.append(((["gh", "pr", "view"]),
                        (0, json.dumps({"state": "OPEN", "mergedAt": None}),
                         "")))
    run.run_pr_gate(7, "feature/one", auto_merge=True)
    assert swept == []
    fake.script.append(((["gh", "pr", "view"]), (0, _merged(), "")))
    run.run_pr_gate(8, "feature/two", auto_merge=True)
    assert len(swept) == 1


def test_a_bridge_that_raises_does_not_take_the_run_with_it(runner,
                                                            monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger down")

    run, fake = runner
    monkeypatch.setattr(rn.hookcapture, "record_post_tool_use_failure", boom)
    fake.script.append((([rn.GATE1_FILES[0]]), (1, "1 failed", "")))
    res = run.run_pr_gate(7, "feature/one")
    assert res.ok is False                    # the verdict survived the bridge


# --- the head sha ---------------------------------------------------------------------


def test_the_recorded_sha_is_the_worktrees_own_head(real_repo):
    run = rn.NativeCIRunner(real_repo, project="test")
    wt = run._acquire_worktree(7, "feature/one")
    try:
        sha = run._get_head_sha(wt)
        assert len(sha) == 40 and sha.isalnum()
        expected = run._run([GIT, "rev-parse", "feature/one"], cwd=real_repo)[1]
        assert sha == expected.strip()
    finally:
        run._cleanup_worktree(wt)


def test_a_sha_that_cannot_be_read_is_empty_not_a_lie(runner, tmp_path):
    run, fake = runner
    fake.script.append(((["rev-parse", "HEAD"]), (128, "", "fatal: no HEAD")))
    assert run._get_head_sha(tmp_path) == ""


# --- the module keeps its own house ---------------------------------------------------


def test_the_runner_module_stays_under_the_meb_ceiling():
    src = Path(rn.__file__).read_text(encoding="utf-8")
    assert len(src.splitlines()) <= 500


def test_importing_the_runner_does_not_drag_in_the_orchestrator():
    # `parents[2]` of `src/mindhive/ci/runner.py` is `src/` -- one level deeper
    # than a top-level module, where the same index is the repo root. Checked
    # before spawning: handed a directory with no package, the child falls
    # back to the editable install (the MAIN checkout) with no error, and this
    # test silently measures the wrong tree.
    root = Path(rn.__file__).resolve().parents[2]
    assert (root / "mindhive" / "__init__.py").is_file(), (
        f"{root} holds no mindhive package: the child would not import this "
        "checkout")
    done = subprocess.run(
        [sys.executable, "-c",
         "import sys, mindhive.ci.runner;"
         "print(sorted(m for m in sys.modules"
         " if m.startswith('mindhive.orchestrator')))"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]", done.stdout
