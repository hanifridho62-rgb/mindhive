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
import json
import subprocess

import pytest

from mindhive.orchestrator import lintester as lint
from mindhive.orchestrator import queue as q


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


def test_the_skill_clause_is_bounded(monkeypatch):
    """Exercises `_skills_for`, not the test's own slicing.

    The version this replaces re-did the trim inline and asserted on its OWN
    result, so it stayed green with `[:MAX_MUST_USE]` deleted from the
    production path — it proved that Python slices lists. Stubbing
    `advice.resolve` is what makes the assertion reach the code under test."""
    from mindhive.mindnet import advice as adv_mod

    class _Ctx:
        evs = ()
        reco = None
        project = "test"

    many = tuple(f"skill-{i} — a long catalog description that must be cut"
                 for i in range(10))
    monkeypatch.setattr(adv_mod, "resolve",
                        lambda *_a, **_k: type("R", (), {"skills": many})())

    orch = lint.LintesterOrchestrator(project="test")
    got = orch._skills_for(lint.AgentTask(task_id="t1"), _Ctx())

    assert len(got) == lint.MAX_MUST_USE == 3
    assert got == ("skill-0", "skill-1", "skill-2"), (
        "the clause carried catalog blurbs instead of bare skill names")


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

    # The default asks for no ceiling, so the answer is the SAME on every
    # platform and the assertion is an equality, not a truthiness check. The
    # weaker `assert got.enforcement_reason` this replaces passed on Windows —
    # where the reason is non-empty because `resource` is missing — and failed on
    # both Ubuntu legs, where it was the empty string. A report that says "not
    # fenced" and gives no reason is the exact gap the field exists to close.
    got = lint.LintesterOrchestrator(
        decomposer=_two_units, project="test").execute_mission("x")
    assert got.enforcement == res_mod.TIMEOUT_ONLY
    assert got.enforcement_reason == res_mod.NO_LIMITS_REASON

    # Asking for a ceiling splits by platform, and each branch is pinned to one
    # value rather than to "something truthy".
    fenced = lint.LintesterOrchestrator(
        decomposer=_two_units, project="test",
        limits=res_mod.Limits(memory_bytes=1 << 30)).execute_mission("x")
    available, why = res_mod.limits_available()
    if available:
        assert fenced.enforcement == res_mod.KERNEL
        assert fenced.enforcement_reason == ""
    else:
        assert fenced.enforcement == res_mod.TIMEOUT_ONLY
        assert fenced.enforcement_reason == why
        assert "resource" in why


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


# --- the posture a child is spawned with --------------------------------------
# Probed on this machine on 2026-09-08 with claude 2.1.198: a headless
# `claude --output-format json -p` child spawned with NO --permission-mode had
# its Write DECLINED -- the tool call listed under `permission_denials`, the
# file never created, exit 0, is_error false. `_run_child` scored that child
# `ok`, because a clean envelope classifies as `ran-unverified`. So a mission
# fanned at a repository would have measured N children that could not touch
# the tree and called every one of them a success -- the same shape of phantom
# measurement `environment.py` was written to stop, one layer further in.


def _child_spy(monkeypatch, started=True):
    """Stand in for `mindnet.agent.run` and RECORD what `_run_child` asked for.

    A recorder rather than a bare stub: the property under test is an argument
    at the spawn seam, and an assertion on the mission's outcome cannot see it
    -- a child with no posture still exits 0 and still reports `ok`."""
    from mindhive.mindnet import agent as ag_mod
    from mindhive.mindnet import executor as x_mod

    seen = []

    def fake(run_key, prompt, **kw):
        seen.append(dict(kw, run_key=run_key))
        return x_mod.RunResult(result="ran-unverified", executed=started,
                               exit_code=0, run_key=run_key)

    monkeypatch.setattr(ag_mod, "run", fake)
    return seen


def _one_child(orch):
    entry = q.QueueEntry(task=lint.AgentTask(task_id="t1", title="t",
                                             description="d"))
    return orch._run_child(entry, q.WorkerLane(0))


