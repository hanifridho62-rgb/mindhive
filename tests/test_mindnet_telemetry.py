# tests/test_mindnet_telemetry.py
"""MN-1 U10 — the faculty activity feed: `mindnet_activity`'s first producer.

Written TEST-FIRST against
``docs/plans/2026-07-25-001-feat-mn1-phase-spine-plan.md`` U10, the
`mindnet_activity` registration comment in `events.py`, and §MN-5 of
``docs/superpowers/specs/2026-07-25-mindnetwork-architecture-decomposition.md``.
No line of `telemetry.py` existed when these were written.

Three properties carry the file, and each one exists because MN-5's defining
claim — **a missing cat means a real bug** (R16) — is only falsifiable if the
feed cannot go missing for any OTHER reason:

1. **Ledger-derived, never in-memory.** The decomposition's first hard
   constraint. MindSwarm's mission state is a plain instance attribute that dies
   with the server; a cat board built the same way would blink out on every
   restart and R16 would be untestable noise.
   `test_the_state_is_read_from_the_ledger_and_not_from_memory` asserts it the
   only honest way: emit, then make the ledger read return nothing, and watch
   the state vanish with it. A module that had cached the emits would pass a
   round-trip test and fail this one.

2. **Best-effort — the observer must never disturb the observed.** A phase
   transition that succeeded must not be reported as failed because a cat
   animation could not be logged. `promptgate.record_direction`
   (`promptgate.py:445-470`) is the committed idiom: swallow, return None.
   Tested in BOTH directions — a telemetry failure must not escape into the
   caller, and (via `busy`) a CALLER failure must not be swallowed by telemetry.

3. **One canonical faculty set, verbatim** (invariant 10). The brief that
   requested this unit proposed `superpower_brain` and `code_auditor`; both are
   inventions. Three independent guards below pin the seven real names to the
   two places that already record them — the decomposition spec and
   `events.py`'s own registration comment — so a synonym fails loudly in this
   file rather than quietly in the UI six weeks later.
"""
import importlib
import re
from pathlib import Path

import pytest

from mindhive import events
from mindhive.mindnet import tasks as t_mod
from mindhive.mindnet import telemetry as tel

REPO = Path(__file__).resolve().parents[1]
DECOMPOSITION = (REPO / "docs" / "superpowers" / "specs"
                 / "2026-07-25-mindnetwork-architecture-decomposition.md")

TASK = "mn1-u10"

# The seven canonical faculty slugs, written out here ON PURPOSE rather than
# imported. A test that asserts `FACULTIES == FACULTIES` proves nothing; this
# literal is the independent copy that makes the assertion mean something.
CANONICAL = ("skill-plugin", "sop", "improvements", "history", "task-graph",
             "compatibility", "pr-green")


# --- helpers -------------------------------------------------------------------

def _ev(payload, ts="2026-07-27T00:00:00Z", type_="mindnet_activity"):
    """An in-memory Event row, so a fold test never needs the store."""
    return events.Event(ts=ts, session_id="s", type=type_, payload=payload,
                        tags=[])


def _rows(project=None):
    """Every `mindnet_activity` event actually on the ledger."""
    return [e for e in events.read_all(project) if e.type == "mindnet_activity"]


def _faculties_in_registration_comment():
    """The faculty slugs `events.py`'s ``mindnet_activity`` comment names.

    Parsed from the source rather than trusted, because the comment IS the
    contract that registration entry advertises to the next reader."""
    src = Path(events.__file__).read_text(encoding="utf-8")
    block = re.search(r"((?:^\s*#.*\n)+)\s*\"mindnet_activity\",", src,
                      re.MULTILINE)
    assert block, ("no comment block precedes the mindnet_activity "
                   "registration in events.py — the guard below has nothing "
                   "to compare against")
    text = " ".join(ln.strip().lstrip("#").strip()
                    for ln in block.group(1).splitlines())
    names = re.search(r"idle\s*\(([^)]+)\)", text)
    assert names, f"no faculty list in the registration comment: {text!r}"
    return tuple(n.strip() for n in names.group(1).split(",") if n.strip())


def _faculties_in_decomposition():
    """§MN-5's faculty list, normalised from its display spelling.

    ``Skill & Plugin`` -> ``skill-plugin``, ``Task Graph`` -> ``task-graph``.
    The doc writes them for humans; the code writes them for string equality,
    and invariant 10 says the two must not drift apart."""
    text = DECOMPOSITION.read_text(encoding="utf-8")
    m = re.search(r"awake MindNetwork faculty\*\*\s*[—-]\s*(.*?)\s*[—-]\s*each",
                  text, re.DOTALL)
    assert m, ("§MN-5's faculty list did not parse — if the spec was reworded, "
               "re-derive this guard rather than deleting it")
    return tuple(n.strip().lower().replace(" & ", "-").replace(" ", "-")
                 for n in m.group(1).split(",") if n.strip())


