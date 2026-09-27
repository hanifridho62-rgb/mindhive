# tests/test_orchestrator_integrate.py
"""The lane merge-back -- validated work reaches a branch a human can pick up.

Before `orchestrator/integrate.py`, the phase the board called "Merging
Worktrees" only tore the lanes down: children are forbidden to commit
(`mindnet/agent.py`), the orchestrator committed nothing on their behalf, and a
mission's validated work stayed in `.worktrees/lane-N` until the teardown kept
it as "uncommitted changes". The first test below is that scenario and it is
RED without the change.

## Real git, real branches

Every mission here runs on a real repository in `tmp_path`; the lane commits,
the integration branch and the merges are real, and the assertions read them
back with `git`. Only `mn_wt.commit_all` (the commit-failure cases) and the
suite runner (the wiring cases) are ever faked, and those tests say so.

## Three facts the merge-back must keep straight

* The orchestrator commits on the child's behalf ONLY past a green gate, and a
  commit failure never fails the unit: the gate proved the work, and the
  repair loop's rollback would discard it.
* Every lane is PROBED (`git merge-tree`) before it is merged. A conflicting
  lane is reported and kept, never half-merged, and the integration tree is
  never left mid-merge.
* The integration branch is made from the lanes' base. The main checkout's
  HEAD never moves and its index is never touched.
* Only what the gate validated is delivered. `commit_lane` stamps what it
  commits with the `Lintester-Task` trailer; a lane holding any commit past
  the base without it -- a child that committed anyway, whatever its gate
  said -- is held whole, named, and kept for a person. Nothing is deleted.
"""
import inspect
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from mindhive import events as ev_mod
from mindhive.mindnet import worktree as mn_wt
from mindhive.orchestrator import gate as gate_mod
from mindhive.orchestrator import integrate as integrate_mod
from mindhive.orchestrator import lintester as lint


# --- fixture builders ---------------------------------------------------------

def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                       capture_output=True, text=True, shell=False)
    assert r.returncode == 0, f"git {' '.join(map(str, args))}: {r.stderr}"
    return r


