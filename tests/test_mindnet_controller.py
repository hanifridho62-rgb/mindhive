# tests/test_mindnet_controller.py
"""MN-1 phase state machine: the fold, the transition table, and `restat`.

Written SPEC-FIRST against
``docs/plans/2026-07-25-001-feat-mn1-phase-spine-plan.md`` (KTD3, KTD4, KTD5,
KTD8, KTD11-KTD14) and the committed contracts in `tasks.py` / `phases.py`.
`controller.py` shipped implementation-first with no tests, so no function body
in it was read while these were written — a test derived from an implementation
asserts what the code DOES, which is precisely how a silent bypass survives a
green suite.

Four exploits carry this file. Each one defeated the machine as originally
specced, and each is written here as the REFUSAL it must now produce:

1. **The eight-satisfy bypass** (KTD5). Eight ``satisfy`` events, one per phase,
   all at epoch 0, then seven ``enter`` moves. Every individual move is a legal
   +1 step and every phase being LEFT looks satisfied — so without binding a
   satisfaction to the task's current phase, a task walks the whole pipeline
   with no work done.
2. **The fall-back ratchet** (KTD4). Without an epoch watermark `fall_back` is a
   no-op loop: the old satisfactions survive and two forward moves return the
   task to the phase that just failed, free.
3. **Authority beats replay order** (KTD3). `events.now_iso` is
   second-resolution and `read_all` breaks a same-second tie by SEGMENT NAME,
   i.e. alphabetically by session id. A verdict that depended on order would be
   forgeable by spelling a session id with an earlier letter.
4. **The `pending` regression guard** (KTD12). `pending` is the dataclass
   default for every declared task, so dropping it from the workable set makes
   the machine unusable — nothing could ever take its first step.

Two standing canaries run alongside: the fold is TOTAL (invariant 3 — the
ledger holds history written by every past version of the code, so a malformed
row is skipped or recorded, never raised) and `can_advance` is PURE (asserted by
monkeypatching `events.read_all` to raise).

## A TASK MUST BE DECLARED ON BOTH AXES — read this before writing U7

A `mindnet_task` declare does NOT create a `PhaseRecord`. Only a `mindnet_phase`
``declare`` does (`controller.fold`, create-only). A task declared on the graph
axis alone is in `tasks.fold`, `phase_of` derives its entry phase, and
`hive append mindnet_phase` will happily accept satisfies for it — and every one
of those phase events lands in the ORPHAN bucket, reported with the same shape as
a typo'd task_id.

So `hive mindnet declare` must append BOTH events. A command that writes only the
`mindnet_task` half looks like it works, prints a task, and then silently does
nothing forever. `_declare` below writes both on purpose;
`test_a_task_declared_only_on_the_graph_axis_orphans_its_phase_events` pins the
requirement so it cannot be discovered the hard way.
"""
import pytest

from mindhive import events
from mindhive.mindnet import controller as c_mod
from mindhive.mindnet import phases as ph_mod
from mindhive.mindnet import tasks as t_mod

PHASES = t_mod.PHASES
TASK = "mn1-u4"


# --- helpers -------------------------------------------------------------------

def _declare(task_id=TASK, title=None, ts=None, session="s1", phase_axis=True,
             **kw):
    """Declare a task on BOTH axes — what `hive mindnet declare` (U7) has to do.

    The graph-axis `mindnet_task` declare carries the entry phase and the
    `triviality_bypass`; the phase-axis `mindnet_phase` declare is what
    materializes the `PhaseRecord`. The controller requires BOTH, and a task
    holding only the first orphans every phase event aimed at it — pinned by
    `test_a_task_declared_only_on_the_graph_axis_orphans_its_phase_events`."""
    ev = events.append_event(
        "mindnet_task",
        {"action": "declare", "task_id": task_id,
         "title": title or f"task {task_id}", **kw},
        session_id=session, ts=ts, tags=["mindnet", "task"])
    if phase_axis:
        _phase_ev({"action": "declare", "task_id": task_id,
                   "verified_by": "python"}, ts=ts, session=session)
    return ev


def _status(task_id, status, session="s1"):
    return events.append_event(
        "mindnet_task", {"action": "status", "task_id": task_id,
                         "status": status},
        session_id=session, tags=["mindnet", "task"])


def _phase_ev(payload, ts=None, session="s1"):
    return events.append_event("mindnet_phase", payload, session_id=session,
                               ts=ts, tags=["mindnet", "phase"])


AUTO = object()          # "derive verified_by from the phase descriptor"


def _satisfy(task_id=TASK, phase="brainstorm", epoch=0, result="ok",
             verified_by=AUTO, ref=None, root=None, branch=None, detail=None,
             ts=None, session="s1"):
    """A stored-shape ``satisfy``. ``verified_by`` defaults to the phase's OWN
    required authority, so a refusal in these tests is never accidentally about
    KTD3's authority rule when the point is KTD5's phase binding.

    ``root`` / ``branch`` (KTD7) are written into the evidence object AND at the
    payload top level: `Satisfaction` carries both fields, the contract in
    `phases.py` constrains neither, and which surface the writer uses is not
    specified anywhere authoritative. `test_file_evidence_records_its_resolving_root`
    is the one test that pins it down."""
    desc = ph_mod.PHASE_EVIDENCE[phase]
    kind = desc["kind"]
    if ref is None:
        ref = (desc["required_dir"][0] + f"{phase}-evidence.md"
               if kind == "file" else f"runs/mn-{task_id}-1/out.txt")
    evidence = {"kind": kind, "ref": ref, "result": result}
    if detail is not None:
        evidence["detail"] = detail
    payload = {"action": "satisfy", "task_id": task_id, "phase": phase,
               "epoch": epoch, "evidence": evidence}
    if verified_by is AUTO:
        payload["verified_by"] = desc["verified_by"]
    elif verified_by is not None:
        payload["verified_by"] = verified_by
    for key, value in (("root", root), ("branch", branch)):
        if value is not None:
            evidence[key] = str(value)
            payload[key] = str(value)
    if kind == "event":
        # `execution`'s exit criterion is "a code_change event joins to this
        # task" (KTD16). Emitted alongside so a fold that performs the join is
        # exercised, and a fold that does not is unaffected.
        events.append_event(
            "code_change",
            {"category": "update", "text": f"work for {task_id}",
             "files": ["src/mindhive/mindnet/controller.py"],
             "task_id": task_id},
            session_id=session, ts=ts, tags=["mindnet"])
    return _phase_ev(payload, ts=ts, session=session)


def _enter(task_id=TASK, phase="plan", epoch=None, ts=None, session="s1"):
    payload = {"action": "enter", "task_id": task_id, "phase": phase,
               "verified_by": "python"}
    if epoch is not None:
        payload["epoch"] = epoch
    return _phase_ev(payload, ts=ts, session=session)


def _fall_back(task_id=TASK, phase="execution", detail="audit-bug: 2 failed",
               ts=None, session="s1"):
    """No ``epoch`` on purpose. The plan makes the epoch DERIVED by the fold
    (Assumption 2); echoing a guess here would couple the test to a number the
    caller does not own."""
    return _phase_ev({"action": "fall_back", "task_id": task_id,
                      "phase": phase, "detail": detail,
                      "verified_by": "python"}, ts=ts, session=session)


