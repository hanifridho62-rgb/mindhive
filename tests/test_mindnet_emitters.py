# tests/test_mindnet_emitters.py
"""MN-17 — every declared faculty must have something that lights it.

The defect this file exists to prevent, found by auditing the live store rather than
the code: `telemetry.FACULTIES` declared seven faculties, `cat_board` drew a labelled
cat for each, and **only one of them was ever emitted**. The live ledger contained zero
faculty activity rows, so every cat was dark — and the user's whole reason for asking
for the cats was to see "apa saja yang menyala dan apa saja yang tidak menyala".

A declared-but-unemitted faculty is worse than a missing one: the board looks complete,
so nothing surfaces the gap. The structural guard below is the point of this module; the
behavioural tests around it prove each emitter actually fires.
"""
import json

import pytest

from mindhive import events, promptgate
from mindhive.mindnet import telemetry as tel
from mindhive.mindnet import ui_cats


# Faculties with no emitter yet, each with the reason it is still dark. Anything in
# this map is EXEMPT from the guard below — and the guard fails if the map names a
# faculty that has since gained an emitter, so an exemption cannot outlive its cause.
PENDING = {
    "compatibility": (
        "No programming-language compatibility gate exists yet. The faculty name and "
        "its cat label shipped ahead of the mechanism, which is exactly the mistake "
        "this module guards against — it stays listed here, with a reason, instead of "
        "being quietly dropped from the legend."),
}


def _prompt_trigger(project):
    """The hook path: a code prompt that opens the catalog and surfaces a record."""
    _seed(project)
    promptgate.record_direction(
        json.dumps({"prompt": "buatkan fitur export CSV di dashboard",
                    "session_id": "s-trigger"}), project=project)


def _task(**kw):
    """A real `tasks.Task`. `advice.task_text` reads `title`/`description` off the
    OBJECT via getattr, so a plain dict yields no keywords and `resolve` returns EMPTY
    without ever reaching a resolver — which is how a first version of this trigger
    silently proved nothing."""
    from mindhive.mindnet import tasks as t_mod
    return t_mod.Task(**kw)


def _seed_incident(project):
    """One incident record, the shape `incidents.summaries` parses.

    Written as a file rather than an event because that is genuinely where the source
    lives — `resolve_constraints` reads `incidents.summaries`, which globs
    `rules/incidents/*.md`. This is also what backs the dashboard's Improvements view,
    so the `improvements` faculty and that section are the same thing."""
    from mindhive import paths
    d = paths.project_dir(project) / "rules" / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "csv-export-buffer.md").write_text(
        "---\nsignature: csv:export:buffer\nstatus: active\noccurrences: 2\n"
        "rule_source: auto\nflag: \nsessions: 1\n---\n\n"
        "# Incident: csv:export:buffer\n\n"
        "## Raw Signal\n- Error excerpt: `buffering the whole csv export "
        "dashboard endpoint exhausted memory`\n\n"
        "## Auto Evaluation\n- Prevention rule: stream the csv export dashboard "
        "endpoint instead of buffering it\n\n"
        "## Human Evaluation\n<!-- -->\n\n"
        "## Active Prevention Rule\n> stream the csv export dashboard endpoint "
        "instead of buffering it\n", encoding="utf-8")


def _advice_trigger(project):
    """CONSULTING resolved advice at prompt time — the SOP and past-failure cats.

    Deliberately not `advice.resolve`: that runs inside a sidecar rebuild, and emitting
    there made the rebuild write to the ledger and broke "same events in, same bytes
    out". The faculties are announced where the advice is USED, so this trigger builds a
    sidecar carrying advice and asks the injector what it engaged.

    Seeds BOTH sections, because the two faculties are announced independently and a
    trigger feeding only one would leave the other unprovable."""
    from mindhive import promptgate as pg
    from mindhive.mindnet import inject as mn_inject
    sc = _spine_with_advice(project)
    pg.announce_faculties(mn_inject.engaged_faculties(sc), project=project)


