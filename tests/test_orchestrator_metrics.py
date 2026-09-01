# tests/test_orchestrator_metrics.py
"""WB2 — quantitative workload weighting.

Written against
``docs/plans/2026-09-01-001-feat-lintester-orchestrator-full-prd-plan.md`` U2.

Two assertions here carry the file, and both exist to prove this is NOT the
incumbent wearing a new name:

* `test_branching_outscores_size_at_equal_line_count` — `clusters.file_complexity`
  is `loc + 3*indent + 2*branches`, so at equal line count it cannot tell a flat
  file from a branchy one by much. An AST cyclomatic count can, and this is the
  case that separates them.
* `test_churn_sums_line_volatility_not_commit_count` — `clusters.git_churn` runs
  `git log --oneline` and counts COMMITS. This runs `--numstat` and sums lines.
  The fixture is built so the two answers differ (10 lines moved across 3
  commits), and the test asserts BOTH, so a future edit that quietly reverts to
  the commit count fails here rather than passing with a worse metric.

Real git, not a patched `subprocess` — this repo's doctrine for anything whose
behaviour under test IS git's. `git init` in `tmp_path` costs milliseconds.
"""
import subprocess

import pytest

from mindhive.orchestrator import metrics as m


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *[str(a) for a in args]],
                       capture_output=True, text=True, shell=False)
    assert r.returncode == 0, f"git {' '.join(map(str, args))}: {r.stderr}"
    return r


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    return root


# --- ComplexityVisitor --------------------------------------------------------

BRANCHY = ("def f(x):\n"
           "    if x:\n"
           "        if x > 1:\n"
           "            for i in x:\n"
           "                pass\n")
FLAT = ("def f(x):\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    return a\n")


def test_branching_outscores_size_at_equal_line_count():
    """The whole reason this module exists rather than reusing the proxy."""
    assert len(BRANCHY.splitlines()) == len(FLAT.splitlines())
    assert m.cyclomatic(BRANCHY) == 3, "two ifs and a for"
    assert m.cyclomatic(FLAT) == 0
    assert m.cyclomatic(BRANCHY) > m.cyclomatic(FLAT)


def test_a_boolop_of_four_values_is_three_decisions():
    """`a and b and c and d` short-circuits three times, not once."""
    assert m.cyclomatic("x = a and b and c and d\n") == 3


def test_a_comprehension_filter_is_its_own_decision():
    assert m.cyclomatic("y = [i for i in xs]\n") == 1
    assert m.cyclomatic("y = [i for i in xs if i]\n") == 2


def test_an_except_handler_counts():
    assert m.cyclomatic("try:\n    f()\nexcept ValueError:\n    pass\n") == 1


def test_a_file_that_does_not_parse_scores_zero_and_does_not_raise():
    assert m.cyclomatic("def (:\n") == 0
    assert m.cyclomatic(None) == 0
    assert m.cyclomatic("") == 0


def test_the_visitor_recurses_so_nesting_depth_does_not_hide_a_branch():
    deep = "def f():\n" + "".join(f"{'    ' * (i + 1)}if x:\n" for i in range(5))
    deep += "    " * 6 + "pass\n"
    assert m.cyclomatic(deep) == 5


# --- churn --------------------------------------------------------------------

def test_churn_sums_line_volatility_not_commit_count(repo):
    """Three commits move ten lines. The two metrics must disagree."""
    from mindhive import clusters as clusters_mod

    target = repo / "f.py"
    target.write_text("\n".join(f"line {i}" for i in range(5)) + "\n",
                      encoding="utf-8")
    _git(repo, "add", "f.py")
    _git(repo, "commit", "-qm", "one")            # +5

    target.write_text("\n".join(f"line {i}" for i in range(8)) + "\n",
                      encoding="utf-8")
    _git(repo, "commit", "-qam", "two")           # +3

    target.write_text("\n".join(f"line {i}" for i in range(6)) + "\n",
                      encoding="utf-8")
    _git(repo, "commit", "-qam", "three")         # -2

    assert m.churn("f.py", root=repo) == 10, "5 added, 3 added, 2 deleted"
    assert clusters_mod.git_churn("f.py", root=repo) == 3, (
        "the incumbent counts commits — if this ever equals 10 the fixture "
        "stopped discriminating and the test above proves nothing")


