# tests/test_cli_mindnet.py
"""MN-1 U7 — `hive mindnet`: the operator surface for the phase spine.

Written TEST-FIRST against
``docs/plans/2026-07-25-001-feat-mn1-phase-spine-plan.md`` U7/KTD2/KTD7/KTD8/
KTD9/KTD15 and the committed contracts in `tasks.py`, `phases.py`,
`controller.py`, `sidecar.py`, `executor.py` and `telemetry.py`. No line of the
`hive mindnet` subparser existed when these were written.

Four properties carry this file, and each one is a way the command surface can
look correct and do nothing:

1. **A declare must write BOTH axes.** A `mindnet_task` declare does not create
   a `PhaseRecord` — only a `mindnet_phase` declare does (`controller.fold`,
   create-only). A CLI that wrote the graph half alone would print a task and
   then silently orphan every phase event that task ever received.
   `test_mindnet_controller.py::test_a_task_declared_only_on_the_graph_axis_orphans_its_phase_events`
   pins the failure mode; `test_declare_writes_both_axes...` is its positive
   counterpart.

2. **The sidecar is written LAST.** `sidecar._staleness` counts ALL events, and
   `telemetry.busy` appends two `mindnet_activity` rows around every mutation.
   A sidecar written before those rows land reads STALE, and U9's injector then
   degrades to its phase-agnostic block — silently disabling the phase-aware
   injection MN-1 exists to deliver.
   `test_every_mutating_command_leaves_the_sidecar_fresh` is the one assertion
   that stops a future reordering from turning U9 off.

3. **`verified_by` is DERIVED, never taken from the operator** (KTD2). There is
   no `--verified-by` flag — asserted structurally, not merely behaviourally —
   and a bare `satisfy` on a phase Python cannot honestly check is REFUSED
   rather than stamped `python`.

4. **A no-op must not print success.** `PHASE_EVIDENCE["audit-bug"]` names
   `python` as its verifier and `controller._apply_satisfy` refuses any weaker
   claim, so `satisfy --agent-reported` there folds as a VIOLATION.
   `executor.agent_satisfy` already reports that with ``clears=False``; the CLI
   has to surface it.

## Every append in this file lands in ONE session — deliberately

`events.read_all` sorts by ``(ts, segment_name, line)``, the segment name is the
session id, and `events.now_iso` is second-resolution. Two appends inside the
same second are therefore ordered ALPHABETICALLY BY SESSION ID: setup written
under ``"s1"`` and command events written under the default ``"manual"`` would
replay commands-before-setup and orphan them, failing for a reason that has
nothing to do with the code under test. The autouse fixture below pins
``MINDHIVE_SESSION`` so every writer — the CLI, `telemetry.emit`,
`executor.validate`, `controller.restat`, and the direct appends here — shares
one segment and one line order.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mindhive import __main__ as cli_mod
from mindhive import events, paths
from mindhive.mindnet import controller as c_mod
from mindhive.mindnet import phases as ph_mod
from mindhive.mindnet import sidecar as sc_mod
from mindhive.mindnet import tasks as t_mod
from mindhive.mindnet import telemetry as tel_mod

SESSION = "s1"
TASK = "mn1-u7"
MOCK = (Path(__file__).resolve().parents[1] / "scripts" / "mindnet"
        / "mock_validator.py")

SPEC = "docs/superpowers/specs/mn1-u7-design.md"
PLAN = "docs/plans/mn1-u7-plan.md"


@pytest.fixture(autouse=True)
def worktree(tmp_path, monkeypatch):
    """One session id (see the module docstring) and a THROWAWAY worktree root.

    `paths.worktree_root` is patched rather than left to resolve the real
    checkout for two reasons: file evidence has to be created and deleted, and
    `executor.execute` runs its child with ``cwd=root``. Every module reaches it
    as ``paths.worktree_root()`` at call time, so one patch covers `__main__`,
    `phases.resolve_ref`, `controller.restat` and `executor.execute`."""
    monkeypatch.setenv("MINDHIVE_SESSION", SESSION)
    root = tmp_path / "wt"
    root.mkdir()
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: root)
    return root


# --- helpers -------------------------------------------------------------------

def _cli(*argv) -> int:
    return cli_mod.main(list(argv))


def _ok(*argv) -> None:
    assert _cli(*argv) == 0, f"expected `hive {' '.join(argv)}` to succeed"


def _doc(root, rel, body="evidence\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def _script(*args) -> str:
    """A validation script that runs the committed mock (U6's seam)."""
    return " ".join([sys.executable, str(MOCK), *[str(a) for a in args]])


def _state():
    evs = events.read_all()
    tasks = t_mod.fold(evs)
    return evs, tasks, c_mod.fold(evs, tasks)


def _records():
    return _state()[2].records


def _phase_events(action=None):
    return [e for e in events.read_all()
            if e.type == "mindnet_phase"
            and (action is None or e.payload.get("action") == action)]


def _code_change(task_id=TASK):
    """The `execution` join (KTD16): a ``code_change`` carrying ``task_id``.

    Appended directly because `hive log --task` is U8's producer. The field is
    already writable — `changelog.validate_payload` requires
    ``{category, text, files}`` and rejects no extra key — so this is the row an
    operator can write today, not a fixture shape."""
    return events.append_event(
        "code_change",
        {"category": "update", "text": "implemented the task's change-set",
         "files": ["src/mindhive/__main__.py"], "task_id": task_id},
        tags=["mindnet"])


def _walk_trivial(task=TASK, target="execution", script=None):
    """Declare a bypassed task (born at `execution`) and walk it forward.

    The bypass is what makes an agent-verified phase reachable in a test without
    touching the real checkout: the three document phases need files under
    `docs/…`, whereas `execution` joins on a ledger event and `audit-bug` is
    cleared by a real validation run against the committed mock."""
    argv = ["mindnet", "declare", task, "--title", "U7 operator surface",
            "--triviality-bypass"]
    if script is not None:
        argv += ["--validation-script", script]
    _ok(*argv)
    if target == "execution":
        return
    _code_change(task)
    _ok("mindnet", "satisfy", task)
    _ok("mindnet", "enter", task, "audit-bug")
    if target == "audit-bug":
        return
    _ok("mindnet", "validate", task)
    _ok("mindnet", "enter", task, "audit-code")
    assert target == "audit-code", f"no walk defined for '{target}'"


# --- declare: BOTH axes, or the machine silently does nothing -------------------

def test_declare_writes_both_axes_and_the_task_folds_into_records(capsys):
    """Trap 1. Exactly two registry events, one per axis, and the task lands in
    `records` rather than in the orphan bucket."""
    _ok("mindnet", "declare", TASK, "--title", "U7 operator surface")

    evs, tasks, state = _state()
    registry = [e for e in evs if e.type in ("mindnet_task", "mindnet_phase")]
    assert len(registry) == 2
    assert {e.type for e in registry} == {"mindnet_task", "mindnet_phase"}
    assert [e.payload["action"] for e in registry] == ["declare", "declare"]

    assert TASK in tasks
    assert TASK in state.records                    # the half that is easy to miss
    assert state.orphans == {}
    assert state.records[TASK].phase == t_mod.PHASES[0]
    assert "mindnet declare" in capsys.readouterr().out


def test_a_declared_task_accepts_a_phase_event_instead_of_orphaning_it():
    """The discriminating half of the test above: a satisfy aimed at the freshly
    declared task must LAND. Without the phase-axis declare it would orphan, and
    the assertions above would still pass on `tasks` alone."""
    _walk_trivial(target="execution")
    _code_change()
    _ok("mindnet", "satisfy", TASK)

    _evs, _tasks, state = _state()
    assert state.orphans == {}
    assert c_mod.satisfied(state.records, TASK, "execution")


def test_declare_bypass_is_born_at_execution():
    _walk_trivial(target="execution")
    assert _records()[TASK].phase == t_mod.TRIVIAL_ENTRY_PHASE


def test_declare_refuses_a_malformed_task_id_and_appends_nothing(capsys):
    assert _cli("mindnet", "declare", "Not A Task Id", "--title", "x") == 2
    assert "task_id" in capsys.readouterr().err
    assert events.read_all() == []


# --- enter ----------------------------------------------------------------------

def test_enter_appends_one_phase_event_and_prints_a_stable_line(capsys):
    _walk_trivial(target="execution")
    _code_change()
    _ok("mindnet", "satisfy", TASK)
    capsys.readouterr()

    _ok("mindnet", "enter", TASK, "audit-bug")
    out = capsys.readouterr().out
    assert "mindnet enter" in out and "audit-bug" in out
    enters = _phase_events("enter")
    assert len(enters) == 1
    assert enters[0].payload["phase"] == "audit-bug"
    assert _records()[TASK].phase == "audit-bug"


def test_an_illegal_transition_exits_non_zero_with_a_corrective_hint(capsys):
    """The gate is `controller.can_advance`; the CLI must report its reason AND
    name the command that unsticks the task (roadmap.record's idiom)."""
    _walk_trivial(target="execution")

    assert _cli("mindnet", "enter", TASK, "audit-bug") == 2
    err = capsys.readouterr().err
    assert "execution" in err                       # the reason names the phase
    assert "hive mindnet satisfy" in err            # ...and the fix
    assert _phase_events("enter") == []             # nothing entered the chain
    assert _records()[TASK].phase == "execution"


def test_enter_refuses_a_phase_that_is_not_a_phase(capsys):
    _walk_trivial(target="execution")
    assert _cli("mindnet", "enter", TASK, "shipit") == 2
    assert "shipit" in capsys.readouterr().err
    assert _phase_events("enter") == []


# --- satisfy: verified_by is derived, never taken from the operator -------------

def test_no_verified_by_flag_exists_on_satisfy():
    """KTD2, asserted STRUCTURALLY. A behavioural check (`--verified-by python`
    exits 2) passes just as well when argparse rejects an unknown flag for some
    unrelated reason; this reads the option table itself."""
    sat = _subparser("mindnet", "satisfy")
    options = [s for a in sat._actions for s in a.option_strings]
    assert options, "the satisfy subparser has no options at all"
    assert not [o for o in options if "verified" in o.lower()]
    # And the whole `mindnet` surface, not just this one subcommand.
    #
    # `brief` joined this set deliberately, and this failing is the point: a
    # frozen surface makes adding a command a decision somebody signed rather
    # than a diff nobody noticed. It reads only — it starts nothing, writes no
    # status row, and takes no `--verified-by`, which is what the loop below
    # checks for every member.
    surface = _subparser_action(_subparser("mindnet")).choices
    assert set(surface) == {"declare", "enter", "satisfy", "fall-back",
                            "validate", "restat", "show", "list", "rebuild",
                            "enforce", "orchestrate", "depend",
                            "decompose", "brief"}
    for name, sp in surface.items():
        bad = [o for a in sp._actions for o in a.option_strings
               if "verified" in o.lower()]
        assert not bad, f"`hive mindnet {name}` exposes {bad}"


def _subparser_action(parser):
    return next(a for a in parser._actions
                if isinstance(a, argparse._SubParsersAction))


def _subparser(*path):
    """The argparse parser for ``hive <path...>``."""
    node = cli_mod._build_parser()
    for name in path:
        node = _subparser_action(node).choices[name]
    return node


def test_satisfy_file_evidence_is_python_verified_and_records_its_root(worktree):
    """A stat IS a Python check, so `python` is the honest label — and KTD7's
    root goes on the evidence so a re-stat from a sibling checkout can answer
    `unknown` instead of `fail`."""
    _ok("mindnet", "declare", TASK, "--title", "U7")
    _doc(worktree, SPEC)

    _ok("mindnet", "satisfy", TASK, "--ref", SPEC)

    sats = _phase_events("satisfy")
    assert len(sats) == 1
    p = sats[0].payload
    assert p["verified_by"] == "python"
    assert p["phase"] == "brainstorm" and p["epoch"] == 0
    assert p["evidence"]["kind"] == "file"
    assert p["evidence"]["result"] == "ok"
    assert p["evidence"]["root"] == str(worktree)
    assert c_mod.satisfied(_records(), TASK, "brainstorm")


def test_satisfy_refuses_a_ref_that_is_not_there(capsys, worktree):
    _ok("mindnet", "declare", TASK, "--title", "U7")
    assert _cli("mindnet", "satisfy", TASK, "--ref", SPEC) == 2
    assert SPEC in capsys.readouterr().err
    assert _phase_events("satisfy") == []


def test_satisfy_refuses_a_ref_that_escapes_its_required_directory(capsys,
                                                                   worktree):
    """`phases.resolve_ref` containment, surfaced. A prefix test would accept
    this."""
    _ok("mindnet", "declare", TASK, "--title", "U7")
    _doc(worktree, "secrets.md")
    assert _cli("mindnet", "satisfy", TASK,
                "--ref", "docs/superpowers/specs/../../../secrets.md") == 2
    assert "resolves outside" in capsys.readouterr().err
    assert _phase_events("satisfy") == []


def test_satisfy_on_a_file_phase_without_a_ref_is_refused(capsys):
    _ok("mindnet", "declare", TASK, "--title", "U7")
    assert _cli("mindnet", "satisfy", TASK) == 2
    assert "--ref" in capsys.readouterr().err
    assert _phase_events("satisfy") == []


def test_satisfy_on_a_command_phase_routes_the_operator_to_validate(capsys):
    """`audit-bug` is python-verified COMMAND evidence: the only honest producer
    is a real run, so a bare satisfy must point at it rather than self-attest."""
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))
    before = len(_phase_events("satisfy"))

    assert _cli("mindnet", "satisfy", TASK) == 2
    err = capsys.readouterr().err
    assert "hive mindnet validate" in err
    assert len(_phase_events("satisfy")) == before


