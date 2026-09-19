# tests/test_worksession_migrate.py — the Opsi A hibrida roadmap bridge
# (blueprint P1b; plan review III.1).
"""`worksession_migrate.py`: roadmap steps copied into plan-phase
``[USER_SPEC: …]`` work-session steps. Pure where it says so, settled roadmap
steps copied settled, idempotent across EVERY session (a plan's owner moves
when another session re-sets it), and honest about a copy whose roadmap text
has changed since."""
from datetime import datetime, timezone

from mindhive import contracts, events, roadmap
from mindhive import worksession_migrate as mig
from mindhive import worksessions as ws

TS = "2026-09-10T15:20:00Z"


def _clock(iso=TS):
    return lambda: datetime.strptime(iso, ws.TS_FORMAT).replace(tzinfo=timezone.utc)


def _plan(plan_id="P1", session_id="rs", steps=None):
    # Each `set` a second after the last one in the store: the ledger orders
    # same-second rows by segment name, and the owner under test is the LATEST
    # event's session.
    n = sum(1 for e in _evs() if e.type == "roadmap")
    events.append_event("roadmap", {
        "action": "set", "plan_id": plan_id, "prompt": "build the parser",
        "title": "Parser",
        "steps": steps if steps is not None else [
            {"text": "write the grammar", "status": "done"},
            {"text": "wire the CLI", "status": "active"},
            {"text": "docs", "status": "pending"},
            {"text": "perf", "status": "skipped"}]},
        session_id=session_id, ts=f"2026-09-10T10:00:{n:02d}Z", project="test")


def _evs():
    return events.read_all("test")


def _migrate(**kw):
    """What `hive worksession migrate` does: plan, then write each report's
    copies and settle its status moves."""
    report = mig.plan_migration(_evs(), now_fn=_clock(), **kw)
    for r in report:
        r["written"] = mig.record_migrated(r["session_id"], r.pop("copies"), project="test")
        r["settled"] = mig.settle_moves(r.pop("moves"), project="test")
    return report


def _step_status(plan_id, index, status, session_id="rs"):
    events.append_event("roadmap", {"action": "step", "plan_id": plan_id,
                                    "index": index, "status": status},
                        session_id=session_id, project="test")


def _ws_rows():
    return [e for e in _evs() if e.type == "work_session"]


# --- 1. the pure copy -------------------------------------------------------------------

def test_migrate_roadmap_copies_steps_into_plan_phase_user_spec_steps():
    _plan()
    before = len(_evs())
    out = mig.migrate_roadmap(_evs(), now_fn=_clock())
    assert list(out) == ["P1"]
    steps = out["P1"]
    assert [s.step_id for s in steps] == ["P1-1", "P1-2", "P1-3", "P1-4"]
    assert all(s.phase_tag == "plan" for s in steps)
    assert steps[0].technical_action == f"[WS: plan | write the grammar | {TS}]"
    assert steps[0].initiative_tag == "[USER_SPEC: write the grammar]"
    assert steps[0].usage_tag == f"[USAGE: 0/{ws.DEFAULT_TOKEN_BUDGET} | 0.0%]"
    # done and skipped are both settled; a skipped step copied as pending kept
    # its session active forever.
    assert [s.status for s in steps] == ["completed", "active", "pending", "completed"]
    assert len(_evs()) == before                                  # nothing appended
    assert roadmap.fold(_evs())["P1"]["status"] == "active"       # roadmap untouched


def test_a_retired_plan_copies_every_step_completed():
    _plan()
    events.append_event("roadmap", {"action": "complete", "plan_id": "P1"},
                        session_id="rs", project="test")
    assert roadmap.fold(_evs())["P1"]["status"] in roadmap.RETIRED_STATUSES
    steps = mig.migrate_roadmap(_evs(), now_fn=_clock())["P1"]
    assert {s.status for s in steps} == {"completed"}


def test_a_step_text_that_cleans_to_nothing_copies_as_the_default_spec():
    _plan(steps=[{"text": "[ | ]", "status": "pending"}])      # valid on the roadmap
    (step,) = mig.migrate_roadmap(_evs(), now_fn=_clock())["P1"]
    assert step.initiative_tag == f"[USER_SPEC: {ws.USER_SPEC_DEFAULT}]"
    assert ws.parse_work_session_tag(step.technical_action)[1] == ws.USER_SPEC_DEFAULT