def _spine_with_advice(project):
    """A sidecar whose block will render, carrying both advice sections.

    Built through the real declare/rebuild path rather than hand-assembled: a fake
    sidecar would let `engaged_faculties` pass against a shape production never
    produces, which is a failure mode this project has hit before."""
    from mindhive.events import Event, append_event
    from mindhive.mindnet import sidecar as sc_mod
    _seed_incident(project)
    append_event("sop", {"action": "add", "sop_id": "sop-export",
                         "title": "csv export dashboard procedure",
                         "text": "always stream the csv export dashboard endpoint, "
                                 "never buffer the whole file in memory"},
                 session_id="s-seed", tags=["sop"], project=project)
    # Tags matter: the declare must carry the same ones production writes, or the
    # rebuild folds no record and this trigger silently proves nothing — which is
    # exactly what happened on the first attempt.
    append_event("mindnet_task",
                 {"action": "declare", "task_id": "mn17-advice",
                  "title": "wire the csv export dashboard endpoint",
                  "description": "stream the csv export"},
                 session_id="s-seed", tags=["mindnet", "task"], project=project)
    # A task declare alone is NOT enough, and finding that out sharpened the audit this
    # unit came from: `sidecar.state.records` holds the controller's PHASE records, so
    # `inject.block` stays silent until a task has entered a phase on that axis. Two
    # axes, two events — the task graph and the phase spine are separate by design.
    append_event("mindnet_phase",
                 {"action": "declare", "task_id": "mn17-advice",
                  "verified_by": "python"},
                 session_id="s-seed", tags=["mindnet", "phase"], project=project)
    sc_mod.rebuild(project=project)
    return sc_mod.read(project)


def _gate_trigger(project):
    """The completion gate — "make sure pr green" actually checking."""
    from mindhive.mindnet import orchestrator as orc
    orc.completion_gate_default({"t1": _task(task_id="t1", title="a task")},
                                "t1", project=project)


def _wave_trigger(project):
    """A parallel wave — the faculty that already had an emitter before MN-17.

    `execute=True` is load-bearing: a dry run never enters `_run_wave`, so the wave
    emitter never fires and this trigger would prove nothing."""
    from mindhive.mindnet import orchestrator as orc
    orc.orchestrate({"t1": _task(task_id="t1", title="a task")},
                    runner=lambda *a, **kw: None, execute=True, project=project)


# Faculty -> the thing that must light it. Keyed by name so a faculty added to the
# legend without a trigger fails `test_every_faculty_outside_pending_has_a_declared_trigger`.
TRIGGERS = {
    "skill-plugin": _prompt_trigger,
    "history": _prompt_trigger,
    "sop": _advice_trigger,
    "improvements": _advice_trigger,
    "pr-green": _gate_trigger,
    "task-graph": _wave_trigger,
}


def _emitting_faculties() -> set:
    """Faculties that light up when their trigger is actually RUN.

    Behavioural, not a text scan. A first version grepped `src/` for the faculty name
    inside a `busy(`/`emit(` call and reported a false failure the moment a name arrived
    through a variable — which is most of them. Worse, a text scan can only ever prove
    that a string appears near a call, not that the call happens.

    **Each trigger gets its own project namespace.** A second version ran them all into
    one store and diffed `activity[before:]`, which quietly proved nothing: ledger
    timestamps are second-resolution, `read_all` orders by them, so rows written inside
    the same second interleave and the slice attributed one trigger's emissions to
    another. Isolation removes the ordering assumption entirely."""
    found = set()
    for faculty, trigger in TRIGGERS.items():
        ns = f"emit-{faculty}"
        try:
            trigger(ns)
        except Exception as exc:                            # noqa: BLE001
            pytest.fail(f"the trigger for {faculty!r} raised {type(exc).__name__}: {exc}")
        found |= {f for f, _s in _activity(ns)}
    return found


def test_every_declared_faculty_lights_up_or_has_a_recorded_reason():
    """The guard, and the reason this module exists. Before MN-17 six of the seven
    declared faculties had a labelled cat and no emitter at all, so the operator read
    "not running" and could not distinguish that from "never wired"."""
    emitting = _emitting_faculties()
    dark = [f for f in tel.FACULTIES if f not in emitting and f not in PENDING]
    assert not dark, (
        f"faculties declared and drawn but never lit by their own trigger: {dark}. "
        f"Either wire an emitter or add it to PENDING with the reason it is dark.")