def test_satisfy_without_the_flag_on_an_agent_only_phase_is_refused(capsys):
    """KTD17: `audit-code` has no review event type to read, so a `python` claim
    for it would be a lie. The refusal must say so and name the flag."""
    _walk_trivial(target="audit-code", script=_script("--mode", "ok"))
    before = len(_phase_events("satisfy"))

    assert _cli("mindnet", "satisfy", TASK) == 2
    err = capsys.readouterr().err
    assert "audit-code" in err
    assert "--agent-reported" in err
    assert len(_phase_events("satisfy")) == before


def test_satisfy_on_execution_joins_a_code_change_event():
    _walk_trivial(target="execution")
    _code_change()
    _ok("mindnet", "satisfy", TASK)

    p = _phase_events("satisfy")[0].payload
    assert p["verified_by"] == "python"
    assert p["evidence"]["kind"] == "event"
    assert c_mod.satisfied(_records(), TASK, "execution")


def test_satisfy_on_execution_is_refused_when_no_code_change_joins(capsys):
    _walk_trivial(target="execution")
    assert _cli("mindnet", "satisfy", TASK) == 2
    err = capsys.readouterr().err
    assert "code_change" in err and TASK in err
    assert _phase_events("satisfy") == []


# --- trap 3: an agent-reported satisfy that clears nothing ----------------------

def test_agent_reported_satisfy_on_audit_bug_warns_and_exits_non_zero(capsys):
    """`PHASE_EVIDENCE["audit-bug"]` requires `python`, and
    `controller._apply_satisfy` refuses a weaker claim — so this satisfy folds
    as a VIOLATION. `executor.agent_satisfy` reports that with ``clears=False``;
    printing a success line over it is the failure mode under test."""
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))

    rc = _cli("mindnet", "satisfy", TASK, "--agent-reported",
              "--detail", "gate refused: destructive-command classifier")
    cap = capsys.readouterr()

    assert rc != 0, "a satisfy that cleared nothing must not exit 0"
    assert "audit-bug" in cap.err
    assert "python" in cap.err                      # names WHY it did not clear
    records = _records()
    assert not c_mod.satisfied(records, TASK, "audit-bug")
    assert any("audit-bug" in v for v in records[TASK].violations)


def test_agent_reported_satisfy_on_audit_code_clears_and_exits_zero(capsys):
    """The discriminating counterpart: the warning above must be about THIS
    phase's verifier, not about `--agent-reported` never working."""
    _walk_trivial(target="audit-code", script=_script("--mode", "ok"))

    rc = _cli("mindnet", "satisfy", TASK, "--agent-reported",
              "--detail", "thermo-nuclear review recorded by the agent")
    assert rc == 0
    assert "did not" not in capsys.readouterr().err
    assert c_mod.satisfied(_records(), TASK, "audit-code")


def test_agent_reported_satisfy_requires_a_detail(capsys):
    _walk_trivial(target="audit-code", script=_script("--mode", "ok"))
    before = len(_phase_events("satisfy"))
    assert _cli("mindnet", "satisfy", TASK, "--agent-reported") == 2
    assert "--detail" in capsys.readouterr().err
    assert len(_phase_events("satisfy")) == before


# --- fall-back -------------------------------------------------------------------

def test_fall_back_bumps_the_epoch_and_invalidates_forward_evidence(capsys):
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))
    assert c_mod.satisfied(_records(), TASK, "execution")
    capsys.readouterr()

    _ok("mindnet", "fall-back", TASK, "execution",
        "--detail", "the bug audit found a regression in the fold")
    out = capsys.readouterr().out
    assert "mindnet fall-back" in out and "epoch 1" in out

    rec = _records()[TASK]
    assert rec.phase == "execution" and rec.epoch == 1
    assert not c_mod.satisfied(_records(), TASK, "execution")


def test_fall_back_without_a_detail_is_refused_by_argparse():
    """The contract refuses a detail-less fall_back at both append surfaces; the
    CLI must not be a third surface that can produce one."""
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))
    with pytest.raises(SystemExit):
        _cli("mindnet", "fall-back", TASK, "execution")


def test_fall_back_to_a_phase_ahead_is_refused_with_the_forward_command(capsys):
    _walk_trivial(target="execution")
    assert _cli("mindnet", "fall-back", TASK, "pr-green",
                "--detail", "sneaking forward") == 2
    err = capsys.readouterr().err
    assert "hive mindnet enter" in err
    assert not [e for e in _phase_events() if e.payload["action"] == "fall_back"]


# --- validate ----------------------------------------------------------------------

def test_validate_runs_the_script_and_records_python_evidence(capsys):
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))
    capsys.readouterr()

    _ok("mindnet", "validate", TASK)
    out = capsys.readouterr().out
    assert "mindnet validate" in out and "exit 0" in out

    sats = [e for e in _phase_events("satisfy")
            if e.payload["evidence"]["kind"] == "command"]
    assert len(sats) == 1
    assert sats[0].payload["verified_by"] == "python"
    assert c_mod.satisfied(_records(), TASK, "audit-bug")