def _state(evs=None, tasks=None):
    """``(state, tasks, records)`` — the three things every assertion needs."""
    evs = events.read_all() if evs is None else evs
    tasks = t_mod.fold(evs) if tasks is None else tasks
    state = c_mod.fold(evs, tasks)
    return state, tasks, state.records


def _advance(task_id=TASK, to_phase="plan"):
    """``(ok, reason)`` from a freshly folded world."""
    state, tasks, records = _state()
    return c_mod.can_advance(records, tasks, task_id, to_phase)


def _walk_to(target, task_id=TASK, declare=True, session="s1", **kw):
    """Drive *task_id* legitimately to *target*: satisfy the current phase at
    the current epoch, then step +1, repeatedly. Setup only — every test that
    uses it asserts the walk actually landed before asserting anything else, so
    a broken walk fails loudly instead of making the real assertion vacuous."""
    if declare:
        _declare(task_id, session=session, **kw)
    start = t_mod.fold(events.read_all())[task_id].phase
    for i in range(t_mod.phase_index(start), t_mod.phase_index(target)):
        _satisfy(task_id, PHASES[i], epoch=0, session=session)
        _enter(task_id, PHASES[i + 1], session=session)
    return task_id


def _phase_events():
    return [e for e in events.read_all() if e.type == "mindnet_phase"]


# --- the declared surface -------------------------------------------------------

def test_workable_statuses_include_pending_KTD12():
    """KTD12, pinned as a constant-level assertion as well as a behavioural one
    (`test_a_freshly_declared_pending_task_can_take_its_first_step_KTD12`).
    `pending` is `tasks.Task`'s default status, so a later tidy-up that "cleans"
    it out of this tuple silently bricks the machine — nothing could ever take
    its first step."""
    assert t_mod.STATUS_PENDING in c_mod.WORKABLE_STATUSES
    assert t_mod.STATUS_READY in c_mod.WORKABLE_STATUSES
    assert t_mod.STATUS_IN_PROGRESS in c_mod.WORKABLE_STATUSES
    for settled in (t_mod.STATUS_BLOCKED, t_mod.STATUS_COMPLETED,
                    t_mod.STATUS_ABANDONED):
        assert settled not in c_mod.WORKABLE_STATUSES


def test_the_named_gate_phases_are_real_phases_in_the_right_order():
    """Invariant 10 canary. Both constants are compared against folded phase
    values, so a typo here gates nothing and reports nothing."""
    assert c_mod.DESCENDANT_GATE_PHASE in PHASES
    assert c_mod.WORK_PHASE in PHASES
    assert (t_mod.phase_index(c_mod.WORK_PHASE)
            < t_mod.phase_index(c_mod.DESCENDANT_GATE_PHASE))
    assert c_mod.WORK_PHASE == t_mod.TRIVIAL_ENTRY_PHASE


def test_restat_can_never_report_a_failure():
    """KTD7/KTD8: a re-stat that cannot find a file reports `unknown`, never
    `fail` — `detect_project` collapses every worktree onto one namespace, so a
    branch-specific document legitimately does not exist from another checkout.
    The status vocabulary itself makes the wrong answer unsayable."""
    assert c_mod.RESTAT_STATUSES == ("ok", "stale", "unknown")
    assert "fail" not in c_mod.RESTAT_STATUSES


def test_a_declared_task_with_no_phase_events_sits_at_its_entry_phase():
    """`phase_of` falls back to the graph's entry phase, so a task with no
    phase-axis history still reports somewhere rather than nowhere."""
    _declare("plain-1", phase_axis=False)
    _declare("trivial-1", phase_axis=False, triviality_bypass=True)
    state, tasks, records = _state()
    assert records == {}
    assert c_mod.phase_of(records, tasks, "plain-1") == PHASES[0]
    assert c_mod.phase_of(records, tasks, "trivial-1") == \
        t_mod.TRIVIAL_ENTRY_PHASE


def test_a_phase_axis_declare_materializes_the_record_at_the_entry_phase():
    _declare("plain-1")
    _declare("trivial-1", triviality_bypass=True)
    state, tasks, records = _state()
    assert records["plain-1"].phase == PHASES[0]
    assert records["plain-1"].epoch == 0
    assert records["trivial-1"].phase == t_mod.TRIVIAL_ENTRY_PHASE
    assert state.orphans == {}


# --- EXPLOIT 1: the eight-satisfy bypass (KTD5) ---------------------------------
# Not "one big jump to pr-green" — the real hole is subtler. Every `enter` below
# is a legal +1 step, and every phase being LEFT already carries a satisfaction,
# because all eight were appended up front. Only the binding of a satisfaction to
# the task's CURRENT phase at that point in the replay stops the walk.

def _eight_satisfies_then_seven_enters(task_id=TASK):
    _declare(task_id)
    for phase in PHASES:                      # eight satisfies, all at epoch 0
        _satisfy(task_id, phase, epoch=0)
    for phase in PHASES[1:]:                  # seven legal-looking +1 moves
        _enter(task_id, phase)


def test_eight_satisfies_and_seven_enters_do_not_reach_the_terminal_phase():
    _eight_satisfies_then_seven_enters()
    state, tasks, records = _state()
    rec = records[TASK]
    assert rec.phase != PHASES[-1], (
        "the bypass exploit walked the task to the terminal gate with no work")
    # Exactly ONE step is legitimate: brainstorm was the current phase when its
    # satisfy landed, so brainstorm -> plan is earned and everything after it is
    # not.
    assert rec.phase == PHASES[1]


def test_only_the_satisfy_for_the_then_current_phase_is_accepted():
    _eight_satisfies_then_seven_enters()
    state, tasks, records = _state()
    rec = records[TASK]
    assert rec.satisfaction(PHASES[0], 0) is not None
    assert c_mod.satisfied(records, TASK, PHASES[0]) is True
    for phase in PHASES[1:]:
        assert rec.satisfaction(phase, 0) is None, phase
        assert c_mod.satisfied(records, TASK, phase) is False, phase


def test_the_out_of_phase_satisfies_fold_as_recorded_violations():
    """A refused event stays on the ledger forever (appends are immutable), so a
    silent drop would leave the projection unable to explain why it disagrees
    with the chain — `tasks.fold`'s own rule, applied to the phase axis."""
    _eight_satisfies_then_seven_enters()
    state, tasks, records = _state()
    rec = records[TASK]
    assert len(rec.violations) >= len(PHASES) - 1, rec.violations
    assert len(rec.violations) <= t_mod.MAX_VIOLATIONS


def test_the_bypass_exploit_does_not_raise_and_leaves_no_orphan():
    """The fold is total (invariant 3). A test asserting an exception here would
    be asserting the wrong contract."""
    _eight_satisfies_then_seven_enters()
    state, tasks, records = _state()
    assert set(records) == {TASK}
    assert state.orphans == {}


def test_a_legitimate_walk_still_advances_one_phase_at_a_time():
    """The discriminating half. Without this the refusal above could be produced
    by a machine that refuses everything."""
    _walk_to("execution")
    state, tasks, records = _state()
    assert records[TASK].phase == "execution"
    for phase in PHASES[:t_mod.phase_index("execution")]:
        assert c_mod.satisfied(records, TASK, phase) is True, phase