def test_the_default_runner_lets_its_child_edit_its_lane(monkeypatch):
    seen = _child_spy(monkeypatch)

    out = _one_child(lint.LintesterOrchestrator(project="test"))

    assert lint.DEFAULT_PERMISSION_MODE == "acceptEdits"
    assert seen[0]["permission_mode"] == lint.DEFAULT_PERMISSION_MODE
    assert out.ok is True


@pytest.mark.parametrize("off", ["", None])
def test_an_explicit_empty_posture_sends_no_flag(monkeypatch, off):
    """Both spellings of "no posture" collapse to the ONE value the spawn seam
    reads as "omit the flag" — `claude_cli.run_headless` appends it only when
    the value is truthy, so an empty string reaching it would be indistinguishable
    from None there but not here."""
    seen = _child_spy(monkeypatch)

    _one_child(lint.LintesterOrchestrator(project="test", permission_mode=off))

    assert seen[0]["permission_mode"] is None


def test_every_child_of_a_real_mission_gets_the_posture(monkeypatch):
    seen = _child_spy(monkeypatch)
    monkeypatch.setattr(lint.wt_mod, "acquire_lanes",
                        lambda root, n, mid="": [
                            lint.wt_mod.LaneWorktree(lane_id=i, ok=True,
                                                     path=f"lane-{i}")
                            for i in range(n)])
    monkeypatch.setattr(lint.wt_mod, "release_lanes", lambda root, trees: [])

    got = lint.LintesterOrchestrator(
        project="test", lanes=2, decomposer=_two_units,
        max_attempts=1).execute_mission("do it", dry_run=False)

    assert got.outcome == "ok", got.reason
    assert len(seen) == 2
    assert all(c["permission_mode"] == "acceptEdits" for c in seen), seen
    # The working directory IS a lane checkout, and that is what bounds the
    # posture: an in-directory edit approval reaches that lane and nothing
    # above it. NOT one lane per unit -- a unit stolen by an idle sibling runs
    # in the THIEF's checkout, which is `WorkStealingScheduler` working, not
    # failing -- so the property pinned here is that no child was ever handed
    # the mission root instead of a lane.
    assert seen and all(c["root"] in {"lane-0", "lane-1"} for c in seen), seen


def test_run_mission_forwards_the_permission_mode(monkeypatch):
    seen = _child_spy(monkeypatch)
    monkeypatch.setattr(lint.wt_mod, "acquire_lanes",
                        lambda root, n, mid="": [
                            lint.wt_mod.LaneWorktree(lane_id=i, ok=True,
                                                     path=f"lane-{i}")
                            for i in range(n)])
    monkeypatch.setattr(lint.wt_mod, "release_lanes", lambda root, trees: [])

    lint.run_mission("do it", project="test", decomposer=_two_units,
                     max_attempts=1, dry_run=False, permission_mode="")

    assert seen and all(c["permission_mode"] is None for c in seen), seen


def test_a_dry_run_grants_no_posture_because_it_spawns_nothing(monkeypatch):
    seen = _child_spy(monkeypatch)

    lint.LintesterOrchestrator(
        project="test", decomposer=_two_units).execute_mission("do it",
                                                               dry_run=True)

    assert seen == []


# --- review fixes: the paths a description must not be able to reach -----------

def test_a_description_cannot_weight_a_file_outside_the_repo_root(tmp_path):
    """`_files_for` is fed model-written text; it must not escape *root*.

    Both escapes are checked because they fail differently: an ABSOLUTE token
    makes `Path(base) / token` discard *base* entirely on POSIX, while `..`
    walks out of it on every platform. A path that got through would be opened
    and AST-parsed by `metrics.calculate_workload_weight`."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    inside = root / "src" / "real.py"
    inside.write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "secret.py"
    outside.write_text("y = 2\n", encoding="utf-8")

    orc = lint.LintesterOrchestrator(root=root, project="test")
    got = orc._files_for(
        f"touch src/real.py and {outside.as_posix()} and ../secret.py")

    assert inside.resolve().as_posix() in got, "the in-repo file was dropped"
    assert all("secret.py" not in p for p in got), (
        f"a path outside the root survived containment: {got}")


def test_the_escape_check_is_what_drops_it_not_a_missing_file(tmp_path):
    """Negative half of the pair. Without it the test above passes for the
    wrong reason — a token that names nothing is dropped anyway, so the
    assertion would hold even with containment removed."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    outside = tmp_path / "secret.py"
    outside.write_text("y = 2\n", encoding="utf-8")

    assert outside.is_file(), "fixture: the outside file must really exist"
    orc = lint.LintesterOrchestrator(root=root, project="test")
    assert orc._files_for(outside.as_posix()) == ()