# --- 2. the plan and the writes -------------------------------------------------------

def test_plan_migration_reports_without_writing():
    _plan()
    before = len(_evs())
    (r,) = mig.plan_migration(_evs(), now_fn=_clock())
    assert {k: v for k, v in r.items() if k != "copies"} == {
        "plan_id": "P1", "session_id": "rs", "steps": 4, "present": 0, "stale": 0,
        "pending": 4, "drifted": 0, "orphaned": 0, "settle": 0, "hand_moved": 0,
        "moves": []}
    assert [s.step_id for s in r["copies"]] == ["P1-1", "P1-2", "P1-3", "P1-4"]
    assert len(_evs()) == before


def test_record_migrated_writes_contract_valid_rows_into_the_owner_session():
    _plan()
    (r,) = _migrate()
    assert r["written"] == 4
    folded = ws.fold(_evs(), "test")
    assert [s.initiative_tag for s in folded["rs"].steps][:2] == \
        ["[USER_SPEC: write the grammar]", "[USER_SPEC: wire the CLI]"]
    assert folded["rs"].active_step_index == 1
    for ev in _ws_rows():
        contracts.validate_typed_payload("work_session", ev.payload)


def test_a_rerun_writes_nothing():
    _plan()
    _migrate()
    (r,) = _migrate()
    assert (r["pending"], r["present"], r["written"]) == (0, 4, 0)
    assert len(_ws_rows()) == 4


def test_a_plan_re_set_from_another_session_is_not_copied_again():
    _plan(steps=[{"text": "a", "status": "done"}, {"text": "b", "status": "pending"}])
    _migrate()
    _plan(session_id="r2",
          steps=[{"text": "a", "status": "done"}, {"text": "b", "status": "active"},
                 {"text": "c", "status": "pending"}])
    assert roadmap.fold(_evs())["P1"]["session_id"] == "r2"       # the owner moved
    (r,) = _migrate()
    # The copies stay with the session already holding them; only the new step lands.
    assert (r["session_id"], r["present"], r["pending"], r["written"]) == ("rs", 2, 1, 1)
    folded = ws.fold(_evs())
    assert set(folded) == {"rs"}
    assert [s.step_id for s in folded["rs"].steps] == ["P1-1", "P1-2", "P1-3"]


def test_a_copy_whose_roadmap_text_changed_is_reported_stale_not_rewritten():
    _plan(plan_id="P2", steps=[{"text": "only", "status": "pending"}])
    _migrate()
    _plan(plan_id="P2", steps=[{"text": "totally different", "status": "pending"},
                               {"text": "another", "status": "pending"}])
    (r,) = _migrate()
    assert (r["steps"], r["present"], r["stale"], r["pending"], r["written"]) == \
        (2, 1, 1, 1, 1)
    steps = ws.fold(_evs())["rs"].steps
    assert steps[0].initiative_tag == "[USER_SPEC: only]"          # never rewritten
    (again,) = mig.plan_migration(_evs(), now_fn=_clock())
    assert (again["present"], again["stale"], again["pending"]) == (2, 1, 0)


def test_a_roadmap_status_move_is_reported_drifted_and_settled_by_a_rerun():
    _plan(steps=[{"text": "a", "status": "active"}, {"text": "b", "status": "pending"},
                 {"text": "c", "status": "pending"}])
    _migrate()
    assert ws.fold(_evs())["rs"].status == "active"
    _step_status("P1", 0, "done")
    _step_status("P1", 1, "skipped")
    _step_status("P1", 2, "done")
    events.append_event("roadmap", {"action": "complete", "plan_id": "P1"},
                        session_id="rs", project="test")
    (plan,) = mig.plan_migration(_evs(), now_fn=_clock())
    assert (plan["present"], plan["drifted"], plan["settle"], plan["hand_moved"]) == \
        (3, 3, 3, 0)
    assert plan["moves"] == [("rs", "P1-1", "completed"), ("rs", "P1-2", "completed"),
                             ("rs", "P1-3", "completed")]
    (r,) = _migrate()
    assert (r["written"], r["settled"]) == (0, 3)
    assert ws.fold(_evs())["rs"].status == "completed"      # the session settles too
    (again,) = mig.plan_migration(_evs(), now_fn=_clock())
    assert (again["drifted"], again["settle"], again["moves"]) == (0, 0, [])
    moves = [e for e in _ws_rows() if e.payload["action"] == "step_status"]
    assert len(moves) == 3                                     # appended, never rewritten


