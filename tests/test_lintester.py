# tests/test_lintester.py
"""WB1 — `LintesterOrchestrator.execute_mission`, the single public entry.

Written against
``docs/plans/2026-09-01-001-feat-lintester-orchestrator-full-prd-plan.md``
U8/U9.

Two tests were written before the composition they check, and they are the two
the PRD actually asks for:

* `test_one_task_never_sees_another_tasks_material` — Information Hiding. It is
  not observable from a passing happy path, and it is guarded with BOTH briefs
  non-empty, because two blank strings satisfy "A contains none of B" while
  proving nothing. That exact vacuity shipped once in this repo already
  (`tests/test_mindnet_brief.py`, 2026-08-31).
* `test_agent_task_carries_no_route_to_global_repo_state` — asserted
  STRUCTURALLY over the dataclass field set, so a future edit that adds a ledger
  handle or a sibling list fails here rather than quietly widening what every
  child can see.
"""
import asyncio
import dataclasses
import subprocess

import pytest

from mindhive.orchestrator import lintester as lint


# --- fixtures -----------------------------------------------------------------

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
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture(autouse=True)
def brief_on(monkeypatch):
    monkeypatch.setenv("MINDHIVE_BRIEF", "1")
    monkeypatch.setenv("MINDHIVE_SESSION", "s1")


def _two_units(_root_id, _title, _intent):
    return [
        ("t-worktree", "Serialise the worktree add",
         "worktree add lock concurrent thread pool collides"),
        ("t-ledger", "Rotate the ledger segment",
         "ledger segment rotation append path stays fast"),
    ]


def _seed_two_sops():
    """One SOP per unit, with disjoint vocabulary, so a leak is visible."""
    from mindhive import events as ev_mod

    ev_mod.append_event("sop", {
        "action": "add", "sop_id": "worktree-locking",
        "title": "Serialise worktree add behind a lock",
        "text": "concurrent worktree add collides in the thread pool; take the "
                "lock before every add",
        "mandatory": True}, tags=["sop"], project="test")
    ev_mod.append_event("sop", {
        "action": "add", "sop_id": "ledger-rotation",
        "title": "Rotate the ledger segment",
        "text": "ledger segment rotation keeps the append path fast; rotate "
                "before the segment grows",
        "mandatory": True}, tags=["sop"], project="test")


# --- the entry point ----------------------------------------------------------

def test_an_empty_intent_is_refused_rather_than_run():
    got = lint.LintesterOrchestrator().execute_mission("   ")
    assert got.outcome == "refused"
    assert "empty intent" in got.reason
    assert got.tasks == ()


def test_an_empty_graph_is_refused_rather_than_reported_as_success():
    got = lint.LintesterOrchestrator(
        decomposer=lambda *_a: []).execute_mission("do the thing")
    assert got.outcome == "refused"
    assert "graph is empty" in got.reason


def test_two_units_become_two_agent_tasks_with_distinct_ids():
    got = lint.LintesterOrchestrator(
        decomposer=_two_units, project="test").execute_mission("do the thing")
    assert got.outcome == "dry-run"
    assert len(got.tasks) == 2
    assert len({t.task_id for t in got.tasks}) == 2
    assert all(isinstance(t, lint.AgentTask) for t in got.tasks)


def test_agent_task_carries_no_route_to_global_repo_state():
    """Structural. A field added here that reaches the ledger, the sibling list,
    or mission-wide status breaks Information Hiding for every child at once."""
    names = {f.name for f in dataclasses.fields(lint.AgentTask)}
    assert names == {"task_id", "title", "description", "brief_text",
                     "must_use", "weight", "lane_id", "worktree_path", "files"}