def test_validate_reports_a_failing_run_non_zero_but_still_records_it(capsys):
    _walk_trivial(target="audit-bug",
                  script=_script("--mode", "fail", "--exit-code", "7"))
    capsys.readouterr()

    assert _cli("mindnet", "validate", TASK) == 1
    assert "exit 7" in capsys.readouterr().out
    sats = [e for e in _phase_events("satisfy")
            if e.payload["evidence"]["kind"] == "command"]
    assert sats and sats[-1].payload["evidence"]["result"] == "fail"
    assert not c_mod.satisfied(_records(), TASK, "audit-bug")


def test_validate_refuses_a_destructive_script_without_executing_it(
        capsys, worktree, tmp_path):
    """KTD15's gate, through the CLI. The refusal is proved by the ABSENCE of
    the marker the mock writes as its very first act — a return value alone
    could be produced after the child had already run."""
    marker = tmp_path / "ran.txt"
    script = (_script("--mode", "ok", "--marker", str(marker))
              + " && rm -rf " + str(worktree))
    _walk_trivial(target="audit-bug", script=script)
    capsys.readouterr()
    before = len(_phase_events("satisfy"))

    assert _cli("mindnet", "validate", TASK) == 2
    assert "destructive" in capsys.readouterr().err
    assert not marker.exists(), "the gate refused but the child still ran"
    assert len(_phase_events("satisfy")) == before


def test_validate_without_a_script_is_refused(capsys):
    _walk_trivial(target="audit-bug")
    assert _cli("mindnet", "validate", TASK) == 2
    assert "validation_script" in capsys.readouterr().err


# --- restat ------------------------------------------------------------------------

def test_restat_marks_vanished_file_evidence_stale_without_moving_the_task(
        capsys, worktree):
    """KTD8: a failed re-stat records the finding and leaves the phase where it
    is — a task must not silently regress with nothing on the ledger."""
    _ok("mindnet", "declare", TASK, "--title", "U7")
    _doc(worktree, SPEC)
    _ok("mindnet", "satisfy", TASK, "--ref", SPEC)
    _ok("mindnet", "enter", TASK, "plan")
    plan_doc = _doc(worktree, PLAN)
    _ok("mindnet", "satisfy", TASK, "--ref", PLAN)
    plan_doc.unlink()
    capsys.readouterr()

    assert _cli("mindnet", "restat", TASK) == 1
    out = capsys.readouterr().out
    assert "stale" in out and PLAN in out
    rec = _records()[TASK]
    assert rec.phase == "plan"                      # NOT moved
    sat = rec.satisfaction("plan", rec.epoch)
    assert sat.result == "fail" and sat.verified_by == "python"


def test_restat_of_present_evidence_reports_ok_and_appends_nothing(capsys,
                                                                   worktree):
    _ok("mindnet", "declare", TASK, "--title", "U7")
    _doc(worktree, SPEC)
    _ok("mindnet", "satisfy", TASK, "--ref", SPEC)
    before = len(_phase_events())
    capsys.readouterr()

    _ok("mindnet", "restat", TASK)
    assert "ok" in capsys.readouterr().out
    assert len(_phase_events()) == before


# --- show / list -------------------------------------------------------------------

def test_show_marks_agent_reported_and_python_verified_distinctly(capsys):
    _walk_trivial(target="audit-code", script=_script("--mode", "ok"))
    _ok("mindnet", "satisfy", TASK, "--agent-reported",
        "--detail", "thermo-nuclear review recorded by the agent")
    capsys.readouterr()

    _ok("mindnet", "show", TASK)
    out = capsys.readouterr().out
    assert "agent-reported" in out                  # the weaker tier, named
    assert "python" in out                          # ...distinct from the strong one
    # ...and they are on DIFFERENT lines, i.e. the tier is per satisfaction.
    agent_line = next(ln for ln in out.splitlines() if "agent-reported" in ln)
    assert "audit-code" in agent_line
    assert "python" not in agent_line


def test_show_renders_orphans(capsys):
    """KTD14. A phase event for a task with no phase record vanishes from every
    projection unless something reports it."""
    events.append_event("mindnet_phase",
                        {"action": "enter", "task_id": "ghost-1",
                         "phase": "plan", "verified_by": "python"},
                        tags=["mindnet", "phase"])

    _ok("mindnet", "show", "ghost-1")
    out = capsys.readouterr().out
    assert "orphan" in out.lower()
    assert "ghost-1" in out
    assert "phase axis" in out                      # the fold's own reason


def test_show_reports_a_stale_or_unknown_restat_distinctly(capsys, worktree):
    """`unknown` is not `fail` (KTD7): command evidence cannot be re-stat'd at
    all, and saying so is different from saying the evidence is gone."""
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))
    _ok("mindnet", "validate", TASK)
    capsys.readouterr()

    _ok("mindnet", "show", TASK)
    out = capsys.readouterr().out
    assert "re-stat: unknown" in out
    # Matched on the rendered MARKS, not on the bare word: the artifact path in
    # the evidence line carries the pytest tmp dir, which is named after this
    # test and therefore contains "stale".
    assert "re-stat: stale" not in out
    assert "(STALE" not in out


def test_show_of_an_unknown_task_exits_non_zero(capsys):
    assert _cli("mindnet", "show", "nobody-here") == 2
    assert "nobody-here" in capsys.readouterr().err


def test_list_renders_tasks_with_phase_and_status(capsys):
    _walk_trivial(target="execution")
    _ok("mindnet", "declare", "mn1-u8", "--title", "Docs and the drift guard")
    capsys.readouterr()

    _ok("mindnet", "list")
    out = capsys.readouterr().out
    assert TASK in out and "execution" in out and "pending" in out
    assert "mn1-u8" in out and "brainstorm" in out
    assert "2 task(s)" in out


def test_list_on_an_empty_store_says_so_and_exits_zero(capsys):
    _ok("mindnet", "list")
    assert "hive mindnet declare" in capsys.readouterr().out


# --- rebuild -----------------------------------------------------------------------

def test_rebuild_reports_the_record_count(capsys):
    _walk_trivial(target="execution")
    _ok("mindnet", "declare", "mn1-u8", "--title", "Docs and the drift guard")
    capsys.readouterr()

    _ok("mindnet", "rebuild")
    out = capsys.readouterr().out
    assert "2 record(s)" in out
    assert set(sc_mod.read().state.records) == {TASK, "mn1-u8"}


def test_rebuild_refuses_to_erase_standing_state_from_an_empty_ledger(capsys):
    """`sidecar.rebuild`'s one refusal (H6), surfaced as a non-zero exit rather
    than as a success line over a wipe."""
    _walk_trivial(target="execution")
    for seg in paths.events_dir().glob("*.jsonl"):
        seg.unlink()
    capsys.readouterr()

    assert _cli("mindnet", "rebuild") == 2
    assert "refusing" in capsys.readouterr().err
    assert TASK in sc_mod.read().state.records      # still there


# --- trap 2: the sidecar is written LAST ----------------------------------------

def _assert_fresh(label):
    side = sc_mod.read()
    assert side.fresh, f"after `{label}` the sidecar reads stale: {side.reason}"


def test_every_mutating_command_leaves_the_sidecar_fresh(worktree):
    """THE ordering assertion (KTD9). `telemetry.busy` appends two
    `mindnet_activity` rows around every mutation and `sidecar._staleness`
    counts ALL events, so a sidecar written before the telemetry lands reads
    stale — and U9 then silently degrades to its phase-agnostic block, which is
    the injection MN-1 exists to deliver.

    Every mutating subcommand is exercised, including the ones that append
    NOTHING of their own (`restat` on command evidence): the telemetry alone is
    enough to make the sidecar stale."""
    steps = [
        ("declare", ("mindnet", "declare", TASK, "--title", "U7",
                     "--validation-script", _script("--mode", "ok"))),
        ("satisfy --ref", ("mindnet", "satisfy", TASK, "--ref", SPEC)),
        ("enter", ("mindnet", "enter", TASK, "plan")),
        ("rebuild", ("mindnet", "rebuild",)),
    ]
    _doc(worktree, SPEC)
    for label, argv in steps:
        _ok(*argv)
        _assert_fresh(label)

    # A read-only command must not leave it stale either.
    _ok("mindnet", "list")
    _assert_fresh("list")
    _ok("mindnet", "show", TASK)
    _assert_fresh("show")

    # fall-back, then the command-evidence half of the surface.
    _ok("mindnet", "fall-back", TASK, "plan", "--detail", "plan was wrong")
    _assert_fresh("fall-back")
    _ok("mindnet", "restat", TASK)
    _assert_fresh("restat")


def test_the_sidecar_would_read_stale_if_a_later_append_were_not_covered():
    """The canary for the assertion above: `_assert_fresh` has to be capable of
    FAILING. One extra event after the last refresh must make it stale."""
    _walk_trivial(target="execution")
    _assert_fresh("declare")
    events.append_event("mindnet_activity",
                        {"faculty": "task-graph", "status": "active"},
                        tags=["mindnet", "activity"])
    assert not sc_mod.read().fresh


