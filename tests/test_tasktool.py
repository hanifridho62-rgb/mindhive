# tests/test_tasktool.py
"""The accumulator that turns per-item task-tool calls back into a whole list.

`roadmap.sync_from_todos` needs the WHOLE todo list, because a `set` replaces a
plan's steps wholesale. The tool that used to supply one -- `TodoWrite`, which
posts its entire list every call -- is not the tool this harness sends. It sends
`TaskCreate` and `TaskUpdate`, one item at a time, and neither the call nor its
result carries the other items. So the list is accumulated across calls, and
these tests pin the three facts a plausible implementation gets wrong: the id
comes off the RESULT (a parallel batch answers out of order), that result is the
STRUCTURED object and not the prose line a human sees, and `deleted` is a real
status that removes a task rather than describing one.
"""
import json
import os
import subprocess
import sys

import pytest

from mindhive import events, paths, tasktool


def _created(n, subject, description=None, **extra):
    """One TaskCreate call, shaped the way the hook receives it."""
    inp = {"subject": subject, "description": description or subject,
           "activeForm": "doing " + subject, **extra}
    return inp, f"Task #{n} created successfully: {subject}"


def _rec(sid, tool, inp, resp, project="test"):
    return tasktool.record(sid, tool, inp, resp, project=project)


# --- the id comes off the result ---------------------------------------------

def test_a_create_lands_one_todo_under_the_id_the_result_reported():
    inp, resp = _created(7, "wire the mirror")
    out = _rec("s1", "TaskCreate", inp, resp)
    assert out == [{"content": "wire the mirror", "status": "pending"}]
    assert list(tasktool.load("test")["s1"]["tasks"]) == ["7"]


def test_a_parallel_batch_keeps_the_harness_ids_not_the_call_order():
    """The mutant this test exists to kill: counting creates instead of reading
    the result. A real session created five tasks in one message and was handed
    ids 5, 4, 1, 3, 2 in that order -- so a counter assigns every later status
    to the wrong task, and the plan reads as someone else's progress."""
    for n, subj in ((3, "third"), (1, "first"), (2, "second")):
        _rec("s1", "TaskCreate", *_created(n, subj))
    _rec("s1", "TaskUpdate", {"taskId": "1", "status": "in_progress"}, "Updated task #1 status")
    out = _rec("s1", "TaskUpdate", {"taskId": "3", "status": "completed"},
               "Updated task #3 status")
    # Ordered by the harness's own ids, not by when the calls arrived.
    assert out == [{"content": "first", "status": "in_progress"},
                   {"content": "second", "status": "pending"},
                   {"content": "third", "status": "completed"}]


def test_ids_sort_numerically_not_lexically():
    """`"10" < "9"` as strings, and a plan whose tenth step sorts before its
    ninth is a plan in the wrong order."""
    for n in (9, 10, 2):
        _rec("s1", "TaskCreate", *_created(n, f"step {n}"))
    out = _rec("s1", "TaskUpdate", {"taskId": "2", "status": "completed"},
               "Updated task #2 status")
    assert [t["content"] for t in out] == ["step 2", "step 9", "step 10"]


@pytest.mark.parametrize("resp", [
    {"content": "Task #4 created successfully: from a dict"},
    {"text": "Task #4 created successfully: from a dict"},
    [{"type": "text", "text": "Task #4 created successfully: from blocks"}],
])
def test_the_id_is_found_whatever_shape_the_response_arrives_in(resp):
    """Every real result observed is a plain string, but the hook payload's
    `tool_response` is not contractually one -- the Skill path already handles
    dicts and content-block lists, so this one reuses that reader rather than
    assuming the shape that happens to be current."""
    out = _rec("s1", "TaskCreate", {"subject": "shape probe"}, resp)
    assert out == [{"content": "shape probe", "status": "pending"}]
    assert list(tasktool.load("test")["s1"]["tasks"]) == ["4"]


# --- statuses ----------------------------------------------------------------