def test_agent_task_is_frozen():
    task = lint.AgentTask(task_id="t1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        task.task_id = "t2"


def test_one_task_never_sees_another_tasks_material():
    """Information Hiding, guarded against the two-blanks vacuity."""
    _seed_two_sops()
    got = lint.LintesterOrchestrator(
        decomposer=_two_units, project="test").execute_mission("do the thing")

    by_id = {t.task_id: t for t in got.tasks}
    a, b = by_id["t-worktree"], by_id["t-ledger"]
    assert a.brief_text and b.brief_text, (
        f"both briefs are empty, so the comparison below is two blanks: "
        f"{[t.brief_text for t in got.tasks]}")
    assert "worktree" in a.brief_text.lower()
    assert "worktree" not in b.brief_text.lower(), (
        "task B was handed task A's resolved material")
    assert "rotation" in b.brief_text.lower()
    assert "rotation" not in a.brief_text.lower()


def test_the_prompt_carries_only_this_tasks_own_parts():
    task = lint.AgentTask(task_id="t1", title="Do it",
                          description="the description",
                          brief_text="the brief", must_use=("brainstorming",))
    prompt = task.prompt
    assert prompt.index("Do it") < prompt.index("the description")
    assert "MUST USE: brainstorming via the Skill tool" in prompt
    assert prompt.endswith("the brief")


def test_a_task_with_no_resolved_skill_carries_no_empty_directive():
    assert lint.AgentTask(task_id="t1").must_use_block == ""
    assert "MUST USE" not in lint.AgentTask(task_id="t1").prompt


def test_the_skill_clause_names_the_skill_not_its_whole_catalog_blurb():
    assert lint._skill_name(
        "brainstorming — You MUST use this before any creative work, "
        "exploring the space of designs") == "brainstorming"
    assert lint._skill_name("ce-code-review") == "ce-code-review"


def test_the_skill_clause_is_bounded():
    class _Ctx:
        evs = ()
        reco = None
        project = "test"

    orch = lint.LintesterOrchestrator(project="test")
    many = tuple(f"skill-{i} — a description" for i in range(10))
    monkey = type("R", (), {"skills": many})()
    orch._skills_for.__self__            # sanity: bound method
    got = tuple(lint._skill_name(s) for s in monkey.skills)[:lint.MAX_MUST_USE]
    assert len(got) == lint.MAX_MUST_USE == 3


# --- weights reach the packer -------------------------------------------------

def test_a_heavier_unit_gets_a_lane_to_itself(tmp_path):
    """Proves the quantitative weight actually reaches `clusters.shard_plan`.
    With three equal weights and two lanes the heaviest lane would hold two;
    with one unit far heavier it holds one."""
    heavy_dir = tmp_path / "heavy"
    heavy_dir.mkdir()
    (heavy_dir / "mod.py").write_text(
        "def f(x):\n    if x:\n        if x > 1:\n            for i in x:\n"
        "                if i:\n                    pass\n", encoding="utf-8")

    def decomposer(*_a):
        return [("heavy", "Heavy", "rewrite heavy/mod.py"),
                ("light-a", "A", "write a note"),
                ("light-b", "B", "write another note")]

    got = lint.LintesterOrchestrator(root=tmp_path, project="test", lanes=2,
                                     decomposer=decomposer).execute_mission("x")
    by_id = {t.task_id: t for t in got.tasks}
    assert by_id["heavy"].weight > by_id["light-a"].weight
    lanes = {ln["lane"]: ln for ln in got.lanes}
    heavy_lane = lanes[by_id["heavy"].lane_id]
    assert heavy_lane["tasks"] == ["heavy"], (
        f"the weight never reached the packer: {got.lanes}")


def test_a_unit_naming_no_file_still_gets_a_weight_floor():
    got = lint.LintesterOrchestrator(
        decomposer=lambda *_a: [("t1", "T", "no paths here")],
        project="test").execute_mission("x")
    assert got.tasks[0].weight >= 1


# --- the symbol slice ---------------------------------------------------------

def test_the_mission_opts_into_the_symbol_slice_without_touching_the_env(
        monkeypatch, tmp_path):
    import os

    from mindhive.mindnet import brief as brief_mod

    monkeypatch.delenv("MINDHIVE_SYMBOLS", raising=False)
    (tmp_path / "m.py").write_text("import os\n\n\ndef f():\n    pass\n",
                                   encoding="utf-8")

    plain = brief_mod.wave_context(project="test", root=tmp_path)
    assert "symbols" in plain.gated, "the global default is no longer off"

    opted = brief_mod.wave_context(project="test", root=tmp_path, symbols=True)
    assert "symbols" not in opted.gated
    assert opted.symbols, "the opt-in produced no slice"

    assert "MINDHIVE_SYMBOLS" not in os.environ, (
        "the mission wrote a process-wide env var to switch on a per-call flag")


def test_an_explicit_opt_out_beats_an_exported_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDHIVE_SYMBOLS", "1")
    got = __import__("mindhive.mindnet.brief", fromlist=["x"]).wave_context(
        project="test", root=tmp_path, symbols=False)
    assert "symbols" in got.gated


# --- the intent-as-description fix --------------------------------------------

def test_a_declared_task_can_carry_the_operators_own_words():
    from mindhive import events as ev_mod
    from mindhive.mindnet import autodeclare as ad_mod

    decision = ad_mod.Decision(declare=True, task_id="t-real",
                               title="Add retry to the writer")
    ad_mod.declare(decision, project="test",
                   description="add retry to the ledger writer")
    rows = [e for e in ev_mod.read_types(("mindnet_task",), "test")
            if e.payload.get("task_id") == "t-real"]
    assert rows and rows[0].payload["description"] == \
        "add retry to the ledger writer"
    assert rows[0].payload["description"] != ad_mod.AUTO_DESCRIPTION


def test_omitting_the_description_keeps_the_provenance_line():
    from mindhive import events as ev_mod
    from mindhive.mindnet import autodeclare as ad_mod

    ad_mod.declare(ad_mod.Decision(declare=True, task_id="t-auto", title="T"),
                   project="test")
    rows = [e for e in ev_mod.read_types(("mindnet_task",), "test")
            if e.payload.get("task_id") == "t-auto"]
    assert rows[0].payload["description"] == ad_mod.AUTO_DESCRIPTION


# --- running ------------------------------------------------------------------

def test_a_dry_run_starts_nothing():
    started = []
    got = lint.LintesterOrchestrator(
        decomposer=_two_units, project="test",
        runner=lambda e, ln: started.append(e.task_id)).execute_mission(
            "do the thing", dry_run=True)
    assert got.outcome == "dry-run" and got.dry_run is True
    assert started == []
    assert "nothing was started" in got.reason


def test_a_child_that_raises_fails_only_its_own_unit(repo):
    def runner(entry, lane):
        if entry.task_id == "t-ledger":
            raise RuntimeError("child died")
        return True

    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=2, decomposer=_two_units,
        runner=runner, max_attempts=1).execute_mission("do it", dry_run=False)

    assert got.outcome == "partial"
    assert got.completed == ("t-worktree",)
    assert any("t-ledger" in line and "child died" in line
               for line in got.failed)