def test_an_interrupt_without_a_checkpoint_dir_does_not_claim_one(monkeypatch):
    """The reason line must not send an operator looking for a file that was
    never written."""
    def _boom(coro):
        coro.close()            # or pytest reports a never-awaited coroutine
        raise KeyboardInterrupt

    monkeypatch.setattr(lint.asyncio, "run", _boom)
    orc = lint.LintesterOrchestrator(
        project="test", decomposer=_two_units,
        runner=lambda e, ln: True)
    monkeypatch.setattr(lint.wt_mod, "acquire_lanes",
                        lambda root, n, mid="": [
                            lint.wt_mod.LaneWorktree(lane_id=i, ok=True,
                                                     path=f"lane-{i}")
                            for i in range(n)])
    monkeypatch.setattr(lint.wt_mod, "release_lanes", lambda root, trees: [])

    bare = orc.execute_mission("x", dry_run=False)
    assert bare.outcome == "partial"
    assert "no checkpoint directory was given" in bare.reason

    kept = orc.execute_mission("x", dry_run=False, checkpoint_dir="somewhere")
    assert "the checkpoint holds what was left" in kept.reason


def test_run_mission_forwards_the_checkpoint_dir_instead_of_raising(tmp_path):
    """It used to land in `**kwargs` and reach the constructor, which has no
    such field — a `TypeError` on the one call that wanted to be resumable."""
    got = lint.run_mission("x", root=tmp_path, project="test",
                           checkpoint_dir=tmp_path / "cp",
                           decomposer=_two_units)
    assert got.outcome == "dry-run"


def test_a_unit_that_fails_then_passes_is_reported_as_a_recovery(monkeypatch):
    """End-to-end wiring of the breaker's success half.

    The unit tests for `on_success` live in `test_orchestrator_queue.py` and
    `test_orchestrator_resilience.py`; this one exists because both stayed green
    with `lintester` passing `on_success=None` — the seam worked and nothing
    used it. Asserted through the Mission report, which is what an operator
    actually reads."""
    monkeypatch.setattr(lint.wt_mod, "acquire_lanes",
                        lambda root, n, mid="": [
                            lint.wt_mod.LaneWorktree(lane_id=i, ok=True,
                                                     path=f"lane-{i}")
                            for i in range(n)])
    monkeypatch.setattr(lint.wt_mod, "release_lanes", lambda root, trees: [])

    tries = {}

    def _flaky(entry, lane):
        n = tries.get(entry.task_id, 0) + 1
        tries[entry.task_id] = n
        return n > 1          # fails once, then passes

    got = lint.LintesterOrchestrator(
        project="test", decomposer=_two_units, runner=_flaky,
        max_attempts=3).execute_mission("x", dry_run=False)

    assert got.outcome == "ok", got.failed
    assert sorted(got.completed) == ["t-ledger", "t-worktree"]
    assert got.recoveries, (
        "a unit failed and then passed, and the report credited nothing — "
        "the scheduler's success hook is not wired to the repair loop")
    assert len(got.recoveries) == 2
    assert got.open_keys == (), (
        f"a recovered target is still reported as broken: {got.open_keys}")
    assert tries == {"t-worktree": 2, "t-ledger": 2}


# --- the local preview decomposer (TODOS 27a) ---------------------------------