def test_a_forward_jump_of_two_phases_is_refused():
    _declare()
    _satisfy(phase="brainstorm", epoch=0)
    ok, reason = _advance(to_phase="prd")
    assert not ok and reason
    _enter(phase="prd")
    state, tasks, records = _state()
    assert records[TASK].phase == PHASES[0]


def test_a_forward_step_with_no_satisfaction_is_refused():
    _declare()
    ok, reason = _advance(to_phase="plan")
    assert not ok and reason
    _enter(phase="plan")
    state, tasks, records = _state()
    assert records[TASK].phase == PHASES[0]


# --- EXPLOIT 2: the fall-back ratchet (KTD4) ------------------------------------

def test_fall_back_increments_the_epoch():
    _walk_to("audit-bug")
    state, tasks, records = _state()
    assert records[TASK].phase == "audit-bug" and records[TASK].epoch == 0
    _fall_back(phase="execution")
    state, tasks, records = _state()
    assert records[TASK].phase == "execution"
    assert records[TASK].epoch == 1


def test_fall_back_invalidates_every_satisfaction_at_or_past_the_target():
    """The watermark. `execution` and `audit-bug` sit at or past the target and
    die; `prd` and everything before it survives, or a single failing audit
    would force a task to re-earn the whole pipeline."""
    _walk_to("audit-bug")
    _fall_back(phase="execution")
    state, tasks, records = _state()
    target = t_mod.phase_index("execution")
    for phase in PHASES[:target]:
        assert c_mod.satisfied(records, TASK, phase) is True, phase
    for phase in PHASES[target:]:
        assert c_mod.satisfied(records, TASK, phase) is False, phase


def test_two_enters_after_a_fall_back_are_refused_until_new_evidence_lands():
    """Without the epoch, `fall_back` is a no-op loop: the old satisfactions
    survive and these two moves return the task to the phase that just failed
    with nothing fixed."""
    _walk_to("audit-bug")
    _fall_back(phase="execution")
    ok, reason = _advance(to_phase="audit-bug")
    assert not ok and reason
    _enter(phase="audit-bug")
    _enter(phase="audit-code")
    state, tasks, records = _state()
    assert records[TASK].phase == "execution"


def test_re_satisfying_at_the_stale_epoch_does_not_reopen_the_ratchet():
    """The backdating path (H18): replaying the ORIGINAL epoch-0 satisfaction
    must not unlock the move it already paid for once."""
    _walk_to("audit-bug")
    _fall_back(phase="execution")
    _satisfy(phase="execution", epoch=0)          # the stale generation
    ok, reason = _advance(to_phase="audit-bug")
    assert not ok and reason
    _enter(phase="audit-bug")
    state, tasks, records = _state()
    assert records[TASK].phase == "execution"
    assert c_mod.satisfied(records, TASK, "execution") is False


def test_new_evidence_at_the_new_epoch_reopens_the_move():
    """The canary against a vacuous ratchet: a task that redoes the work must be
    able to walk forward again, or every failing task deadlocks."""
    _walk_to("audit-bug")
    _fall_back(phase="execution")
    _satisfy(phase="execution", epoch=1)
    ok, reason = _advance(to_phase="audit-bug")
    assert ok, reason
    _enter(phase="audit-bug")
    state, tasks, records = _state()
    assert records[TASK].phase == "audit-bug"
    assert records[TASK].epoch == 1


def test_a_second_fall_back_increments_the_epoch_again():
    _walk_to("audit-bug")
    _fall_back(phase="execution")
    _satisfy(phase="execution", epoch=1)
    _enter(phase="audit-bug")
    _fall_back(phase="execution", detail="audit-bug: still red")
    state, tasks, records = _state()
    assert records[TASK].epoch == 2
    assert c_mod.satisfied(records, TASK, "execution") is False


@pytest.mark.parametrize("start", PHASES[1:])
def test_backward_movement_is_always_legal_and_never_evidence_gated(start):
    """Backward movement carries NO evidence requirement, from any phase to any
    earlier one. Gating it would deadlock every failing task — and the directive
    that asked for these tests claimed a `fall_back` needs a prior `reject` or
    `audit_failed` event, neither of which exists in `phases.ACTIONS`."""
    _walk_to(start)
    state, tasks, records = _state()
    assert records[TASK].phase == start, "the walk did not land"
    for earlier in PHASES[:t_mod.phase_index(start)]:
        ok, reason = c_mod.can_advance(records, tasks, TASK, earlier)
        assert ok, f"{start} -> {earlier} was refused: {reason}"


def test_a_settled_task_may_still_record_that_it_fell_over():
    """`blocked` / `completed` / `abandoned` may only move backward — and
    backward is never gated, so recording a failure is always possible."""
    _walk_to("audit-bug")
    _status(TASK, t_mod.STATUS_BLOCKED)
    state, tasks, records = _state()
    ok, reason = c_mod.can_advance(records, tasks, TASK, "execution")
    assert ok, reason
    _fall_back(phase="execution")
    state, tasks, records = _state()
    assert records[TASK].phase == "execution" and records[TASK].epoch == 1


# --- EXPLOIT 3: authority beats replay order (KTD3) -----------------------------
# `read_all` sorts by ``(ts, segment_name, line)`` and a segment is named
# ``<session_id>.jsonl``, so a same-second tie between two sessions resolves BY
# SPELLING. These four cases pin the verdict to the authority instead.

SAME_TS = "2026-07-25T12:00:00Z"
DECLARE_TS = "2026-07-25T11:00:00Z"    # strictly before, so the setup replays first
EARLY_SESSION = "aaa-session"          # sorts first
LATE_SESSION = "zzz-session"           # sorts last


def test_the_ledger_really_does_order_same_second_rows_by_session_spelling():
    """Proves the exploit is real and not a strawman. If this ever stops
    holding, the four tests below have stopped discriminating."""
    _phase_ev({"action": "enter", "task_id": TASK, "phase": "plan",
               "verified_by": "python"}, ts=SAME_TS, session=LATE_SESSION)
    _phase_ev({"action": "enter", "task_id": TASK, "phase": "prd",
               "verified_by": "python"}, ts=SAME_TS, session=EARLY_SESSION)
    evs = [e for e in events.read_all() if e.type == "mindnet_phase"]
    assert [e.session_id for e in evs] == [EARLY_SESSION, LATE_SESSION], (
        "same-second rows no longer resolve alphabetically by session id")


@pytest.mark.parametrize("python_first", [True, False])
@pytest.mark.parametrize("python_session,agent_session", [
    (LATE_SESSION, EARLY_SESSION),     # python resolves LAST in replay order
    (EARLY_SESSION, LATE_SESSION),     # python resolves FIRST in replay order
])
def test_a_python_fail_is_never_overridden_by_an_agent_ok(
        python_first, python_session, agent_session):
    """All four combinations: both append orders x both session-id spellings.
    A python `fail` is the verdict whichever way the ledger happens to sort.

    The declare is pinned STRICTLY EARLIER than the contradicting pair. Both
    contradicting records must be ACCEPTED for KTD3 to have anything to resolve,
    and acceptance runs through KTD5's phase binding, which is replay-order
    dependent by construction — so a setup event tied on the same second would
    be testing the deferred `ts` root cause instead of the authority rule."""
    _declare(ts=DECLARE_TS)

    def _py():
        _satisfy(phase="brainstorm", epoch=0, result="fail",
                 verified_by="python", ts=SAME_TS, session=python_session,
                 detail="the spec file is empty")

    def _agent():
        _satisfy(phase="brainstorm", epoch=0, result="ok",
                 verified_by="agent", ts=SAME_TS, session=agent_session)

    (_py(), _agent()) if python_first else (_agent(), _py())

    state, tasks, records = _state()
    verdict = records[TASK].satisfaction("brainstorm", 0)
    assert verdict is not None, "the contradiction folded to nothing at all"
    assert verdict.result == "fail"
    assert verdict.verified_by == "python"
    assert c_mod.satisfied(records, TASK, "brainstorm") is False
    ok, reason = c_mod.can_advance(records, tasks, TASK, "plan")
    assert not ok and reason