def _write(root, rel, text):
    target = Path(root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return target


GREEN_WRITER = """
    def write(x):
        return 1
"""

BROKEN_WRITER = """
    def write(x):
        return 0
"""

# Still green under `tests/test_writer.py`, and carrying a marker the
# assertions can look for in a branch's tree.
CHANGED_WRITER = GREEN_WRITER + """
    CHANGED = True
"""

OTHER_WRITER = GREEN_WRITER + """
    OTHER_LANE = True
"""

CHANGED_OTHER = """
    def other():
        return 2


    OTHER = True
"""

INTENT = "make write return 1"


@pytest.fixture
def repo(tmp_path):
    """A real repository holding a package and a suite, so a lane has tests to
    run -- the same shape as `test_orchestrator_gate.py`'s."""
    root = tmp_path / "repo"
    _write(root, "src/pkg/__init__.py", "")
    _write(root, "src/pkg/writer.py", GREEN_WRITER)
    _write(root, "src/pkg/other.py", """
        def other():
            return 2
    """)
    _write(root, "tests/test_writer.py", """
        from pkg import writer


        def test_write():
            assert writer.write(1) == 1
    """)
    _write(root, "tests/test_other.py", """
        from pkg import other


        def test_other():
            assert other.other() == 2
    """)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture(autouse=True)
def session(monkeypatch):
    monkeypatch.setenv("MINDHIVE_SESSION", "s1")


def _one_unit(_root_id, _title, _intent):
    return [("t-writer", "Make write return 1", "fix src/pkg/writer.py")]


def _two_units(_root_id, _title, _intent):
    return [("t-writer", "Make write return 1", "fix src/pkg/writer.py"),
            ("t-other", "Keep other at 2", "fix src/pkg/other.py")]


def _writes(files, ok=True):
    """A runner standing in for a child: it writes *files* into ITS LANE and
    reports the exit its child would have."""
    def runner(entry, lane):
        for rel, text in files.items():
            _write(lane.worktree_path, rel, text)
        return ok
    return runner


def _per_task(files_by_task):
    """A runner whose child writes a different file set per unit."""
    def runner(entry, lane):
        for rel, text in files_by_task.get(entry.task_id, {}).items():
            _write(lane.worktree_path, rel, text)
        return True
    return runner


TWO_FILES = {"t-writer": {"src/pkg/writer.py": CHANGED_WRITER},
             "t-other": {"src/pkg/other.py": CHANGED_OTHER}}

# Two children that both rewrite `writer.py`, differently -- the second one
# outside the scope it was given, which is exactly what a real child can do.
# Whichever lane merges second conflicts with the integration tree.
SAME_FILE = {"t-writer": {"src/pkg/writer.py": CHANGED_WRITER},
             "t-other": {"src/pkg/writer.py": OTHER_WRITER}}


def _fake_record(exit_code=0, counts=None, tail="1 passed in 0.10s"):
    """The dict `verify.run_and_record` answers, for the wiring tests."""
    return {"exit_code": int(exit_code), "counts": dict(counts or {}),
            "error": "", "artifact": None, "excerpt": tail, "tail": tail}


def _mission(repo, runner, decomposer=_one_unit, lanes=1, **kw):
    kw.setdefault("max_attempts", 1)
    kw.setdefault("merge_back", True)
    return lint.LintesterOrchestrator(
        root=repo, project="test", lanes=lanes, decomposer=decomposer,
        runner=runner, **kw,
    ).execute_mission(INTENT, dry_run=False)


def _mission_id():
    return lint.LintesterOrchestrator()._mission_id(INTENT)


def _sha(repo, ref):
    return _git(repo, "rev-parse", "--verify", ref).stdout.strip()


def _ahead(repo, branch):
    return int(_git(repo, "rev-list", "--count", f"main..{branch}").stdout)


def _branches(repo, pattern):
    out = _git(repo, "branch", "--list", "--format=%(refname:short)", pattern)
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _integration(mission):
    return f"lint/{mission.mission_id}/integration"


# --- the change ----------------------------------------------------------------

def test_a_validated_unit_is_committed_on_its_lane_branch_with_the_trailer(repo):
    """THE regression. A green child's work used to stay uncommitted in its
    lane, where the teardown could only keep it as "uncommitted changes"."""
    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}))

    assert got.outcome == "ok", got.reason
    lane = f"lint/{got.mission_id}/lane-0"
    assert _ahead(repo, lane) == 1, "the lane branch holds no commit"
    body = _git(repo, "log", "-1", "--format=%B", lane).stdout
    assert body.startswith(f"lint({got.mission_id})"), body
    assert f"{integrate_mod.COMMIT_TRAILER}: t-writer" in body, body
    assert "Co-Authored-By: Claude" in body, body
    shown = _git(repo, "show", f"{lane}:src/pkg/writer.py").stdout
    assert "CHANGED = True" in shown
    assert len(got.commits) == 1
    row = got.commits[0]
    assert row["task_id"] == "t-writer" and row["ok"] and row["changed"]
    assert row["sha"] == _sha(repo, lane)


def test_a_gate_failed_unit_is_not_committed(repo):
    """The committer hangs on `gated`'s `after`, which fires only past a green
    gate. A real gate: a real red pytest run in the lane."""
    got = _mission(repo, _writes({"src/pkg/writer.py": BROKEN_WRITER}),
                   validator=gate_mod.SuiteGate(root=repo, project="test"))

    assert got.outcome == "partial", got.reason
    assert got.completed == ()
    assert got.commits == ()
    assert _ahead(repo, f"lint/{got.mission_id}/lane-0") == 0
    assert _branches(repo, "lint/*/integration") == []


def test_a_commit_failure_keeps_the_work_and_does_not_fail_the_unit(
        repo, monkeypatch):
    """The gate proved the work; a failed `git commit` is an integration fact.
    Routing it through the repair loop would roll the lane back and DISCARD
    the validated work -- so the unit stays completed, the lane stays on disk,
    and the mission says `partial` for the honest reason."""
    monkeypatch.setattr(mn_wt, "commit_all",
                        lambda path, message: mn_wt.Committed(
                            ok=False, reason="no identity configured"))

    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}))

    assert got.completed == ("t-writer",)
    assert got.failed == ()
    assert got.outcome == "partial"
    assert "not committed" in got.reason, got.reason
    row = got.commits[0]
    assert row["ok"] is False and "no identity" in row["reason"]
    lane_dir = repo / ".worktrees" / "lane-0"
    kept_text = (lane_dir / "src" / "pkg" / "writer.py").read_text(encoding="utf-8")
    assert "CHANGED = True" in kept_text
    assert any("lane-0" in line for line in got.kept), got.kept
    assert _branches(repo, "lint/*/integration") == []