# --- the canonical set (invariant 10) ---------------------------------------------

def test_the_canonical_tuple_is_the_seven_names_verbatim_and_in_order():
    assert tel.FACULTIES == CANONICAL


def test_the_canonical_tuple_matches_the_event_registration_comment():
    assert set(tel.FACULTIES) == set(_faculties_in_registration_comment())


def test_the_canonical_tuple_matches_the_decomposition_spec():
    assert set(tel.FACULTIES) == set(_faculties_in_decomposition())


def test_both_cross_file_guards_actually_found_seven_names():
    """The canary. Without it, a parser that silently matched nothing would
    make the two guards above pass by comparing empty sets."""
    assert len(_faculties_in_registration_comment()) == 7
    assert len(_faculties_in_decomposition()) == 7


@pytest.mark.parametrize("invented", ["superpower_brain", "code_auditor",
                                      "skill_plugin", "task_graph", "taskgraph",
                                      "pr_green", "SOP"])
def test_an_invented_faculty_name_is_not_canonical(invented):
    """The named inventions plus the underscore/case spellings a caller would
    reach for. Each one is a cat that never appears; R16 would read it as a bug
    in the faculty rather than a typo in the emit."""
    assert invented not in tel.FACULTIES
    assert tel.emit(invented, tel.STATUS_ACTIVE) is None


def test_the_two_statuses_are_active_and_idle():
    assert tel.STATUSES == ("active", "idle")


# --- emit ---------------------------------------------------------------------------

def test_an_emit_appends_exactly_one_activity_event():
    ev = tel.emit("sop", tel.STATUS_ACTIVE)
    assert ev is not None
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].type == "mindnet_activity"
    assert rows[0].payload == {"faculty": "sop", "status": "active"}


@pytest.mark.parametrize("faculty", CANONICAL)
@pytest.mark.parametrize("status", ["active", "idle"])
def test_every_canonical_faculty_emits_in_both_statuses(faculty, status):
    assert tel.emit(faculty, status) is not None
    assert [(r.payload["faculty"], r.payload["status"]) for r in _rows()] == \
        [(faculty, status)]


def test_the_optional_task_id_and_phase_ride_the_payload():
    tel.emit("task-graph", tel.STATUS_ACTIVE, task_id=TASK, phase="execution")
    assert _rows()[0].payload == {"faculty": "task-graph", "status": "active",
                                  "task_id": TASK, "phase": "execution"}


def test_the_event_is_tagged_for_the_mindnet_feed():
    tel.emit("history", tel.STATUS_IDLE)
    assert _rows()[0].tags == ["mindnet", "activity", "auto"]


def test_a_faculty_outside_the_canonical_set_is_refused_without_raising():
    """Refused means: return None, append NOTHING, raise nothing. A rejection
    that raised would be a telemetry write disturbing its caller — property 2 —
    and one that appended anyway would put a name in the ledger that MN-5 has no
    cat for."""
    assert tel.emit("superpower_brain", tel.STATUS_ACTIVE) is None
    assert _rows() == []


@pytest.mark.parametrize("status", ["awake", "ACTIVE", "", None, True, 1])
def test_an_unknown_status_is_refused_the_same_way(status):
    assert tel.emit("sop", status) is None
    assert _rows() == []


@pytest.mark.parametrize("faculty", [None, "", 3, ["sop"], object()])
def test_a_non_string_faculty_is_refused_rather_than_coerced(faculty):
    assert tel.emit(faculty, tel.STATUS_ACTIVE) is None
    assert _rows() == []