def _two_dir_repo(root):
    """A repo whose files sit in two directories, so a split has something to
    split on. Returns the intent naming one file from each."""
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "src" / "pkg" / "writer.py").write_text(
        "def write(x):\n    if x:\n        return 1\n    return 0\n",
        encoding="utf-8")
    (root / "tests" / "unit" / "test_writer.py").write_text(
        "def test_write():\n    assert True\n", encoding="utf-8")
    return ("retry the writer in src/pkg/writer.py and cover it in "
            "tests/unit/test_writer.py")


def test_a_dry_run_splits_the_intent_by_the_directories_it_names(repo):
    """TODOS 27a. A dry run used to answer one unit whatever the intent said,
    so the lane plan it printed could never be more than a placeholder."""
    intent = _two_dir_repo(repo)
    got = lint.LintesterOrchestrator(root=repo, project="test",
                                     lanes=2).execute_mission(intent)

    assert got.outcome == "dry-run"
    assert got.decomposition == lint.PREVIEW
    assert len(got.tasks) == 2, [t.task_id for t in got.tasks]


def test_a_preview_unit_owns_only_its_own_directorys_files(repo):
    """The ownership invariant, which is the whole reason the preview carries
    its own file set instead of letting `_files_for` re-read a description that
    still names the entire intent. Two lanes claiming one file is exactly what
    `clusters.shard_plan` exists to prevent."""
    intent = _two_dir_repo(repo)
    got = lint.LintesterOrchestrator(root=repo, project="test",
                                     lanes=2).execute_mission(intent)

    owned = [set(t.files) for t in got.tasks]
    assert all(owned), f"a preview unit owns no file at all: {owned}"
    assert owned[0].isdisjoint(owned[1]), (
        f"two units claim the same file: {owned}")
    assert {len(o) for o in owned} == {1}, (
        f"a unit was handed the whole intent's files, not its own: {owned}")


def test_one_directory_is_not_a_split_and_is_not_reported_as_one(repo):
    """The negative case that makes the one above mean something. With every
    named file in one directory there is nothing to preview, and the mission
    must say `root-only` rather than dress a single unit up as a split."""
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("b = 2\n", encoding="utf-8")
    got = lint.LintesterOrchestrator(root=repo, project="test").execute_mission(
        "touch src/a.py and src/b.py")

    assert got.decomposition == lint.ROOT_ONLY
    assert len(got.tasks) == 1


def test_the_preview_never_stands_in_for_an_injected_decomposer(repo):
    """A caller who supplied a decomposer gets that decomposer's split, and the
    report says so. The preview is a fallback, never an override."""
    got = lint.LintesterOrchestrator(root=repo, project="test",
                                     decomposer=_two_units).execute_mission(
        _two_dir_repo(repo))

    assert got.decomposition == lint.INJECTED
    assert sorted(t.task_id for t in got.tasks) == ["t-ledger", "t-worktree"]


def test_the_split_source_reaches_the_operator(repo, capsys):
    """A locally-derived preview reported as a real decomposition would be a
    lie about what the run would produce, so it has to be legible in BOTH the
    machine report and the printed one."""
    from mindhive.orchestrator import cli as cli_mod

    got = lint.LintesterOrchestrator(root=repo, project="test",
                                     lanes=2).execute_mission(
        _two_dir_repo(repo))

    assert got.as_dict()["decomposition"] == lint.PREVIEW
    cli_mod._print(got)
    assert lint.PREVIEW in capsys.readouterr().out


def test_a_preview_unit_id_is_derived_from_its_directory(repo):
    """Ids are read by people and used in paths, so `src/mindhive` may not
    travel as itself."""
    got = lint.LintesterOrchestrator(root=repo, project="test",
                                     lanes=2).execute_mission(
        _two_dir_repo(repo))

    ids = sorted(t.task_id for t in got.tasks)
    assert all(("/" not in i and "\\" not in i) for i in ids), ids
    assert any(i.endswith("-src-pkg") for i in ids), ids
    assert any(i.endswith("-tests-unit") for i in ids), ids


