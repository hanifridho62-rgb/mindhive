# tests/test_mindnet_advice_persistence.py
"""MN-3 U3/U4 — carrying resolved advice across the process boundary.

Written TEST-FIRST against
``docs/plans/2026-07-27-001-feat-mindnet-mn3-mn4-mn5-mechanical-enforcement-plan.md``
U3 and U4.

Kept in its own file rather than appended to `test_mindnet_sidecar.py` for the
plan's own no-monoliths reason: the sidecar suite is already 612 lines about the
freshness stamp and the fold, and the advice section is a separate concern that
happens to ride the same document.

## The two properties that make the write-time design honest

The whole MN-3 architecture rests on one claim: resolution costs nothing extra
because it happens where a ledger read is ALREADY being paid.
`test_rebuild_does_not_add_a_second_ledger_read` is what turns that from a
design intention into a checked property. Without it, the natural
implementation — `advice.resolve(task)` with `evs` left to default — reads the
ledger a second time inside `rebuild`, doubling the cost of every projection
refresh, and every other test in this file still passes.

The second is `test_a_resolver_fault_does_not_lose_the_sidecar`. Advice is the
least important thing a rebuild does. An exception escaping the resolver would
take the phase projection down with it, which inverts the priority exactly.

## Backward compatibility is tested against a real v2 document

`test_a_v2_document_still_decodes` hand-builds a document with no ``advice``
key rather than round-tripping one through the new writer, because a writer that
always emits the key would make a round-trip version of this test vacuous — and
every sidecar currently on disk is a v2 document.
"""
import json

import pytest

from mindhive import events
from mindhive import incidents as inc_mod
from mindhive import plugins as plugins_mod
from mindhive.mindnet import advice as adv
from mindhive.mindnet import inject as inj
from mindhive.mindnet import sidecar as sc_mod
from mindhive.mindnet import tasks as t_mod


# --- helpers -------------------------------------------------------------------

def _task_ev(task_id, title, description="", ts="2026-07-27T00:00:00Z"):
    return events.Event(ts=ts, session_id="s", type="mindnet_task",
                        payload={"action": "declare", "task_id": task_id,
                                 "title": title, "description": description},
                        tags=["mindnet"])


def _phase_ev(task_id, ts="2026-07-27T00:00:00Z"):
    """The phase-axis declare. Advice is keyed off the task `inject.active`
    would steer, and `controller.fold` only builds a record from a
    `mindnet_phase` event — a task with no record gets no injected block at all,
    so there would be nothing for advice to ride."""
    return events.Event(ts=ts, session_id="s", type="mindnet_phase",
                        payload={"action": "declare", "task_id": task_id},
                        tags=["mindnet"])


def _declare(task_id, title, description="", ts="2026-07-27T00:00:00Z"):
    """Both halves of bringing a task into existence: the graph row and the
    phase record."""
    return [_task_ev(task_id, title, description, ts), _phase_ev(task_id, ts)]


def _sop_ev(sop_id, title, text, ts="2026-07-27T00:00:01Z"):
    return events.Event(ts=ts, session_id="s", type="sop",
                        payload={"action": "add", "sop_id": sop_id,
                                 "title": title, "text": text}, tags=["sop"])


@pytest.fixture
def quiet(monkeypatch):
    """Registries silent unless a test opts in — see test_mindnet_advice.py."""
    monkeypatch.setattr(plugins_mod, "recommend_for", lambda *a, **k: {
        "coverage": {}, "recommendations": [], "discontinued": []})
    monkeypatch.setattr(inc_mod, "summaries", lambda *a, **k: [])
    return monkeypatch


# --- U3: the sidecar carries the section -------------------------------------------

def test_schema_version_is_bumped_for_the_new_section():
    """A reader must be able to tell WHICH shape it is looking at — the reason
    the constant exists at all (`sidecar.py:81-89`)."""
    assert sc_mod.SCHEMA_VERSION == 3


def test_advice_round_trips_through_the_sidecar(tmp_path, monkeypatch):
    payload = {"sops": ["[change-audit] Log it: TURTLE-SENTINEL-4417"],
               "skills": ["ce-migrate — migrates schemas"],
               "constraints": ["SYSTEM WARNING: ... forbidden ..."]}
    sc_mod.write(sc_mod.empty_state(), {}, advice=payload)
    got = sc_mod.read()
    assert got.advice == payload


def test_the_advice_section_is_written_into_the_document(tmp_path):
    """Asserted at the JSON level, not through `read`: a writer that dropped the
    section and a reader that invented it would agree with each other."""
    p = sc_mod.write(sc_mod.empty_state(), {},
                     advice={"sops": ["TURTLE-SENTINEL-4417"]})
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["advice"]["sops"] == ["TURTLE-SENTINEL-4417"]
    assert doc["version"] == 3