def test_an_agent_ok_alone_still_satisfies_a_phase_it_is_trusted_for():
    """The canary. Authority ordering must not degrade into "agents never count"
    — `audit-code` is honestly agent-reported (KTD17), and if this stops passing
    the test above proves nothing about ORDER."""
    _walk_to("audit-code")
    state, tasks, records = _state()
    assert records[TASK].phase == "audit-code"
    assert ph_mod.PHASE_EVIDENCE["audit-code"]["verified_by"] == "agent"
    _satisfy(phase="audit-code", epoch=0, result="ok", verified_by="agent")
    ok, reason = _advance(to_phase="audit-system")
    assert ok, reason


def test_a_verifier_weaker_than_the_phase_requires_folds_as_a_violation():
    """`brainstorm` demands `python`. An agent-reported claim for it is not a
    weaker satisfaction, it is not a satisfaction — KTD3, and the honesty rule
    KTD2 exists to protect."""
    _declare()
    _satisfy(phase="brainstorm", epoch=0, result="ok", verified_by="agent")
    state, tasks, records = _state()
    assert records[TASK].satisfaction("brainstorm", 0) is None
    assert c_mod.satisfied(records, TASK, "brainstorm") is False
    assert records[TASK].violations != ()
    ok, reason = c_mod.can_advance(records, tasks, TASK, "plan")
    assert not ok and reason


def test_a_stored_satisfy_with_no_verified_by_folds_as_a_violation():
    """"A stored `satisfy` lacking `verified_by` … folds as a recorded violation
    and never as a satisfaction" — KTD3, verbatim. `validate_stored` refuses to
    write one, but the ledger already holds rows written by earlier code."""
    _declare()
    _satisfy(phase="brainstorm", epoch=0, verified_by=None)
    state, tasks, records = _state()
    assert records[TASK].satisfaction("brainstorm", 0) is None
    assert records[TASK].violations != ()


def test_a_recorded_fail_does_not_gate_a_forward_move():
    _declare()
    _satisfy(phase="brainstorm", epoch=0, result="fail",
             detail="0 files in docs/superpowers/specs/")
    ok, reason = _advance(to_phase="plan")
    assert not ok and reason


# --- EXPLOIT 4: the `pending` regression guard (KTD12) --------------------------

def test_a_freshly_declared_pending_task_can_take_its_first_step_KTD12():
    """DO NOT "tidy" `pending` out of `WORKABLE_STATUSES`. It is the dataclass
    default for every declared task (`tasks.Task.status`), so excluding it means
    no task can ever take its first forward step and the whole machine is
    unusable. This test exists to fail loudly when that happens."""
    _declare()
    state, tasks, records = _state()
    assert tasks[TASK].status == t_mod.STATUS_PENDING, (
        "the premise moved: a fresh declare is no longer `pending`")
    _satisfy(phase="brainstorm", epoch=0)
    ok, reason = _advance(to_phase="plan")
    assert ok, f"a pending task could not take its first step: {reason}"
    _enter(phase="plan")
    state, tasks, records = _state()
    assert records[TASK].phase == "plan"


@pytest.mark.parametrize("status", [t_mod.STATUS_READY,
                                    t_mod.STATUS_IN_PROGRESS])
def test_the_other_workable_statuses_advance_too(status):
    _declare()
    _status(TASK, status)
    _satisfy(phase="brainstorm", epoch=0)
    ok, reason = _advance(to_phase="plan")
    assert ok, reason


@pytest.mark.parametrize("status", [t_mod.STATUS_BLOCKED,
                                    t_mod.STATUS_COMPLETED,
                                    t_mod.STATUS_ABANDONED])
def test_a_non_workable_status_may_only_fall_back(status):
    _walk_to("prd")
    _satisfy(phase="prd", epoch=0)
    _status(TASK, status)
    state, tasks, records = _state()
    assert records[TASK].phase == "prd", "the walk did not land"
    forward_ok, forward_reason = c_mod.can_advance(records, tasks, TASK,
                                                   "execution")
    assert not forward_ok, f"{status} advanced forward"
    assert forward_reason
    back_ok, back_reason = c_mod.can_advance(records, tasks, TASK, "plan")
    assert back_ok, f"{status} could not fall back: {back_reason}"


def test_the_fold_does_not_apply_the_status_gate_retroactively():
    """Status is an END-STATE fact of a different event stream. Applying it per
    replay step would judge a two-week-old transition against today's graph and
    retroactively unwind a move that was legal when it happened."""
    _walk_to("prd")
    _status(TASK, t_mod.STATUS_BLOCKED)
    state, tasks, records = _state()
    assert records[TASK].phase == "prd"


# --- KTD14: orphans ------------------------------------------------------------

def test_a_phase_event_for_an_undeclared_task_lands_in_the_orphan_bucket():
    """H19 on the phase axis: these events pass `validate_payload`, ride the
    hash chain, and then vanish from every projection with nothing reporting the
    loss. A typo'd task_id must be FINDABLE."""
    _declare("real-1")
    _phase_ev({"action": "enter", "task_id": "ghost-1", "phase": "plan",
               "verified_by": "python"}, ts="2026-07-25T09:00:00Z",
              session="s9")
    state, tasks, records = _state()
    assert "ghost-1" not in records
    assert "ghost-1" in state.orphans, state.orphans
    entry = state.orphans["ghost-1"]
    items = list(entry) if isinstance(entry, (list, tuple)) else [entry]
    assert items, "the orphan bucket key exists but carries nothing"
    first = items[0]
    assert first.task_id == "ghost-1"
    # ts and session_id are the point — they are how an operator finds the typo.
    assert first.ts == "2026-07-25T09:00:00Z"
    assert first.session_id == "s9"
    assert first.reason


def test_an_orphaned_satisfy_is_not_silently_dropped_and_does_not_raise():
    _satisfy("ghost-2", phase="brainstorm", epoch=0)
    state, tasks, records = _state()
    assert records == {}
    assert "ghost-2" in state.orphans


def test_a_task_declared_only_on_the_graph_axis_orphans_its_phase_events():
    """The controller is STRICTER than KTD14 reads. KTD14 buckets events for
    "undeclared task_id"s — a typo. This task is declared: it is in the graph,
    `phase_of` derives its entry phase, and `hive append mindnet_phase` will
    accept satisfies for it. They all orphan anyway, because the record is
    created ONLY by a phase-axis `declare`.

    Pinned rather than judged. It is a defensible two-axis discipline, but it
    means a well-formed satisfy for a real task is reported with the same shape
    as a typo, so U7's `hive mindnet declare` MUST write both events or the
    whole machine silently does nothing."""
    _declare("real-1", phase_axis=False)
    _satisfy("real-1", phase="brainstorm", epoch=0)
    state, tasks, records = _state()
    assert "real-1" in tasks                       # declared on the graph axis
    assert records == {}                           # ...and nowhere on this one
    assert "real-1" in state.orphans
    assert "phase axis" in state.orphans["real-1"][0].reason