def test_no_pending_exemption_outlives_its_reason():
    """An exemption that quietly becomes false is how the original gap would return."""
    emitting = _emitting_faculties()
    stale = sorted(set(PENDING) & emitting)
    assert not stale, (
        f"these now light up and must be removed from PENDING: {stale}")


def test_every_faculty_outside_pending_has_a_declared_trigger():
    """The coverage half. A faculty with no trigger cannot be proven either way, and an
    unprovable faculty is how the original silence hid."""
    missing = [f for f in tel.FACULTIES
               if f not in TRIGGERS and f not in PENDING]
    assert not missing, f"no trigger declared for: {missing}"


def test_pending_names_are_real_faculties_with_real_reasons():
    unknown = sorted(set(PENDING) - set(tel.FACULTIES))
    assert not unknown, f"PENDING names something not in the legend: {unknown}"
    for name, reason in PENDING.items():
        assert len(reason) > 40, f"{name}'s exemption needs an actual reason"


def test_the_emitter_scan_can_actually_find_something():
    """Canary. If the scanner returned an empty set the guard above would pass by
    finding nothing — the exact failure mode this project has hit twice before."""
    found = _emitting_faculties()
    assert found, "the scanner found no emitters at all; it is broken, not the code"
    assert "task-graph" in found, (
        "the orchestrator's wave emitter is the one that existed before MN-17 — if "
        "the scanner cannot see it, it cannot see any of them")


# --- the emitters, behaviourally ------------------------------------------------

def _activity(project="test"):
    """Every faculty activity row in the store, as `(faculty, status)` pairs."""
    return [(e.payload["faculty"], e.payload["status"])
            for e in events.read_all(project)
            if e.type == tel.EVENT_TYPE and isinstance(e.payload, dict)
            and e.payload.get("faculty")]


_SKILLS = [
    {"name": "planner", "plugin": "p",
     "description": "plan break down structured multi-step task goal requirements"},
]


def _stats(spec):
    return {n: {"calls": c, "ok": o, "fail": c - o,
                "completion_rate": (o / c) if c else None}
            for n, (o, c) in spec.items()}


def _seed(project="test", with_records=True):
    from mindhive.events import Event
    evs = []
    if with_records:
        evs.append(Event(ts="2026-07-01T00:00:00Z", session_id="s", type="caveat",
                         payload={"text": "dashboard export panel needs the CSV "
                                          "button wired to the endpoint"}))
    man = promptgate.train(_SKILLS, _stats({"planner": (5, 5)}), evs=evs,
                           now="2026-07-14T00:00:00Z")
    promptgate._write_manifest(project, man)
    return man


def test_the_catalog_lights_the_skill_and_plugin_cat():
    """The user's own example: "ketika menggunakan skill superpower brain maka akan
    memanggil kucing yang diatas kepala nya bertulisan Skill and plugin". Opening the
    catalog for a prompt is that moment."""
    _seed()
    promptgate.record_direction(
        json.dumps({"prompt": "buatkan fitur export CSV di dashboard",
                    "session_id": "s-cat"}), project="test")
    assert ("skill-plugin", "active") in _activity()


def test_surfacing_ledger_records_lights_the_history_cat():
    """Reading the record is the "check historis masa lalu" the founding prompt
    demands, so it is a faculty the operator should see light up."""
    _seed()
    promptgate.record_direction(
        json.dumps({"prompt": "buatkan fitur export CSV di dashboard",
                    "session_id": "s-cat"}), project="test")
    assert ("history", "active") in _activity()


def test_a_prompt_with_no_matching_record_does_not_light_history():
    """Honesty of the signal: a lit cat must mean the faculty ran. If history lights
    whether or not anything was found, the board stops carrying information."""
    _seed(with_records=False)
    promptgate.record_direction(
        json.dumps({"prompt": "buatkan fitur export CSV di dashboard",
                    "session_id": "s-cat"}), project="test")
    facs = {f for f, _s in _activity()}
    assert "history" not in facs