def test_a_deleted_task_leaves_the_list_instead_of_becoming_a_step():
    """`deleted` is a real status (5 of 346 observed updates) and is not a step
    state. An abandoned batch has to stop showing up as pending work -- and
    `_TODO_STATUS` has no entry for it, so a naive pass-through would silently
    drop the whole step and leave the count wrong instead."""
    _rec("s1", "TaskCreate", *_created(1, "keep me"))
    _rec("s1", "TaskCreate", *_created(2, "drop me"))
    out = _rec("s1", "TaskUpdate", {"taskId": "2", "status": "deleted"},
               "Updated task #2 deleted")
    assert out == [{"content": "keep me", "status": "pending"}]
    assert list(tasktool.load("test")["s1"]["tasks"]) == ["1"]


def test_deleting_the_last_task_yields_an_empty_list_not_none():
    """An empty list and None mean different things to the caller: None is
    "nothing to say about this call", [] is "this session's list is now
    empty". `sync_from_todos` no-ops on both, but only one of them is true."""
    _rec("s1", "TaskCreate", *_created(1, "only"))
    assert _rec("s1", "TaskUpdate", {"taskId": "1", "status": "deleted"},
                "Updated task #1 deleted") == []


def test_an_update_can_rename_its_task():
    """`subject` rides 4 of the observed updates and `description` 6. A rename
    that did not land would leave the plan naming work that no longer exists."""
    _rec("s1", "TaskCreate", *_created(1, "old name"))
    out = _rec("s1", "TaskUpdate",
               {"taskId": "1", "subject": "new name", "status": "in_progress"},
               "Updated task #1 subject, status")
    assert out == [{"content": "new name", "status": "in_progress"}]


def test_an_unknown_status_leaves_the_previous_one_alone():
    _rec("s1", "TaskCreate", *_created(1, "steady"))
    _rec("s1", "TaskUpdate", {"taskId": "1", "status": "in_progress"},
         "Updated task #1 status")
    out = _rec("s1", "TaskUpdate", {"taskId": "1", "status": "wat"},
               "Updated task #1 status")
    assert out == [{"content": "steady", "status": "in_progress"}]


def test_a_blockedby_only_update_still_reports_the_list():
    """14 observed updates carry only `addBlockedBy`. They change nothing this
    module models, but the caller still wants the current list -- returning
    None there would make the roadmap miss a legitimate resync opportunity."""
    _rec("s1", "TaskCreate", *_created(1, "blocked thing"))
    out = _rec("s1", "TaskUpdate", {"taskId": "1", "addBlockedBy": ["2"]},
               "Updated task #1 blockedBy")
    assert out == [{"content": "blocked thing", "status": "pending"}]


def test_a_create_may_declare_its_own_status():
    inp, resp = _created(1, "already going", status="in_progress")
    assert _rec("s1", "TaskCreate", inp, resp) == \
        [{"content": "already going", "status": "in_progress"}]


# --- the degraded paths ------------------------------------------------------

def test_a_create_with_no_readable_id_still_lands_exactly_once():
    """The batch form (`{"tasks": [...]}`) is the one observed result of 239
    that carries no id. Losing the step entirely under-reports the plan;
    landing it twice misreports the count. Keyed by its own subject, it does
    neither -- it simply can never be updated by id afterwards."""
    out = _rec("s1", "TaskCreate", {"subject": "idless"}, "created, somehow")
    assert out == [{"content": "idless", "status": "pending"}]
    again = _rec("s1", "TaskCreate", {"subject": "idless"}, "created, somehow")
    assert again == [{"content": "idless", "status": "pending"}]


def test_an_idless_task_sorts_after_every_numbered_one():
    _rec("s1", "TaskCreate", {"subject": "idless"}, None)
    _rec("s1", "TaskCreate", *_created(99, "numbered"))
    out = _rec("s1", "TaskUpdate", {"taskId": "99", "status": "completed"},
               "Updated task #99 status")
    assert [t["content"] for t in out] == ["numbered", "idless"]


def test_an_update_for_a_task_never_seen_is_not_invented():
    """The sidecar can legitimately be missing a task -- it was created before
    the hook was wired, or the file was evicted. Materialising a step with no
    text from a bare id would put a blank line on the plan."""
    assert _rec("s1", "TaskUpdate", {"taskId": "5", "status": "completed"},
                "Updated task #5 status") is None