def test_a_mutating_command_emits_faculty_telemetry():
    """U10's feed has to have a producer, and the faculty must come from the ONE
    canonical set (invariant 10) rather than from a plausible-sounding
    invention."""
    _walk_trivial(target="execution")
    acts = [e for e in events.read_all() if e.type == "mindnet_activity"]
    assert acts, "no faculty telemetry was emitted"
    faculties = {e.payload["faculty"] for e in acts}
    assert faculties <= set(tel_mod.FACULTIES)
    statuses = [e.payload["status"] for e in acts]
    assert tel_mod.STATUS_ACTIVE in statuses and tel_mod.STATUS_IDLE in statuses


def _cat_rows():
    """Every `task-graph` activity payload in the store."""
    return [e.payload for e in events.read_all()
            if e.type == "mindnet_activity"
            and e.payload.get("faculty") == "task-graph"]


def test_a_declare_names_on_the_feed_the_phase_it_landed_in():
    """RED before this unit: the feed carried the task and an EMPTY phase.

    `_mn_work` was handed a phase by every other mutating command and by
    `declare` alone was not, so the one command that CREATES a task -- the first
    an operator runs, and the only cat they see if they run nothing else --
    reported half an answer. `cat_board.catPanel` renders an em-dash for an
    empty field, which reads as "this faculty has no phase" rather than "nobody
    passed one", and that is the exact failure MN-17 exists to prevent: a board
    that looks complete while a field was never wired."""
    _ok("mindnet", "declare", TASK, "--title", "U7 operator surface")
    rows = _cat_rows()
    assert rows, "the declare lit no cat at all"
    assert {r.get("task_id") for r in rows} == {TASK}
    assert {r.get("phase") for r in rows} == {t_mod.PHASES[0]}, rows


def test_the_phase_on_the_feed_is_the_one_the_graph_actually_recorded():
    """Not a literal, and this is the half that matters.

    A hardcoded "brainstorm" would pass here while telling the operator
    something false about a BYPASSED task, which `tasks.fold` is required to
    bear at `execution`. So both entries are exercised and the value asserted
    against is the record's own phase -- if the derivation ever moves, this test
    follows it instead of pinning a stale copy of it."""
    for task, extra in ((TASK, ()), ("mn1-u7-trivial", ("--triviality-bypass",))):
        _ok("mindnet", "declare", task, "--title", "U7", *extra)
        want = t_mod.fold(events.read_all())[task].phase
        rows = [r for r in _cat_rows() if r.get("task_id") == task]
        assert rows, f"no cat row for {task}"
        assert {r.get("phase") for r in rows} == {want}, (task, want, rows)
    # ...and the two entries really do differ, or the loop proved nothing.
    folded = t_mod.fold(events.read_all())
    assert folded[TASK].phase != folded["mn1-u7-trivial"].phase


def test_the_declare_line_and_the_cat_feed_cannot_disagree(capsys):
    """One home for the entry phase. The printed line reads the projection and
    the feed is stamped before that projection exists, so they are two paths to
    the same fact -- which is precisely how they drift."""
    _ok("mindnet", "declare", "mn1-u7-trivial", "--title", "U7",
        "--triviality-bypass")
    printed = capsys.readouterr().out
    rows = _cat_rows()
    assert t_mod.TRIVIAL_ENTRY_PHASE in printed, printed
    assert {r.get("phase") for r in rows} == {t_mod.TRIVIAL_ENTRY_PHASE}, rows


def test_a_re_declare_reports_where_the_task_IS_not_where_tasks_start():
    """`declare` is idempotent BY DESIGN -- its own docstring invites re-running
    it to sharpen a title -- and a task being re-declared at `audit-bug` is not
    at `brainstorm`. Deriving the entry unconditionally would put a confidently
    WRONG phase on the board where the old code put nothing at all, and of the
    two that is the worse failure: an em-dash reads as "unknown", a phase name
    reads as fact."""
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))
    before = len(_cat_rows())
    _ok("mindnet", "declare", TASK, "--title", "U7 operator surface, sharpened")
    fresh = _cat_rows()[before:]
    assert fresh, "the re-declare lit no cat"
    assert {r.get("phase") for r in fresh} == {"audit-bug"}, fresh


# --- purity / totality canaries ---------------------------------------------------

def test_the_phase_axis_payloads_the_cli_writes_pass_the_writer_contract():
    """Every `mindnet_phase` row this surface produces must satisfy
    `phases.validate_stored` — the writer's half of KTD2. A row that does not
    folds as a violation and gates nothing."""
    _walk_trivial(target="audit-code", script=_script("--mode", "ok"))
    rows = _phase_events()
    assert len(rows) >= 4
    for ev in rows:
        ph_mod.validate_stored(ev.payload)


def test_the_append_surface_still_refuses_the_payloads_the_cli_writes():
    """...and the OTHER half: the same rows must be refused by
    `validate_payload`, because they carry a derived `verified_by`. If
    `hive append mindnet_phase` accepted them the honesty rule would be
    decoration."""
    _walk_trivial(target="execution")
    for ev in _phase_events():
        with pytest.raises(ValueError):
            ph_mod.validate_payload(ev.payload)


def test_the_mock_validator_is_committed_and_runnable():
    """Guards the walk helper itself: every `audit-bug` test above is vacuous if
    the mock cannot run."""
    r = subprocess.run([sys.executable, str(MOCK), "--mode", "ok"],
                       capture_output=True, text=True, timeout=60,
                       creationflags=paths.NO_WINDOW)
    assert r.returncode == 0


# --- MN-6: `hive mindnet enforce` — the opt-in toggle -------------------------------
#
# The toggle for a rule that can freeze a repository is the most safety-critical
# command on this surface, because it is the remedy. MN-4 shipped three
# deadlocks and every one of them was a remedy that could not be reached: the
# phase block denied `Bash` unconditionally, so `hive mindnet satisfy`,
# `hive mindnet enter` and `hive rules enforce --disable` were all locked inside
# the room they existed to open. `test_the_toggle_is_reachable_while_*` below is
# that lesson turned into a test, and it asserts against the REAL enforcer, not
# a restatement of the exemption list.

def _enforce_optin():
    from mindhive.mindnet import optin
    return optin


def _pick_up(task=TASK):
    """Mark *task* `in_progress` and refresh the sidecar.

    Two things this has to get right, and both are contracts the enforcer
    depends on rather than test scaffolding:

    * `in_progress` is the ONLY status the phase block acts on — MN-4's
      anti-brick guard deliberately excludes `pending` (the dataclass default),
      because a task declared months ago and forgotten would otherwise deny
      every write in the repo forever. Nothing on the `mindnet` subparser writes
      the status axis, so the row is appended directly.
    * `sidecar._staleness` counts ALL events, so a row appended after the last
      sidecar write reads STALE — and a stale projection allows everything. A
      test that skipped the rebuild would assert 'allowed' about staleness while
      believing it had asserted something about the opt-in."""
    events.append_event("mindnet_task",
                        {"action": "status", "task_id": task,
                         "status": t_mod.STATUS_IN_PROGRESS}, tags=["mindnet"])
    _ok("mindnet", "rebuild")


@pytest.fixture(autouse=True)
def _no_ambient_optin(monkeypatch):
    """A real `MINDNETWORK_ENFORCE` in the developer's shell would out-rank the
    persisted record and silently invert every assertion below."""
    monkeypatch.delenv(_enforce_optin().ENV_VAR, raising=False)


def test_enforce_with_no_flag_reports_dormant(capsys):
    """A status read, and the only place the dormant note is surfaced to a
    human. Exit 0: 'dormant' is the correct default state, not an error."""
    assert _cli("mindnet", "enforce") == 0
    out = capsys.readouterr().out
    assert _enforce_optin().DORMANT_NOTE in out


def test_enforce_enable_then_status_reports_armed(worktree, monkeypatch, capsys):
    """The chdir is not scaffolding: `status` resolves the current checkout from
    `Path.cwd()`, which is the SAME input the `PreToolUse` hook uses. Anchoring
    the status read on anything else would let `hive mindnet enforce` and the
    hook disagree, and the operator would find out at the next denial."""
    monkeypatch.chdir(worktree)
    _ok("mindnet", "enforce", "--enable")
    capsys.readouterr()
    assert _cli("mindnet", "enforce") == 0
    out = capsys.readouterr().out
    assert _enforce_optin().DORMANT_NOTE not in out
    assert str(worktree).replace("\\", "/") in out.replace("\\", "/")


def test_a_dormant_status_names_the_mechanism_holding_the_gate_shut(
        worktree, monkeypatch, capsys):
    """A remedy that does not name the deciding mechanism is unactionable — the
    same defect as MN-4's original one-sentence refusal. `MINDNETWORK_ENFORCE=0`
    out-ranks the record, so pointing the operator at `--enable` there sends
    them to a flag that changes the file and nothing else."""
    optin = _enforce_optin()
    monkeypatch.chdir(worktree)
    _ok("mindnet", "enforce", "--enable")
    monkeypatch.setenv(optin.ENV_VAR, "0")
    capsys.readouterr()

    assert _cli("mindnet", "enforce") == 0
    out = capsys.readouterr().out
    assert optin.ENV_VAR in out
    assert "--enable" not in out


def test_enabling_while_the_env_var_forces_dormant_says_so(worktree, monkeypatch,
                                                           capsys):
    """`--enable` writes the record honestly but must not report success as
    though the teeth are now live, or the operator learns otherwise at the next
    write that sails through."""
    optin = _enforce_optin()
    monkeypatch.chdir(worktree)
    monkeypatch.setenv(optin.ENV_VAR, "0")
    _ok("mindnet", "enforce", "--enable")
    out = capsys.readouterr().out
    assert "dormant" in out
    assert optin.armed(None, cwd=worktree) is False