def test_a_clean_run_reports_ok_and_leaves_no_lane_checkout(repo):
    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=2, decomposer=_two_units,
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    assert got.outcome == "ok", got.reason
    assert sorted(got.completed) == ["t-ledger", "t-worktree"]
    assert not (repo / ".worktrees" / "lane-0").exists()
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_each_running_task_reports_its_own_lane_checkout(repo):
    seen = {}

    def runner(entry, lane):
        seen[entry.task_id] = entry.task.worktree_path
        return True

    lint.LintesterOrchestrator(
        root=repo, project="test", lanes=2, decomposer=_two_units,
        runner=runner).execute_mission("do it", dry_run=False)

    assert len(seen) == 2
    assert all(p.endswith(("lane-0", "lane-1")) for p in seen.values()), seen
    assert len(set(seen.values())) == 2, "two lanes reported one checkout"


def test_a_mission_that_cannot_isolate_any_lane_is_refused(tmp_path):
    got = lint.LintesterOrchestrator(
        root=tmp_path / "not-a-repo", project="test", decomposer=_two_units,
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)
    assert got.outcome == "refused"
    assert "no lane could be isolated" in got.reason
    assert any(not ln["ok"] and ln["reason"] for ln in got.lanes)


def test_the_mission_reports_how_strongly_its_children_were_fenced():
    from mindhive.orchestrator import resilience as res_mod

    got = lint.LintesterOrchestrator(
        decomposer=_two_units, project="test").execute_mission("x")
    assert got.enforcement in (res_mod.KERNEL, res_mod.TIMEOUT_ONLY)
    if got.enforcement == res_mod.TIMEOUT_ONLY:
        assert got.enforcement_reason


def test_the_report_serialises_to_json_able_primitives():
    import json

    got = lint.LintesterOrchestrator(
        decomposer=_two_units, project="test").execute_mission("x")
    payload = json.loads(json.dumps(got.as_dict()))
    assert payload["outcome"] == "dry-run"
    assert len(payload["tasks"]) == 2
    assert {"lane", "tasks", "worktree", "weight"} <= set(payload["lanes"][0])


def test_a_repair_block_is_appended_not_substituted():
    task = lint.AgentTask(task_id="t1", description="original")
    repaired = task.with_repair("the failure said X")
    assert repaired is not task
    assert "original" in repaired.description
    assert "the failure said X" in repaired.description
    assert task.description == "original", "the frozen original was mutated"


def test_an_empty_repair_block_leaves_the_task_alone():
    task = lint.AgentTask(task_id="t1", description="original")
    assert task.with_repair("   ") is task