@pytest.mark.parametrize("sid,tool,inp", [
    ("", "TaskCreate", {"subject": "no session"}),
    (None, "TaskCreate", {"subject": "no session"}),
    ("s1", "TaskCreate", {}),
    ("s1", "TaskCreate", "not a dict"),
    ("s1", "Edit", {"file_path": "a.py"}),
    ("s1", "TaskUpdate", {}),
])
def test_nothing_it_cannot_use_reaches_the_ledger(sid, tool, inp):
    assert _rec(sid, tool, inp, "whatever") is None
    assert not events.read_all("test")


def test_a_corrupt_sidecar_is_treated_as_empty_rather_than_fatal():
    """Fail-open, like every other read on the hook path: a session must never
    break because a JSON file on disk got truncated."""
    p = tasktool.sidecar_path("test")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert tasktool.load("test") == {}
    assert _rec("s1", "TaskCreate", *_created(1, "after the corruption")) == \
        [{"content": "after the corruption", "status": "pending"}]


def test_the_sidecar_lands_beside_the_other_namespace_sidecars():
    _rec("s1", "TaskCreate", *_created(1, "somewhere"))
    p = tasktool.sidecar_path("test")
    assert p.parent == paths.project_dir("test")
    assert json.loads(p.read_text(encoding="utf-8"))["s1"]["tasks"]["1"]["text"] \
        == "somewhere"


# --- bounds ------------------------------------------------------------------

def test_one_session_cannot_grow_the_sidecar_without_bound():
    for n in range(1, tasktool.MAX_TASKS + 6):
        _rec("s1", "TaskCreate", *_created(n, f"task {n}"))
    kept = tasktool.load("test")["s1"]["tasks"]
    assert len(kept) == tasktool.MAX_TASKS
    # The newest work is what a live plan is about, so the lowest ids go.
    assert "1" not in kept and str(tasktool.MAX_TASKS + 5) in kept


def test_old_sessions_are_evicted_rather_than_accumulating_forever():
    for i in range(tasktool.MAX_SESSIONS + 3):
        _rec(f"s{i}", "TaskCreate", *_created(1, f"in session {i}"))
    sessions = tasktool.load("test")
    assert len(sessions) == tasktool.MAX_SESSIONS
    assert "s0" not in sessions
    assert f"s{tasktool.MAX_SESSIONS + 2}" in sessions


def test_eviction_keeps_the_session_that_was_touched_most_recently():
    """Insertion order is not recency: a long-running session that keeps
    updating tasks must outlive newer ones that went quiet."""
    for i in range(tasktool.MAX_SESSIONS):
        _rec(f"s{i}", "TaskCreate", *_created(1, f"in session {i}"))
    _rec("s0", "TaskUpdate", {"taskId": "1", "status": "completed"},
         "Updated task #1 status")
    _rec("fresh", "TaskCreate", *_created(1, "brand new"))
    sessions = tasktool.load("test")
    assert "s0" in sessions and "fresh" in sessions
    assert "s1" not in sessions


def test_recency_is_counted_not_timed():
    """The bug this pins was found by the eviction test above: `seen` is a
    one-second ledger stamp, and two tool calls inside one second is the
    ordinary case -- so ranking by it made eviction a coin toss. The counter is
    exact whatever the clock does."""
    _rec("a", "TaskCreate", *_created(1, "first"))
    _rec("b", "TaskCreate", *_created(1, "second"))
    state = tasktool.load("test")
    # NOT `seen == seen`. That was the assert, and it was an intermittent of
    # exactly the kind this repo already tracks two of: `seen` is a
    # one-second stamp, two sidecar writes take real milliseconds, and a run
    # straddling a UTC second boundary went red with correct code. The claim
    # this test makes is carried entirely by the counter; the stamp is only
    # required to be monotone.
    assert state["b"]["n"] > state["a"]["n"]
    assert state["a"]["seen"] <= state["b"]["seen"]