def test_a_declared_task_never_produces_an_orphan():
    """The canary: if everything landed in orphans the test above would still
    pass."""
    _walk_to("plan")
    state, tasks, records = _state()
    assert state.orphans == {}
    assert TASK in records


# --- totality and determinism ---------------------------------------------------

TOTALITY_ROWS = {
    "payload is not an object": "just a string",
    "payload is a list": [1, 2, 3],
    "no action": {"task_id": TASK, "phase": "plan"},
    "unknown action": {"action": "teleport", "task_id": TASK, "phase": "plan"},
    "unknown phase": {"action": "enter", "task_id": TASK, "phase": "audit_bug",
                      "verified_by": "python"},
    "phase is not a string": {"action": "enter", "task_id": TASK, "phase": 7,
                              "verified_by": "python"},
    "no task_id": {"action": "enter", "phase": "plan",
                   "verified_by": "python"},
    "task_id is not a string": {"action": "enter", "task_id": 12,
                                "phase": "plan", "verified_by": "python"},
    "enter with no phase": {"action": "enter", "task_id": TASK,
                            "verified_by": "python"},
    # THE STANDING CANARY (plan, U4): an event written by an older version of
    # the code, carrying neither `epoch` nor `detail`, must still fold.
    "satisfy with no epoch and no detail": {
        "action": "satisfy", "task_id": TASK, "phase": "brainstorm",
        "evidence": {"kind": "file",
                     "ref": "docs/superpowers/specs/x-design.md",
                     "result": "ok"},
        "verified_by": "python"},
    "fall_back with no detail": {"action": "fall_back", "task_id": TASK,
                                 "phase": "brainstorm",
                                 "verified_by": "python"},
    "satisfy with no evidence": {"action": "satisfy", "task_id": TASK,
                                 "phase": "brainstorm", "epoch": 0,
                                 "verified_by": "python"},
    "evidence is not an object": {"action": "satisfy", "task_id": TASK,
                                  "phase": "brainstorm", "epoch": 0,
                                  "evidence": "a file somewhere",
                                  "verified_by": "python"},
    "boolean epoch": {"action": "satisfy", "task_id": TASK,
                      "phase": "brainstorm", "epoch": True,
                      "evidence": {"kind": "file", "ref": "docs/x.md",
                                   "result": "ok"},
                      "verified_by": "python"},
    "negative epoch": {"action": "satisfy", "task_id": TASK,
                       "phase": "brainstorm", "epoch": -3,
                       "evidence": {"kind": "file", "ref": "docs/x.md",
                                    "result": "ok"},
                       "verified_by": "python"},
    "bogus verified_by": {"action": "enter", "task_id": TASK, "phase": "plan",
                          "verified_by": "wizard"},
}


@pytest.mark.parametrize("name", sorted(TOTALITY_ROWS))
def test_the_fold_is_total(name):
    """Invariant 3. The ledger holds history written by every past version of
    the code, so a read that raises takes down every projection at once."""
    _declare()
    _phase_ev(TOTALITY_ROWS[name])
    state, tasks, records = _state()            # must not raise
    assert TASK in records or state.orphans is not None
    assert c_mod.phase_of(records, tasks, TASK) == PHASES[0]


@pytest.mark.parametrize("name", sorted(TOTALITY_ROWS))
def test_a_malformed_row_never_advances_a_task(name):
    """Totality is not permissiveness: a row the fold cannot understand must not
    become a free transition."""
    _declare()
    _phase_ev(TOTALITY_ROWS[name])
    state, tasks, records = _state()
    assert c_mod.phase_of(records, tasks, TASK) == PHASES[0]


def test_the_fold_ignores_foreign_event_types():
    _walk_to("plan")
    events.append_event("decision", {"text": "unrelated"}, session_id="s1")
    events.append_event("mindnet_task", {"action": "status",
                                         "task_id": TASK,
                                         "status": t_mod.STATUS_READY},
                        session_id="s1")
    state, tasks, records = _state()
    assert records[TASK].phase == "plan"


def test_the_fold_is_deterministic():
    """Invariant 2: same events in, same records out. No clock, no RNG."""
    _walk_to("prd")
    _fall_back(phase="plan")
    evs = events.read_all()
    tasks = t_mod.fold(evs)
    assert c_mod.fold(evs, tasks) == c_mod.fold(evs, tasks)


def test_the_fold_is_deterministic_across_repeated_reads():
    _walk_to("prd")
    first = c_mod.fold(events.read_all(), t_mod.fold(events.read_all()))
    second = c_mod.fold(events.read_all(), t_mod.fold(events.read_all()))
    assert first == second


def test_satisfaction_predicate_matches_satisfied():
    """`can_complete` takes this injected predicate rather than importing the
    controller (Assumption 1), so the two must not be able to disagree."""
    _walk_to("plan")
    state, tasks, records = _state()
    predicate = c_mod.satisfaction_predicate(records)
    for phase in PHASES:
        assert predicate(TASK, phase) == c_mod.satisfied(records, TASK, phase)
    assert predicate(TASK, "brainstorm") is True
    assert predicate(TASK, PHASES[-1]) is False
    assert predicate("no-such-task", "brainstorm") is False


# --- KTD10: the trivial-entry exception ----------------------------------------
# A bypassed task is BORN at `execution`, so the brainstorm -> execution skip is
# the move it needs to get back after falling over. Permitted iff the bypass is
# set AND no bypassed phase carries a satisfaction.

def test_a_trivial_task_is_born_at_the_work_phase():
    _declare("triv-1", triviality_bypass=True)
    state, tasks, records = _state()
    assert c_mod.phase_of(records, tasks, "triv-1") == t_mod.TRIVIAL_ENTRY_PHASE


def test_trivial_entry_skips_the_method_phases():
    _declare("triv-1", triviality_bypass=True)
    _fall_back("triv-1", phase="brainstorm", detail="rethinking the fix")
    state, tasks, records = _state()
    assert records["triv-1"].phase == "brainstorm", "the fall-back did not land"
    ok, reason = c_mod.can_advance(records, tasks, "triv-1",
                                   t_mod.TRIVIAL_ENTRY_PHASE)
    assert ok, reason
    _enter("triv-1", phase=t_mod.TRIVIAL_ENTRY_PHASE)
    state, tasks, records = _state()
    assert records["triv-1"].phase == t_mod.TRIVIAL_ENTRY_PHASE


def test_trivial_entry_is_refused_once_a_bypassed_phase_carries_evidence():
    """A task that produced a plan is not a trivial task, whatever a later
    bypass claims — otherwise the skip becomes a way to abandon the method
    halfway through and still claim it was never needed."""
    _declare("triv-1", triviality_bypass=True)
    _fall_back("triv-1", phase="brainstorm", detail="rethinking the fix")
    _satisfy("triv-1", phase="brainstorm", epoch=1)
    _enter("triv-1", phase="plan")
    _satisfy("triv-1", phase="plan", epoch=1)
    _fall_back("triv-1", phase="brainstorm", detail="the plan was wrong")
    state, tasks, records = _state()
    assert records["triv-1"].phase == "brainstorm", "setup did not land"
    assert records["triv-1"].history("plan") != (), (
        "the bypassed phase carries no satisfaction — nothing to refuse")
    ok, reason = c_mod.can_advance(records, tasks, "triv-1",
                                   t_mod.TRIVIAL_ENTRY_PHASE)
    assert not ok and reason