def test_a_status_moved_by_hand_is_reported_never_overwritten():
    _plan(steps=[{"text": "a", "status": "active"}, {"text": "b", "status": "pending"}])
    _migrate()
    ws.record_step_status("rs", "P1-2", "blocked", project="test")    # by hand
    _step_status("P1", 0, "done")
    _step_status("P1", 1, "done")
    (plan,) = mig.plan_migration(_evs(), now_fn=_clock())
    assert (plan["drifted"], plan["settle"], plan["hand_moved"]) == (2, 1, 1)
    assert plan["moves"] == [("rs", "P1-1", "completed")]
    _migrate()
    steps = {s.step_id: s.status for s in ws.fold(_evs())["rs"].steps}
    assert steps == {"P1-1": "completed", "P1-2": "blocked"}


def test_a_step_that_left_the_roadmap_is_reported_orphaned_and_settled():
    _plan(steps=[{"text": "x", "status": "pending"}, {"text": "y", "status": "pending"},
                 {"text": "z", "status": "pending"}])
    _migrate()
    _plan(steps=[{"text": "x", "status": "pending"}, {"text": "y", "status": "pending"}])
    (plan,) = mig.plan_migration(_evs(), now_fn=_clock())
    assert (plan["steps"], plan["present"], plan["orphaned"], plan["settle"]) == (2, 2, 1, 1)
    assert plan["moves"] == [("rs", "P1-3", "completed")]
    _migrate()
    steps = {s.step_id: s.status for s in ws.fold(_evs())["rs"].steps}
    assert steps == {"P1-1": "pending", "P1-2": "pending", "P1-3": "completed"}
    (again,) = mig.plan_migration(_evs(), now_fn=_clock())
    # Still orphaned -- a fact, like `stale` -- but nothing left to settle.
    assert (again["orphaned"], again["settle"], again["hand_moved"]) == (1, 0, 0)


def test_orphans_are_matched_per_plan_id_not_by_prefix():
    _plan("P1", steps=[{"text": "a", "status": "pending"}])
    _plan("P1-2", steps=[{"text": "b", "status": "pending"}, {"text": "c", "status": "pending"}])
    _migrate()
    by_plan = {r["plan_id"]: r for r in mig.plan_migration(_evs(), now_fn=_clock())}
    # P1-2's copies are P1-2-1 and P1-2-2; neither is P1's step 2.
    assert (by_plan["P1"]["orphaned"], by_plan["P1-2"]["orphaned"]) == (0, 0)


def test_the_target_is_the_given_session_until_a_copy_exists():
    _plan()
    (r,) = _migrate(session="chosen")
    assert r["session_id"] == "chosen"
    (r,) = mig.plan_migration(_evs(), session="elsewhere", now_fn=_clock())
    assert r["session_id"] == "chosen" and r["pending"] == 0


def test_plan_id_filters_and_an_unknown_plan_reports_nothing():
    _plan("P1")
    _plan("P2")
    assert [r["plan_id"] for r in mig.plan_migration(_evs(), plan_id="P2")] == ["P2"]
    assert mig.plan_migration(_evs(), plan_id="P9") == []
    assert mig.plan_migration([], now_fn=_clock()) == []


def test_migrated_copies_ignores_steps_that_are_not_plan_phase_specs():
    ws.record_work_session_step("x", ws.create_work_session_step(
        "execution", "P1-1", False, now_fn=_clock()), project="test")
    ws.record_work_session_step("y", ws.create_work_session_step(
        "plan", "x", True, now_fn=_clock()), project="test")
    assert mig.migrated_copies(_evs()) == {}


def test_migrated_copies_carries_the_folded_and_the_copied_status():
    _plan(steps=[{"text": "a", "status": "active"}])
    _migrate()
    ws.record_step_status("rs", "P1-1", "blocked", project="test")
    assert mig.migrated_copies(iter(_evs())) == {
        "P1-1": ("rs", "[USER_SPEC: a]", "blocked", "active")}
