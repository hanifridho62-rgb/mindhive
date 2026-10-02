# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.watch` -- one pass over the open PRs, and the loop around it.

`Premerge` and gh are fakes; the sidecar is the real one in the test's own
store, because what `watch` decides is a function of what `premerge` last
remembered."""
import errno
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mindhive import events, paths
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
        self.listed = 0

    def open_prs(self):
        self.listed += 1
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
    assert w.once() == [{"pr": 151, "action": "error", "verdict": "error", "exit_code": 2,
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
    assert w.once() == [{"pr": 0, "action": "error", "verdict": "error", "exit_code": 2,
                         "detail": "gh pr list: not logged in"}]


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


# --- what a pass reports, in one shape ---------------------------------------------------


def test_every_action_carries_the_same_five_keys():
    remembered()
    prs = [pr(149, head="8" * 40, draft=True), pr(150, head="9" * 40, fork=True), pr(151),
           pr(152, head="5" * 40)]
    w, _ = watcher(FakePremerge(FakeGitHub(prs)), skip_drafts=True)
    actions = w.once()
    assert [a["action"] for a in actions] == ["skipped", "skipped", "unchanged", "tested"]
    for action in actions:
        assert set(action) >= {"pr", "action", "verdict", "exit_code", "detail"}, action
    assert [(a["verdict"], a["exit_code"]) for a in actions] == [
        (None, None), (None, None), ("green", 0), ("green", 0)]


def test_the_loop_reports_each_pass_and_a_failed_pass_as_one_error_action():
    pm = FakePremerge(FakeGitHub(prs=[], fail=[RuntimeError("the network went away")]))
    reported = []
    w, _ = watcher(pm, sleep=lambda seconds: None)
    assert w.loop(passes=2, report=reported.append) == 2
    assert reported == [
        [{"pr": 0, "action": "error", "verdict": "error", "exit_code": 2,
          "detail": "a pass failed: RuntimeError: the network went away"}],
        []]


def test_each_pass_asks_this_machine_again_what_it_can_run():
    forgot = []

    class Probe:
        def forget(self):
            forgot.append("forgot")
    pm = FakePremerge(FakeGitHub(prs=[]))
    pm.probe = Probe()
    w, _ = watcher(pm)
    w.once()
    w.once()
    assert forgot == ["forgot", "forgot"]


# --- one pass at a time ----------------------------------------------------------------


# Another process, holding the watch lock as a pass would until its stdin closes,
# then ending as a crash ends one: no release, no cleanup. Told `idle`, it holds
# nothing and only lives.
_HOLDER = ("import os, sys\n"
           "from mindhive.ci import watch\n"
           "lock = watch.PassLock(sys.argv[1])\n"
           "print(lock.take() if sys.argv[2] == 'hold' else 'idle', os.getpid(), flush=True)\n"
           "sys.stdin.read()\n"
           "os._exit(0)\n")


class OtherPass:
    """A pass in another process, mid-run -- or, with *hold* False, a live
    process that holds nothing. Its pid is the one it reports: on Windows a
    venv's python.exe is a launcher, whose pid is not the interpreter's."""

    def __init__(self, path, hold=True):
        src = str(Path(watch.__file__).resolve().parents[2])
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (src, os.environ.get("PYTHONPATH")) if p)}
        self.proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path),
                                      "hold" if hold else "idle"], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True)
        said = self.proc.stdout.readline().split()
        if said[:1] != (["True"] if hold else ["idle"]):
            self.end()
            raise AssertionError(f"the other process did not start as asked: {said!r}")
        self.pid = int(said[1])

    def end(self):
        """End it as a crash would: it lets go of nothing itself."""
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=60)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdout.close()


def eventually(check, within_s=5.0):
    """Whether *check* comes true inside *within_s* seconds. The OS lets go of
    a dead process's lock on its own schedule, a millisecond or two here; five
    seconds leaves room for a loaded machine and none for a stale age like the
    30 s and 60 s that file locks keep."""
    deadline = time.monotonic() + within_s
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def write_record(path, pid, age_s):
    """Write a holder's record into *path*, *age_s* seconds old, through a
    descriptor of the test's own: the record's bytes stay writable while the
    byte after them is locked."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        os.write(fd, f"{pid} {int(time.time()) - age_s}".encode("ascii").ljust(64))
    finally:
        os.close(fd)


def identity(path):
    """Which file *path* is: a lock file removed and made again is another."""
    st = os.stat(path)
    return st.st_dev, st.st_ino