def test_two_lanes_merge_into_one_integration_branch_made_from_base(repo):
    base = _sha(repo, "main")

    got = _mission(repo, _per_task(TWO_FILES), decomposer=_two_units, lanes=2)

    assert got.outcome == "ok", got.reason
    integration = _integration(got)
    assert _sha(repo, f"{integration}~2") == base, \
        "the integration branch was not made from the lanes' base"
    # Two lane commits and two `--no-ff` merges, nothing else.
    assert _ahead(repo, integration) == 4
    assert "CHANGED = True" in _git(repo, "show",
                                    f"{integration}:src/pkg/writer.py").stdout
    assert "OTHER = True" in _git(repo, "show",
                                  f"{integration}:src/pkg/other.py").stdout
    report = got.integration
    assert report["ok"] is True
    assert report["branch"] == integration
    assert report["sha"] == _sha(repo, integration)
    assert report["base"] == base
    assert [m["lane_id"] for m in report["merged"]] == [0, 1]
    assert report["skipped"] == []
    assert report["next"] == f"git switch {integration}"
    for row in report["merged"]:
        _git(repo, "merge-base", "--is-ancestor", row["branch"], integration)
    rt = json.loads(json.dumps(got.as_dict()))
    assert rt["integration"]["branch"] == integration
    assert len(rt["commits"]) == 2


def test_a_conflicting_lane_is_probed_first_and_never_merged(repo, monkeypatch):
    """U15's discipline in code: `git merge-tree` answers "does this conflict"
    before anything is mutated. The recorder sits on the incumbent's git
    primitive, so the ORDER of the real commands is what is asserted."""
    calls = []
    real = mn_wt._git_rc

    def rec(root, *args):
        calls.append(tuple(str(a) for a in args))
        return real(root, *args)

    monkeypatch.setattr(mn_wt, "_git_rc", rec)

    got = _mission(repo, _per_task(SAME_FILE), decomposer=_two_units, lanes=2)

    assert got.outcome == "partial", got.reason
    merged, skipped = got.integration["merged"], got.integration["skipped"]
    assert len(merged) == 1 and len(skipped) == 1, got.integration
    loser = skipped[0]
    assert loser["merged"] is False
    assert loser["conflicts"] == ["src/pkg/writer.py"]
    assert f"lane {loser['lane_id']} was not merged" in got.reason, got.reason
    assert got.integration["ok"] is False

    for row in merged + skipped:
        kinds = [c[0] for c in calls if row["branch"] in c]
        assert "merge-tree" in kinds, f"lane {row['lane_id']} was never probed"
        if row["merged"]:
            assert kinds.index("merge-tree") < kinds.index("merge")
        else:
            assert "merge" not in kinds, "the conflicting lane was merged anyway"

    # The loser's work is intact on its branch and its lane is kept; the
    # winner's lane is released; the integration tree is gone, not mid-merge.
    assert _ahead(repo, loser["branch"]) == 1
    assert (repo / ".worktrees" / f"lane-{loser['lane_id']}").exists()
    assert not (repo / ".worktrees" / f"lane-{merged[0]['lane_id']}").exists()
    assert not (repo / ".worktrees" / f"integration-{got.mission_id}").exists()
    assert _ahead(repo, _integration(got)) == 2


def test_the_main_checkout_never_moves_and_stays_clean(repo):
    head = _sha(repo, "HEAD")

    got = _mission(repo, _per_task(TWO_FILES), decomposer=_two_units, lanes=2)

    assert got.outcome == "ok", got.reason
    assert _sha(repo, "HEAD") == head
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    text = (repo / "src" / "pkg" / "writer.py").read_text(encoding="utf-8")
    assert "CHANGED" not in text