def test_enforce_enable_records_the_current_worktree_root(worktree):
    """The git call happens HERE, once, in the CLI — never on the hook path."""
    _ok("mindnet", "enforce", "--enable")
    rec = json.loads(
        _enforce_optin().record_path(None).read_text(encoding="utf-8"))
    assert list(rec["roots"]) == [str(worktree.resolve()).replace("\\", "/")]


def test_enforce_enable_actually_arms_the_hook(worktree, monkeypatch):
    """The verifier that matters: not 'the file changed' but 'the PreToolUse
    surface now refuses'. A toggle that writes a record nothing reads is the
    exact shape of MN-4's first P0."""
    from mindhive import enforce as enf
    monkeypatch.chdir(worktree)
    _walk_trivial(target="execution")
    _ok("mindnet", "fall-back", TASK, "brainstorm", "--detail", "reopened")
    _pick_up()
    payload = json.dumps({"tool_name": "Write",
                          "tool_input": {"file_path": str(worktree / "x.py")}})

    assert enf.hook_decision(payload) == {}, (
        "canary: enforcement must be dormant before the opt-in")

    _ok("mindnet", "enforce", "--enable")

    out = enf.hook_decision(payload)
    assert (out.get("hookSpecificOutput") or {}).get(
        "permissionDecision") == "deny"

    _ok("mindnet", "enforce", "--disable")
    assert enf.hook_decision(payload) == {}


def test_enforce_disable_from_a_nested_checkout_beats_an_armed_parent(
        tmp_path, monkeypatch):
    """Linked worktrees live under the main checkout in this repo, so a
    `--disable` that could not out-rank an armed ancestor would be a no-op
    exactly when it is needed."""
    optin = _enforce_optin()
    main = tmp_path / "main"
    nested = main / ".claude" / "worktrees" / "wt"
    nested.mkdir(parents=True)

    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: main)
    _ok("mindnet", "enforce", "--enable")
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: nested)
    _ok("mindnet", "enforce", "--disable")

    assert optin.armed(None, cwd=nested) is False
    assert optin.armed(None, cwd=main) is True


def test_enforce_rejects_asking_for_both_directions_at_once():
    """`--enable --disable` has no defensible reading; argparse's mutually
    exclusive group makes it exit 2 rather than silently picking one."""
    with pytest.raises(SystemExit) as e:
        _cli("mindnet", "enforce", "--enable", "--disable")
    assert e.value.code == 2


def test_enforce_json_status_is_machine_readable(worktree, monkeypatch, capsys):
    monkeypatch.chdir(worktree)
    _ok("mindnet", "enforce", "--enable")
    capsys.readouterr()
    assert _cli("mindnet", "enforce", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["armed"] is True
    assert data["source"] == "file"
    assert Path(data["root"]) == worktree.resolve()


def test_the_toggle_appends_no_ledger_event(worktree):
    """Two reasons, both load-bearing. (1) `sidecar._staleness` counts ALL
    events, so an event appended without rewriting the sidecar reads STALE and
    U9's phase-aware injection silently degrades. (2) An operator disarming a
    frozen repository must not have that write depend on the ledger being
    writable at all."""
    before = len(events.read_all())
    _ok("mindnet", "enforce", "--enable")
    _ok("mindnet", "enforce", "--disable")
    assert len(events.read_all()) == before


def test_the_toggle_leaves_the_sidecar_fresh(worktree):
    """The consequence of the rule above, asserted through the projection the
    injector actually consults."""
    _walk_trivial(target="execution")
    _ok("mindnet", "enforce", "--enable")
    assert sc_mod.read().fresh is True


@pytest.mark.parametrize("command", [
    "hive mindnet enforce --disable",
    "hive mindnet satisfy mn1-u7 --ref docs/plans/x.md",
    "hive rules enforce --disable policy:gate:mindnet-phase",
    "hive log update 'recorded the change'",
])
def test_the_toggle_is_reachable_while_the_phase_block_denies_everything(
        worktree, monkeypatch, command):
    """MN-4's third P0, generalized: every documented remedy runs through Bash,
    and the phase block denied Bash unconditionally. Asserted against the real
    `enforce.hook_decision` with enforcement ARMED and a task parked at
    `brainstorm` — the state that froze the repo."""
    from mindhive import enforce as enf
    optin = _enforce_optin()
    monkeypatch.setenv(optin.ENV_VAR, "1")
    monkeypatch.chdir(worktree)
    _walk_trivial(target="execution")
    _ok("mindnet", "fall-back", TASK, "brainstorm", "--detail", "reopened")
    _pick_up()

    blocked = enf.hook_decision(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "py -m pytest -q"}}))
    assert (blocked.get("hookSpecificOutput") or {}).get(
        "permissionDecision") == "deny", (
        "canary: the phase block must actually be biting on this state")

    assert enf.hook_decision(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}})) == {}


def test_the_toggle_is_reachable_while_the_language_lock_denies_writes(
        worktree, monkeypatch):
    """The composed case — MN-4's SECOND P0 was two individually sound guards
    that deadlocked together, so the escape has to be checked against both rules
    at once, not each alone."""
    from mindhive import enforce as enf
    optin = _enforce_optin()
    monkeypatch.setenv(optin.ENV_VAR, "1")
    monkeypatch.chdir(worktree)
    _ok("mindnet", "declare", "lang-task", "--title", "python only",
        "--language", "python", "--triviality-bypass")

    denied = enf.hook_decision(json.dumps(
        {"tool_name": "Write",
         "tool_input": {"file_path": str(worktree / "app.js")}}))
    assert (denied.get("hookSpecificOutput") or {}).get(
        "permissionDecision") == "deny", (
        "canary: the language lock must actually be biting")

    assert enf.hook_decision(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "hive mindnet enforce --disable"}})) == {}


# --- MN-7 U6: `hive mindnet orchestrate` -------------------------------------------
#
# The dangerous command on this surface, so the safe form is the default: bare
# `orchestrate` plans and spawns nothing, and `--execute` is explicit.

def _orc_graph():
    """A -> {B, C} -> D on the ledger, all declared on both axes."""
    _ok("mindnet", "declare", "a", "--title", "root")
    for tid in ("b", "c"):
        _ok("mindnet", "declare", tid, "--title", f"arm {tid}")
        events.append_event("mindnet_task",
                            {"action": "depend", "task_id": tid, "on": "a"},
                            tags=["mindnet", "task"])
    _ok("mindnet", "declare", "d", "--title", "join")
    for tid in ("b", "c"):
        events.append_event("mindnet_task",
                            {"action": "depend", "task_id": "d", "on": tid},
                            tags=["mindnet", "task"])
    _ok("mindnet", "rebuild")


# --- MN-8: `hive mindnet depend` ---------------------------------------------------
#
# Until this existed the only way to write the edge the whole orchestrator is
# built on was raw JSON through `hive append`. Every refusal is checked BEFORE the
# append, because `tasks.fold` declines an unsound edge by silently continuing —
# so appending first would print success for an event that folds to nothing.

def test_depend_records_the_edge_and_the_fold_sees_it(capsys):
    _ok("mindnet", "declare", "a", "--title", "root")
    _ok("mindnet", "declare", "b", "--title", "waiter")
    capsys.readouterr()

    _ok("mindnet", "depend", "b", "a")
    assert "b now waits for a" in capsys.readouterr().out
    assert t_mod.fold(events.read_all())["b"].dependencies == ("a",)


def test_depend_is_what_the_frontier_then_honours():
    """The edge is only worth anything if the orchestrator acts on it."""
    from mindhive.mindnet import orchestrator as orc_mod
    _ok("mindnet", "declare", "a", "--title", "root")
    _ok("mindnet", "declare", "b", "--title", "waiter")
    _ok("mindnet", "depend", "b", "a")
    assert orc_mod.frontier(t_mod.fold(events.read_all())) == ["a"]


def test_depend_refuses_a_cycle_and_writes_nothing(capsys):
    """`fold` would silently drop this edge, so the CLI has to refuse it loudly —
    otherwise the command prints success and the graph is unchanged."""
    _ok("mindnet", "declare", "a", "--title", "a")
    _ok("mindnet", "declare", "b", "--title", "b")
    _ok("mindnet", "depend", "b", "a")
    before = len(events.read_all())
    capsys.readouterr()

    assert _cli("mindnet", "depend", "a", "b") == 2
    assert "cycle" in capsys.readouterr().err
    assert len(events.read_all()) == before, "a refused edge still wrote an event"


def test_depend_refuses_a_parent_child_deadlock(capsys):
    """A parent waiting on its own child is unsatisfiable: `can_complete` refuses
    to close the parent while the child is open, and the dependency refuses to
    start the parent until the child completes."""
    _ok("mindnet", "declare", "p", "--title", "parent")
    _ok("mindnet", "declare", "c", "--title", "child", "--parent", "p")
    capsys.readouterr()

    assert _cli("mindnet", "depend", "p", "c") == 2
    assert "deadlock" in capsys.readouterr().err


def test_depend_refuses_self_dependency(capsys):
    _ok("mindnet", "declare", "a", "--title", "a")
    capsys.readouterr()
    assert _cli("mindnet", "depend", "a", "a") == 2
    assert "itself" in capsys.readouterr().err