@pytest.mark.parametrize("task_id,phase,survives", [
    ("Not A Task Id", "execution", {"phase": "execution"}),
    (TASK, "shipping", {"task_id": TASK}),
    (17, None, {}),
    (None, "PR-GREEN", {}),
    ("", "", {}),
])
def test_a_malformed_optional_field_is_dropped_and_the_event_still_lands(
        task_id, phase, survives):
    """The deliberate asymmetry: a bad REQUIRED field refuses the event, a bad
    OPTIONAL one is dropped and the event still lands.

    Both fields are decoration on the cat's detail panel; the faculty and its
    status are the signal R16 rests on. Refusing the whole event over a
    mistyped task_id would make a cat vanish and send someone hunting a
    faculty bug that does not exist — the exact false positive R16 exists to
    rule out.

    ``survives`` makes the drop TARGETED rather than wholesale: the valid
    companion field in each pair must still be on the payload, so "drop the bad
    one" cannot be satisfied by dropping both."""
    ev = tel.emit("improvements", tel.STATUS_ACTIVE, task_id=task_id,
                  phase=phase)
    assert ev is not None
    assert _rows()[0].payload == {"faculty": "improvements",
                                  "status": "active", **survives}


# --- best-effort: the observer never disturbs the observed --------------------------

def test_an_append_failure_returns_none_and_never_propagates(monkeypatch):
    def boom(*a, **kw):
        raise OSError("ledger segment is read-only")

    monkeypatch.setattr(events, "append_event", boom)
    assert tel.emit("sop", tel.STATUS_ACTIVE) is None


def test_a_transition_that_succeeded_is_still_reported_as_succeeding(
        monkeypatch):
    """The whole reason property 2 exists, spelled as the scenario it protects:
    a phase transition must not be reported as failed because a cat animation
    could not be logged."""
    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(events, "append_event", boom)

    def transition():                      # stands in for controller/executor
        tel.emit("pr-green", tel.STATUS_ACTIVE, task_id=TASK)
        return "moved"

    assert transition() == "moved"


def test_busy_emits_active_then_idle():
    with tel.busy("compatibility", task_id=TASK):
        assert [r.payload["status"] for r in _rows()] == ["active"]
    assert [r.payload["status"] for r in _rows()] == ["active", "idle"]
    assert {r.payload["faculty"] for r in _rows()} == {"compatibility"}


def test_busy_goes_idle_even_when_the_observed_work_raises():
    """A cat stuck awake forever is the same false signal as a missing one, and
    it is the failure mode every hand-written try/finally eventually forgets."""
    with pytest.raises(ValueError):
        with tel.busy("task-graph"):
            raise ValueError("the validation run blew up")
    assert [r.payload["status"] for r in _rows()] == ["active", "idle"]


def test_busy_never_swallows_the_callers_exception():
    """Property 2 in the OTHER direction. Telemetry that ate a real error would
    disturb the observed far more than a missing cat ever could."""
    with pytest.raises(KeyError, match="boom"):
        with tel.busy("sop"):
            raise KeyError("boom")


def test_busy_runs_its_block_even_when_every_emit_fails(monkeypatch):
    def boom(*a, **kw):
        raise OSError("nope")

    monkeypatch.setattr(events, "append_event", boom)
    ran = []
    with tel.busy("history"):
        ran.append(1)
    assert ran == [1]
    assert _rows() == []


def test_busy_refuses_an_unknown_faculty_without_disturbing_the_block():
    ran = []
    with tel.busy("superpower_brain"):
        ran.append(1)
    assert ran == [1]
    assert _rows() == []


# --- the fold: deterministic and total ------------------------------------------------

MALFORMED = [
    None,                                             # not an Event at all
    "a torn line",
    _ev(None),                                        # payload is not a dict
    _ev([("faculty", "sop")]),
    _ev({}),                                          # no faculty, no status
    _ev({"faculty": "sop"}),                          # no status
    _ev({"status": "active"}),                        # no faculty
    _ev({"faculty": "superpower_brain", "status": "active"}),
    _ev({"faculty": "sop", "status": "awake"}),
    _ev({"faculty": None, "status": None}),
    _ev({"faculty": ["sop"], "status": "active"}),
    _ev({"faculty": "sop", "status": "active"}, type_="decision"),
]


@pytest.mark.parametrize("row", MALFORMED)
def test_the_fold_skips_a_malformed_row_rather_than_raising(row):
    """Reads stay TOTAL (ledger-schema SOP): this feed will be written by every
    future version of the emitter, and one bad row must never take out the cat
    board — or, since MN-5 polls it, the whole MindSwarm iframe."""
    assert tel.fold([row]) == {}


def test_the_fold_keeps_the_good_rows_beside_the_bad_ones():
    """The discriminating half: skipping malformed rows is only correct if the
    valid ones in the same batch still fold."""
    good = _ev({"faculty": "sop", "status": "active"})
    state = tel.fold([*MALFORMED, good, *MALFORMED])
    assert set(state) == {"sop"}
    assert state["sop"].status == "active"