def test_an_entry_written_before_the_counter_existed_still_sorts():
    """Totality over its own on-disk history: a sidecar from before `n` reads
    as oldest rather than raising."""
    p = tasktool.sidecar_path("test")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"legacy": {"tasks": {"1": {"text": "old",
                                                       "status": "pending"}},
                                        "seen": "2026-01-01T00:00:00Z"}}),
                 encoding="utf-8")
    for i in range(tasktool.MAX_SESSIONS):
        _rec(f"s{i}", "TaskCreate", *_created(1, f"new {i}"))
    assert "legacy" not in tasktool.load("test")


# --- the shape the hook is ACTUALLY handed -----------------------------------
# Every helper above feeds `_created`, whose response is the prose line
# `Task #N created successfully: ...`. That is what the transcript RENDERS and
# what a human reads back; it is NOT what the hook is handed. Measured on the
# 18 MB transcript of the session that caught this, 2026-08-20:
# `"toolUseResult":"Task #` appears 0 times, while every task result carries a
# structured `taskId`. Reading only the prose put every create under
# `_SUBJECT_KEY`, so every later update looked up an id that was never a key and
# the plan froze at 0/N -- live, on the first real traffic the feature ever saw.

def _created_real(n, subject, **extra):
    """One TaskCreate, with the STRUCTURED result the hook receives."""
    inp = {"subject": subject, "description": subject,
           "activeForm": "doing " + subject, **extra}
    return inp, {"success": True, "taskId": str(n), "subject": subject}


def _updated_real(n, frm, to):
    """One TaskUpdate, verbatim the shape the transcript recorded:
    {"success": true, "taskId": "1", "updatedFields": ["status"],
     "statusChange": {"from": "pending", "to": "in_progress"}}"""
    return ({"taskId": str(n), "status": to},
            {"success": True, "taskId": str(n), "updatedFields": ["status"],
             "statusChange": {"from": frm, "to": to}})


def test_the_structured_result_yields_an_id_not_the_subject_fallback():
    inp, resp = _created_real(1, "restart the dashboard")
    assert _rec("s1", "TaskCreate", inp, resp) == [
        {"content": "restart the dashboard", "status": "pending"}]
    assert list(tasktool.load("test")["s1"]["tasks"]) == ["1"], (
        "the create fell back to the subject key, so no update can reach it")


def test_create_then_update_moves_the_step_with_the_real_shapes():
    """THE REGRESSION, replayed exactly as production sent it: five creates
    and one update. Before the fix the update was dropped and the plan stayed
    at 0/5 -- the very symptom the whole feature exists to remove."""
    for i in (1, 2, 3, 4, 5):
        _rec("s1", "TaskCreate", *_created_real(i, f"step {i}"))
    out = _rec("s1", "TaskUpdate", *_updated_real(3, "pending", "completed"))
    assert out is not None, "the update was dropped, so progress cannot move"
    assert out[2] == {"content": "step 3", "status": "completed"}
    assert [x["status"] for x in out] == ["pending", "pending", "completed",
                                          "pending", "pending"]


def test_the_structured_id_wins_over_a_number_in_the_prose():
    """Both readers can fire on one response. The structured field is the
    harness's own answer; the prose is a rendering of it, and a rendering that
    disagrees must not win."""
    resp = {"taskId": "4",
            "text": "Task #9 created successfully: two numbers"}
    _rec("s1", "TaskCreate", {"subject": "two numbers"}, resp)
    assert list(tasktool.load("test")["s1"]["tasks"]) == ["4"]


@pytest.mark.parametrize("raw,expected", [
    ({"taskId": 7}, "7"),                    # an int id is still an id
    ({"taskId": "  8  "}, "8"),              # trimmed, like every other field
    ({"task_id": "9"}, "9"),                 # snake spelling
    ({"id": "10"}, "10"),                    # bare
    ({"taskId": True}, ""),                  # isinstance(True, int) is True
    ({"taskId": None}, ""),
    ({"taskId": ""}, ""),
    ({"success": True}, ""),                 # nothing readable at all
    ("Task #11 created successfully: x", "11"),   # the prose still works
])
def test_the_id_reader_takes_the_spellings_and_refuses_the_rest(raw, expected):
    assert tasktool._result_id(raw) == expected