@pytest.mark.parametrize("missing", [("ghost", "a"), ("a", "ghost")])
def test_depend_refuses_an_unknown_task(capsys, missing):
    _ok("mindnet", "declare", "a", "--title", "a")
    capsys.readouterr()
    assert _cli("mindnet", "depend", *missing) == 2
    assert "no such task" in capsys.readouterr().err


def test_depend_is_idempotent_and_says_so(capsys):
    _ok("mindnet", "declare", "a", "--title", "a")
    _ok("mindnet", "declare", "b", "--title", "b")
    _ok("mindnet", "depend", "b", "a")
    before = len(events.read_all())
    capsys.readouterr()

    _ok("mindnet", "depend", "b", "a")
    assert "already depends" in capsys.readouterr().out
    assert len(events.read_all()) == before


def test_depend_leaves_the_sidecar_fresh():
    _ok("mindnet", "declare", "a", "--title", "a")
    _ok("mindnet", "declare", "b", "--title", "b")
    _ok("mindnet", "depend", "b", "a")
    assert sc_mod.read().fresh is True


def test_depend_is_on_the_frozen_subcommand_surface():
    assert "depend" in _subparser_action(_subparser("mindnet")).choices


def test_orchestrate_bare_is_a_dry_run_that_spawns_nothing(capsys, monkeypatch):
    """KTD5. Nothing may start without `--execute`."""
    from mindhive.mindnet import orchestrator as orc_mod
    _orc_graph()
    calls = []
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda *a, **k: calls.append(a) or ([], [], [], {}, {}, False, []))

    assert _cli("mindnet", "orchestrate") == 0
    out = capsys.readouterr().out
    assert calls == [], "a dry run started work"
    assert "dry-run" in out
    assert "a" in out and "b" in out, "a dry run must say what it WOULD do"


def test_orchestrate_dry_run_appends_nothing_to_the_ledger():
    _orc_graph()
    before = len(events.read_all())
    _ok("mindnet", "orchestrate")
    assert len(events.read_all()) == before


def test_orchestrate_execute_drives_a_diamond_to_settled(capsys, monkeypatch):
    from mindhive.mindnet import orchestrator as orc_mod
    # Permissive completion gate: this test is about the COMMAND, not the
    # pr-green gate. The real gate refuses a bare fixture task (no evidence,
    # not at its terminal phase), which is asserted directly in
    # test_a_task_walked_to_its_terminal_phase_really_does_complete.
    monkeypatch.setattr(orc_mod, "completion_gate_default",
                        lambda graph, tid, **kw: None)
    _orc_graph()
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda batch, *a, **k: (sorted(batch), [], [], {}, {}, False, []))

    assert _cli("mindnet", "orchestrate", "--execute") == 0
    out = capsys.readouterr().out
    assert "settled" in out


def test_orchestrate_exits_non_zero_when_the_graph_stalls(capsys, monkeypatch):
    """A stall is an operator-actionable state, not a success: work remains and
    nothing can advance it. A zero exit there would let a CI step that wraps this
    pass while the graph is stuck."""
    from mindhive.mindnet import orchestrator as orc_mod
    _orc_graph()
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda batch, *a, **k: (
                            [], sorted(batch), [],
                            {t: "synthetic failure" for t in batch}, {}, False, []))

    assert _cli("mindnet", "orchestrate", "--execute") == 1
    assert "stalled" in capsys.readouterr().out


def test_orchestrate_exits_non_zero_when_the_wave_only_awaits_validation(
        capsys, monkeypatch):
    """`awaiting-validation` is on the FAILURE side of the exit code, decided in
    /review 2026-08-29.

    Splitting it out of `stalled` was about labelling the state honestly, not
    about promoting it to a success. The agents ran and replied; nothing is
    verified until `hive mindnet validate` says so, and the pre-branch behaviour
    for exactly this situation was exit 1. An automation that wraps this command
    would otherwise read exit 0 and treat unvalidated work as finished — a silent
    change nobody asked for, which is what this test exists to prevent.

    The seventh tuple slot is `unverified`: the tasks that RAN and came back
    without writing a status row, which is what distinguishes this from a stall.
    """
    from mindhive.mindnet import orchestrator as orc_mod
    _orc_graph()
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda batch, *a, **k: (
                            [], [], sorted(batch),
                            {t: "ran-unverified" for t in batch}, {}, False,
                            sorted(batch)))

    assert _cli("mindnet", "orchestrate", "--execute") == 1
    out = capsys.readouterr().out
    assert orc_mod.OUTCOME_AWAITING_VALIDATION in out
    assert "stalled" not in out, "the state it was split out of must not reappear"


def test_orchestrate_json_carries_the_whole_run(capsys, monkeypatch):
    from mindhive.mindnet import orchestrator as orc_mod
    # Permissive completion gate: this test is about the COMMAND, not the
    # pr-green gate. The real gate refuses a bare fixture task (no evidence,
    # not at its terminal phase), which is asserted directly in
    # test_a_task_walked_to_its_terminal_phase_really_does_complete.
    monkeypatch.setattr(orc_mod, "completion_gate_default",
                        lambda graph, tid, **kw: None)
    _orc_graph()
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda batch, *a, **k: (sorted(batch), [], [], {}, {}, False, []))
    capsys.readouterr()                       # drop the fixture's declare noise

    assert _cli("mindnet", "orchestrate", "--execute", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "settled"
    assert data["completed"] == ["a", "b", "c", "d"]
    assert data["blocked"] == []
    assert [w["tasks"] for w in data["waves"]] == [["a"], ["b", "c"], ["d"]]


def test_orchestrate_honours_the_limit_end_to_end(capsys):
    _orc_graph()
    capsys.readouterr()                       # drop the fixture's declare noise
    assert _cli("mindnet", "orchestrate", "--limit", "1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert all(len(w["tasks"]) <= 1 for w in data["waves"])


def test_orchestrate_reports_a_graph_with_nothing_to_do(capsys):
    assert _cli("mindnet", "orchestrate") == 0
    assert "no mindnet" in capsys.readouterr().out.lower()


def test_orchestrate_leaves_the_sidecar_fresh(monkeypatch):
    """The property `test_every_mutating_command_leaves_the_sidecar_fresh`
    protects, extended to the one command that writes a whole wave of rows."""
    from mindhive.mindnet import orchestrator as orc_mod
    # Permissive completion gate: this test is about the COMMAND, not the
    # pr-green gate. The real gate refuses a bare fixture task (no evidence,
    # not at its terminal phase), which is asserted directly in
    # test_a_task_walked_to_its_terminal_phase_really_does_complete.
    monkeypatch.setattr(orc_mod, "completion_gate_default",
                        lambda graph, tid, **kw: None)
    _orc_graph()
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda batch, *a, **k: (sorted(batch), [], [], {}, {}, False, []))
    _ok("mindnet", "orchestrate", "--execute")
    assert sc_mod.read().fresh is True


def test_orchestrate_text_output_names_why_each_task_blocked(capsys,
                                                            monkeypatch):
    """"blocked: a" is not actionable, and the old text pointed operators at
    `hive mindnet show`, which had nothing recorded to show."""
    from mindhive.mindnet import orchestrator as orc_mod
    _orc_graph()
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda batch, *a, **k: (
                            [], sorted(batch), [],
                            {t: "the validation run exited 3" for t in batch}, {}, False,
                            []))
    capsys.readouterr()

    assert _cli("mindnet", "orchestrate", "--execute") == 1
    out = capsys.readouterr().out
    assert "a: the validation run exited 3" in out


def test_orchestrate_json_carries_the_blocked_reasons(capsys, monkeypatch):
    """The agent-facing surface needs the reason as data, not only as prose on
    stdout — otherwise an agent hitting `blocked` has to reconstruct the run-key
    convention and hunt for the log by hand."""
    from mindhive.mindnet import orchestrator as orc_mod
    _orc_graph()
    monkeypatch.setattr(orc_mod, "_run_wave",
                        lambda batch, *a, **k: (
                            [], sorted(batch), [], {t: "refused" for t in batch}, {}, False,
                            []))
    capsys.readouterr()

    assert _cli("mindnet", "orchestrate", "--execute", "--json") == 1
    data = json.loads(capsys.readouterr().out)
    assert data["reasons"]["a"] == "refused"
    assert data["waves"][0]["reasons"]["a"] == "refused"


def test_orchestrate_reports_an_orchestrator_fault_as_an_error_not_a_traceback(
        capsys, monkeypatch):
    """`main()` only catches JSONDecodeError, so without local handling a ledger
    or projection fault reached the operator as a raw stack trace — and the two
    faults _persist raises are exactly the ones carrying the repair instructions."""
    from mindhive.mindnet import orchestrator as orc_mod
    _orc_graph()

    def boom(*a, **k):
        raise RuntimeError("the projection is now STALE")

    monkeypatch.setattr(orc_mod, "orchestrate", boom)
    assert _cli("mindnet", "orchestrate", "--execute") == 2
    assert "STALE" in capsys.readouterr().err


def test_orchestrate_max_workers_is_distinct_from_limit():
    """Two knobs, two meanings: --limit decides which tasks run this wave at all,
    --max-workers how many run at once. Collapsing them would make a wide graph
    either serial or unbounded."""
    sp = _subparser("mindnet", "orchestrate")
    opts = [s for a in sp._actions for s in a.option_strings]
    assert "--limit" in opts and "--max-workers" in opts