def test_the_fold_of_nothing_is_empty():
    assert tel.fold(None) == {}
    assert tel.fold([]) == {}


def test_the_fold_sanitizes_optional_fields_it_reads_back():
    """A row written by an older, laxer emitter still lands in MN-5's DOM. The
    fold is the last honest place to drop a phase name that is not a phase."""
    state = tel.fold([_ev({"faculty": "sop", "status": "active",
                           "task_id": "Not A Task Id", "phase": "shipping"})])
    assert state["sop"].task_id == ""
    assert state["sop"].phase == ""


def test_the_fold_is_deterministic():
    rows = [_ev({"faculty": f, "status": "active"}) for f in CANONICAL]
    assert tel.fold(rows) == tel.fold(rows)


def test_the_fold_is_bounded_by_the_canonical_set():
    """Seven faculties, so the projection is bounded by construction however
    many events the feed carries — no cap constant needed, unlike the graph."""
    rows = [_ev({"faculty": CANONICAL[i % 7], "status": "active"})
            for i in range(500)]
    assert len(tel.fold(rows)) == 7


# --- round-trip ------------------------------------------------------------------------

def test_emitted_events_fold_back_to_the_states_they_recorded():
    tel.emit("sop", tel.STATUS_ACTIVE, task_id=TASK, phase="plan",
             session_id="s-round-trip")
    tel.emit("history", tel.STATUS_ACTIVE)
    tel.emit("pr-green", tel.STATUS_IDLE)
    state = tel.fold(events.read_all())
    assert set(state) == {"sop", "history", "pr-green"}
    # `session` is the one field of this round trip that comes off the
    # ENVELOPE rather than the payload -- `emit` never writes it into the
    # body -- so naming it here is what proves the fold reads the event and
    # not just its dict. `ui_cats.skill_overlay` joins on exactly this.
    assert state["sop"] == tel.Activity(
        faculty="sop", status="active", task_id=TASK, phase="plan",
        ts=state["sop"].ts, session="s-round-trip")
    assert state["sop"].ts
    assert tel.awake(state) == ("sop", "history")


def test_a_later_idle_supersedes_an_earlier_active():
    tel.emit("improvements", tel.STATUS_ACTIVE)
    tel.emit("improvements", tel.STATUS_IDLE)
    state = tel.fold(events.read_all())
    assert state["improvements"].status == "idle"
    assert tel.awake(state) == ()


def test_a_later_active_wakes_a_faculty_that_had_gone_idle():
    tel.emit("improvements", tel.STATUS_ACTIVE)
    tel.emit("improvements", tel.STATUS_IDLE)
    tel.emit("improvements", tel.STATUS_ACTIVE)
    assert tel.awake(tel.fold(events.read_all())) == ("improvements",)


def test_a_repeated_active_is_harmless():
    """`emit` is unconditional by design — deduping would cost a ledger read on
    a path that must stay cheap. The fold is idempotent under repeats, so the
    caller may emit freely."""
    for _ in range(3):
        tel.emit("sop", tel.STATUS_ACTIVE)
    assert tel.fold(events.read_all())["sop"].status == "active"
    assert len(_rows()) == 3


def test_awake_reports_in_canonical_order_not_arrival_order():
    for f in reversed(CANONICAL):
        tel.emit(f, tel.STATUS_ACTIVE)
    assert tel.awake(tel.fold(events.read_all())) == CANONICAL


def test_awake_is_total():
    assert tel.awake(None) == ()
    assert tel.awake({}) == ()


def test_the_fold_ignores_every_other_event_type():
    events.append_event("decision", {"text": "unrelated"})
    tel.emit("sop", tel.STATUS_ACTIVE)
    events.append_event("mindnet_task", {"action": "declare", "task_id": TASK,
                                         "title": "t"}, tags=["mindnet"])
    assert set(tel.fold(events.read_all())) == {"sop"}


# --- ledger-derived, never in-memory ----------------------------------------------------

def test_the_state_is_read_from_the_ledger_and_not_from_memory(monkeypatch):
    """The decomposition's hard constraint, asserted where it can actually
    fail. `current` with an empty ledger read MUST be empty: a module that had
    remembered its own emits would answer from that memory and pass every
    round-trip test above while failing R16 on the first server restart."""
    tel.emit("sop", tel.STATUS_ACTIVE)
    assert set(tel.current()) == {"sop"}

    monkeypatch.setattr(events, "read_all", lambda project=None: [])
    assert tel.current() == {}