def test_a_merged_lane_is_released_even_when_its_tests_left_caches_behind(repo):
    """The gate's pytest run leaves `__pycache__/` and `.pytest_cache/` in the
    lane. Before, `state` counted those as work, so every gated lane was kept
    after its work had merged -- and `add -A` would have committed bytecode."""
    got = _mission(repo, _writes({
        "src/pkg/writer.py": CHANGED_WRITER,
        "src/pkg/__pycache__/writer.cpython-312.pyc": "bytecode",
        ".pytest_cache/v/cache/nodeids": "[]",
    }))

    assert got.outcome == "ok", got.reason
    assert got.kept == (), got.kept
    assert not (repo / ".worktrees" / "lane-0").exists()
    tree = _git(repo, "ls-tree", "-r", "--name-only",
                f"lint/{got.mission_id}/lane-0").stdout
    assert "__pycache__" not in tree and ".pytest_cache" not in tree, tree
    assert "src/pkg/writer.py" in tree


def test_ignored_output_that_is_not_a_cache_still_keeps_the_lane(repo):
    """The canary for the test above: the incumbent's rule that ignored files
    count as work is untouched. Only the named cache directories are exempt."""
    _write(repo, ".gitignore", "dist/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore dist")

    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER,
                                  "dist/out.bin": "built"}))

    assert got.outcome == "ok", got.reason
    assert _ahead(repo, f"lint/{got.mission_id}/lane-0") == 1
    assert (repo / ".worktrees" / "lane-0" / "dist" / "out.bin").exists()
    assert any("lane-0" in line for line in got.kept), got.kept


def test_the_integration_branch_survives_its_worktree(repo):
    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}))

    assert got.outcome == "ok", got.reason
    assert not (repo / ".worktrees" / f"integration-{got.mission_id}").exists()
    listed = _git(repo, "worktree", "list", "--porcelain").stdout
    assert "integration-" not in listed
    assert _ahead(repo, _integration(got)) == 2
    assert got.integration["retired"] is True


def test_nothing_is_created_when_no_lane_committed_anything(repo):
    got = _mission(repo, lambda entry, lane: True)

    assert got.outcome == "ok", got.reason
    assert _branches(repo, "lint/*/integration") == []
    assert list(repo.glob(".worktrees/integration-*")) == []
    assert got.integration["ok"] is True
    assert got.integration["reason"] == "no lane committed anything"
    assert got.integration["branch"] == ""
    assert not (repo / ".worktrees" / "lane-0").exists()
    assert len(got.commits) == 1
    assert got.commits[0]["ok"] is True and got.commits[0]["changed"] is False


def test_a_leftover_integration_branch_holding_commits_is_refused_not_reset(repo):
    """`mindnet.worktree.add`'s own rule, reached through `start_point`: a
    branch holding commits is never force-moved. The lane keeps its commit and
    the mission says why nothing merged."""
    integration = f"lint/{_mission_id()}/integration"
    _git(repo, "checkout", "-q", "-b", integration)
    _write(repo, "leftover.txt", "from an earlier run\n")
    _git(repo, "add", "leftover.txt")
    _git(repo, "commit", "-qm", "leftover")
    _git(repo, "checkout", "-q", "main")
    leftover = _sha(repo, integration)

    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}))

    assert got.outcome == "partial", got.reason
    assert got.integration["ok"] is False
    assert "already holds" in got.integration["reason"], got.integration
    assert "merge-back" in got.reason, got.reason
    assert _sha(repo, integration) == leftover
    assert _ahead(repo, f"lint/{got.mission_id}/lane-0") == 1
    assert (repo / ".worktrees" / "lane-0").exists()


# --- the closing suite ----------------------------------------------------------

def test_the_full_suite_runs_once_on_the_integration_checkout(repo):
    """Wiring, with the suite runner faked: two targeted runs (one per unit,
    `record=False`) and then ONE full run, on the integration tree -- the
    once-per-lane loop the gate shipped with is replaced."""
    calls = []

    def spy(argv, **kw):
        calls.append((kw.get("record"), str(kw.get("cwd") or "")))
        return _fake_record(counts={"passed": 1})

    got = _mission(repo, _per_task(TWO_FILES), decomposer=_two_units, lanes=2,
                   validator=gate_mod.SuiteGate(root=repo, project="test",
                                                runner=spy))

    assert got.outcome == "ok", (got.reason, got.gates, got.suites)
    assert len([c for c in calls if c[0] is False]) == 2, calls
    full = [c for c in calls if c[0] is True]
    assert len(full) == 1, calls
    assert Path(full[0][1]).name == f"integration-{got.mission_id}"
    assert len(got.suites) == 1
    assert got.suites[0]["lane_id"] == integrate_mod.INTEGRATION_LANE
    assert got.suites[0]["ok"] and got.suites[0]["scope"] == gate_mod.FULL