# --- the per-unit symbol slice (WB1.4) ----------------------------------------

def _py_repo(tmp_path):
    """Two units' worth of Python, in two directories, plus a shared module."""
    for rel, text in (
            ("src/pkg/writer.py",
             "from pkg.shared import fmt\n\n\ndef write(rows):\n"
             "    return fmt(rows)\n"),
            ("src/pkg/shared.py", "def fmt(rows):\n    return list(rows)\n"),
            ("tests/unit/test_writer.py",
             "def test_write():\n    assert True\n")):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_each_unit_is_briefed_with_the_slice_of_the_map_it_owns(tmp_path):
    """WB1.4's open half, closed. `wave_context` resolves ONE symbol slice and
    every task in the wave used to receive the same bytes — measured at 3,896
    characters, identical in every brief. A mission's units own different
    directories by construction, so a repo-wide map tells each of them mostly
    about code it does not own."""
    from mindhive.mindnet import brief as brief_mod

    root = _py_repo(tmp_path)
    orc = lint.LintesterOrchestrator(root=root, project="test")
    ctx = brief_mod.WaveContext(project="test", symbols="WAVE-WIDE-BLOCK")

    writer = orc._task_context([str(root / "src" / "pkg" / "writer.py")], ctx)
    covers = orc._task_context([str(root / "tests" / "unit" / "test_writer.py")],
                               ctx)

    assert writer.symbols != ctx.symbols, "the unit was given the wave's block"
    assert writer.symbols != covers.symbols, (
        "two units in different directories were given the same map")
    assert "writer.py" in writer.symbols
    assert "test_writer.py" in covers.symbols


def test_a_unit_that_owns_no_module_keeps_the_wave_slice(tmp_path):
    """The fall-back, and it is deliberate: a unit that touches no Python is
    better served by the repository's shape than by a header telling it the
    repository has nothing in it."""
    from mindhive.mindnet import brief as brief_mod

    root = _py_repo(tmp_path)
    (root / "README.md").write_text("# docs\n", encoding="utf-8")
    ctx = brief_mod.WaveContext(project="test", symbols="WAVE-WIDE-BLOCK")
    orc = lint.LintesterOrchestrator(root=root, project="test")

    assert orc._task_context([str(root / "README.md")], ctx) is ctx
    assert orc._task_context([], ctx) is ctx
    assert orc._task_context(["x"], None) is None


def test_the_symbol_map_is_built_once_for_the_whole_mission(tmp_path,
                                                            monkeypatch):
    """A scan per unit would be a full parse of the tree per unit. The cache is
    the reason `build_map` and `focus` are separate functions at all."""
    from mindhive import symbols as sym_mod
    from mindhive.mindnet import brief as brief_mod

    root = _py_repo(tmp_path)
    built = []
    real = sym_mod.build_map
    monkeypatch.setattr(sym_mod, "build_map",
                        lambda r=None: built.append(r) or real(r))
    orc = lint.LintesterOrchestrator(root=root, project="test")
    ctx = brief_mod.WaveContext(project="test", symbols="W")

    for rel in ("src/pkg/writer.py", "src/pkg/shared.py",
                "tests/unit/test_writer.py"):
        orc._task_context([str(root / rel)], ctx)

    assert len(built) == 1, f"the tree was parsed {len(built)} times"


def test_a_symbol_map_that_cannot_be_built_is_asked_for_once(tmp_path,
                                                             monkeypatch):
    """Total, and not retried. A map that failed will fail again, and a mission
    that retried it per unit would pay the failure N times over."""
    from mindhive import symbols as sym_mod
    from mindhive.mindnet import brief as brief_mod

    calls = []

    def _boom(_root=None):
        calls.append(1)
        raise OSError("no tree here")

    monkeypatch.setattr(sym_mod, "build_map", _boom)
    orc = lint.LintesterOrchestrator(root=tmp_path, project="test")
    ctx = brief_mod.WaveContext(project="test", symbols="W")

    assert orc._task_context(["a.py"], ctx) is ctx
    assert orc._task_context(["b.py"], ctx) is ctx
    assert len(calls) == 1