def test_a_non_trivial_task_may_never_skip_to_the_work_phase():
    _declare()
    _satisfy(phase="brainstorm", epoch=0)
    ok, reason = _advance(to_phase=t_mod.TRIVIAL_ENTRY_PHASE)
    assert not ok and reason


# --- KTD13: parent and child phases are coupled --------------------------------

def _parent_and_child(parent="p-1", child="c-1"):
    _declare(parent)
    _declare(child, parent_id=parent)
    return parent, child


def test_a_parent_cannot_enter_the_audit_gate_over_an_unstarted_descendant():
    """Otherwise a parent reports its bug audit green precisely because its
    child's code was never written."""
    parent, child = _parent_and_child()
    _walk_to("execution", task_id=parent, declare=False)
    _satisfy(parent, phase="execution", epoch=0)
    state, tasks, records = _state()
    assert records[parent].phase == "execution", "the walk did not land"
    assert c_mod.phase_of(records, tasks, child) == PHASES[0]
    ok, reason = c_mod.can_advance(records, tasks, parent,
                                   c_mod.DESCENDANT_GATE_PHASE)
    assert not ok, "the parent passed the audit gate over an unstarted child"
    assert reason
    # `coupling_violations` is a DIFFERENT rule (see the inversion tests below):
    # it reports a child running ahead of its parent. A parent that has not yet
    # tried to pass the gate is not an inversion, so it must stay out of it.
    assert c_mod.coupling_violations(tasks, records) == {}


def test_the_audit_gate_opens_once_the_descendant_reaches_the_work_phase():
    """Canary: the coupling check must not refuse everything forever."""
    parent, child = _parent_and_child()
    _walk_to("execution", task_id=parent, declare=False)
    _satisfy(parent, phase="execution", epoch=0)
    _walk_to("execution", task_id=child, declare=False)
    state, tasks, records = _state()
    assert records[child].phase == "execution", "the child walk did not land"
    ok, reason = c_mod.can_advance(records, tasks, parent,
                                   c_mod.DESCENDANT_GATE_PHASE)
    assert ok, reason
    assert parent not in c_mod.coupling_violations(tasks, records)


def test_a_parent_cannot_reach_the_terminal_phase_over_an_open_descendant():
    parent, child = _parent_and_child()
    _walk_to("audit-system", task_id=parent, declare=False)
    _satisfy(parent, phase="audit-system", epoch=0)
    _walk_to("execution", task_id=child, declare=False)
    state, tasks, records = _state()
    assert records[parent].phase == "audit-system", "the walk did not land"
    ok, reason = c_mod.can_advance(records, tasks, parent, PHASES[-1])
    assert not ok, "the parent went PR-green over an open child"
    assert reason


def test_a_closed_descendant_does_not_block_the_terminal_phase():
    """Abandoned and completed work is settled, not outstanding — without the
    exemption an abandoned subtask would block its parent forever."""
    parent, child = _parent_and_child()
    _walk_to("audit-system", task_id=parent, declare=False)
    _satisfy(parent, phase="audit-system", epoch=0)
    _walk_to("execution", task_id=child, declare=False)
    _status(child, t_mod.STATUS_ABANDONED)
    state, tasks, records = _state()
    ok, reason = c_mod.can_advance(records, tasks, parent, PHASES[-1])
    assert ok, reason


def test_the_coupling_check_reaches_a_grandchild():
    """Transitive on purpose: if only the immediate child counted, the coupling
    would be escaped by nesting one level deeper."""
    _declare("p-1")
    _declare("c-1", parent_id="p-1")
    _declare("g-1", parent_id="c-1")
    _walk_to("execution", task_id="p-1", declare=False)
    _satisfy("p-1", phase="execution", epoch=0)
    _walk_to("execution", task_id="c-1", declare=False)
    state, tasks, records = _state()
    assert c_mod.phase_of(records, tasks, "g-1") == PHASES[0]
    ok, reason = c_mod.can_advance(records, tasks, "p-1",
                                   c_mod.DESCENDANT_GATE_PHASE)
    assert not ok and reason


# --- the parent/child phase INVERSION (the reparent check U2 deferred) ------------
# A separate rule from KTD13's descendant gate above, and narrower than "the
# parent is behind the child" — KTD13 REQUIRES descendants to finish first, so a
# bare index comparison would freeze every correctly-built tree. What is unsound
# is a child at or past the work boundary under a parent that never reached it:
# the parent's plan never described this work. Reparenting a finished subtree
# under a fresh parent is how that gets manufactured.

def test_a_child_past_the_work_phase_under_an_unplanned_parent_is_an_inversion():
    parent, child = _parent_and_child()
    _walk_to(c_mod.WORK_PHASE, task_id=child, declare=False)
    state, tasks, records = _state()
    assert records[child].phase == c_mod.WORK_PHASE, "the child walk did not land"
    assert c_mod.phase_of(records, tasks, parent) == PHASES[0]
    assert child in c_mod.coupling_violations(tasks, records)


def test_only_the_inverted_child_is_frozen_not_its_parent():
    """Blocking the parent too would deadlock the one move that fixes the
    inversion — the parent catching up."""
    parent, child = _parent_and_child()
    _walk_to(c_mod.WORK_PHASE, task_id=child, declare=False)
    _satisfy(parent, phase="brainstorm", epoch=0)
    _satisfy(child, phase=c_mod.WORK_PHASE, epoch=0)
    state, tasks, records = _state()
    child_ok, child_reason = c_mod.can_advance(records, tasks, child,
                                               c_mod.DESCENDANT_GATE_PHASE)
    assert not child_ok, "the inverted child advanced"
    assert child_reason
    parent_ok, parent_reason = c_mod.can_advance(records, tasks, parent, "plan")
    assert parent_ok, f"the parent could not catch up: {parent_reason}"


def test_a_correctly_built_tree_is_never_an_inversion():
    """The canary. KTD13 mandates children finishing ahead of their parent, so
    an over-broad inversion rule would flag the flow the spec requires."""
    parent, child = _parent_and_child()
    _walk_to("audit-bug", task_id=parent, declare=False)
    _walk_to("pr-green", task_id=child, declare=False)
    state, tasks, records = _state()
    assert records[child].phase == "pr-green"
    assert c_mod.coupling_violations(tasks, records) == {}


# --- KTD12: subtree soundness ---------------------------------------------------

def test_a_check_graph_violation_in_the_subtree_blocks_forward_transitions():
    """`check_graph` gets its first consumer anywhere in `src/`. A dangling
    dependency is the violation that survives `tasks.fold` (cycles are refused
    outright), so it is the one this can actually be built from."""
    _declare("p-1")
    _declare("c-1", parent_id="p-1")
    events.append_event("mindnet_task",
                        {"action": "depend", "task_id": "c-1",
                         "on": "ghost-1"},
                        session_id="s1", tags=["mindnet", "task"])
    _walk_to("brainstorm", task_id="p-1", declare=False)
    _satisfy("p-1", phase="brainstorm", epoch=0)
    state, tasks, records = _state()
    assert t_mod.check_graph(tasks) != [], "the graph is sound — nothing to gate"
    assert "c-1" in c_mod.unsound_tasks(tasks), c_mod.unsound_tasks(tasks)
    ok, reason = c_mod.can_advance(records, tasks, "p-1", "plan")
    assert not ok, "an unsound subtree advanced"
    assert reason


