# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.premerge` -- one PR tested the way Actions tested it.

Everything outside the module is a fake with the real one's contract: git
(fetch copies the remote's refs into the refspec's destination, rev-parse
reads them back, for-each-ref and `update-ref --stdin` manage them), gh (the
PR as GitHub answers it, statuses, the merge, the read-back), the probe, the
merge preview and the leg runner. What is asserted is what the module decides
and what it tells GitHub -- the ledger it writes is the real one, in the
test's own store.
"""
import itertools
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mindhive import events
from mindhive.ci import cli as ci_cli
from mindhive.ci import github as gh_mod
from mindhive.ci import legrun, legs, premerge
from mindhive.ci import preview as preview_mod

BASE, HEAD, NEWBASE, NEWHEAD, MERGED = ("1" * 40, "2" * 40, "3" * 40, "4" * 40,
                                        "5" * 40)
TREE, TREE2, TESTED, TESTED2 = "a" * 40, "b" * 40, "c" * 40, "d" * 40
NOW = "2026-09-30T04:05:06Z"


def pr_view(state="OPEN", draft=False, head=HEAD, base="master", fork=False):
    return {"number": 151, "state": state, "isDraft": draft,
            "isCrossRepository": fork,
            "headRepositoryOwner": {"login": "stranger" if fork else "owner"},
            "headRefName": "claude/feature", "headRefOid": head,
            "baseRefName": base, "title": "a feature", "url": "https://x/151"}


class FakeGit:
    def __init__(self, fetch_rc=0, trees=None, local=None):
        self.remote = {"refs/heads/master": BASE, "refs/pull/151/head": HEAD,
                       "refs/pull/149/head": NEWHEAD}
        self.refs = {}
        self.local = dict(local or {"master": BASE})
        self.trees = dict(trees or {MERGED: TREE})
        self.fetch_rc = fetch_rc
        self.calls = []

    def __call__(self, argv, cwd=None, env=None, timeout_s=None, stdin=None, log=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb = argv[1]
        if verb == "fetch":
            if self.fetch_rc:
                return self.fetch_rc, "", "fatal: unable to access: Could not resolve host"
            for spec in argv[argv.index("origin") + 1:]:
                src, dst = spec.lstrip("+").split(":", 1)
                if src not in self.remote:
                    return 128, "", f"fatal: couldn't find remote ref {src}"
                self.refs[dst] = self.remote[src]
            return 0, "", ""
        if verb == "rev-parse":
            name, _, kind = argv[-1].partition("^{")
            if kind == "commit}":
                sha = self.refs.get(name) or self.local.get(name)
            else:
                sha = self.trees.get(name)
            return (0, sha + "\n", "") if sha else (1, "", "")
        if verb == "for-each-ref":
            return 0, "".join(r + "\n" for r in sorted(self.refs)
                              if r.startswith(argv[-1])), ""
        if verb == "update-ref":
            for line in (stdin or "").splitlines():
                assert line.startswith("delete "), line
                self.refs.pop(line[len("delete "):], None)
            return 0, "", ""
        return 1, "", f"git {verb}: not in this fake"

    def run_refs(self):
        return [r for r in self.refs if r.startswith(premerge.REF_ROOT + "/")]


class FakeGitHub:
    def __init__(self, prs=None, merge_answer=(0, "merged"),
                 merged_answer=("MERGED", MERGED)):
        self.prs = list(prs or [pr_view()])
        self.pr_calls = 0
        self.statuses, self.merges = [], []
        self.merge_answer, self.merged_answer = merge_answer, merged_answer

    def pr(self, number):
        assert number == 151
        answer = self.prs[min(self.pr_calls, len(self.prs) - 1)]
        self.pr_calls += 1
        return dict(answer)

    def post_status(self, sha, state, context="mindhive-ci", description=""):
        self.statuses.append((sha, state, context, description))
        return ""

    def merge(self, number, head_sha):
        self.merges.append((number, head_sha))
        return self.merge_answer

    def merged(self, number):
        return self.merged_answer


class NoGitHub:
    def __getattr__(self, name):
        raise AssertionError(f"branch mode must not touch GitHub ({name})")


class FakeProbe:
    def __init__(self, missing=None):
        self.missing = dict(missing or {})

    def check(self, leg):
        if leg.name in self.missing:
            return legs.Availability(leg, False, reason=self.missing[leg.name])
        return legs.Availability(leg, True, python=f"py{leg.python or '-host'}",
                                 via="native")

    def wsl_prefix(self):
        return ["wsl.exe", "--exec"]

    def wsl_env(self):
        return {"WSLENV": ""}


def only(*names):
    """A machine that can run *names* and no other leg."""
    return FakeProbe({leg.name: "not on this machine" for leg in legs.CATALOG
                      if leg.name not in names})


class Legs:
    """The leg-runner factory: each leg answers the state it is told to."""

    def __init__(self, states=None, raises=(), on_run=None):
        self.states, self.raises, self.on_run = dict(states or {}), set(raises), on_run
        self.runs = []

    def __call__(self, run_tag):
        outer = self

        class _Runner:
            def run(self, leg, avail, commit):
                outer.runs.append((run_tag, leg.name, commit))
                if outer.on_run:
                    outer.on_run()
                if leg.name in outer.raises:
                    raise RuntimeError("the leg runner broke")
                state = outer.states.get(leg.name, "success")
                return legrun.LegResult(
                    name=leg.name, job=leg.job, ci_name=leg.ci_name, tier=leg.tier,
                    state=state, exit_code={"success": 0, "failure": 1}.get(state, 2),
                    counts={"passed": 10} if state == "success" else {"failed": 1, "passed": 9},
                    duration_s=61.0, step="pytest")
        return _Runner()


class Previews:
    def __init__(self, answers=None, tree_of=None, raises=False):
        self.answers = dict(answers or {})
        self.tree_of = tree_of or (lambda base: TREE)
        self.raises = raises
        self.asked = []

    def __call__(self, base, head):
        self.asked.append((base, head))
        if self.raises:
            raise RuntimeError("merge-tree went away")
        if (base, head) in self.answers:
            return self.answers[(base, head)]
        return preview_mod.Preview(base, head, clean=True, tree=self.tree_of(base),
                                   tested_sha=TESTED)


class Rig:
    def __init__(self, tmp_path, *, git=None, gh=None, probe=None, legs_=None,
                 previews=None, **kw):
        self.git = git or FakeGit()
        self.gh = gh or FakeGitHub()
        self.legs = legs_ or Legs()
        self.previews = previews or Previews()
        self.said = []
        self.pm = premerge.Premerge(tmp_path / "repo", project="test", run=self.git,
                                    environ={}, github=self.gh,
                                    probe=probe or FakeProbe(), leg_runner=self.legs,
                                    previewer=self.previews, now_iso=lambda: NOW,
                                    work_root=tmp_path / "work", say=self.said.append,
                                    **kw)


def recorded():
    return [e.payload for e in events.read_all("test")
            if e.type == "verification" and (e.payload or {}).get("gate") == "premerge"]


def leg_result(name, state="success"):
    leg = legs.select(name)[0]
    return legrun.LegResult(name=name, tier=leg.tier, state=state)


# --- the verdict --------------------------------------------------------------------


def test_the_verdict_table():
    clean = preview_mod.Preview(BASE, HEAD, clean=True, tree=TREE, tested_sha=TESTED)
    ok = [leg_result("windows-py3.12"), leg_result("node")]
    assert premerge.decide(preview_mod.Preview(BASE, HEAD, error="git merge-tree: boom"),
                           ok) == ("error", 2, "git merge-tree: boom")
    conflicted = preview_mod.Preview(BASE, HEAD, conflicts=("a.py", "b.py"), tree=TREE)
    assert premerge.decide(conflicted, []) == (
        "red", 1, "conflicts with the base in 2 file(s): a.py, b.py")
    assert premerge.decide(clean, [leg_result("windows-py3.12", "failure"),
                                   leg_result("node", "error")]) == (
        "red", 1, "red: windows-py3.12 (failure), node (error)")
    assert premerge.decide(clean, [leg_result("node")]) == (
        "error", 2, "no full-suite leg ran on this machine")
    assert premerge.decide(clean, ok) == ("green", 0, "green: 2 leg(s) passed")
    assert premerge.decide(clean, ok, [{"name": "pg", "reason": "no dsn"}]) == (
        "green", 0, "green: 2 leg(s) passed; not run here: pg")
    many = preview_mod.Preview(BASE, HEAD, conflicts=tuple(f"f{i}.py" for i in range(7)))
    assert premerge.decide(many, [])[2].endswith("f4.py and 2 more")


# --- a green run ------------------------------------------------------------------------


def test_a_green_run_posts_pending_then_each_leg_then_the_verdict_on_the_head(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12", "node"))
    res = rig.pm.run_pr(151)
    reason = "green: 2 leg(s) passed; not run here: " + ", ".join(
        leg.name for leg in legs.CATALOG if leg.name not in ("windows-py3.12", "node"))
    assert (res.verdict, res.exit_code, res.reason) == ("green", 0, reason)
    assert (res.base_sha, res.head_sha, res.tested_sha, res.tree) == (BASE, HEAD, TESTED, TREE)
    assert rig.previews.asked == [(BASE, HEAD)]
    assert [(name, commit) for _, name, commit in rig.legs.runs] in (
        [("windows-py3.12", TESTED), ("node", TESTED)],
        [("node", TESTED), ("windows-py3.12", TESTED)])
    posted = rig.gh.statuses
    assert {sha for sha, *_ in posted} == {HEAD}
    assert posted[0] == (HEAD, "pending", "mindhive-ci",
                         "testing the merge into master on 2 leg(s)")
    assert posted[1:3] == [(HEAD, "pending", "mindhive-ci/windows-py3.12", "queued"),
                           (HEAD, "pending", "mindhive-ci/node", "queued")]
    assert set(posted[3:5]) == {(HEAD, "success", "mindhive-ci/windows-py3.12", "10 passed in 1m01s"),
                                (HEAD, "success", "mindhive-ci/node", "10 passed in 1m01s")}
    assert posted[5:] == [(HEAD, "success", "mindhive-ci", reason)]
    assert [leg.name for leg in res.legs] == ["windows-py3.12", "node"]   # catalog order
    assert any(line.startswith("node: success") for line in rig.said)
    assert (res.merge_state, res.merge_reason) == ("skipped", "") and rig.gh.merges == []
    assert f"verdict: GREEN (exit 0) -- {reason}" in res.report_text
    # Nothing was asked to merge, so no merge line: "premerge:" in the title
    # holds the same letters, hence a line match rather than a substring one.
    assert not [line for line in res.report_text.splitlines() if line.startswith("merge:")]


def test_the_run_is_recorded_once_and_remembered_for_watch(tmp_path):
    Rig(tmp_path).pm.run_pr(151, legs_spec="windows-py3.12")
    rows = recorded()
    assert len(rows) == 1
    row = rows[0]
    assert row["argv"] == ["hive", "ci", "premerge", "151", "--legs", "windows-py3.12"]
    assert (row["exit_code"], row["verdict"], row["verified_by"]) == (0, "green", "python")
    assert (row["scope"], row["gate"], row["mode"], row["pr_id"]) == ("ci", "premerge", "github", 151)
    assert (row["commit_sha"], row["base_sha"], row["tested_sha"], row["tree"]) == (
        HEAD, BASE, TESTED, TREE)
    assert (row["merge_state"], row["merge_reason"]) == ("skipped", "")
    assert row["counts"] == {"passed": 10}
    assert [leg["name"] for leg in row["legs"]] == ["windows-py3.12"]
    assert "verdict: GREEN" in row["excerpt"] and row["artifact"]
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["head_sha"], kept["base_sha"], kept["tree"], kept["verdict"]) == (
        HEAD, BASE, TREE, "green")
    assert kept["legs"] == {"windows-py3.12": "success"} and kept["base_ref"] == "master"


# --- red, unavailable, quiet ------------------------------------------------------------


def test_a_conflict_is_red_at_once_and_no_leg_runs(tmp_path):
    conflicted = preview_mod.Preview(BASE, HEAD, conflicts=("src/mindhive/x.py",), tree=TREE)
    rig = Rig(tmp_path, previews=Previews({(BASE, HEAD): conflicted}))
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("red", 1)
    assert res.conflicts == ("src/mindhive/x.py",) and rig.legs.runs == []
    assert rig.gh.statuses == [(HEAD, "failure", "mindhive-ci",
                                "conflicts with the base in 1 file(s): src/mindhive/x.py")]
    assert "conflicts (1): src/mindhive/x.py" in res.report_text


def test_a_leg_this_machine_cannot_run_is_named_and_gets_no_status(tmp_path):
    why = "no MINDHIVE_CI_PG_DSN: the pg tier needs a throwaway Postgres 17 database"
    rig = Rig(tmp_path, probe=FakeProbe({"pg": why}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12,pg")
    assert res.verdict == "green" and res.reason == "green: 1 leg(s) passed; not run here: pg"
    assert res.unavailable == [{"name": "pg", "reason": why}]
    contexts = [ctx for _, _, ctx, _ in rig.gh.statuses]
    assert "mindhive-ci/windows-py3.12" in contexts        # canary: statuses were posted
    assert "mindhive-ci/pg" not in contexts
    assert "not run on this machine:\n  pg: " + why in res.report_text


def test_no_status_means_no_status(tmp_path):
    rig = Rig(tmp_path, post_statuses=False)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12")
    assert res.verdict == "green" and rig.gh.statuses == []
    assert recorded()[0]["argv"][-1] == "--no-status"


def test_a_fetch_that_fails_is_an_error_and_nothing_runs_or_posts(tmp_path):
    rig = Rig(tmp_path, git=FakeGit(fetch_rc=128))
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert res.reason.startswith("git fetch origin: fatal: unable to access")
    assert rig.legs.runs == [] and rig.gh.statuses == [] and rig.previews.asked == []


@pytest.mark.parametrize("case", ["green", "conflict", "preview-raises"])
def test_the_runs_refs_are_dropped_whatever_happened(tmp_path, case):
    previews = {"green": Previews(),
                "conflict": Previews({(BASE, HEAD): preview_mod.Preview(
                    BASE, HEAD, conflicts=("a.py",), tree=TREE)}),
                "preview-raises": Previews(raises=True)}[case]
    rig = Rig(tmp_path, previews=previews)
    res = rig.pm.run_pr(151)
    assert rig.git.run_refs() == []
    assert any(argv[1] == "update-ref" for argv in rig.git.calls)
    if case == "preview-raises":
        assert (res.verdict, res.exit_code) == ("error", 2)
        assert res.reason == "premerge broke: RuntimeError: merge-tree went away"
        assert rig.gh.statuses[-1] == (HEAD, "error", "mindhive-ci", res.reason)


def test_a_leg_runner_that_raises_is_a_red_leg_not_a_crash(tmp_path):
    rig = Rig(tmp_path, legs_=Legs(raises={"node"}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12,node")
    assert (res.verdict, res.reason) == ("red", "red: node (error)")
    node = res.legs[1]
    assert (node.state, node.step) == ("error", "runner")
    assert (HEAD, "error", "mindhive-ci/node",
            "runner: RuntimeError: the leg runner broke") in rig.gh.statuses


# --- the guarded merge ----------------------------------------------------------------


def test_the_merge_is_the_pinned_merge_commit_and_is_read_back(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12"))
    res = rig.pm.run_pr(151, merge=True)
    assert rig.gh.merges == [(151, HEAD)]
    assert (res.merge_state, res.merge_commit, res.exit_code) == ("merged", MERGED, 0)
    assert res.landed_tree_matches is True
    assert "merge: merged as 555555555555; the landed tree is the tested tree" in res.report_text
    assert recorded()[0]["merge_state"] == "merged"


@pytest.mark.parametrize("prs,why", [
    ([pr_view(), pr_view(head=NEWHEAD)],
     "the head moved to 444444444444 after 222222222222 was tested"),
    ([pr_view(draft=True)], "the PR is a draft"),
    ([pr_view(), pr_view(state="CLOSED")], "the PR is CLOSED"),
    ([pr_view(), pr_view(base="release")], "the PR now targets release, not the tested master"),
])
def test_a_merge_is_refused_when_the_pr_is_not_the_one_tested(tmp_path, prs, why):
    rig = Rig(tmp_path, gh=FakeGitHub(prs=prs), probe=only("windows-py3.12"))
    res = rig.pm.run_pr(151, merge=True)
    assert res.verdict == "green" and rig.gh.merges == []
    assert (res.merge_state, res.merge_reason, res.exit_code) == (
        "refused", why, premerge.EXIT_NOT_MERGED)
    assert f"merge: refused -- {why}" in res.report_text


def test_a_base_that_moved_to_a_different_merge_is_tested_again(tmp_path):
    git = FakeGit(trees={MERGED: TREE2})
    rig = Rig(tmp_path, git=git, probe=only("windows-py3.12"),
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): preview_mod.Preview(
                  NEWBASE, HEAD, clean=True, tree=TREE2, tested_sha=TESTED2)}))
    res = rig.pm.run_pr(151, merge=True)
    assert res.rounds == 2
    assert [commit for _, _, commit in rig.legs.runs] == [TESTED, TESTED2]
    assert rig.gh.merges == [(151, HEAD)]
    assert (res.merge_state, res.exit_code, res.landed_tree_matches) == ("merged", 0, True)
    assert (res.base_sha, res.tree) == (NEWBASE, TREE2)
    assert any("the base moved to 333333333333 after 111111111111 was tested and "
               "the merge changed; testing again" == note for note in res.notes)
    assert len({tag for tag, _, _ in rig.legs.runs}) == 2      # one leg dir per round


def test_allow_base_drift_merges_an_untested_tree_and_says_so(tmp_path):
    git = FakeGit(trees={MERGED: TREE2})
    rig = Rig(tmp_path, git=git, probe=only("windows-py3.12"),
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): preview_mod.Preview(
                  NEWBASE, HEAD, clean=True, tree=TREE2, tested_sha=TESTED2)}))
    res = rig.pm.run_pr(151, merge=True, allow_base_drift=True)
    assert res.rounds == 1 and rig.gh.merges == [(151, HEAD)]
    assert res.merge_state == "merged" and res.landed_tree_matches is False
    assert any("--allow-base-drift merged a tree this run did not test" in n for n in res.notes)
    assert any("carries a tree other than the tested" in n for n in res.notes)


def test_a_base_that_never_stops_moving_is_refused_after_the_last_round(tmp_path):
    git = FakeGit()
    moves = itertools.count(100)
    rig = Rig(tmp_path, git=git, probe=only("windows-py3.12"),
              legs_=Legs(on_run=lambda: git.remote.update(
                  {"refs/heads/master": f"{next(moves):040x}"})),
              previews=Previews(tree_of=lambda base: "t" + base[1:]))
    res = rig.pm.run_pr(151, merge=True)
    assert res.rounds == premerge.MAX_ROUNDS and rig.gh.merges == []
    assert (res.merge_state, res.merge_reason) == (
        "refused", "the base kept moving; 3 rounds tested without landing")
    assert res.exit_code == premerge.EXIT_NOT_MERGED


def test_a_merge_github_did_not_make_exits_3_in_ghs_words(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12"), gh=FakeGitHub(
        merge_answer=(1, "GraphQL: Base branch was modified. Review and try the merge again."),
        merged_answer=("OPEN", "")))
    res = rig.pm.run_pr(151, merge=True)
    assert (res.merge_state, res.merge_reason) == (
        "failed", "gh pr merge exit 1: GraphQL: Base branch was modified. Review and "
                  "try the merge again.")
    assert res.exit_code == premerge.EXIT_NOT_MERGED and res.merge_commit == ""


def test_a_landed_tree_other_than_the_tested_one_is_reported(tmp_path):
    rig = Rig(tmp_path, git=FakeGit(trees={MERGED: TREE2}), probe=only("windows-py3.12"))
    res = rig.pm.run_pr(151, merge=True)
    assert res.merge_state == "merged" and res.landed_tree_matches is False
    assert "the landed tree is NOT the tested tree" in res.report_text


# --- branch mode, and the checks before anything runs ------------------------------------


@pytest.mark.parametrize("fetch_rc", [0, 128])
def test_branch_mode_tests_the_merge_locally_and_never_touches_github(tmp_path, fetch_rc):
    git = FakeGit(fetch_rc=fetch_rc, local={"master": BASE, "claude/feature": HEAD})
    rig = Rig(tmp_path, git=git, gh=NoGitHub())
    res = rig.pm.run_branch("claude/feature", legs_spec="windows-py3.12")
    assert (res.verdict, res.mode, res.base_sha, res.head_sha) == (
        "green", "offline", BASE, HEAD)
    assert git.run_refs() == []
    if fetch_rc:
        assert res.notes[0].endswith("; tested against the local master")
    assert premerge.load_sidecar("test")["branches"]["claude/feature"]["verdict"] == "green"
    assert recorded()[0]["argv"] == ["hive", "ci", "premerge", "--branch",
                                     "claude/feature", "--base", "master",
                                     "--legs", "windows-py3.12"]


def test_branch_mode_names_a_branch_git_does_not_know(tmp_path):
    rig = Rig(tmp_path, gh=NoGitHub())
    res = rig.pm.run_branch("no/such-branch")
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert res.reason == "no commit for the branch no/such-branch"


def test_a_pr_that_is_not_open_is_an_error_before_any_fetch(tmp_path):
    rig = Rig(tmp_path, gh=FakeGitHub(prs=[pr_view(state="MERGED")]))
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert res.reason == "PR #151 is MERGED, not OPEN"
    assert rig.git.calls == [] and recorded() == []


def test_an_unknown_leg_is_an_error_naming_the_legs(tmp_path):
    rig = Rig(tmp_path)
    res = rig.pm.run_pr(151, legs_spec="nope")
    assert res.exit_code == 2 and res.reason.startswith("no leg matches nope; legs are: ")
    assert rig.gh.pr_calls == 0 and rig.git.calls == []


# --- the pieces watch and conflicts use ---------------------------------------------------


def test_fetch_base_and_fetch_heads_leave_no_ref_behind(tmp_path):
    rig = Rig(tmp_path)
    assert rig.pm.fetch_base("master") == (BASE, "")
    assert rig.pm.fetch_heads([151, 149], "master") == (BASE, {151: HEAD, 149: NEWHEAD}, "")
    assert rig.git.run_refs() == []
    base, heads, error = rig.pm.fetch_heads([151, 7], "master")
    assert error.startswith("git fetch origin: fatal: couldn't find remote ref refs/pull/7/head")


def test_the_sidecar_survives_a_corrupt_file_and_a_write_that_fails(tmp_path, monkeypatch):
    premerge.sidecar_path("test").write_text("{not json", encoding="utf-8")
    assert premerge.load_sidecar("test") == {"version": 1, "prs": {}, "branches": {}}
    assert premerge.remember("test", 151, {"verdict": "green"}) is True
    assert premerge.load_sidecar("test")["prs"] == {"151": {"verdict": "green"}}

    def refuse(path, text):
        raise OSError("disk full")
    monkeypatch.setattr(premerge.paths, "write_text_atomic", refuse)
    assert premerge.remember("test", 152, {"verdict": "red"}) is False


def test_the_default_leg_runner_runs_under_the_work_root_with_the_probes_wsl(tmp_path):
    pm = premerge.Premerge(tmp_path / "repo", project="test", environ={"A": "1"},
                           github=FakeGitHub(), probe=FakeProbe(),
                           work_root=tmp_path / "work", timeout_s=77, keep=True)
    runner = pm._runner_for("pr151-x-r1")
    assert isinstance(runner, legrun.LegRunner)
    assert (runner.work_root, runner.run_id, runner.timeout_s, runner.keep) == (
        tmp_path / "work" / "pr151-x-r1", "pr151-x-r1", 77.0, True)
    assert (runner.wsl_prefix, runner.wsl_env) == (["wsl.exe", "--exec"], {"WSLENV": ""})
    # By default the legs run outside the repository: under it, each would sit
    # inside the operator's checkout -- a `.claude/worktrees/...` path when the
    # checkout is a linked worktree, which tests that resolve the memory folder
    # from the path read differently -- and deeper than Windows' MAX_PATH wants.
    default = premerge.Premerge(tmp_path / "repo", github=FakeGitHub(),
                                probe=FakeProbe()).work_root
    assert default == Path(tempfile.gettempdir()) / "mindhive-ci"
    assert tmp_path / "repo" not in default.parents


# --- a premerge row is a verdict on a PR's merge, not a run of this checkout ------------


def test_a_row_counts_one_suite_and_keeps_the_sum_apart(tmp_path):
    Rig(tmp_path).pm.run_pr(151, legs_spec="windows-py3.12,ubuntu-py3.12,node")
    row = recorded()[0]
    assert (row["counts"], row["counts_leg"]) == ({"passed": 10}, "windows-py3.12")
    assert row["counts_total"] == {"passed": 30}


def test_no_reader_of_this_checkouts_runs_takes_a_premerge_row_for_one(tmp_path):
    from mindhive import premise, replenish, verify
    Rig(tmp_path).pm.run_pr(151, legs_spec="windows-py3.12,ubuntu-py3.12")
    evs = events.read_all("test")
    row = next(e for e in evs if e.type == "verification")
    assert row.payload["exit_code"] == 0                 # canary: a green run
    assert verify.last_run(evs) is None
    claim = {"kind": premise.KIND_TEST_COUNT, "value": 10}
    assert premise.check([claim], evs)[0]["outcome"] == premise.UNKNOWN
    assert replenish._passing_verification_ts(evs) == ""
    assert replenish.iteration_outcome(row) == ""


def test_every_module_that_spells_the_premerge_gate_spells_it_alike():
    from mindhive import oversee, verify
    assert premerge.GATE == verify.PREMERGE_GATE == oversee._PREMERGE_GATE


# --- forks and partial runs ---------------------------------------------------------------


def test_a_pr_from_a_fork_is_not_run_unless_trusted(tmp_path):
    rig = Rig(tmp_path, gh=FakeGitHub(prs=[pr_view(fork=True)]))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12")
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert res.reason.startswith("PR #151 comes from a fork (stranger): its code would "
                                 "run here as you")
    assert res.reason.endswith("`hive ci premerge 151 --trust`")
    assert rig.git.calls == [] and rig.legs.runs == [] and rig.gh.statuses == []
    assert recorded() == []
    trusted = Rig(tmp_path, gh=FakeGitHub(prs=[pr_view(fork=True)]))
    res = trusted.pm.run_pr(151, legs_spec="windows-py3.12", trust=True)
    assert res.verdict == "green" and res.argv[-1] == "--trust"   # canary: trust runs it


def test_a_pr_gh_did_not_place_counts_as_a_fork(tmp_path):
    unplaced = pr_view()
    del unplaced["isCrossRepository"]
    rig = Rig(tmp_path, gh=FakeGitHub(prs=[unplaced]))
    res = rig.pm.run_pr(151)
    assert res.exit_code == 2 and "comes from a fork (owner)" in res.reason
    assert rig.legs.runs == []


def test_a_partial_run_posts_its_legs_but_never_the_verdict_and_never_merges(tmp_path):
    rig = Rig(tmp_path)
    refused = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert (refused.exit_code, refused.reason) == (
        2, "--merge tests every leg: a partial run (--legs) is never merged")
    assert rig.gh.pr_calls == 0 and rig.git.calls == [] and recorded() == []
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12")
    assert res.verdict == "green" and res.partial and res.selected == ["windows-py3.12"]
    contexts = [ctx for _, _, ctx, _ in rig.gh.statuses]
    assert contexts and set(contexts) == {"mindhive-ci/windows-py3.12"}
    assert any(note.startswith("a partial run (1 of ") for note in res.notes)
    row = recorded()[0]
    assert (row["partial"], row["selected"]) == (True, ["windows-py3.12"])
    assert premerge.load_sidecar("test")["prs"]["151"]["selected"] == ["windows-py3.12"]
    full = Rig(tmp_path, probe=only("windows-py3.12")).pm.run_pr(151)
    assert not full.partial and full.selected == [leg.name for leg in legs.CATALOG]
    assert full.as_dict()["partial"] is False


# --- the merge guard: every refusal, every read-back ------------------------------


@pytest.mark.parametrize("probe,states,verdict,code", [
    (("windows-py3.12",), {"windows-py3.12": "failure"}, "red", 1),
    (("node",), {}, "error", 2),                     # no full-suite leg could run
])
def test_merge_is_not_attempted_unless_the_verdict_is_green(tmp_path, probe, states,
                                                            verdict, code):
    rig = Rig(tmp_path, probe=only(*probe), legs_=Legs(states=states))
    res = rig.pm.run_pr(151, merge=True)
    assert (res.verdict, res.exit_code) == (verdict, code)
    assert rig.gh.merges == [] and res.merge_state == "skipped"
    assert rig.gh.pr_calls == 1                       # never re-read for a merge


def test_allow_base_drift_never_merges_over_a_conflict(tmp_path):
    git = FakeGit()
    conflicted = preview_mod.Preview(NEWBASE, HEAD, conflicts=("a.py",), tree=TREE2)
    rig = Rig(tmp_path, probe=only("windows-py3.12"), git=git,
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): conflicted}))
    res = rig.pm.run_pr(151, merge=True, allow_base_drift=True)
    assert rig.gh.merges == []
    assert (res.verdict, res.exit_code, res.rounds) == ("red", 1, 2)

def test_a_moved_base_with_the_tested_merge_tree_merges_without_a_retest(tmp_path):
    git = FakeGit()
    rig = Rig(tmp_path, probe=only("windows-py3.12"), git=git,
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})))
    res = rig.pm.run_pr(151, merge=True)
    assert (res.rounds, res.merge_state) == (1, "merged")
    assert rig.gh.merges == [(151, HEAD)]
    assert any("the merge tree is the one tested" in n for n in res.notes)

class ReadBackFails(FakeGitHub):
    def merged(self, number):
        raise gh_mod.GitHubError("gh pr view 151: HTTP 502")

def test_a_failed_read_back_is_a_failed_merge_not_a_merged_one(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12"), gh=ReadBackFails())
    res = rig.pm.run_pr(151, merge=True)
    assert res.merge_state == "failed"
    assert res.merge_reason.startswith("gh pr merge exit 0")
    assert (res.exit_code, res.merge_commit) == (premerge.EXIT_NOT_MERGED, "")
    assert any("could not read the PR back after the merge" in n for n in res.notes)

class RereadFails(FakeGitHub):
    def pr(self, number):
        if self.pr_calls >= 1:
            raise gh_mod.GitHubError("gh pr view: HTTP 502")
        return super().pr(number)

def test_gh_failing_to_re_read_the_pr_refuses_the_merge(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12"), gh=RereadFails())
    res = rig.pm.run_pr(151, merge=True)
    assert (res.merge_state, res.merge_reason) == (
        "refused", "could not read the PR again: gh pr view: HTTP 502")
    assert rig.gh.merges == [] and res.exit_code == premerge.EXIT_NOT_MERGED

class FetchDiesLater(FakeGit):
    def __call__(self, argv, **kw):
        if str(argv[1]) == "fetch" and any(c[1] == "fetch" for c in self.calls):
            self.calls.append([str(a) for a in argv])
            return 128, "", "fatal: unable to access: Could not resolve host"
        return super().__call__(argv, **kw)

def test_a_base_that_cannot_be_re_fetched_before_the_merge_refuses_it(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12"), git=FetchDiesLater())
    res = rig.pm.run_pr(151, merge=True)
    assert res.merge_state == "refused"
    assert res.merge_reason.startswith("git fetch origin: fatal: unable to access")
    assert rig.gh.merges == [] and res.exit_code == premerge.EXIT_NOT_MERGED

def test_a_merge_commit_git_cannot_read_leaves_the_landed_tree_unknown(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12"), gh=FakeGitHub(merged_answer=("MERGED", "")))
    res = rig.pm.run_pr(151, merge=True)
    assert res.merge_state == "merged" and res.landed_tree_matches is None
    assert "merge: merged" in res.report_text                       # canary
    assert "the landed tree is" not in res.report_text

def test_a_nonzero_gh_exit_with_a_merged_pr_is_still_merged(tmp_path):
    rig = Rig(tmp_path, probe=only("windows-py3.12"), gh=FakeGitHub(merge_answer=(1, "warning: could not delete branch")))
    res = rig.pm.run_pr(151, merge=True)
    assert (res.merge_state, res.exit_code) == ("merged", 0)

def test_a_moved_base_whose_new_merge_cannot_be_computed_refuses_the_merge(tmp_path):
    git = FakeGit()
    broken = preview_mod.Preview(NEWBASE, HEAD, error="git merge-tree: boom")
    rig = Rig(tmp_path, probe=only("windows-py3.12"), git=git,
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): broken}))
    res = rig.pm.run_pr(151, merge=True)
    assert rig.gh.merges == []
    assert res.merge_state == "refused"
    assert res.merge_reason.endswith("and the new merge failed: git merge-tree: boom")
    assert res.exit_code == premerge.EXIT_NOT_MERGED


# --- what a run says when GitHub, git or the ledger will not ------------------------


class StatusesRefused(FakeGitHub):
    def post_status(self, sha, state, context="mindhive-ci", description=""):
        super().post_status(sha, state, context, description)
        return f"status {context}={state} on {sha[:12]}: HTTP 403: Resource not accessible"

def test_a_status_that_cannot_be_posted_is_reported_and_the_verdict_stands(tmp_path):
    rig = Rig(tmp_path, gh=StatusesRefused(), probe=only("windows-py3.12"))
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("green", 0)
    assert res.status_errors and "HTTP 403" in res.status_errors[0]
    assert len(res.status_errors) == len(rig.gh.statuses)
    assert "could not be posted" in res.report_text


class FetchesNothing(FakeGit):
    def __call__(self, argv, **kw):
        if str(argv[1]) == "fetch":
            self.calls.append([str(a) for a in argv])
            return 0, "", ""
        return super().__call__(argv, **kw)

def test_a_fetch_that_leaves_nothing_to_resolve_is_an_error(tmp_path):
    rig = Rig(tmp_path, git=FetchesNothing())
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert "does not resolve" in res.reason and rig.legs.runs == []

class NoSuchPR(FakeGitHub):
    def pr(self, number):
        raise gh_mod.GitHubError("gh pr view: Could not resolve to a PullRequest")

def test_gh_that_cannot_read_the_pr_is_an_error_naming_why(tmp_path):
    rig = Rig(tmp_path, gh=NoSuchPR())
    res = rig.pm.run_pr(151)
    assert (res.verdict, res.exit_code) == ("error", 2)
    assert "Could not resolve" in res.reason and rig.git.calls == [] and recorded() == []

def test_a_ledger_that_cannot_be_written_does_not_turn_a_verdict_into_a_crash(
        tmp_path, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(premerge.events, "append_event", broken)
    res = Rig(tmp_path).pm.run_pr(151, legs_spec="windows-py3.12")
    assert res.verdict == "green"
    assert any(n.startswith("the run was not recorded: OSError") for n in res.notes)


# --- the report, the defaults, the bounds -----------------------------------------


def test_render_names_a_red_legs_failed_tests_and_its_log():
    red = legrun.LegResult(name="windows-py3.12", state="failure",
                           counts={"failed": 1, "passed": 9},
                           failed_tests=("FAILED tests/test_a.py::test_b - boom",),
                           artifact="C:/runs/premerge-x.txt")
    ok = legrun.LegResult(name="node", state="success", counts={"passed": 3},
                          artifact="C:/runs/premerge-y.txt")
    res = premerge.PremergeResult(pr_id=151, branch="b", base_ref="master", legs=[red, ok],
                                  verdict="red", exit_code=1,
                                  reason="red: windows-py3.12 (failure)")
    text = premerge.render(res)
    assert "      FAILED tests/test_a.py::test_b - boom" in text
    assert "      log: C:/runs/premerge-x.txt" in text
    assert "premerge-y.txt" not in text                        # canary: green legs stay quiet

def test_the_default_previewer_merges_head_into_base_and_the_default_probe_sees_the_environment(
        tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append([str(a) for a in argv])
        return 1, "", "stop here"
    dsn = "postgresql://ci@localhost/throwaway"
    pm = premerge.Premerge(tmp_path / "repo", project="test", run=run,
                           environ={"PATH": "P", legs.PG_DSN_ENV: dsn}, github=FakeGitHub())
    pv = pm._preview(BASE, HEAD)
    assert seen[0][:2] == ["git", "merge-tree"] and seen[0][-2:] == [BASE, HEAD]
    assert pv.error.startswith("git merge-tree: ") and not pv.clean
    assert pm.probe.environ[legs.PG_DSN_ENV] == dsn

def test_status_prints_what_a_real_premerge_run_recorded(tmp_path, capsys):
    rig = Rig(tmp_path, probe=FakeProbe({"pg": "no dsn"}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12,pg")
    assert res.verdict == "green" and res.reason.endswith("not run here: pg")
    args = SimpleNamespace(project="test", pr=None, page=1, json=False)
    assert ci_cli.cmd_ci_status(args) == 0
    out = capsys.readouterr().out
    assert f"verdict: green -- {res.reason}" in out
    assert "legs: 1 success; not run: pg" in out

def _peak_concurrency(tmp_path, jobs):
    import threading
    import time

    lock, live, peak = threading.Lock(), [0], [0]

    def hold():
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
    rig = Rig(tmp_path, legs_=Legs(on_run=hold), jobs=jobs)
    res = rig.pm.run_pr(151, legs_spec="ubuntu-*,windows-*")
    return res, len(rig.legs.runs), peak[0]

@pytest.mark.parametrize("jobs,bound", [(2, 2), (1, 1), (0, 1)])
def test_jobs_bounds_how_many_legs_run_at_once(tmp_path, jobs, bound):
    res, ran, peak = _peak_concurrency(tmp_path, jobs)
    assert res.verdict == "green" and ran >= 4          # canary: enough legs to overlap
    assert peak == bound

def _leaving(tmp_path):

    class LeavesTrees(Legs):
        def __call__(self, run_tag):
            inner = super().__call__(run_tag)

            class _Runner:
                def run(_self, leg, avail, commit):
                    d = tmp_path / "work" / run_tag / leg.name
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "left.txt").write_text("x")
                    return inner.run(leg, avail, commit)
            return _Runner()
    return LeavesTrees()

@pytest.mark.parametrize("keep,left", [(False, False), (True, True)])
def test_the_run_directory_is_removed_unless_keep_is_asked_for(tmp_path, keep, left):
    rig = Rig(tmp_path, legs_=_leaving(tmp_path), keep=keep)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12")
    assert res.verdict == "green" and len(rig.legs.runs) == 1          # canary: a leg did run
    assert any((tmp_path / "work").rglob("left.txt")) is left


# --- a verdict GitHub did not take, a conflict the watch found, the sidecar's writers ----


def test_a_verdict_github_took_is_published_and_one_it_refused_is_not(tmp_path):
    ok = Rig(tmp_path / "a", probe=only("windows-py3.12")).pm.run_pr(151)
    assert ok.published is True
    refused = Rig(tmp_path / "b", gh=StatusesRefused(),
                  probe=only("windows-py3.12")).pm.run_pr(151)
    assert refused.published is False
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["published"], kept["reason"]) == (False, refused.reason)
    assert recorded()[-1]["published"] is False


def test_a_run_that_posts_no_verdict_has_nothing_to_publish(tmp_path):
    partial = Rig(tmp_path / "a").pm.run_pr(151, legs_spec="windows-py3.12")
    silent = Rig(tmp_path / "b", probe=only("windows-py3.12"),
                 post_statuses=False).pm.run_pr(151)
    assert (partial.published, silent.published) == (None, None)


def test_a_conflict_the_watch_finds_is_a_recorded_red_verdict(tmp_path):
    rig = Rig(tmp_path)
    files = tuple(f"src/f{i}.py" for i in range(7))
    res = rig.pm.record_conflict(151, branch="claude/x", base_ref="master",
                                 base_sha=NEWBASE, head_sha=HEAD, conflicts=files,
                                 tree=TREE2)
    assert (res.verdict, res.exit_code, res.published) == ("red", 1, True)
    assert res.reason == ("conflicts with master at 333333333333: src/f0.py, src/f1.py, "
                          "src/f2.py, src/f3.py, src/f4.py and 2 more")
    assert rig.gh.statuses == [(HEAD, "failure", "mindhive-ci", res.reason)]
    row = recorded()[0]
    assert (row["verdict"], row["exit_code"], row["argv"]) == ("red", 1, ["hive", "ci", "watch"])
    assert (row["commit_sha"], row["base_sha"], row["conflicts"]) == (HEAD, NEWBASE, list(files))
    assert row["partial"] is False and row["legs"] == []
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["head_sha"], kept["base_sha"], kept["checked_base_sha"]) == (HEAD, NEWBASE, NEWBASE)
    assert (kept["verdict"], kept["conflicts"]) == ("red", list(files))
    assert rig.git.calls == [] and rig.legs.runs == []       # one merge-tree was the check


def test_a_conflict_with_statuses_off_posts_nothing(tmp_path):
    rig = Rig(tmp_path, post_statuses=False)
    res = rig.pm.record_conflict(151, branch="b", base_ref="master", base_sha=NEWBASE,
                                 head_sha=HEAD, conflicts=("a.py",))
    assert res.verdict == "red" and rig.gh.statuses == [] and res.published is None


def test_a_retried_mark_survives_a_run_of_the_same_head_and_no_other(tmp_path):
    premerge.remember("test", 151, {"head_sha": HEAD, "retried": True})
    Rig(tmp_path / "a", probe=only("windows-py3.12")).pm.run_pr(151)
    assert premerge.load_sidecar("test")["prs"]["151"]["retried"] is True
    premerge.remember("test", 151, {"head_sha": NEWHEAD, "retried": True})
    Rig(tmp_path / "b", probe=only("windows-py3.12")).pm.run_pr(151)
    assert "retried" not in premerge.load_sidecar("test")["prs"]["151"]


def test_a_sidecar_that_cannot_be_read_is_never_overwritten(monkeypatch):
    premerge.remember("test", 146, {"head_sha": "x"})
    before = premerge.sidecar_path("test").read_text(encoding="utf-8")

    def denied(path):
        raise PermissionError(13, "the file is being replaced")
    monkeypatch.setattr(premerge.paths, "read_text_retry", denied)
    assert premerge.remember("test", 151, {"head_sha": "y"}) is False
    assert premerge.sidecar_path("test").read_text(encoding="utf-8") == before


def test_writers_of_the_sidecar_take_turns_and_lose_no_entry(monkeypatch):
    real = premerge.paths.write_text_atomic

    def slow(path, text):
        time.sleep(0.05)
        real(path, text)
    monkeypatch.setattr(premerge.paths, "write_text_atomic", slow)
    writers = [threading.Thread(target=premerge.remember, args=("test", n, {"head_sha": str(n)}))
               for n in range(8)]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    assert sorted(premerge.load_sidecar("test")["prs"]) == [str(n) for n in range(8)]


# --- Ctrl+C -------------------------------------------------------------------------------


def test_ctrl_c_stops_every_leg_marks_them_interrupted_and_records_the_run(
        tmp_path, monkeypatch):
    stops = []
    monkeypatch.setattr(premerge.proc, "stop_all", lambda: stops.append("stop"))
    rig = Rig(tmp_path, probe=only("windows-py3.12", "node"))

    def interrupt(text):
        if " -- " in text and not text.startswith("testing"):
            raise KeyboardInterrupt
    rig.pm._say = interrupt
    with pytest.raises(KeyboardInterrupt):
        rig.pm.run_pr(151)
    assert stops == ["stop"]
    why = "interrupted: the run was stopped by hand"
    assert {c: (s, d) for _, s, c, d in rig.gh.statuses if s != "pending"} == {
        "mindhive-ci/windows-py3.12": ("error", "interrupted"),
        "mindhive-ci/node": ("error", "interrupted"),
        "mindhive-ci": ("error", why)}
    row = recorded()[0]
    assert (row["verdict"], row["exit_code"], row["reason"]) == ("error", 2, why)
    assert "151" not in premerge.load_sidecar("test")["prs"]   # the watch tests it again
    assert rig.git.run_refs() == []



def test_ctrl_c_during_a_branch_run_is_recorded_as_an_error_and_goes_on_up(
        tmp_path, monkeypatch):
    monkeypatch.setattr(premerge.proc, "stop_all", lambda: None)
    git = FakeGit(local={"master": BASE, "claude/feature": HEAD})
    rig = Rig(tmp_path, git=git, gh=NoGitHub(), probe=only("windows-py3.12"))

    def interrupt(text):
        if " -- " in text and not text.startswith("testing"):
            raise KeyboardInterrupt
    rig.pm._say = interrupt
    with pytest.raises(KeyboardInterrupt):
        rig.pm.run_branch("claude/feature")
    row = recorded()[0]
    assert (row["verdict"], row["exit_code"], row["reason"], row["mode"]) == (
        "error", 2, premerge.INTERRUPTED, "offline")
    assert "claude/feature" not in premerge.load_sidecar("test")["branches"]
    assert git.run_refs() == []


@pytest.mark.parametrize("where,state", [("pr", "skipped"), ("merge", "unknown")])
def test_ctrl_c_in_the_guarded_merge_says_whether_gh_pr_merge_had_begun(
        tmp_path, monkeypatch, where, state):
    monkeypatch.setattr(premerge.proc, "stop_all", lambda: None)
    rig = Rig(tmp_path, probe=only("windows-py3.12"))
    real, calls = getattr(rig.gh, where), []

    def stop(*args):
        calls.append(args)
        if where == "merge" or len(calls) > 1:
            raise KeyboardInterrupt
        return real(*args)
    setattr(rig.gh, where, stop)
    with pytest.raises(KeyboardInterrupt):
        rig.pm.run_pr(151, merge=True)
    row = recorded()[0]
    assert (row["verdict"], row["reason"]) == ("error", premerge.INTERRUPTED)
    assert row["merge_state"] == state
    assert row["merge_reason"] == (premerge.MERGE_UNKNOWN if state == "unknown" else "")


# --- what a run's record says, in words a script can match ---------------------------


def test_a_merge_state_is_one_word_of_its_domain_and_the_why_is_kept_apart(tmp_path):
    assert premerge.MERGE_STATES == ("merged", "refused", "failed", "unknown", "skipped")
    one = only("windows-py3.12")
    for name in "abcd":
        (tmp_path / name).mkdir()
    Rig(tmp_path / "a", probe=one).pm.run_pr(151)
    Rig(tmp_path / "b", probe=one, gh=FakeGitHub(prs=[pr_view(draft=True)])).pm.run_pr(
        151, merge=True)
    Rig(tmp_path / "c", probe=one, gh=FakeGitHub(merge_answer=(1, "GraphQL: no"),
                                                 merged_answer=("OPEN", ""))).pm.run_pr(
        151, merge=True)
    Rig(tmp_path / "d", probe=one).pm.run_pr(151, merge=True)
    assert [(r["merge_state"], r["merge_reason"]) for r in recorded()] == [
        ("skipped", ""), ("refused", "the PR is a draft"),
        ("failed", "gh pr merge exit 1: GraphQL: no"), ("merged", "")]


def test_the_json_a_run_prints_is_what_its_row_records(tmp_path):
    res = Rig(tmp_path, probe=only("windows-py3.12", "node")).pm.run_pr(151)
    row, printed = recorded()[0], res.as_dict()
    assert set(printed) <= set(row), sorted(set(printed) - set(row))
    assert {key: row[key] for key in printed} == printed
    assert row["head_sha"] == row["commit_sha"] == HEAD
    assert printed["counts"] == {"passed": 10} and printed["artifact"]
    assert {"notes", "status_errors", "up_to_date", "merge_reason"} <= set(row)