def test_a_red_integration_suite_makes_the_mission_partial_and_keeps_the_branch(
        repo):
    def spy(argv, **kw):
        if kw.get("record"):
            return _fake_record(exit_code=1, counts={"failed": 2},
                                tail="2 failed in 0.20s")
        return _fake_record(counts={"passed": 1})

    got = _mission(repo, _per_task(TWO_FILES), decomposer=_two_units, lanes=2,
                   validator=gate_mod.SuiteGate(root=repo, project="test",
                                                runner=spy))

    assert got.outcome == "partial"
    assert "on the integration tree" in got.reason, got.reason
    assert got.suites[0]["ok"] is False
    assert _ahead(repo, _integration(got)) == 4, \
        "the branch is the deliverable, red suite or not"


def test_a_lane_whose_commit_failed_is_judged_where_it_stands(repo, monkeypatch):
    """No integration tree means the fallback: the closing suite runs in the
    lane, exactly as it did before the merge-back."""
    monkeypatch.setattr(mn_wt, "commit_all",
                        lambda path, message: mn_wt.Committed(
                            ok=False, reason="no identity configured"))
    calls = []

    def spy(argv, **kw):
        calls.append((kw.get("record"), str(kw.get("cwd") or "")))
        return _fake_record(counts={"passed": 1})

    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}),
                   validator=gate_mod.SuiteGate(root=repo, project="test",
                                                runner=spy))

    full = [c for c in calls if c[0] is True]
    assert len(full) == 1 and Path(full[0][1]).name == "lane-0", calls
    assert got.suites[0]["lane_id"] == 0


# --- the guards ---------------------------------------------------------------

def test_every_git_call_goes_through_the_incumbent():
    """`mindnet.worktree` is the only git driver. A second one drifting from
    its rules is how an agent's work gets deleted by code that looks right."""
    src = inspect.getsource(integrate_mod)
    assert "subprocess" not in src
    assert "shell=" not in src


def test_commit_lane_refuses_the_main_checkout(repo):
    _write(repo, "src/pkg/writer.py", CHANGED_WRITER)
    head = _sha(repo, "HEAD")
    task = lint.AgentTask(task_id="t-writer", title="Make write return 1",
                          lane_id=0)

    row = integrate_mod.commit_lane(repo, repo, task, "lm-x")

    assert row.ok is False
    assert "checkout" in row.reason, row.reason
    assert _sha(repo, "HEAD") == head
    assert "writer.py" in _git(repo, "status", "--porcelain").stdout


def test_an_in_scope_commit_appends_one_code_change_event(repo):
    """CLAUDE.md's mandatory change-log holds for orchestrator-made changes
    too: the files the child changed under `src/mindhive/` or `tests/` get
    one `code_change` row, under the child's own session id."""
    got = _mission(repo, _writes({"tests/test_writer.py": """
        from pkg import writer


        def test_write():
            assert writer.write(1) == 1


        def test_write_again():
            assert writer.write(2) == 1
    """}))

    assert got.outcome == "ok", got.reason
    rows = [e for e in ev_mod.read_all() if e.type == "code_change"]
    assert len(rows) == 1, rows
    assert rows[0].payload["files"] == ["tests/test_writer.py"]
    assert rows[0].payload["category"] == "update"
    assert got.mission_id in rows[0].payload["text"]
    assert "Make write return 1" in rows[0].payload["text"]
    assert rows[0].session_id == f"{lint.MISSION_PREFIX}-t-writer"
    assert got.commits[0]["logged"], got.commits[0]


def test_an_out_of_scope_commit_appends_no_code_change_event(repo):
    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}))

    assert got.outcome == "ok", got.reason
    assert [e for e in ev_mod.read_all() if e.type == "code_change"] == []


def test_the_library_default_makes_no_branch(repo):
    """The canary for every runner-fake test in the suite: `merge_back` is off
    unless asked for, and off means the old teardown, byte for byte."""
    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}),
                   merge_back=False)

    assert got.outcome == "ok", got.reason
    assert got.commits == () and got.integration == {}
    assert _ahead(repo, f"lint/{got.mission_id}/lane-0") == 0
    assert _branches(repo, "lint/*/integration") == []
    assert any("lane-0" in line for line in got.kept), \
        "uncommitted work was not kept"


# --- only what the gate validated is delivered -----------------------------------