def test_an_unsound_task_cannot_advance_itself():
    _declare("c-1")
    events.append_event("mindnet_task",
                        {"action": "depend", "task_id": "c-1",
                         "on": "ghost-1"},
                        session_id="s1", tags=["mindnet", "task"])
    _satisfy("c-1", phase="brainstorm", epoch=0)
    state, tasks, records = _state()
    ok, reason = c_mod.can_advance(records, tasks, "c-1", "plan")
    assert not ok and reason


def test_subtree_unsoundness_is_scoped_to_the_subtree():
    """The discriminating half: one bad edge must not freeze the whole graph."""
    _declare("bad-1")
    events.append_event("mindnet_task",
                        {"action": "depend", "task_id": "bad-1",
                         "on": "ghost-1"},
                        session_id="s1", tags=["mindnet", "task"])
    _declare("good-1")
    _satisfy("good-1", phase="brainstorm", epoch=0)
    state, tasks, records = _state()
    assert "bad-1" in c_mod.unsound_tasks(tasks)
    assert "good-1" not in c_mod.unsound_tasks(tasks)
    ok, reason = c_mod.can_advance(records, tasks, "good-1", "plan")
    assert ok, reason


def test_unsound_tasks_is_empty_for_a_sound_graph():
    _declare("p-1")
    _declare("c-1", parent_id="p-1")
    tasks = t_mod.fold(events.read_all())
    assert t_mod.check_graph(tasks) == []
    assert c_mod.unsound_tasks(tasks) == {}


# --- KTD8 / KTD7: restat ---------------------------------------------------------
# `restat` is the ONE impure function here and owns the sole append. Its root and
# branch are injectable, so these tests never shell out to git.

BRANCH = "claude/mindnetwork-system-init-30cb2a"
# Ordering is pinned, not left to the clock. `now_iso` is second-resolution and
# `read_all` breaks a tie by SEGMENT NAME, while `restat` writes to the `manual`
# segment — so an unpinned setup races its own re-stat. Same-second setup rows
# share one segment and stay line-ordered; the re-stat lands strictly later.
SETUP_TS = "2026-07-25T10:00:00Z"
RESTAT_TS = "2026-07-25T11:00:00Z"

FILE_PHASES = tuple(ph for ph, d in ph_mod.PHASE_EVIDENCE.items()
                    if d["kind"] == "file")


def _EVIDENCE_FILE(root, phase):
    """The path `_satisfy` builds a ref for, resolved under *root*."""
    return root / ph_mod.PHASE_EVIDENCE[phase]["required_dir"][0] / \
        f"{phase}-evidence.md"


def _worktree(tmp_path, name="wt"):
    root = tmp_path / name
    for phase in FILE_PHASES:
        target = _EVIDENCE_FILE(root, phase)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# evidence\n", encoding="utf-8")
    return root


def _walk_with_file_evidence(target, root, task_id=TASK, branch=BRANCH,
                             ts=SETUP_TS):
    """Like `_walk_to`, but every satisfaction records the resolving worktree
    root and branch (KTD7) so a later re-stat has something to compare — and the
    TARGET phase carries its own satisfaction, because `restat` re-checks the
    current phase's evidence and nothing else."""
    assert target in FILE_PHASES, f"{target} takes no re-stat-able evidence"
    _declare(task_id, ts=ts)
    for i in range(t_mod.phase_index(target)):
        _satisfy(task_id, PHASES[i], epoch=0, root=root, branch=branch, ts=ts)
        _enter(task_id, PHASES[i + 1], ts=ts)
    _satisfy(task_id, target, epoch=0, root=root, branch=branch, ts=ts)
    return task_id


def test_file_evidence_records_its_resolving_root_and_branch(tmp_path):
    """KTD7. Everything below depends on this being captured — if the writer
    puts the root somewhere else, the re-stat tests are measuring nothing, so
    this failure must be reported on its own."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("plan", root)
    state, tasks, records = _state()
    sat = records[TASK].satisfaction("brainstorm", 0)
    assert sat is not None
    assert sat.root == str(root), (
        "the fold did not capture the resolving worktree root")
    assert sat.branch == BRANCH


def test_restat_of_a_present_file_reports_ok_and_appends_nothing(tmp_path):
    """The canary against a vacuously stale re-stat."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root)
    before = len(_phase_events())
    result = c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    assert result.status == "ok", result.reason
    assert result.event is None
    assert len(_phase_events()) == before


def test_restat_of_a_missing_file_marks_the_phase_stale(tmp_path):
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root)
    _EVIDENCE_FILE(root, "prd").unlink()
    result = c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    assert result.status == "stale", result.reason
    assert result.event is not None
    assert result.reason


def test_a_failed_restat_never_moves_the_current_phase(tmp_path):
    """KTD8, the headline. A task at `prd` must not silently regress with no
    `fall_back` anywhere on the ledger — the epoch is the only thing allowed to
    move a task backward, and it only moves on a recorded backward transition."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root)
    _EVIDENCE_FILE(root, "prd").unlink()
    c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    state, tasks, records = _state()
    assert records[TASK].phase == "prd"
    assert records[TASK].epoch == 0


def test_a_stale_phase_blocks_the_forward_transition_it_paid_for(tmp_path):
    """Otherwise the re-stat reports a problem and changes nothing."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root)
    state, tasks, records = _state()
    ok, _ = c_mod.can_advance(records, tasks, TASK, "execution")
    assert ok, "the walk did not leave a satisfied `prd` to invalidate"
    _EVIDENCE_FILE(root, "prd").unlink()
    c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    state, tasks, records = _state()
    assert records[TASK].satisfaction("prd", 0).stale is True
    ok, reason = c_mod.can_advance(records, tasks, TASK, "execution")
    assert not ok, "a stale phase still gated a forward transition"
    assert reason