def test_isolation_defaults_to_worktree_for_agent_runs(monkeypatch):
    """Flipped deliberately. Sharing one working tree is the DANGEROUS mode — two
    agents editing it corrupt each other's edits and the git index — so `none`
    cannot be what an operator gets by accident. It also delivers the founding ask
    that parallel work happen "secara paksa": `--execute` alone now fans out.

    `--via-script` defaults the other way, because re-checking a task in place has
    nothing to isolate."""
    from mindhive.mindnet import orchestrator as orc_mod
    seen = {}
    monkeypatch.setattr(orc_mod, "orchestrate",
                        lambda *a, **k: seen.update(k) or orc_mod.Orchestration(
                            outcome=orc_mod.OUTCOME_SETTLED))
    _ok("mindnet", "declare", "a", "--title", "a")

    _ok("mindnet", "orchestrate", "--execute")
    assert seen["isolate"] is True, "an agent run defaulted to a shared tree"

    _ok("mindnet", "orchestrate", "--execute", "--via-script")
    assert seen["isolate"] is False

    _ok("mindnet", "orchestrate", "--execute", "--isolation", "none")
    assert seen["isolate"] is False, "an explicit --isolation was ignored"


def test_parallelism_needs_no_extra_flag_now(monkeypatch):
    """The founding ask: when tasks CAN run in parallel, do it — not "ask the
    operator to opt in". With isolation on by default, the worker cap is the
    machine's, not 1."""
    from mindhive.mindnet import orchestrator as orc_mod
    seen = {}
    monkeypatch.setattr(orc_mod, "orchestrate",
                        lambda *a, **k: seen.update(k) or orc_mod.Orchestration(
                            outcome=orc_mod.OUTCOME_SETTLED))
    _ok("mindnet", "declare", "a", "--title", "a")
    _ok("mindnet", "orchestrate", "--execute")
    assert seen["max_workers"] is None, (
        "the shared-tree cap of 1 is still being applied")


def test_isolation_worktree_lifts_the_shared_tree_worker_cap(monkeypatch):
    """MN-8 capped agents at one because they shared a checkout. With one each,
    the cap has no reason to apply — this is the whole point of the flag."""
    from mindhive.mindnet import orchestrator as orc_mod
    seen = {}
    monkeypatch.setattr(orc_mod, "orchestrate",
                        lambda *a, **k: seen.update(k) or orc_mod.Orchestration(
                            outcome=orc_mod.OUTCOME_SETTLED))
    _ok("mindnet", "declare", "a", "--title", "a")

    _ok("mindnet", "orchestrate", "--execute", "--isolation", "worktree",
        "--max-workers", "4")
    assert seen["max_workers"] == 4 and seen["isolate"] is True

    # The cap now needs an EXPLICIT shared tree, since isolation is the default.
    _ok("mindnet", "orchestrate", "--execute", "--isolation", "none",
        "--max-workers", "4")
    assert seen["max_workers"] == 1, "the shared-tree cap stopped applying"
    assert seen["isolate"] is False


def test_isolation_is_refused_for_a_via_script_run(capsys):
    """A validation script re-checks a task in place; isolating it would run the
    check against an empty branch and prove nothing. Refusing beats silently
    ignoring a flag the operator typed."""
    _ok("mindnet", "declare", "a", "--title", "a")
    capsys.readouterr()
    assert _cli("mindnet", "orchestrate", "--execute", "--via-script",
                "--isolation", "worktree") == 2
    assert "isolation" in capsys.readouterr().err


def test_an_invalid_isolation_value_exits_2():
    with pytest.raises(SystemExit) as e:
        _cli("mindnet", "orchestrate", "--isolation", "chroot")
    assert e.value.code == 2


def test_orchestrate_reports_branches_and_kept_worktrees(capsys, monkeypatch):
    from mindhive.mindnet import orchestrator as orc_mod
    monkeypatch.setattr(orc_mod, "orchestrate", lambda *a, **k:
                        orc_mod.Orchestration(
                            outcome=orc_mod.OUTCOME_SETTLED, completed=["a"],
                            branches={"a": "mn9/a"},
                            kept={"a": "/tmp/wt/mn9-a"}))
    _ok("mindnet", "declare", "a", "--title", "a")
    capsys.readouterr()

    _ok("mindnet", "orchestrate", "--execute", "--isolation", "worktree")
    out = capsys.readouterr().out
    assert "mn9/a" in out
    assert "/tmp/wt/mn9-a" in out
    assert "hold work" in out, "a kept worktree must say WHY it matters"


def test_orchestrate_json_carries_branches_and_kept(capsys, monkeypatch):
    from mindhive.mindnet import orchestrator as orc_mod
    monkeypatch.setattr(orc_mod, "orchestrate", lambda *a, **k:
                        orc_mod.Orchestration(
                            outcome=orc_mod.OUTCOME_SETTLED,
                            branches={"a": "mn9/a"}, kept={"a": "/tmp/x"}))
    _ok("mindnet", "declare", "a", "--title", "a")
    capsys.readouterr()

    _ok("mindnet", "orchestrate", "--execute", "--isolation", "worktree",
        "--json")
    data = json.loads(capsys.readouterr().out)
    assert data["branches"] == {"a": "mn9/a"}
    assert data["kept"] == {"a": "/tmp/x"}


def test_a_task_walked_to_its_terminal_phase_really_does_complete(monkeypatch):
    """The end-to-end proof for the pr-green gate, and the one test that cannot be
    satisfied by a stand-in.

    `tests/test_mindnet_orchestrator.py` patches the gate permissive so its loop
    assertions stay about the loop; that leaves an obligation here — showing that a
    REAL task, walked through the REAL phase spine to its terminal phase with REAL
    recorded evidence, is actually allowed to complete. Without this the gate could
    be refusing everything forever and every other test would still pass."""
    from mindhive.mindnet import orchestrator as orc_mod
    _walk_trivial(target="audit-bug", script=_script("--mode", "ok"))
    _ok("mindnet", "validate", TASK)          # records python-verified evidence
    _ok("mindnet", "rebuild")

    graph = t_mod.fold(events.read_all())
    # The evidence axis is now populated by the validation run itself. Asserted
    # here because it was EMPTY before this change: `can_complete`'s message said
    # "a validation run must record its artifact", and no such producer existed, so
    # the gate was unsatisfiable for every non-bypassed task.
    assert graph[TASK].evidence, "the validation run recorded no task evidence"
    assert str(graph[TASK].evidence[0]).endswith(".txt"), (
        f"the ref is not the run artifact: {graph[TASK].evidence}")

    why = orc_mod.completion_gate_default(graph, TASK)
    assert why is None, f"a fully-walked task was still refused: {why}"


def test_the_same_task_is_refused_one_phase_earlier(monkeypatch):
    """The canary for the test above: at `execution` — before the terminal phase is
    satisfied — the very same task must NOT be completable. If both passed, the
    gate would be doing nothing."""
    from mindhive.mindnet import orchestrator as orc_mod
    _walk_trivial(target="execution")
    _ok("mindnet", "rebuild")

    graph = t_mod.fold(events.read_all())
    why = orc_mod.completion_gate_default(graph, TASK)
    assert why is not None, "a task at 'execution' was allowed to complete"


def test_orchestrate_is_on_the_frozen_subcommand_surface():
    surface = _subparser_action(_subparser("mindnet")).choices
    assert "orchestrate" in surface


def test_orchestrate_is_reachable_while_the_phase_block_denies_everything(
        worktree, monkeypatch):
    """The locked-room lesson applied to the new command. An operator may well
    need to drive the graph while a task sits at a planning gate, and `hive` being
    exempt is what makes that possible — asserted against the real enforcer with
    enforcement ARMED, not against a restatement of the exemption list."""
    from mindhive import enforce as enf
    optin = _enforce_optin()
    monkeypatch.setenv(optin.ENV_VAR, "1")
    monkeypatch.chdir(worktree)
    _walk_trivial(target="execution")
    _ok("mindnet", "fall-back", TASK, "brainstorm", "--detail", "reopened")
    _pick_up()

    blocked = enf.hook_decision(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "py -m pytest -q"}}))
    assert (blocked.get("hookSpecificOutput") or {}).get(
        "permissionDecision") == "deny", "canary: the phase block is not biting"

    assert enf.hook_decision(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "hive mindnet orchestrate --execute"}})) == {}


def test_enforce_works_outside_a_git_worktree(tmp_path, monkeypatch):
    """`paths.worktree_root` answers None off a repo (total by contract). The
    toggle must still have something to record rather than crashing — the cwd
    is the honest answer."""
    monkeypatch.setattr(paths, "worktree_root", lambda cwd=None: None)
    monkeypatch.chdir(tmp_path)
    _ok("mindnet", "enforce", "--enable")
    assert _enforce_optin().armed(None, cwd=tmp_path) is True

# --- MN-11: `hive mindnet decompose` -----------------------------------------------

def _fake_decomposer(monkeypatch, *pairs, executed=True):
    """Patch the agent seam so no real spawn happens."""
    import json as _json
    from mindhive.mindnet import decompose as dc_mod
    from mindhive.mindnet import executor as x_mod
    body = _json.dumps({"subtasks": [{"title": t, "description": ""}
                                     for t in pairs]})

    def fake(run_key, prompt, **kw):
        return x_mod.RunResult(result="ran-unverified", executed=executed,
                               exit_code=0, excerpt=body,
                               reason="" if executed else "never ran")

    monkeypatch.setattr(dc_mod.ag_mod, "run", fake)
    return dc_mod