def test_a_frozen_plan_is_never_silent_again():
    """The class, not the instance. If EVERY stored key is subject-based then
    no create ever read an id, so no update can ever land and the plan freezes at
    0/N. That is precisely how this feature spent its whole life dead while the
    hook around it appended 23,973 events without one error. One line in
    hooks_errors.log is the difference between finding that in minutes and
    finding it in months."""
    log = paths.store_home() / "hooks_errors.log"
    _rec("s1", "TaskCreate", {"subject": "no id anywhere"}, {"success": True})
    assert list(tasktool.load("test")["s1"]["tasks"]) == \
        [tasktool._SUBJECT_KEY + "no id anywhere"]
    assert _rec("s1", "TaskUpdate", {"taskId": "1", "status": "completed"},
                {"success": True, "taskId": "1"}) is None
    assert "tasktool_id_drift" in log.read_text(encoding="utf-8")


def test_the_alarm_stays_quiet_when_ids_plainly_work():
    """An update for a task that was evicted, or created before the hook was
    wired, is ordinary and must not cry drift. The signature is that NO key has an
    id -- not that this one is missing."""
    log = paths.store_home() / "hooks_errors.log"
    _rec("s1", "TaskCreate", *_created_real(1, "has an id"))
    assert _rec("s1", "TaskUpdate", {"taskId": "99", "status": "completed"},
                {"success": True, "taskId": "99"}) is None
    assert not log.exists() or "tasktool_id_drift" not in \
        log.read_text(encoding="utf-8")


# --- what the pre-merge audit found ------------------------------------------

def test_a_batch_records_every_task_not_just_the_first():
    """`any()` over a GENERATOR stops at the first truthy element, and
    `_create` returns True for every entry with a subject -- so entry 1 landed
    and entries 2..N were never folded at all. The docstring one line above
    said "folding each entry keeps the work visible"; the code dropped all but
    one step of the plan, and the batch branch had no test that entered it.

    A batch result carries no id, so the lost tasks can never be reintroduced
    by a later TaskUpdate -- the loss is permanent, not a missed resync."""
    batch = {"tasks": [{"subject": "alpha"}, {"subject": "beta"},
                       {"subject": "gamma"}, {"subject": "delta"}]}
    out = _rec("s1", "TaskCreate", batch, "created 4 tasks")
    # Every entry lands. The ORDER is lexical, not the batch's own: entries in a
    # batch carry no id, so they are keyed by SUBJECT -- which is exactly what
    # makes the same create arriving twice update one entry instead of
    # duplicating a step -- and `todos()` sorts its keys. Declared order is
    # cosmetic, dedup is correctness, so the trade is deliberate and "delta"
    # sorting before "gamma" is the shape of it.
    assert [x["content"] for x in out] == ["alpha", "beta", "delta", "gamma"]
    assert len(tasktool.load("test")["s1"]["tasks"]) == 4


def test_a_refused_create_neither_lands_nor_overwrites():
    """An error envelope is not a creation receipt.

    `_response_text` reads a dict's `error` key by design, so a failure whose
    message happens to name a task number handed that number to `_create`,
    which wrote its new record straight over the existing one -- destroying a
    completed step's text and reverting its status, then writing that
    regression into the ledger through the roadmap `set`.

    `_response_ok` exists in the same module for exactly this discrimination
    and the Skill path already calls it."""
    _rec("s1", "TaskCreate", *_created_real(12, "deploy"))
    _rec("s1", "TaskUpdate", *_updated_real(12, "pending", "completed"))
    refused = {"is_error": True,
               "error": "Cannot create: task #12 must be unblocked first"}
    assert _rec("s1", "TaskCreate", {"subject": "write docs"},
                refused) is None
    tasks = tasktool.load("test")["s1"]["tasks"]
    assert tasks == {"12": {"text": "deploy", "status": "completed"}}