def test_conversation_lights_nothing():
    """The board must stay dark when MindNetwork did not engage, or "what is running"
    becomes meaningless."""
    _seed()
    promptgate.record_direction(
        json.dumps({"prompt": "terima kasih", "session_id": "s-cat"}), project="test")
    assert _activity() == []


def test_a_lit_faculty_is_also_reported_idle_so_its_cat_vanishes():
    """The user asked that the cat disappear when the section is not running.
    `ui_cats` implements that by hiding an `idle` faculty once the 15-second flicker
    grace passes — which only works if something actually reports idle."""
    _seed()
    promptgate.record_direction(
        json.dumps({"prompt": "buatkan fitur export CSV di dashboard",
                    "session_id": "s-cat"}), project="test")
    rows = _activity()
    for faculty in {f for f, _s in rows}:
        assert (faculty, "idle") in rows, f"{faculty} never reported idle"
    assert ui_cats.classify(ui_cats.IDLE_VANISH_SECS + 1, tel.STATUS_IDLE) is None


def test_emission_failure_never_disturbs_the_prompt(monkeypatch):
    """Same contract as every other write on this path: a cat animation that cannot be
    logged must not break the user's prompt."""
    _seed()
    monkeypatch.setattr(tel, "emit",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    ev = promptgate.record_direction(
        json.dumps({"prompt": "buatkan fitur export CSV di dashboard",
                    "session_id": "s-cat"}), project="test")
    assert ev is not None, "the direction record was lost to a telemetry failure"


def test_the_prompt_path_does_not_read_the_ledger_to_emit(monkeypatch):
    """`events.read_all` costs ~1.09 s on the live store and this runs on every prompt.
    `telemetry.emit` documents that it never reads; this pins it at the seam."""
    _seed()

    def explode(*a, **kw):
        raise AssertionError("the emit path read the ledger")

    monkeypatch.setattr(events, "read_all", explode)
    promptgate.record_direction(
        json.dumps({"prompt": "buatkan fitur export CSV di dashboard",
                    "session_id": "s-cat"}), project="test")


def test_consulting_advice_lights_the_sop_cat():
    """Both `sop` and `improvements` were in the legend from the start and both had
    working resolvers — they were simply never announced, so the operator could not tell
    that a standing procedure or a past-failure constraint had been applied.

    Uses the shared trigger so this test and the guard exercise the same path; a fixture
    that drifted from the trigger would let one pass while the other proved nothing."""
    _advice_trigger("test")
    facs = {f for f, _s in _activity()}
    assert "sop" in facs, f"advice was consulted but announced nothing: {_activity()}"


def test_resolving_advice_writes_no_telemetry():
    """The invariant that decided where this emitter lives. `advice.resolve` runs during
    a sidecar rebuild, so a ledger write inside it makes the rebuild mutate the store and
    "same events in, same bytes out" stops holding — which is what
    `test_rebuild_stays_byte_idempotent_with_advice` caught when the emitter was there."""
    from mindhive.mindnet import advice as adv
    _seed_incident("test")
    adv.resolve(_task(task_id="t1", title="wire the csv export dashboard endpoint",
                      description="stream the csv export"), evs=[], project="test")
    assert _activity() == [], "resolving advice emitted telemetry"


def test_a_spine_with_nothing_to_say_announces_nothing():
    """Honesty of the signal, same rule as history: a lit cat must mean the faculty
    actually contributed something to this turn."""
    from mindhive.mindnet import inject as mn_inject
    from mindhive.mindnet import sidecar as sc_mod
    assert mn_inject.engaged_faculties(sc_mod.read("test")) == set()
    assert mn_inject.engaged_faculties(None) == set()


@pytest.mark.parametrize("faculty", ["skill-plugin", "history", "sop", "improvements",
                                     "pr-green", "task-graph"])
def test_each_wired_faculty_has_a_cat_label(faculty):
    """A lit faculty with no caption renders its raw id above the cat's head. The
    legend and the board must stay in step — that pairing is the whole UI."""
    from mindhive import cat_board
    assert cat_board.CAT_LABEL.get(faculty), f"{faculty} has no cat caption"