# --- the lane rollback, wired (WB5.5) -----------------------------------------

def test_a_real_run_hands_the_repair_loop_a_rollback_bound_to_its_own_root(
        repo, monkeypatch):
    """The seam is wired at the mission, never as a library default, and it
    carries the root that `worktree.rollback`'s guard checks the lane path
    against — the only thing standing between a repair and the main checkout."""
    seen = {}
    real = lint.res_mod.RepairLoop

    def _spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(lint.res_mod, "RepairLoop", _spy)
    lint.LintesterOrchestrator(
        root=repo, project="test", lanes=2, decomposer=_two_units,
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    assert callable(seen.get("rollback")), seen
    lane = type("L", (), {"worktree": None})()
    assert seen["rollback"](lane).ok is False, (
        "a lane with no checkout must be refused, not guessed at")


def test_a_failed_unit_has_its_lane_rolled_back_before_the_retry(repo):
    """End to end on a real checkout: the child writes a file, fails, and the
    next attempt must not find that file waiting for it."""
    seen = []

    def runner(entry, lane):
        from pathlib import Path
        scratch = Path(lane.worktree_path) / "half-written.txt"
        seen.append(scratch.exists())
        scratch.write_text("partial\n", encoding="utf-8")
        return len(seen) > 1

    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=1,
        decomposer=lambda *_a: [("t-one", "One", "one unit")],
        runner=runner, max_attempts=3).execute_mission("do it", dry_run=False)

    assert seen == [False, False], (
        "the retry saw the failed attempt's leftovers still on disk")
    assert got.outcome == "ok", got.reason
    assert any("discarded" in line for line in got.rollbacks), got.rollbacks


# --- what the units really cost (TODOS 27's open item) ------------------------

def test_a_real_run_scores_the_weight_against_the_time_it_predicted(repo):
    """The open question TODOS 27 leaves: every scheduling number in this
    package assumes `calculate_workload_weight` predicts how long a child runs,
    and nothing measured it. `elapsed_ms` measures it and this is where the two
    are put beside each other."""
    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=2, decomposer=_two_units,
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    cal = got.calibration
    assert cal["units"] == 2, cal
    assert {s["unit"] for s in cal["samples"]} == {"t-ledger", "t-worktree"}
    assert all(s["ms"] > 0 for s in cal["samples"]), (
        "a unit that ran must report the time it took")
    assert cal["verdict"]