DOCTORED_TEST = """
    from pkg import writer


    def test_write():
        assert writer.write(1) == 0
"""


def _commits_in_lane(lane, rel, text, message):
    """What a disobedient child does: writes *rel* and commits it, in its own
    lane, against the standing "do NOT commit" rule."""
    _write(lane.worktree_path, rel, text)
    _git(lane.worktree_path, "add", "-A")
    _git(lane.worktree_path, "commit", "-qm", message)
    return _sha(lane.worktree_path, "HEAD")


def _lane_branch(got, lane_id=0):
    return f"lint/{got.mission_id}/lane-{lane_id}"


def test_a_lane_holding_a_commit_the_orchestrator_did_not_make_is_held_not_merged(
        repo):
    """THE hole. Children are told not to commit, nothing stops one, and
    `rollback` keeps commits on purpose -- so `integrate_lanes` merged the
    lane's TIP, and whatever a child had committed rode in under the next
    unit's green gate. A lane is merged only when every commit past the base
    is one `commit_lane` stamped; this one is kept whole, its foreign commit
    named, and no integration branch is made of it."""
    foreign = {}

    def runner(entry, lane):
        if entry.task_id == "t-writer":
            foreign["sha"] = _commits_in_lane(lane, "src/pkg/writer.py",
                                              BROKEN_WRITER, "child: my commit")
        else:
            _write(lane.worktree_path, "src/pkg/other.py", CHANGED_OTHER)
        return True

    got = _mission(repo, runner, decomposer=_two_units, lanes=1)

    assert set(got.completed) == {"t-writer", "t-other"}
    assert got.outcome == "partial", got.reason
    assert "merge-back" in got.reason and foreign["sha"][:12] in got.reason
    assert _branches(repo, "lint/*/integration") == []
    assert list(repo.glob(".worktrees/integration-*")) == []
    report = got.integration
    assert report["ok"] is False and report["merged"] == []
    [row] = report["skipped"]
    assert row["lane_id"] == 0 and row["merged"] is False
    assert row["foreign"] == [foreign["sha"]]
    assert foreign["sha"][:12] in row["reason"]
    # Nothing was deleted: the lane, its branch, the child's commit and the
    # stamped one above it are all kept, for a person.
    assert _ahead(repo, _lane_branch(got)) == 2
    assert (repo / ".worktrees" / "lane-0").exists()
    assert any("lane-0" in line for line in got.kept), got.kept
    assert "OTHER = True" in _git(repo, "show",
                                  f"{_lane_branch(got)}:src/pkg/other.py").stdout
    rt = json.loads(json.dumps(got.as_dict()))
    assert rt["integration"]["skipped"][0]["foreign"] == [foreign["sha"]]


def test_a_refused_units_committed_edit_never_reaches_the_integration_branch(
        repo):
    """The scenario the hole was found by. Unit 1's child doctors the test its
    broken work would fail and COMMITS the doctored test; unit 2 on the same
    lane is honest. Whatever the gate makes of unit 1 -- this repository's
    gate has judged that differently as K13 landed -- the doctored test must
    not become the mission's deliverable, and must not be deleted either."""
    doctored = {}

    def runner(entry, lane):
        if entry.task_id == "t-writer":
            doctored["sha"] = _commits_in_lane(lane, "tests/test_writer.py",
                                               DOCTORED_TEST, "child: doctor")
            _write(lane.worktree_path, "src/pkg/writer.py", BROKEN_WRITER)
        else:
            _write(lane.worktree_path, "src/pkg/other.py", CHANGED_OTHER)
        return True

    got = _mission(repo, runner, decomposer=_two_units, lanes=1,
                   validator=gate_mod.SuiteGate(root=repo, project="test"))

    assert got.outcome == "partial", got.reason
    assert _branches(repo, "lint/*/integration") == []
    assert got.integration["ok"] is False and got.integration["merged"] == []
    [row] = got.integration["skipped"]
    assert row["foreign"] == [doctored["sha"]], row
    shown = _git(repo, "show", f"{_lane_branch(got)}:tests/test_writer.py").stdout
    assert "== 0" in shown, "the child's commit is kept on the lane, for a person"
    assert (repo / ".worktrees" / "lane-0").exists()