def test_the_state_survives_a_module_reload():
    """A reload is the cheapest stand-in for the restart that kills MindSwarm's
    instance attribute (`workspace.py:472`) — the precise failure this feed is
    specified not to share."""
    tel.emit("compatibility", tel.STATUS_ACTIVE)
    reloaded = importlib.reload(tel)
    assert set(reloaded.current()) == {"compatibility"}


def test_the_module_keeps_no_mutable_state():
    """Structural guard for the same property: a `_STATE = {}` added later is
    caught here rather than in production at 3am."""
    mutable = {k: type(v).__name__ for k, v in vars(tel).items()
               if not k.startswith("__") and isinstance(v, (dict, list, set))}
    assert mutable == {}


def test_current_accepts_injected_events_so_a_caller_folds_once():
    """MN-5 polls; the sidecar path already holds the events. Passing them in
    keeps the feed from re-reading the ledger per poll."""
    rows = [_ev({"faculty": "sop", "status": "active"})]
    assert set(tel.current(evs=rows)) == {"sop"}


# --- registry alignment -------------------------------------------------------------------

def test_the_event_type_is_registered_and_deterministic():
    """Deterministic, NOT semantic: high-volume UI telemetry must not ride the
    semantic memo layer or the Memory view."""
    assert tel.EVENT_TYPE == "mindnet_activity"
    assert tel.EVENT_TYPE in events.DETERMINISTIC_TYPES
    assert tel.EVENT_TYPE not in events.SEMANTIC_TYPES
    assert tel.EVENT_TYPE not in events.MEMORY_VIEW_TYPES


def test_the_phase_field_is_bound_to_the_one_legend():
    """Invariant 10 again: the phase on an activity event is `tasks.PHASES`'s
    string, not a second spelling of it."""
    for phase in t_mod.PHASES:
        tel.emit("sop", tel.STATUS_ACTIVE, phase=phase)
    assert [r.payload["phase"] for r in _rows()] == list(t_mod.PHASES)


def test_the_docstring_names_every_module_that_writes_this_feed():
    """The module docstring lists the emit seams and calls that list "the whole
    producer set". A reader trusts it to tell a faculty with a labelled cat and no
    emitter from one that is merely quiet — the distinction R16 rests on.

    Nothing checked the claim, so the next producer wired up somewhere else would
    make it quietly false. That is the doc-vs-code drift this repo has already
    shipped once (docs/plans/2026-08-02-002), caught here by the source rather
    than by review."""
    import ast

    src = REPO / "src" / "mindhive"
    doc = tel.__doc__ or ""
    writers = set()
    for path in src.rglob("*.py"):
        if path.name == "telemetry.py":
            continue
        # Parsed, not grepped. Half this package DISCUSSES `telemetry.busy` in
        # prose — `ui_cats`'s own docstring explains the flicker grace in terms
        # of it — and a text search cannot tell a sentence from a call site.
        # Attribute ACCESS, not a call: `workspace` resolves `tel_mod.emit` into
        # a local before calling it, so matching calls alone would miss the very
        # producer this guard was written for.
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                     # pragma: no cover
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr in ("emit", "busy")
                    and isinstance(node.value, ast.Name)
                    and "tel" in node.value.id):
                writers.add(path.stem)
    assert writers, "the scan matched nothing — the guard would pass vacuously"
    missing = sorted(w for w in writers if w not in doc)
    assert not missing, (
        f"these modules write mindnet_activity but the telemetry docstring's "
        f"producer list does not name them: {missing}")


def test_the_cross_module_faculty_constant_is_a_real_member():
    """`enforce.hook_decision` emits `FACULTY_COMPATIBILITY` from outside this
    package. If it ever drifts from `FACULTIES`, `_payload` refuses the row and
    returns None — the cat stops waking with no error anywhere, which is exactly
    the bug the constant exists to prevent.

    Here rather than as a module-level `assert`: asserts are stripped under
    `python -O`, so that guard would be absent precisely where a silent failure
    is least recoverable."""
    assert tel.FACULTY_COMPATIBILITY in tel.FACULTIES


def test_the_signature_that_routes_the_cat_is_the_registered_one():
    """Invariant 10 across a module boundary. `hook_decision` decides whether to
    wake `compatibility` by comparing the fired signature against
    `MINDNET_LANGUAGE_SIGNATURE`; if that string ever stops being the one in
    `POLICY_CHECKS`, the comparison silently never matches."""
    from mindhive import enforce
    assert enforce.MINDNET_LANGUAGE_SIGNATURE in [
        pid for pid, _decision, _fn in enforce.POLICY_CHECKS]