def a_dead_pid():
    """The pid of a process that has ended, as it reported it."""
    proc = subprocess.Popen([sys.executable, "-c", "import os; print(os.getpid())"],
                            stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=60)
    return int(out)


def test_a_pass_that_finds_another_still_running_does_nothing_and_names_it():
    gh = FakeGitHub()
    pm = FakePremerge(gh)
    w, said = watcher(pm)
    path = watch.lock_path("test")
    other = OtherPass(path)
    try:
        [action] = w.once()
    finally:
        other.end()
    held_s = action.get("held_s")
    assert other.pid != os.getpid() and isinstance(held_s, int) and 0 <= held_s < 600
    detail = f"another watch pass holds {path}, pid {other.pid}, for {held_s}s"
    assert action == {"pr": 0, "action": "skipped", "verdict": None, "exit_code": None,
                      "detail": detail, "lock": str(path), "holder_pid": other.pid,
                      "held_s": held_s}
    assert (gh.listed, pm.runs, pm.fetches) == (0, [], [])
    assert said == [f"watch: skipped ({detail})"]


def test_a_live_pass_keeps_the_lock_however_long_it_has_held_it():
    # No age frees it: a pass two hours in is a pass that is running.
    path = watch.lock_path("test")
    other = OtherPass(path)
    try:
        write_record(path, other.pid, 7200)
        pm = FakePremerge(FakeGitHub())
        [action] = watcher(pm)[0].once()
    finally:
        other.end()
    assert (action["action"], action["holder_pid"], pm.runs) == ("skipped", other.pid, [])
    assert 7200 <= action["held_s"] < watch.STUCK_S


def test_a_pass_held_past_any_honest_run_is_an_error_and_still_no_pass():
    # A pass wedged on a child that never returns holds the lock for ever; a
    # skip would keep every scheduled --once exiting 0 over a watch that stopped.
    path = watch.lock_path("test")
    other = OtherPass(path)
    try:
        write_record(path, other.pid, watch.STUCK_S + 60)
        gh = FakeGitHub()
        pm = FakePremerge(gh)
        w, said = watcher(pm)
        [action] = w.once()
    finally:
        other.end()
    held_s = action["held_s"]
    assert watch.STUCK_S + 60 <= held_s < watch.STUCK_S + 660
    detail = (f"another watch pass holds {path}, pid {other.pid}, for {held_s}s: "
              f"past {watch.STUCK_S}s, it looks stuck")
    assert action == {"pr": 0, "action": "error", "verdict": "error", "exit_code": 2,
                      "detail": detail, "lock": str(path), "holder_pid": other.pid,
                      "held_s": held_s}
    assert (gh.listed, pm.runs) == (0, [])
    assert said == [f"watch: could not check -- {detail}"]


def test_a_record_a_dead_pass_left_names_no_holder():
    # The next holder has not written its own yet: the skip must not send anyone
    # after a pid that is gone, or that the OS has since handed to something else.
    path = watch.lock_path("test")
    other = OtherPass(path)
    try:
        write_record(path, a_dead_pid(), watch.STUCK_S + 60)
        [action] = watcher(FakePremerge(FakeGitHub()))[0].once()
    finally:
        other.end()
    assert action == {"pr": 0, "action": "skipped", "verdict": None, "exit_code": None,
                      "detail": f"another watch pass holds {path}", "lock": str(path),
                      "holder_pid": None, "held_s": None}


def test_a_pass_that_died_holding_the_lock_leaves_none_behind_however_fresh():
    path = watch.lock_path("test")
    OtherPass(path).end()                   # died mid-pass, a moment ago
    before = identity(path)                 # the file stays, by design; the lock does not
    pm = FakePremerge(FakeGitHub())
    w, _ = watcher(pm)
    assert eventually(lambda: w.once()[0]["action"] != "skipped")
    assert pm.runs == [(151, None, False)] and identity(path) == before


def test_a_lock_file_naming_a_live_process_that_holds_nothing_stops_nothing():
    # A reboot mid-pass leaves the file naming a pid the OS may since have given
    # to an unrelated live process. The record names a holder; only the lock decides.
    path = watch.lock_path("test")
    live = OtherPass(path, hold=False)
    try:
        write_record(path, live.pid, 7200)
        pm = FakePremerge(FakeGitHub())
        actions = watcher(pm)[0].once()
    finally:
        live.end()
    assert [a["action"] for a in actions] == ["tested"] and pm.runs == [(151, None, False)]


