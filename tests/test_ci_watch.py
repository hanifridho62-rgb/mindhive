# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.watch` -- one pass over the open PRs, and the loop around it.

`Premerge` and gh are fakes; the sidecar is the real one in the test's own
store, because what `watch` decides is a function of what `premerge` last
remembered."""
import pytest

from mindhive import events
from mindhive.ci import github as gh_mod
from mindhive.ci import premerge, watch

BASE, HEAD, NEWBASE, NEWERBASE = "1" * 40, "2" * 40, "3" * 40, "6" * 40
TREE, TREE2 = "a" * 40, "b" * 40


def pr(number=151, head=HEAD, draft=False, base="master", fork=False):
    return {"number": number, "state": "OPEN", "isDraft": draft,
            "isCrossRepository": fork,
            "headRefName": f"claude/f{number}", "headRefOid": head, "baseRefName": base}


class FakeGitHub:
    def __init__(self, prs=None, fail=None):
        self.prs = list(prs if prs is not None else [pr()])
        self.fail = list(fail or [])
        self.statuses = []

    def open_prs(self):
        if self.fail:
            raise self.fail.pop(0)
        return [dict(p) for p in self.prs]

    def post_status(self, sha, state, context="mindhive-ci", description=""):
        self.statuses.append((sha, state, context, description))
        return ""


class FakePremerge:
    """`run_pr` and `fetch_base` are fakes; `record_conflict` is the real one,
    so what a conflict records and posts is what premerge would."""

    def __init__(self, gh, base=BASE, fetch_error="", verdict="green", status_errors=()):
        self.github, self.project, self.post_statuses = gh, "test", True
        self.base, self.fetch_error, self.verdict = base, fetch_error, verdict
        self.status_errors = list(status_errors)
        self.runs, self.fetches = [], []

    def run_pr(self, number, *, legs_spec=None, merge=False, allow_base_drift=False):
        self.runs.append((number, legs_spec, merge))
        return premerge.PremergeResult(pr_id=number, verdict=self.verdict,
                                       exit_code={"green": 0, "red": 1}.get(self.verdict, 2),
                                       reason=f"{self.verdict}: 9 leg(s)",
                                       status_errors=list(self.status_errors))

    def record_conflict(self, number, **kw):
        real = premerge.Premerge("repo", project=self.project, github=self.github,
                                 probe=object(), post_statuses=self.post_statuses)
        return real.record_conflict(number, **kw)

    def fetch_base(self, base_ref="master"):
        self.fetches.append(base_ref)
        return ("", self.fetch_error) if self.fetch_error else (self.base, "")


class MergeTree:
    def __init__(self, answer=(TREE, (), "")):
        self.answer, self.asked = answer, []

    def __call__(self, a, b):
        self.asked.append((a, b))
        return self.answer


def remembered(verdict="green", conflicts=(), **extra):
    entry = {"head_sha": HEAD, "base_ref": "master", "base_sha": BASE, "tree": TREE,
             "tested_sha": "c" * 40, "verdict": verdict, "exit_code": 0,
             "conflicts": list(conflicts), "legs": {"windows-py3.12": "success"}}
    entry.update(extra)
    premerge.remember("test", 151, entry)
    return entry


def watcher(pm, merge_tree=None, **kw):
    said = []
    w = watch.Watcher(pm, say=said.append, merge_tree=merge_tree or MergeTree(), **kw)
    return w, said


def test_a_head_it_never_tested_gets_a_full_run_and_never_a_merge():
    pm = FakePremerge(FakeGitHub())
    w, said = watcher(pm, legs_spec="windows-*")
    assert w.once() == [{"pr": 151, "action": "tested", "verdict": "green",
                         "exit_code": 0, "detail": "green: 9 leg(s)"}]
    assert pm.runs == [(151, "windows-*", False)]
    assert said == ["PR #151: tested -- GREEN: green: 9 leg(s)"]


def test_a_new_head_on_a_known_pr_is_tested_again():
    remembered()
    pm = FakePremerge(FakeGitHub([pr(head="7" * 40)]))
    w, _ = watcher(pm)
    assert w.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]


def test_the_same_head_on_the_same_base_is_left_alone():
    remembered()
    pm = FakePremerge(FakeGitHub())
    tree = MergeTree()
    w, said = watcher(pm, merge_tree=tree)
    assert w.once()[0]["action"] == "unchanged"
    assert pm.runs == [] and tree.asked == []
    assert said == ["PR #151: unchanged since its last test (green)"]


def test_a_base_that_moved_but_still_merges_keeps_its_verdict():
    remembered()
    pm = FakePremerge(FakeGitHub(), base=NEWBASE)
    tree = MergeTree((TREE2, (), ""))
    w, _ = watcher(pm, merge_tree=tree)
    action = w.once()[0]
    assert (action["action"], action["verdict"]) == ("base-moved", "green")
    assert tree.asked == [(NEWBASE, HEAD)] and pm.runs == []
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["checked_base_sha"], kept["checked_tree"]) == (NEWBASE, TREE2)
    assert (kept["base_sha"], kept["tree"]) == (BASE, TREE)        # what was TESTED
    assert w.once()[0]["action"] == "unchanged" and len(tree.asked) == 1


def test_a_base_that_moved_into_a_conflict_turns_the_pr_red_at_once():
    remembered()
    gh = FakeGitHub()
    pm = FakePremerge(gh, base=NEWBASE)
    w, said = watcher(pm, merge_tree=MergeTree((TREE2, ("src/a.py",), "")))
    action = w.once()[0]
    assert (action["action"], action["verdict"]) == ("conflict", "red")
    assert gh.statuses == [(HEAD, "failure", "mindhive-ci",
                            "conflicts with master at 333333333333: src/a.py")]
    kept = premerge.load_sidecar("test")["prs"]["151"]
    assert (kept["verdict"], kept["conflicts"], kept["checked_base_sha"]) == (
        "red", ["src/a.py"], NEWBASE)
    assert pm.runs == []
    assert said == ["PR #151: RED -- conflicts with master at 333333333333: src/a.py"]


def test_no_status_is_posted_for_a_conflict_when_statuses_are_off():
    remembered()
    gh = FakeGitHub()
    pm = FakePremerge(gh, base=NEWBASE)
    pm.post_statuses = False
    w, _ = watcher(pm, merge_tree=MergeTree((TREE2, ("src/a.py",), "")))
    assert w.once()[0]["action"] == "conflict" and gh.statuses == []


def test_a_conflict_that_clears_gets_the_full_run_it_never_had():
    remembered(verdict="red", conflicts=["src/a.py"], checked_base_sha=NEWBASE)
    pm = FakePremerge(FakeGitHub(), base=NEWERBASE)
    w, _ = watcher(pm, merge_tree=MergeTree((TREE2, (), "")))
    assert w.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]


def test_an_error_verdict_is_tried_again_when_the_base_moves_and_only_then():
    remembered(verdict="error")
    pm = FakePremerge(FakeGitHub())
    w, _ = watcher(pm)
    assert w.once()[0]["action"] == "unchanged" and pm.runs == []
    pm.base = NEWBASE
    assert w.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]


def test_a_merge_tree_that_cannot_answer_gets_a_full_run():
    remembered()
    pm = FakePremerge(FakeGitHub(), base=NEWBASE)
    w, _ = watcher(pm, merge_tree=MergeTree(("", (), "fatal: bad object")))
    assert w.once()[0]["action"] == "tested"


def test_a_base_that_cannot_be_fetched_is_an_error_not_a_verdict():
    remembered()
    pm = FakePremerge(FakeGitHub(), fetch_error="git fetch origin: Could not resolve host")
    w, said = watcher(pm)
    assert w.once() == [{"pr": 151, "action": "error",
                         "detail": "git fetch origin: Could not resolve host"}]
    assert said == ["PR #151: could not check -- git fetch origin: Could not resolve host"]


@pytest.mark.parametrize("skip,expected", [(True, ["skipped", "tested"]),
                                           (False, ["tested", "tested"])])
def test_drafts_are_skipped_only_when_asked(skip, expected):
    pm = FakePremerge(FakeGitHub([pr(149, head="8" * 40, draft=True), pr(151)]))
    w, _ = watcher(pm, skip_drafts=skip)
    assert [a["action"] for a in w.once()] == expected


def test_gh_that_cannot_list_the_prs_is_one_error_action():
    pm = FakePremerge(FakeGitHub(fail=[gh_mod.GitHubError("gh pr list: not logged in")]))
    w, _ = watcher(pm)
    assert w.once() == [{"pr": 0, "action": "error", "detail": "gh pr list: not logged in"}]


def test_the_loop_survives_a_failing_pass_and_sleeps_between_passes():
    pm = FakePremerge(FakeGitHub(prs=[], fail=[RuntimeError("the network went away")]))
    slept = []
    w, said = watcher(pm, sleep=slept.append, interval_s=42)
    assert w.loop(passes=2) == 2
    assert said == ["watch: a pass failed: RuntimeError: the network went away"]
    assert slept == [42.0]


def test_a_pr_from_a_fork_is_never_run_by_the_watch():
    pm = FakePremerge(FakeGitHub([pr(149, head="8" * 40, fork=True), pr(151)]))
    w, said = watcher(pm)
    actions = w.once()
    assert [a["action"] for a in actions] == ["skipped", "tested"]   # canary: 151 runs
    assert pm.runs == [(151, None, False)]
    assert "`hive ci premerge 149 --trust`" in actions[0]["detail"]
    assert said[0].startswith("PR #149: skipped (from a fork")


def test_a_run_of_fewer_legs_than_the_watch_asks_for_is_tested_again():
    remembered(selected=["windows-py3.12"])
    pm = FakePremerge(FakeGitHub())
    narrow, _ = watcher(pm, legs_spec="windows-py3.12")
    assert narrow.once()[0]["action"] == "unchanged" and pm.runs == []
    wide, _ = watcher(pm)
    assert wide.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]


def test_a_red_pr_whose_base_moves_but_still_merges_stays_red_without_a_rerun():
    remembered(verdict="red")
    pm = FakePremerge(FakeGitHub(), base=NEWBASE)
    w, _ = watcher(pm, merge_tree=MergeTree((TREE2, (), "")))
    action = w.once()[0]
    assert (action["action"], action["verdict"]) == ("base-moved", "red") and pm.runs == []

def test_a_pr_retargeted_to_another_base_is_tested_again():
    remembered()
    pm = FakePremerge(FakeGitHub([pr(base="release")]))
    w, _ = watcher(pm)
    assert w.once()[0]["action"] == "tested" and pm.runs == [(151, None, False)]

def test_a_conflict_over_many_files_names_five_and_counts_the_rest():
    remembered()
    gh = FakeGitHub()
    pm = FakePremerge(gh, base=NEWBASE)
    files = tuple(f"src/f{i}.py" for i in range(7))
    w, _ = watcher(pm, merge_tree=MergeTree((TREE2, files, "")))
    detail = w.once()[0]["detail"]
    assert detail.endswith("src/f0.py, src/f1.py, src/f2.py, src/f3.py, src/f4.py and 2 more")
    assert gh.statuses[0][3] == detail


class _RefusingGitHub(FakeGitHub):
    def post_status(self, sha, state, context="mindhive-ci", description=""):
        super().post_status(sha, state, context, description)
        return "status mindhive-ci=failure on 222222222222: HTTP 403: Resource not accessible"


def test_a_conflict_whose_status_could_not_be_posted_says_so():
    remembered()
    pm = FakePremerge(_RefusingGitHub(), base=NEWBASE)
    w, said = watcher(pm, merge_tree=MergeTree((TREE2, ("src/a.py",), "")))
    action = w.once()[0]
    assert action["action"] == "conflict"
    assert "HTTP 403" in action["detail"] and "HTTP 403" in said[0]
    assert premerge.load_sidecar("test")["prs"]["151"]["published"] is False


def test_a_conflict_the_watch_finds_reaches_the_ledger():
    remembered()
    pm = FakePremerge(FakeGitHub(), base=NEWBASE)
    w, _ = watcher(pm, merge_tree=MergeTree((TREE2, ("src/a.py",), "")))
    w.once()
    rows = [e.payload for e in events.read_all("test") if e.type == "verification"]
    assert [(r["gate"], r["verdict"], r["conflicts"]) for r in rows] == [
        ("premerge", "red", ["src/a.py"])]


def test_a_red_that_errors_alone_made_is_tried_again_once():
    remembered(verdict="red", legs={"windows-py3.12": "error", "node": "success"})
    pm = FakePremerge(FakeGitHub(), verdict="red")
    w, said = watcher(pm)
    action = w.once()[0]
    assert action["action"] == "tested" and pm.runs == [(151, None, False)]
    assert said[0].startswith("PR #151: tested -- RED: tried again once")
    assert premerge.load_sidecar("test")["prs"]["151"]["retried"] is True
    assert w.once()[0]["action"] == "unchanged" and len(pm.runs) == 1


@pytest.mark.parametrize("legs_", [{"windows-py3.12": "failure", "node": "error"},
                                   {"windows-py3.12": "failure"}])
def test_a_red_a_failing_test_made_is_not_tried_again(legs_):
    remembered(verdict="red", legs=legs_)
    pm = FakePremerge(FakeGitHub())
    w, _ = watcher(pm)
    assert w.once()[0]["action"] == "unchanged" and pm.runs == []


def test_a_verdict_github_refused_is_posted_again_on_the_next_pass():
    remembered(published=False, reason="green: 8 leg(s) passed")
    gh = FakeGitHub()
    pm = FakePremerge(gh)
    w, said = watcher(pm)
    action = w.once()[0]
    assert (action["action"], action["verdict"]) == ("republished", "green")
    assert gh.statuses == [(HEAD, "success", "mindhive-ci", "green: 8 leg(s) passed")]
    assert said == ["PR #151: its green verdict is posted again -- green: 8 leg(s) passed"]
    assert premerge.load_sidecar("test")["prs"]["151"]["published"] is True
    assert w.once()[0]["action"] == "unchanged" and len(gh.statuses) == 1
    assert pm.runs == []


def test_a_watch_with_statuses_off_posts_no_verdict_again():
    remembered(published=False, reason="green: 8 leg(s) passed")
    gh = FakeGitHub()
    pm = FakePremerge(gh)
    pm.post_statuses = False
    w, _ = watcher(pm)
    assert w.once()[0]["action"] == "unchanged" and gh.statuses == []


def test_a_tested_pr_whose_statuses_were_refused_says_so():
    pm = FakePremerge(FakeGitHub(), status_errors=["status mindhive-ci on 2222: HTTP 403"])
    w, said = watcher(pm)
    action = w.once()[0]
    assert action["status_errors"] == ["status mindhive-ci on 2222: HTTP 403"]
    assert said == ["PR #151: tested -- GREEN: green: 9 leg(s); "
                    "1 status(es) could not be posted"]