def test_two_processes_do_not_clobber_each_others_lists(tmp_path):
    """MEASURED, not reasoned: this is the P0.

    `record` was load-modify-save with no serialisation, and every PostToolUse
    hook is its own process. Each concurrent writer folded onto a stale
    whole-file snapshot and the last one to land became the file -- dropping
    not just the other call's task but every task, and every SESSION, the
    loser had not yet seen. The audit measured 13 of 50 tasks surviving with
    this exact shape, and whole sessions absent from the file in two of three
    trials.

    Real processes on purpose. An in-process simulation would prove something
    about monkeypatching; the defect is about two interpreters."""
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys" + chr(10)
        + "from mindhive import tasktool" + chr(10)
        + "sid = sys.argv[1]" + chr(10)
        + "for i in range(25):" + chr(10)
        + "    tasktool.record(sid, 'TaskCreate',"
        + "                    {'subject': sid + ' task ' + str(i)},"
        + "                    {'success': True,"
        + "                     'taskId': sid + '-' + str(i)},"
        + "                    project='probe')" + chr(10),
        encoding="utf-8")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ,
               PYTHONPATH=os.path.join(repo, "src"),
               MINDHIVE_HOME=str(tmp_path / "store"))
    procs = [subprocess.Popen([sys.executable, str(worker), sid], env=env)
             for sid in ("sess-a", "sess-b")]
    for p in procs:
        assert p.wait(timeout=180) == 0

    sidecar = tmp_path / "store" / "probe" / "task_tools.json"
    state = json.loads(sidecar.read_text(encoding="utf-8"))
    assert set(state) == {"sess-a", "sess-b"}, (
        "a whole session's list was clobbered: %s" % sorted(state))
    for sid in ("sess-a", "sess-b"):
        assert len(state[sid]["tasks"]) == 25, (
            "%s kept %d of 25 tasks" % (sid, len(state[sid]["tasks"])))


def test_deleting_the_last_task_clears_the_plan():
    """The final delete has to reach the ledger.

    `record` returns `[]` for "the list is empty now" and None for "nothing to
    say", and the docstring calls that distinction deliberate -- but the only
    consumer collapsed it: `sync_from_todos` computed `steps` and returned None
    `if not steps`. So deleting tasks one at a time worked until the last one,
    and then the final step stayed pending on the plan forever. The Oversee
    card and the awaiting-resume list kept reporting work the harness had
    deleted, and no later call could clear it because every later list is empty.

    `roadmap set requires a non-empty steps list`, so the honest exit is
    `archive` with a reason -- the documented state between finished and still
    open."""
    from mindhive import roadmap, roadmap_auto

    out = _rec("sess-2", "TaskCreate", *_created_real(1, "only step"))
    assert roadmap_auto.sync_from_todos("sess-2", out, project="test")
    gone = _rec("sess-2", "TaskUpdate",
                {"taskId": "1", "status": "deleted"},
                {"success": True, "taskId": "1"})
    assert gone == []
    assert roadmap_auto.sync_from_todos("sess-2", gone, project="test"), \
        "the final delete never reached the ledger"
    rec = roadmap.fold(events.read_all("test"))["sess-2"]
    assert rec["status"] == "archived"
    assert "deleted" in rec["archive_reason"].lower()


def test_an_empty_list_keeps_injected_steps_instead_of_archiving():
    """The other half of the same decision. An injected step is
    evidence-backed work the replenishment injector put on the plan, and this
    module already holds that a trimmed todo reappears while an injected step
    never comes back. So an empty todo list clears the harness's steps and
    keeps the injected ones -- a `set` with what is left, not an archive."""
    from mindhive import events as ev, roadmap, roadmap_auto

    ev.append_event("roadmap",
                    {"action": "set", "plan_id": "sess-2",
                     "prompt": "the ask", "title": "the ask",
                     "steps": [{"text": "harness step", "status": "pending"},
                               {"text": "injected step", "status": "pending",
                                "new": True}]},
                    session_id="sess-2", tags=["roadmap", "auto"],
                    project="test")
    assert roadmap_auto.sync_from_todos("sess-2", [], project="test")
    rec = roadmap.fold(events.read_all("test"))["sess-2"]
    assert rec["status"] == "active"
    assert [s["text"] for s in rec["steps"]] == ["injected step"]