def test_two_consecutive_restats_append_exactly_one_event(tmp_path):
    """KTD8's second constraint: the re-stat may not spam the ledger. It is
    emitted only when the folded record does not already carry the same failure
    for the same ref — and `hive mindnet show` calls it, so a guard that fails
    open grows the hash chain once per look."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root)
    _EVIDENCE_FILE(root, "prd").unlink()
    before = len(_phase_events())
    first = c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    second = c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    third = c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    assert first.status == "stale", first.reason
    assert second.status == "stale", second.reason
    assert third.status == "stale", third.reason
    assert second.event is None, "the second re-stat appended a duplicate"
    assert third.event is None, "the third re-stat appended a duplicate"
    assert len(_phase_events()) == before + 1


def test_a_restat_whose_own_event_the_fold_rejects_appends_without_end(tmp_path):
    """KNOWN FAILING — a real, unbounded-growth defect, reported not fixed.

    `restat`'s idempotence guard is `sat.ref in sat.failed_refs`, i.e. it asks
    the FOLD whether the failure it just wrote is already recorded. The guard is
    therefore only as good as the fold's acceptance of `restat`'s own write, and
    it fails OPEN: whenever that write is not folded back, every call appends
    again, forever.

    This is reachable today. `restat` appends with `session_id=None` (segment
    ``manual.jsonl``) and `ts=now_iso()`, which is second-resolution, while
    `read_all` sorts by ``(ts, segment_name, line)``. So a re-stat landing in the
    same wall-clock second as the evidence it is re-checking, from any session
    whose id sorts after ``manual`` (``s1``, ``session-*``, ``run-*`` — most of
    them), replays BEFORE the `enter` that put the task in that phase. Its own
    `fail` becomes an orphan of its own task, `failed_refs` stays empty, and:
    the phase is never marked stale, `can_advance` keeps letting the task
    through, and the ledger grows by one event per `hive mindnet show`.

    The `ts` root cause is deferred by the plan (Scope Boundaries), but the
    FAIL-OPEN GUARD is not: an append gate that only holds when an unrelated
    ordering assumption holds is the defect this pins."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root, ts=SETUP_TS)
    _EVIDENCE_FILE(root, "prd").unlink()
    before = len(_phase_events())
    for _ in range(4):
        c_mod.restat(TASK, root=root, branch=BRANCH, ts=SETUP_TS)
    assert len(_phase_events()) == before + 1, (
        "restat appended once per call — its idempotence guard failed open")


def test_a_restat_from_another_session_still_appends_exactly_once(tmp_path):
    """The durability case the guard exists for, and the one a process-local or
    same-session fix would miss.

    `restat` runs from a DIFFERENT session than the one that advanced the task,
    in the same second, with a session id that sorts EARLIER — so its own write
    replays ahead of the `enter` it answers and the fold will not carry it back.
    The guard must hold anyway, because it asks the raw ledger rather than the
    folded record or the previous call's return value."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root, ts=SETUP_TS)      # session "s1"
    _EVIDENCE_FILE(root, "prd").unlink()
    before = len(_phase_events())
    other = "aaa-other-session"                             # sorts before "s1"
    first = c_mod.restat(TASK, root=root, branch=BRANCH, ts=SETUP_TS,
                         session_id=other)
    second = c_mod.restat(TASK, root=root, branch=BRANCH, ts=SETUP_TS,
                          session_id=other)
    assert first.event is not None, "the first re-stat recorded nothing"
    assert first.event.session_id == other, (
        "restat did not honour the caller's session — the segment is the "
        "store's join key")
    assert second.event is None, "the cross-session re-stat appended twice"
    assert len(_phase_events()) == before + 1


def test_a_rejected_fail_by_a_weaker_authority_does_not_suppress_the_restat(
        tmp_path):
    """The hole a bare "any fail exists" guard would open. `failed_refs` is built
    from ACCEPTED records only, so an agent-reported `fail` for a python-verified
    phase is invisible to the fold and gates nothing. It must not stand in for
    the python-verified finding `restat` is there to record."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root, ts=SETUP_TS)
    ref = "docs/prds/prd-evidence.md"
    _satisfy(TASK, phase="prd", epoch=0, result="fail", verified_by="agent",
             ref=ref, root=root, branch=BRANCH, ts=SETUP_TS)
    state, tasks, records = _state()
    assert records[TASK].satisfaction("prd", 0).result == "ok", (
        "the weak fail was accepted — this test no longer discriminates")
    _EVIDENCE_FILE(root, "prd").unlink()
    before = len(_phase_events())
    result = c_mod.restat(TASK, root=root, branch=BRANCH, ts=RESTAT_TS)
    assert result.event is not None, (
        "a fail the fold rejects suppressed the real finding")
    assert len(_phase_events()) == before + 1


def test_restat_from_a_different_worktree_root_reports_unknown(tmp_path):
    """KTD7. `detect_project` deliberately collapses every worktree onto one
    namespace (SOP 10), so a branch-specific plan file legitimately does not
    exist from another checkout. Calling that `fail` would mark honest work as
    broken."""
    root = _worktree(tmp_path)
    other = _worktree(tmp_path, name="other-checkout")
    _walk_with_file_evidence("prd", root)
    result = c_mod.restat(TASK, root=other, branch=BRANCH, ts=RESTAT_TS)
    assert result.status == "unknown", result.reason
    assert result.status != "fail"
    assert result.event is None


def test_restat_from_a_different_branch_reports_unknown(tmp_path):
    """The other half of KTD7: same checkout, different branch."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root)
    _EVIDENCE_FILE(root, "prd").unlink()
    result = c_mod.restat(TASK, root=root, branch="some-other-branch",
                          ts=RESTAT_TS)
    assert result.status == "unknown", result.reason
    assert result.event is None


def test_restat_defaults_to_the_live_worktree_root(tmp_path):
    """`root=None` resolves the CURRENT checkout, which is not the one that
    recorded this evidence — so the answer is `unknown` and nothing is written.
    Never `fail`, and never a stale mark against an innocent task."""
    root = _worktree(tmp_path)
    _walk_with_file_evidence("prd", root)
    result = c_mod.restat(TASK, root=None, branch=None, ts=RESTAT_TS)
    assert result.status == "unknown", result.reason
    assert result.event is None


def test_restat_of_an_unknown_task_is_total(tmp_path):
    root = _worktree(tmp_path)
    _declare("real-1")
    result = c_mod.restat("no-such-task", root=root, branch=BRANCH)
    assert result.status in c_mod.RESTAT_STATUSES
    assert result.event is None
    assert result.reason


# --- purity: the hook path must never read the ledger ----------------------------

def test_can_advance_does_no_io(monkeypatch):
    """KTD8 keeps `can_advance` pure so the injection hook can call it without
    paying for a ledger read. Monkeypatching `read_all` to raise is the only
    assertion that cannot be satisfied by a fast implementation that still
    reads."""
    _declare()
    _satisfy(phase="brainstorm", epoch=0)
    evs = events.read_all()
    tasks = t_mod.fold(evs)
    records = c_mod.fold(evs, tasks).records

    def boom(*a, **kw):
        raise AssertionError("can_advance read the ledger")

    monkeypatch.setattr(events, "read_all", boom)
    ok, reason = c_mod.can_advance(records, tasks, TASK, "plan")
    assert ok, reason


def test_the_predicates_do_no_io(monkeypatch):
    _walk_to("plan")
    evs = events.read_all()
    tasks = t_mod.fold(evs)
    state = c_mod.fold(evs, tasks)

    def boom(*a, **kw):
        raise AssertionError("a pure predicate read the ledger")

    monkeypatch.setattr(events, "read_all", boom)
    assert c_mod.satisfied(state.records, TASK, "brainstorm") is True
    assert c_mod.phase_of(state.records, tasks, TASK) == "plan"
    assert c_mod.unsound_tasks(tasks) == {}
    assert c_mod.coupling_violations(tasks, state.records) == {}
    assert c_mod.satisfaction_predicate(state.records)(TASK, "brainstorm")


def test_the_fold_does_no_io(monkeypatch):
    _walk_to("plan")
    evs = events.read_all()
    tasks = t_mod.fold(evs)

    def boom(*a, **kw):
        raise AssertionError("the fold read the ledger")

    monkeypatch.setattr(events, "read_all", boom)
    assert c_mod.fold(evs, tasks).records[TASK].phase == "plan"