def test_two_passes_never_overlap():
    started, finish = threading.Event(), threading.Event()
    first_pm = FakePremerge(FakeGitHub())
    run_pr = first_pm.run_pr

    def parked(number, **kw):                   # the first pass, mid-run
        started.set()
        finish.wait(60)
        return run_pr(number, **kw)
    first_pm.run_pr = parked
    first, _ = watcher(first_pm)
    done = []
    thread = threading.Thread(target=lambda: done.append(first.once()))
    thread.start()
    try:
        assert started.wait(60)
        second_pm = FakePremerge(FakeGitHub())
        [action] = watcher(second_pm)[0].once()
    finally:
        finish.set()
        thread.join(60)
    assert (action["action"], action["holder_pid"]) == ("skipped", os.getpid())
    assert second_pm.runs == [] and [a["action"] for a in done[0]] == ["tested"]
    assert watch.holder(watch.lock_path("test")) == (None, None)    # let go, record blanked


def test_a_loop_lets_go_of_the_lock_while_it_sleeps():
    path = watch.lock_path("test")
    free = []

    def sleeping(seconds):                      # a scheduled --once, between two passes
        lock = watch.PassLock(path)
        free.append(lock.take())
        lock.release()
    w, _ = watcher(FakePremerge(FakeGitHub()), sleep=sleeping)
    assert w.loop(passes=2) == 2
    assert free == [True]


def test_a_loop_that_finds_the_lock_held_skips_each_such_pass_and_goes_on():
    other = OtherPass(watch.lock_path("test"))
    reports = []
    try:
        pm = FakePremerge(FakeGitHub())
        w, _ = watcher(pm, sleep=lambda seconds: None)
        assert w.loop(passes=2, report=reports.append) == 2
    finally:
        other.end()
    assert [[a["action"] for a in r] for r in reports] == [["skipped"], ["skipped"]]
    assert pm.runs == []


def test_a_pass_lets_go_of_the_lock_however_it_ends():
    pm = FakePremerge(FakeGitHub(fail=[RuntimeError("gh exploded")]))
    w, _ = watcher(pm)
    path = watch.lock_path("test")
    with pytest.raises(RuntimeError):
        w.once()
    before = identity(path)
    assert watch.holder(path) == (None, None)
    assert w.once()[0]["action"] == "tested"        # the next pass is not shut out
    assert identity(path) == before                 # let go of, never removed


def test_a_holder_that_has_not_written_its_record_yet_still_holds():
    path = watch.lock_path("test")
    lock = watch.PassLock(path)
    assert lock.take()
    try:
        fd = os.open(str(path), os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.write(fd, b"?" * 32)             # over a record no holder has finished
        finally:
            os.close(fd)
        [action] = watcher(FakePremerge(FakeGitHub()))[0].once()
    finally:
        lock.release()
    assert action == {"pr": 0, "action": "skipped", "verdict": None, "exit_code": None,
                      "detail": f"another watch pass holds {path}", "lock": str(path),
                      "holder_pid": None, "held_s": None}


def test_the_lock_is_the_namespaces_own():
    assert watch.lock_path("test") == paths.state_dir("test") / watch.LOCK
    elsewhere = watch.PassLock(watch.lock_path("other"))
    assert elsewhere.take()
    try:
        here, there = FakePremerge(FakeGitHub()), FakePremerge(FakeGitHub())
        there.project = "other"
        assert [a["action"] for a in watcher(here)[0].once()] == ["tested"]
        assert [a["action"] for a in watcher(there)[0].once()] == ["skipped"]
    finally:
        elsewhere.release()


def test_a_lock_that_cannot_be_taken_is_an_error_and_no_pass():
    namespace = paths.project_dir("test")
    namespace.mkdir(parents=True, exist_ok=True)
    (namespace / "state").write_text("a file where the state directory belongs",
                                     encoding="utf-8")
    gh = FakeGitHub()
    pm = FakePremerge(gh)
    w, said = watcher(pm)
    [action] = w.once()
    assert (action["pr"], action["action"], action["verdict"], action["exit_code"],
            action["lock"]) == (0, "error", "error", 2, None)
    assert action["detail"].startswith("the watch lock ci_watch.lock could not be taken: ")
    assert (gh.listed, pm.runs) == (0, [])
    assert said == [f"watch: could not check -- {action['detail']}"]


def test_a_lock_the_os_refuses_for_another_reason_is_an_error_not_a_skip(monkeypatch):
    def refused(fd):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(watch, "_lock_byte", refused)
    pm = FakePremerge(FakeGitHub())
    w, _ = watcher(pm)
    [action] = w.once()
    assert (action["action"], action["verdict"], action["exit_code"], action["lock"]) == (
        "error", "error", 2, str(watch.lock_path("test")))
    assert "No locks available" in action["detail"] and pm.runs == []