def test_churn_is_zero_without_git(monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    assert m.churn("f.py", root=tmp_path) == 0


def test_churn_on_an_untracked_path_is_zero_not_an_error(repo):
    assert m.churn("never-existed.py", root=repo) == 0


# --- usage --------------------------------------------------------------------

def test_usage_counts_only_the_rows_for_that_task():
    from mindhive import events as ev_mod
    from mindhive.mindnet import telemetry as tel_mod

    tel_mod.emit("history", tel_mod.STATUS_ACTIVE, task_id="t1", project="test")
    tel_mod.emit("history", tel_mod.STATUS_IDLE, task_id="t1", project="test")
    tel_mod.emit("history", tel_mod.STATUS_ACTIVE, task_id="t2", project="test")
    rows = list(ev_mod.read_types((tel_mod.EVENT_TYPE,), "test"))

    assert m.usage("t1", evs=rows) == 2
    assert m.usage("t2", evs=rows) == 1
    assert m.usage("t3", evs=rows) == 0
    assert m.usage("", evs=rows) == 0


def test_usage_survives_a_row_it_cannot_read():
    assert m.usage("t1", evs=[None, {"type": "x"}, 7]) == 0


# --- calculate_workload_weight ------------------------------------------------

def test_a_missing_path_weighs_nothing_and_says_so(tmp_path):
    got = m.calculate_workload_weight(tmp_path / "nope.py")
    assert got.total == 0
    assert "no such file" in got.reason


def test_an_unparseable_file_still_returns_a_weight(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n", encoding="utf-8")
    got = m.calculate_workload_weight(bad, root=tmp_path)
    assert got.complexity == 0
    assert got.total == 0


def test_weights_order_a_branchy_file_above_a_flat_one(tmp_path):
    heavy = tmp_path / "heavy.py"
    light = tmp_path / "light.py"
    heavy.write_text(BRANCHY, encoding="utf-8")
    light.write_text(FLAT, encoding="utf-8")
    a = m.calculate_workload_weight(heavy, root=tmp_path)
    b = m.calculate_workload_weight(light, root=tmp_path)
    assert a.total > b.total
    assert a.complexity == 3 and b.complexity == 0


def test_all_three_terms_reach_the_total(repo):
    from mindhive import events as ev_mod
    from mindhive.mindnet import telemetry as tel_mod

    target = repo / "f.py"
    target.write_text(BRANCHY, encoding="utf-8")
    _git(repo, "add", "f.py")
    _git(repo, "commit", "-qm", "one")
    tel_mod.emit("history", tel_mod.STATUS_ACTIVE, task_id="t1", project="test")
    rows = list(ev_mod.read_types((tel_mod.EVENT_TYPE,), "test"))

    got = m.calculate_workload_weight(target, root=repo, evs=rows,
                                      usage_key="t1")
    assert got.complexity == 3
    assert got.churn == 5, "BRANCHY is five lines, added once"
    assert got.usage == 1
    assert got.total == (m.W_COMPLEXITY * 3 + m.W_CHURN * 5 + m.W_USAGE * 1)


def test_weigh_files_answers_the_shape_shard_plan_wants(tmp_path):
    (tmp_path / "a.py").write_text(BRANCHY, encoding="utf-8")
    got = m.weigh_files(["a.py", "missing.py"], root=tmp_path)
    assert set(got) == {"a.py", "missing.py"}
    assert got["a.py"] > 0 and got["missing.py"] == 0
    assert all(isinstance(v, int) for v in got.values())