def test_a_v2_document_still_decodes():
    """Every sidecar currently on disk is a v2 document. Built by hand rather
    than round-tripped, so a writer that always emits the key cannot make this
    vacuous."""
    p = sc_mod.sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 2, "stamp": {}, "records": {},
                             "orphans": {}, "tasks": {}}), encoding="utf-8")
    got = sc_mod.read()
    assert got.advice == {}
    assert got.state.records == {}


@pytest.mark.parametrize("junk", ["a string", 42, [], None,
                                  {"sops": "not-a-list"}, {"sops": [1, 2]},
                                  {7: ["x"]}])
def test_a_malformed_advice_section_decodes_empty_rather_than_raising(junk):
    """Read-path totality. The sidecar is a file on disk written by every past
    version of this code, and this decode runs on the hook path."""
    p = sc_mod.sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 3, "stamp": {}, "records": {},
                             "orphans": {}, "tasks": {}, "advice": junk}),
                 encoding="utf-8")
    got = sc_mod.read()
    assert isinstance(got.advice, dict)
    assert all(isinstance(v, list) for v in got.advice.values())


def test_writing_no_advice_leaves_a_readable_document():
    sc_mod.write(sc_mod.empty_state(), {})
    assert sc_mod.read().advice == {}


def test_empty_lists_are_not_persisted():
    """Keeps the round trip an exact equality and keeps the document honest —
    the same call `_decode` makes for empty orphan buckets."""
    p = sc_mod.write(sc_mod.empty_state(), {},
                     advice={"sops": [], "skills": ["kept"]})
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert "sops" not in doc["advice"]
    assert doc["advice"]["skills"] == ["kept"]


def test_the_encoded_document_is_still_byte_stable():
    """`rebuild`'s idempotence is byte-for-byte (`sidecar.py:218-221`). A
    section serialized from an unsorted structure would break that."""
    a = sc_mod._encode(sc_mod.empty_state(), {}, None,
                       {"skills": ["x"], "sops": ["y"]})
    b = sc_mod._encode(sc_mod.empty_state(), {}, None,
                       {"sops": ["y"], "skills": ["x"]})
    assert a == b


# --- the shape contract across the serialization boundary ----------------------------

def test_the_injector_and_the_resolver_agree_on_the_section_keys():
    """`inject` renders the plain dict without importing `advice` (that would
    close an import cycle), so the two ends know the key names independently.
    This is the assertion that makes the duplication safe: rename a key on
    either side and it fails here rather than silently rendering nothing."""
    assert set(inj.ADVICE_KEYS) == set(adv.to_dict(adv.EMPTY))


def test_the_injector_and_the_resolver_agree_on_the_item_cap():
    """The OTHER half of the same duplication, which went unpinned while
    `inject.py`'s own comment claimed this suite already covered it.

    Live consequence of the drift: raise `advice.MAX_ITEM_CHARS` without
    touching inject's copy and every long item gets sliced mid-sentence by
    `safe_text` at render time — breaking `advice_block`'s guarantee that whole
    items are dropped rather than truncated, which exists because a
    half-sentence SOP reads as a complete instruction that happens to be
    wrong."""
    assert inj.MAX_ADVICE_ITEM_CHARS == adv.MAX_ITEM_CHARS


def test_the_render_budget_fits_a_fully_populated_advice_set():
    """Asserted by RENDERING, not by arithmetic over the item caps.

    The original guard multiplied the caps ((3+2+2) * 240 = 1680) and compared
    against the budget. That misses the 195 chars the renderer itself adds —
    three section headers, seven `- ` bullets, ten newlines — so a full set
    rendered to 1875 against an 1800 budget and the last skill was silently
    dropped, while the guard stayed green."""
    worst = {"sops": ["S" * adv.MAX_ITEM_CHARS] * adv.MAX_SOPS,
             "constraints": ["C" * adv.MAX_ITEM_CHARS] * adv.MAX_CONSTRAINTS,
             "skills": ["K" * adv.MAX_ITEM_CHARS] * adv.MAX_SKILLS}
    rendered = inj.advice_block(worst)
    kept = len([ln for ln in rendered.splitlines() if ln.startswith("- ")])
    assert kept == adv.MAX_SOPS + adv.MAX_CONSTRAINTS + adv.MAX_SKILLS, (
        "a fully populated advice set must survive the render budget intact")
    assert len(rendered) <= inj.MAX_ADVICE_CHARS