def test_work_a_child_committed_itself_is_held_even_when_the_gate_passed(repo):
    """The decision, pinned: a commit the orchestrator did not make is not
    delivered even when the unit's gate was green. From the branch the merge
    reads, a child's commit of validated work is indistinguishable from a
    refused unit's, and `commit_lane` -- which finds nothing left to commit --
    is the only thing that stamps. The work is not lost: the lane is kept
    whole and the report names the commit for a person to pick up."""
    own = {}

    def runner(entry, lane):
        own["sha"] = _commits_in_lane(lane, "src/pkg/writer.py",
                                      CHANGED_WRITER, "child: my own commit")
        return True

    got = _mission(repo, runner,
                   validator=gate_mod.SuiteGate(root=repo, project="test"))

    assert got.completed == ("t-writer",)
    [commit] = got.commits
    assert commit["ok"] is True and commit["changed"] is False
    assert _branches(repo, "lint/*/integration") == []
    assert got.integration["ok"] is False
    assert got.integration["skipped"][0]["foreign"] == [own["sha"]]
    assert got.outcome == "partial" and own["sha"][:12] in got.reason
    shown = _git(repo, "show", f"{_lane_branch(got)}:src/pkg/writer.py").stdout
    assert "CHANGED = True" in shown
    assert (repo / ".worktrees" / "lane-0").exists()


def test_two_units_on_one_lane_both_stamped_still_merge(repo):
    """The canary for the hold: the rule is "every commit past the base is
    stamped", not "the tip is". Two honest units on one lane are two stamped
    commits, and the lane merges as before."""
    got = _mission(repo, _per_task(TWO_FILES), decomposer=_two_units, lanes=1)

    assert got.outcome == "ok", got.reason
    integration = _integration(got)
    assert _ahead(repo, integration) == 3          # two lane commits, one merge
    assert got.integration["skipped"] == []
    assert [m["lane_id"] for m in got.integration["merged"]] == [0]
    shas = _git(repo, "rev-list", f"main..{_lane_branch(got)}").stdout.split()
    assert len(shas) == 2
    for sha in shas:
        body = _git(repo, "log", "-1", "--format=%B", sha).stdout
        assert f"{integrate_mod.COMMIT_TRAILER}: t-" in body, body


def test_a_lane_whose_history_git_could_not_read_is_held_not_merged(
        repo, monkeypatch):
    """Unproven is not validated: when git cannot list what is past the base,
    the lane is kept, never merged on the assumption that it is clean."""
    monkeypatch.setattr(mn_wt, "commits_since", lambda *a, **k: None)

    got = _mission(repo, _writes({"src/pkg/writer.py": CHANGED_WRITER}))

    assert got.outcome == "partial", got.reason
    assert _branches(repo, "lint/*/integration") == []
    [row] = got.integration["skipped"]
    assert row["foreign"] == [] and "could not" in row["reason"], row
    assert _ahead(repo, _lane_branch(got)) == 1
    assert (repo / ".worktrees" / "lane-0").exists()


def test_a_held_lane_does_not_hold_the_others(repo):
    """One lane's foreign commit keeps that lane out; the honest lane still
    merges, the integration branch is made, and the report carries both."""
    foreign = {}

    def runner(entry, lane):
        if entry.task_id == "t-writer":
            foreign["sha"] = _commits_in_lane(lane, "src/pkg/writer.py",
                                              CHANGED_WRITER, "child: my commit")
        else:
            _write(lane.worktree_path, "src/pkg/other.py", CHANGED_OTHER)
        return True

    got = _mission(repo, runner, decomposer=_two_units, lanes=2)

    assert got.outcome == "partial", got.reason
    integration = _integration(got)
    assert _ahead(repo, integration) == 2          # one lane commit, one merge
    assert "OTHER = True" in _git(repo, "show",
                                  f"{integration}:src/pkg/other.py").stdout
    assert "CHANGED" not in _git(repo, "show",
                                 f"{integration}:src/pkg/writer.py").stdout
    report = got.integration
    assert report["ok"] is False
    [merged] = report["merged"]
    [held] = report["skipped"]
    assert {merged["lane_id"], held["lane_id"]} == {0, 1}
    assert held["foreign"] == [foreign["sha"]]
    assert report["next"] == f"git switch {integration}"
    assert (repo / ".worktrees" / f"lane-{held['lane_id']}").exists()
    assert not (repo / ".worktrees" / f"lane-{merged['lane_id']}").exists()
