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
    surface = _subparser_action(_subparser("mindnet")).choices
    assert set(surface) == {"declare", "enter", "satisfy", "fall-back",
                            "validate", "restat", "show", "list", "rebuild"}
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