# --- U4: resolution rides the read the caller already paid ---------------------------

def test_rebuild_resolves_advice_for_the_active_task(quiet):
    """End to end through the real fold: a declared task plus a matching SOP on
    the ledger produces a sidecar carrying the SOP's real body."""
    evs = _declare("t1", "database migration for the ledger") + [
        _sop_ev("change-audit", "Migration rules",
                "database migration ledger guidance TURTLE-SENTINEL-4417")]
    quiet.setattr(sc_mod.ev_mod, "read_all", lambda *a, **k: evs)
    out = sc_mod.rebuild()
    assert out.written
    assert "TURTLE-SENTINEL-4417" in json.dumps(sc_mod.read().advice)


def test_rebuild_does_not_add_a_second_ledger_read(quiet):
    """THE property behind the whole write-time design. A resolver called
    without the caller's `evs` doubles the cost of every rebuild and passes
    every other test in this file."""
    evs = _declare("t1", "database migration for the ledger") + [
        _sop_ev("change-audit", "Migration rules",
                "database migration ledger guidance")]
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return evs

    quiet.setattr(sc_mod.ev_mod, "read_all", counting)
    quiet.setattr(adv.ev_mod, "read_all", counting)
    sc_mod.rebuild()
    assert calls["n"] == 1, (
        f"rebuild read the ledger {calls['n']} times; advice resolution must "
        f"reuse the events already in hand")


def test_rebuild_with_injected_events_reads_the_ledger_not_at_all(quiet):
    """The CLI path passes `evs` in. Resolution must not undo that."""
    evs = _declare("t1", "database migration for the ledger")

    def boom(*a, **k):
        raise AssertionError("the ledger must not be read here")

    quiet.setattr(sc_mod.ev_mod, "read_all", boom)
    quiet.setattr(adv.ev_mod, "read_all", boom)
    assert sc_mod.rebuild(evs=evs).written


def test_a_resolver_fault_does_not_lose_the_sidecar(quiet):
    """Advice is the least important thing a rebuild does. An escaping
    exception would take the phase projection with it."""
    evs = _declare("t1", "database migration for the ledger")

    def boom(*a, **k):
        raise RuntimeError("resolver exploded")

    quiet.setattr(adv, "resolve", boom)
    out = sc_mod.rebuild(evs=evs)
    assert out.written, "the phase projection must survive an advice fault"
    assert sc_mod.read().advice == {}


def test_rebuild_with_no_tasks_writes_empty_advice(quiet):
    evs = [_sop_ev("s", "T", "database migration ledger guidance")]
    assert sc_mod.rebuild(evs=evs).written
    assert sc_mod.read().advice == {}


def test_rebuild_stays_byte_idempotent_with_advice(quiet):
    """`rebuild`'s contract is that the same ledger produces an identical file.
    Advice resolution is deterministic, so that must still hold."""
    evs = _declare("t1", "database migration for the ledger") + [
        _sop_ev("change-audit", "Migration rules",
                "database migration ledger guidance")]
    quiet.setattr(sc_mod.ev_mod, "read_all", lambda *a, **k: evs)
    sc_mod.rebuild(evs=evs)
    first = sc_mod.sidecar_path().read_text(encoding="utf-8")
    sc_mod.rebuild(evs=evs)
    assert sc_mod.sidecar_path().read_text(encoding="utf-8") == first


def test_advice_tracks_the_workable_task_not_a_completed_one(quiet):
    """Advice steers the task `inject.active` will steer. Resolving for a task
    finished months ago would inject guidance for work nobody is doing."""
    evs = _declare("done", "database migration for the ledger") + [
        events.Event(ts="2026-07-27T00:00:02Z", session_id="s",
                     type="mindnet_task",
                     payload={"action": "status", "task_id": "done",
                              "status": t_mod.STATUS_COMPLETED},
                     tags=["mindnet"]),
    ] + _declare("live", "css gradient footer styling",
                 ts="2026-07-27T00:00:03Z") + [
        _sop_ev("styling", "Gradient rules",
                "css gradient footer styling guidance ZEBRA-4417",
                ts="2026-07-27T00:00:04Z"),
        _sop_ev("migrations", "Migration rules",
                "database migration ledger guidance TURTLE-4417",
                ts="2026-07-27T00:00:05Z"),
    ]
    quiet.setattr(sc_mod.ev_mod, "read_all", lambda *a, **k: evs)
    sc_mod.rebuild(evs=evs)
    blob = json.dumps(sc_mod.read().advice)
    assert "ZEBRA-4417" in blob
    assert "TURTLE-4417" not in blob