def test_the_samples_outlive_the_mission_that_measured_them(repo):
    """One mission's units answer nothing about an estimator; forty units over
    ten missions begin to. So the samples accumulate rather than being reported
    and dropped."""
    from mindhive.orchestrator import payoff as pf

    for _ in range(2):
        lint.LintesterOrchestrator(
            root=repo, project="test", lanes=2, decomposer=_two_units,
            runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    assert len(pf.history("test")) == 4


def test_a_dry_run_measures_nothing_and_says_so_by_staying_empty(repo):
    """A zeroed calibration would read as a measurement that came back flat."""
    got = lint.LintesterOrchestrator(
        root=repo, project="test", decomposer=_two_units,
        runner=lambda e, ln: True).execute_mission("do it", dry_run=True)

    assert got.calibration == {}
    assert got.rollbacks == ()


def _two_directories(tmp_path):
    """Two units' worth of Python, in two directories, with one symbol each
    that appears NOWHERE in the descriptions — so an assertion on that name can
    only be satisfied by the AST slice, never by the prompt echoing its own
    task description back."""
    for rel, text in (
            ("src/alpha/writer.py",
             "def alphaonlysymbol(rows):\n    return list(rows)\n"),
            ("src/beta/checker.py",
             "def betaonlysymbol(rows):\n    return len(rows)\n")):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _two_scoped_units(_root_id, _title, _intent):
    return [
        ("t-alpha", "Alpha", "rework src/alpha/writer.py end to end", None),
        ("t-beta", "Beta", "rework src/beta/checker.py end to end", None),
    ]


def test_each_briefed_task_carries_its_own_ast_scope_and_not_its_siblings(
        tmp_path):
    """WB1.4's acceptance criterion, end to end rather than at the seam.

    `test_each_unit_is_briefed_with_the_slice_of_the_map_it_owns` proves
    `_task_context` narrows the context. This proves the narrowed context
    survives `brief.build` and reaches `AgentTask.brief_text` — the field an
    agent is actually handed. The two are different claims and only the second
    is what the requirement says."""
    root = _two_directories(tmp_path)
    got = lint.LintesterOrchestrator(
        root=root, project="test",
        decomposer=_two_scoped_units).execute_mission("rework both")

    by_id = {t.task_id: t for t in got.tasks}
    alpha, beta = by_id["t-alpha"].brief_text, by_id["t-beta"].brief_text

    assert alpha and beta, "both briefs are empty, so this compares two blanks"
    assert "alphaonlysymbol" in alpha
    assert "alphaonlysymbol" not in beta, (
        "task B was handed task A's AST scope")
    assert "betaonlysymbol" in beta
    assert "betaonlysymbol" not in alpha


def test_the_briefed_slice_says_it_is_scoped_to_the_unit(tmp_path):
    """The header is the operator's only way to tell a per-unit slice from the
    repo-wide one, and the two are one string apart in the composed prompt."""
    root = _two_directories(tmp_path)
    got = lint.LintesterOrchestrator(
        root=root, project="test",
        decomposer=_two_scoped_units).execute_mission("rework both")

    assert "this unit owns" in got.tasks[0].brief_text


def test_a_unit_naming_no_file_is_still_briefed(tmp_path):
    """The fall-back reaching the composed prompt. A unit that owns no module
    must come back with the repository's shape, not with an empty brief."""
    root = _two_directories(tmp_path)
    got = lint.LintesterOrchestrator(
        root=root, project="test",
        decomposer=lambda *_a: [("t-none", "None", "no path here at all",
                                 None)]).execute_mission("do it")

    assert got.tasks[0].brief_text
    assert "this unit owns" not in got.tasks[0].brief_text


# --- the real agent decomposer (the AGENT branch) -----------------------------

_AGENT_REPLY = json.dumps({"subtasks": [
    {"title": "build", "description": "wire the writer"},
    {"title": "test", "description": "cover the writer"}]})


def _agent_stub(monkeypatch, text=_AGENT_REPLY, executed=True, reason=""):
    """Stand in for `mindnet.agent.run` — the seam `decompose.run` spawns
    through — so the REAL decomposer runs end to end (prompt, parse, validate,
    `tasks.check_graph`) with only the model call replaced. An injected
    `decomposer=` would skip every one of those, which is how the branch below
    went untested."""
    from mindhive.mindnet import agent as ag_mod
    from mindhive.mindnet import executor as x_mod

    def fake(run_key, prompt, **_kw):
        fake.calls.append((run_key, prompt))
        return x_mod.RunResult(result="ran-unverified", executed=executed,
                               exit_code=0 if executed else None,
                               excerpt=text, reason=reason)
    fake.calls = []
    monkeypatch.setattr(ag_mod, "run", fake)
    return fake


def test_a_real_mission_takes_the_agents_split_not_the_root_alone(
        repo, monkeypatch):
    """The AGENT branch, driven for real. Every other test in this file either
    injects a decomposer or takes the dry-run preview, and the branch a live
    `hive lintester --execute` actually runs was never exercised: it built its
    graph root as an `AgentTask`, `tasks.check_graph` read `parent_id` off it,
    and the blanket `except` turned that AttributeError into `root-only`.
    Every real mission collapsed to one unit on one lane, silently."""
    stub = _agent_stub(monkeypatch)
    orch = lint.LintesterOrchestrator(root=repo, project="test", lanes=2)
    intent = "retry the writer and cover it"
    root_id, title = orch._name(intent)

    units, source, why = orch._subtasks(root_id, title, intent, dry_run=False)

    assert source == lint.AGENT, why
    assert why == ""
    assert sorted(u[1] for u in units) == ["build", "test"]
    assert len(stub.calls) == 1 and intent in stub.calls[0][1], (
        "the real decomposer was not the one asked")


def test_the_agents_split_reaches_the_lanes_and_the_report(repo, monkeypatch):
    _agent_stub(monkeypatch)
    got = lint.LintesterOrchestrator(
        root=repo, project="test", lanes=2,
        runner=lambda e, ln: True).execute_mission(
            "retry the writer and cover it", dry_run=False)

    assert got.outcome == "ok", got.reason
    assert got.decomposition == lint.AGENT
    assert got.decomposition_reason == ""
    assert len(got.tasks) == 2 and len(got.completed) == 2
    assert sorted(t.title for t in got.tasks) == ["build", "test"]


def test_an_agent_that_never_ran_is_recorded_not_read_as_atomic(
        repo, monkeypatch):
    """`decompose.run` answers an auth failure as a REASON rather than an
    empty list — its docstring says so — and the orchestrator threw that
    reason away, so "Not logged in" and "already atomic" printed the same
    `root-only` line."""
    _agent_stub(monkeypatch, executed=False, reason="Not logged in")
    got = lint.LintesterOrchestrator(
        root=repo, project="test",
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    assert got.decomposition == lint.ROOT_ONLY
    assert "Not logged in" in got.decomposition_reason
    assert len(got.tasks) == 1


def test_an_empty_answer_is_recorded_as_the_agents_own_answer(
        repo, monkeypatch):
    """The one legitimate `root-only` on the agent path, and it still has to
    say so: an operator reading the report must be able to tell "the agent
    said atomic" from "the agent was never asked"."""
    _agent_stub(monkeypatch, text='{"subtasks": []}')
    got = lint.LintesterOrchestrator(
        root=repo, project="test",
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    assert got.decomposition == lint.ROOT_ONLY
    assert "no subtasks" in got.decomposition_reason
    assert len(got.tasks) == 1


def test_a_decomposer_that_raises_is_recorded_not_swallowed(repo, monkeypatch):
    """The exact shape that hid the bug above. An exception escaping
    `decompose.run` is a wiring fault — the runner's own failures are already
    folded into `Decomposition.reason` inside it — and a wiring fault reported
    as a bare `root-only` is indistinguishable from an atomic task."""
    from mindhive.mindnet import decompose as dec_mod

    def boom(*_a, **_k):
        raise AttributeError("'AgentTask' object has no attribute 'parent_id'")
    monkeypatch.setattr(dec_mod, "run", boom)

    got = lint.LintesterOrchestrator(
        root=repo, project="test",
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    assert got.decomposition == lint.ROOT_ONLY
    assert "AttributeError" in got.decomposition_reason
    assert "parent_id" in got.decomposition_reason
    assert len(got.tasks) == 1


def test_an_injected_decomposer_that_raises_is_recorded(repo):
    def bad(*_a):
        raise RuntimeError("decomposer died")

    got = lint.LintesterOrchestrator(
        root=repo, project="test", decomposer=bad).execute_mission("do it")

    assert got.outcome == "refused"
    assert got.decomposition == lint.INJECTED
    assert "decomposer died" in got.decomposition_reason


def test_the_decomposition_reason_reaches_the_operator(repo, monkeypatch,
                                                       capsys):
    """Same contract as `test_the_split_source_reaches_the_operator`: the
    reason a split is not what was asked for has to be legible in BOTH the
    machine report and the printed one."""
    from mindhive.orchestrator import cli as cli_mod

    _agent_stub(monkeypatch, executed=False, reason="Not logged in")
    got = lint.LintesterOrchestrator(
        root=repo, project="test",
        runner=lambda e, ln: True).execute_mission("do it", dry_run=False)

    assert got.as_dict()["decomposition_reason"] == got.decomposition_reason
    cli_mod._print(got)
    assert "Not logged in" in capsys.readouterr().out
