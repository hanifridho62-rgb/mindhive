# tests/test_tasktool.py
"""The accumulator that turns per-item task-tool calls back into a whole list.

`roadmap.sync_from_todos` needs the WHOLE todo list, because a `set` replaces a
plan's steps wholesale. The tool that used to supply one -- `TodoWrite`, which
posts its entire list every call -- is not the tool this harness sends. It sends
`TaskCreate` and `TaskUpdate`, one item at a time, and neither the call nor its
result carries the other items. So the list is accumulated across calls, and
these tests pin the two facts a plausible implementation gets wrong: the id
comes off the RESULT (a parallel batch answers out of order), and `deleted` is a
real status that removes a task rather than describing one.
"""
import json

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
    assert state["a"]["seen"] == state["b"]["seen"]     # same second, normally
    assert state["b"]["n"] > state["a"]["n"]


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