def test_decompose_is_a_dry_run_that_declares_nothing(capsys, monkeypatch):
    """Same default as `orchestrate --execute`: this writes to an append-only
    ledger where a mistake cannot be deleted, only superseded."""
    _fake_decomposer(monkeypatch, "visualisasi data", "kapan digenerate")
    _ok("mindnet", "declare", "graph", "--title", "bikin graph")
    before = len(events.read_all())
    capsys.readouterr()

    assert _cli("mindnet", "decompose", "graph") == 0
    out = capsys.readouterr().out
    assert "would declare 2" in out
    assert "visualisasi" in out
    assert len(events.read_all()) == before, "a dry run wrote to the ledger"


def test_decompose_execute_declares_the_subtasks_under_their_parent(monkeypatch):
    _fake_decomposer(monkeypatch, "visualisasi data", "kapan digenerate")
    _ok("mindnet", "declare", "graph", "--title", "bikin graph")

    _ok("mindnet", "decompose", "graph", "--execute")
    folded = t_mod.fold(events.read_all())
    kids = [t for t in folded.values() if t.parent_id == "graph"]
    assert len(kids) == 2
    assert {k.title for k in kids} == {"visualisasi data", "kapan digenerate"}


def test_decomposed_subtasks_land_on_the_phase_axis_too(monkeypatch):
    """A task on the graph axis alone orphans every phase event it receives."""
    _fake_decomposer(monkeypatch, "one thing")
    _ok("mindnet", "declare", "graph", "--title", "bikin graph")
    _ok("mindnet", "decompose", "graph", "--execute")

    _evs, _tasks, state = _state()
    assert state.orphans == {}, f"orphaned phase events: {state.orphans}"
    kids = [t for t in _tasks.values() if t.parent_id == "graph"]
    assert kids and kids[0].task_id in state.records


def test_decompose_leaves_the_sidecar_fresh(monkeypatch):
    _fake_decomposer(monkeypatch, "one thing")
    _ok("mindnet", "declare", "graph", "--title", "bikin graph")
    _ok("mindnet", "decompose", "graph", "--execute")
    assert sc_mod.read().fresh is True


def test_decompose_refuses_an_unknown_task(capsys, monkeypatch):
    _fake_decomposer(monkeypatch, "x")
    capsys.readouterr()
    assert _cli("mindnet", "decompose", "ghost") == 2
    assert "no such task" in capsys.readouterr().err


def test_decompose_reports_an_agent_that_never_ran(capsys, monkeypatch):
    """A usage limit must not read as "this task is already atomic"."""
    _fake_decomposer(monkeypatch, "x", executed=False)
    _ok("mindnet", "declare", "graph", "--title", "bikin graph")
    capsys.readouterr()
    assert _cli("mindnet", "decompose", "graph") == 2
    assert capsys.readouterr().err.strip()


def test_decompose_json_is_machine_readable(capsys, monkeypatch):
    _fake_decomposer(monkeypatch, "alpha")
    _ok("mindnet", "declare", "graph", "--title", "bikin graph")
    capsys.readouterr()

    assert _cli("mindnet", "decompose", "graph", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["task_id"] == "graph"
    assert data["declared"] == []
    assert [p["title"] for p in data["proposals"]] == ["alpha"]


def test_decompose_is_on_the_frozen_subcommand_surface():
    assert "decompose" in _subparser_action(_subparser("mindnet")).choices


# --- `hive mindnet brief` — the briefing's inspection surface ------------------
#
# A subcommand rather than an `orchestrate --preview-brief` flag, because
# `__main__.py` had six lines of ratchet headroom and a flag does not fit in six.
# It registers itself, the trade `bench` and `pgcli` already took.

def test_brief_is_on_the_subcommand_surface():
    """The registration itself. `briefcli.register(mnsub)` is two lines in
    `__main__.py`, and two lines are easy to lose in a merge — at which point the
    command silently stops existing."""
    assert "brief" in _subparser_action(_subparser("mindnet")).choices


def test_brief_names_the_task_and_its_counts(capsys):
    """The counts are the point. A brief that arrived empty and a brief that
    never ran look identical in the prompt; they do not look identical here."""
    _orc_graph()
    capsys.readouterr()

    assert _cli("mindnet", "brief", "a") == 0
    out = capsys.readouterr().out
    assert "a " in out
    for label in ("skills=", "sops=", "constraints=", "symbols="):
        assert label in out, f"the counts do not report {label}"


def test_brief_reports_a_gated_source_as_gated_not_as_zero(capsys, monkeypatch):
    """`symbols` ships inert unless `MINDHIVE_SYMBOLS` is set. Printing `0` for a
    source nobody switched on sends the operator looking for a broken index."""
    monkeypatch.delenv("MINDHIVE_SYMBOLS", raising=False)
    _orc_graph()
    capsys.readouterr()

    _cli("mindnet", "brief", "a")
    assert "gated=" in capsys.readouterr().out


def test_brief_refuses_a_missing_task_the_way_its_siblings_do(capsys):
    """The only failure this command has, and it must refuse in the house shape.

    An EMPTY brief is NOT a failure — it is a normal outcome with a reason, and
    exiting non-zero on it would fail a CI step for a task nobody has recorded
    anything about yet. A MISSING task is, and the first version answered it with
    prose on stdout and exit 1 while every sibling answered `error:` on stderr and
    `MN_REFUSED` — so a caller could not tell this command's refusal from the
    others' by either channel or code."""
    from mindhive.mindnet import briefcli as bc_mod
    _orc_graph()
    capsys.readouterr()

    assert _cli("mindnet", "brief", "nope") == cli_mod.MN_REFUSED
    cap = capsys.readouterr()
    assert "no task" in cap.err, "the refusal must reach stderr, like its siblings"
    assert cap.out == "", "nothing on stdout — a --json caller parses that stream"
    # The local copy of the exit code cannot drift from the one `__main__` owns.
    assert bc_mod.REFUSED == cli_mod.MN_REFUSED


def test_brief_refuses_a_missing_task_without_writing_to_the_json_stream(capsys):
    """The refusal fires BEFORE the `--json` branch, so `--json` must not emit
    half a document. The first version printed prose to stdout ahead of that
    branch, which handed a parsing caller a stream that was neither."""
    _orc_graph()
    capsys.readouterr()

    assert _cli("mindnet", "brief", "nope", "--json") == cli_mod.MN_REFUSED
    cap = capsys.readouterr()
    assert cap.out == "", "a --json refusal must leave stdout empty, not partial"
    with pytest.raises(json.JSONDecodeError):
        json.loads(cap.out or "")


def test_brief_json_keys_tasks_by_id_like_its_sibling(capsys):
    """`mindnet list --json` emits `tasks` as an OBJECT keyed by task id. Two
    shapes under one key name in one command family is a discrimination every
    consumer has to make at runtime, so this pins them equal."""
    _orc_graph()
    capsys.readouterr()

    assert _cli("mindnet", "brief", "--json") == 0
    tasks = json.loads(capsys.readouterr().out)["tasks"]
    assert isinstance(tasks, dict), "tasks must be an object, not an array"
    assert "a" in tasks and tasks["a"]["task_id"] == "a"


def test_brief_takes_the_same_scoping_flags_as_every_other_mindnet_command(capsys):
    """`--project` and `--session` are not extras — every sibling takes both.

    Without them argparse rejected the flags outright (exit 2, "unrecognized
    arguments") and `cmd_brief`'s `getattr(args, "project", None)` could only ever
    answer None, so the command was pinned to the default namespace while reading
    as though it were not. The canary below is what makes this non-vacuous: the
    same parse against a flag that really is unknown must still fail."""
    _orc_graph()
    capsys.readouterr()

    assert _cli("mindnet", "brief", "--project", "test") == 0
    assert _cli("mindnet", "brief", "--session", "s1") == 0
    # CANARY: the parser is genuinely capable of rejecting an unknown flag, so
    # the two passes above are the flags being ACCEPTED, not the parse being lax.
    with pytest.raises(SystemExit):
        _cli("mindnet", "brief", "--no-such-flag")


def test_brief_json_carries_the_counts_and_the_reason(capsys):
    """The agent-facing surface. Without the reason as data, an agent seeing an
    empty brief has to guess between 'switched off', 'no context published' and
    'matched nothing'."""
    _orc_graph()
    capsys.readouterr()

    assert _cli("mindnet", "brief", "a", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    row = data["tasks"]["a"]
    assert row["task_id"] == "a"
    for key in ("skills", "sops", "constraints", "symbols", "gated",
                "empty_reason", "phase", "chars"):
        assert key in row, f"the json does not carry {key}"
    assert row["text"] is None, "the full text should be behind --verbose"


def test_brief_verbose_prints_the_block_the_agent_would_receive(capsys):
    """The whole reason the command exists: seeing the actual text, without
    starting an agent to find out."""
    from mindhive.mindnet import brief as br_mod
    _orc_graph()
    capsys.readouterr()

    _cli("mindnet", "brief", "a", "--verbose")
    out = capsys.readouterr().out
    # Either the fence is there, or the reason it is not is stated. Silence is
    # the one answer this command must never give.
    assert br_mod.FENCE_OPEN in out or "empty:" in out
