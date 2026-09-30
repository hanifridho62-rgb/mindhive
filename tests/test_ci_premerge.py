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

import pytest

from mindhive import events
from mindhive.ci import legrun, legs, premerge
from mindhive.ci import preview as preview_mod

BASE, HEAD, NEWBASE, NEWHEAD, MERGED = ("1" * 40, "2" * 40, "3" * 40, "4" * 40,
                                        "5" * 40)
TREE, TREE2, TESTED, TESTED2 = "a" * 40, "b" * 40, "c" * 40, "d" * 40
NOW = "2026-09-30T04:05:06Z"


def pr_view(state="OPEN", draft=False, head=HEAD, base="master"):
    return {"number": 151, "state": state, "isDraft": draft,
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
    rig = Rig(tmp_path)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12,node")
    assert (res.verdict, res.exit_code, res.reason) == ("green", 0, "green: 2 leg(s) passed")
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
    assert posted[5:] == [(HEAD, "success", "mindhive-ci", "green: 2 leg(s) passed")]
    assert [leg.name for leg in res.legs] == ["windows-py3.12", "node"]   # catalog order
    assert any(line.startswith("node: success") for line in rig.said)
    assert res.merge_state == "" and rig.gh.merges == []
    assert "verdict: GREEN (exit 0) -- green: 2 leg(s) passed" in res.report_text


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
    assert row["merge_state"] == "skipped" and row["counts"] == {"passed": 10}
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
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12")
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
    rig = Rig(tmp_path)
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
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
])
def test_a_merge_is_refused_when_the_pr_is_not_the_one_tested(tmp_path, prs, why):
    rig = Rig(tmp_path, gh=FakeGitHub(prs=prs))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.verdict == "green" and rig.gh.merges == []
    assert (res.merge_state, res.exit_code) == (f"refused: {why}", premerge.EXIT_NOT_MERGED)


def test_a_base_that_moved_to_a_different_merge_is_tested_again(tmp_path):
    git = FakeGit(trees={MERGED: TREE2})
    rig = Rig(tmp_path, git=git,
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): preview_mod.Preview(
                  NEWBASE, HEAD, clean=True, tree=TREE2, tested_sha=TESTED2)}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
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
    rig = Rig(tmp_path, git=git,
              legs_=Legs(on_run=lambda: git.remote.update({"refs/heads/master": NEWBASE})),
              previews=Previews({(NEWBASE, HEAD): preview_mod.Preview(
                  NEWBASE, HEAD, clean=True, tree=TREE2, tested_sha=TESTED2)}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True, allow_base_drift=True)
    assert res.rounds == 1 and rig.gh.merges == [(151, HEAD)]
    assert res.merge_state == "merged" and res.landed_tree_matches is False
    assert any("--allow-base-drift merged a tree this run did not test" in n for n in res.notes)
    assert any("carries a tree other than the tested" in n for n in res.notes)


def test_a_base_that_never_stops_moving_is_refused_after_the_last_round(tmp_path):
    git = FakeGit()
    moves = itertools.count(100)
    rig = Rig(tmp_path, git=git,
              legs_=Legs(on_run=lambda: git.remote.update(
                  {"refs/heads/master": f"{next(moves):040x}"})),
              previews=Previews(tree_of=lambda base: "t" + base[1:]))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.rounds == premerge.MAX_ROUNDS and rig.gh.merges == []
    assert res.merge_state == ("refused: the base kept moving; 3 rounds tested "
                               "without landing")
    assert res.exit_code == premerge.EXIT_NOT_MERGED


def test_a_merge_github_did_not_make_exits_3_in_ghs_words(tmp_path):
    rig = Rig(tmp_path, gh=FakeGitHub(
        merge_answer=(1, "GraphQL: Base branch was modified. Review and try the merge again."),
        merged_answer=("OPEN", "")))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
    assert res.merge_state == ("failed: gh pr merge exit 1: GraphQL: Base branch was "
                               "modified. Review and try the merge again.")
    assert res.exit_code == premerge.EXIT_NOT_MERGED and res.merge_commit == ""


def test_a_landed_tree_other_than_the_tested_one_is_reported(tmp_path):
    rig = Rig(tmp_path, git=FakeGit(trees={MERGED: TREE2}))
    res = rig.pm.run_pr(151, legs_spec="windows-py3.12", merge=True)
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
    assert premerge.Premerge(tmp_path / "repo", github=FakeGitHub(),
                             probe=FakeProbe()).work_root == (
        tmp_path / "repo" / ".worktrees" / "premerge")


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
