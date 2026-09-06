"""WB7 — the worker-cat board.

Four things are worth stating about how this file is organised, because each one
is a claim the tests have to actually earn rather than assert.

**The decoupling is checked as TEXT.** `dashboard.py`'s first paragraph promises
that `queue`, `resilience` and `lintester` do not import it. A test that only
constructed a bus and watched events arrive would pass just as happily with the
import present, so the guard reads the source and looks for the import — with a
canary proving the scanner can find one.

**The string seam is checked against a REAL run.** Producers publish
`"work_stolen"`, not `WorkStolen`, precisely so they need no import — and a
string seam drifts silently. `EventBus.unknown` records every kind it did not
recognise and every field it could not place, and the drift tests drive a real
`WorkStealingScheduler` and a real `LintesterOrchestrator` through a real bus and
assert that tuple is empty.

**`rich` is not installed on CI.** `dev` does not pull it in, so the fallback is
the path CI takes and the live board is the path this machine takes. Both are
covered: the fallback tests run everywhere, and the rich tests `importorskip`.
A suite that only tested the branch its own machine happens to have would ship a
broken renderer to whichever half of the matrix it is not.

**Two of these tests exist because a live run found a bug.** They are labelled.
`test_a_fall_back_on_lane_zero_is_not_reported_as_lane_minus_one` pins the `x or
-1` idiom that turned lane 0 into a phantom row on every board, and
`test_a_retried_attempt_is_not_counted_as_a_lost_unit` pins a header that read
`done 4  failed 1` for a mission of four units that lost none of them.
"""
from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

import pytest

from mindhive.orchestrator import dashboard as dash
from mindhive.orchestrator import lintester as lint_mod
from mindhive.orchestrator import queue as q_mod
from mindhive.orchestrator import resilience as res_mod


# --- helpers ------------------------------------------------------------------

class Recorder:
    """A bus stand-in with nothing but `emit`.

    Its whole point is that it is NOT an `EventBus`: if a producer needed
    anything more than the one method, this would fail and the seam would be
    wider than the docstring claims."""

    def __init__(self):
        self.rows: list = []

    def emit(self, kind, **fields):
        self.rows.append((kind, fields))

    def kinds(self) -> list:
        return [k for k, _f in self.rows]

    def first(self, kind):
        for k, fields in self.rows:
            if k == kind:
                return fields
        return None

    def all_of(self, kind) -> list:
        return [fields for k, fields in self.rows if k == kind]


class Exploding:
    """A bus that raises on every publish. Nothing may notice."""

    def __init__(self):
        self.calls = 0

    def emit(self, kind, **fields):
        self.calls += 1
        raise RuntimeError("the display is on fire")


class Failed:
    """What a runner returns to mean failure, in the shape `_succeeded` reads."""

    ok = False
    reason = "the child exited 1"
    stderr = "Traceback (most recent call last):\nValueError: nope"


def entry(task_id, files=(), weight=0):
    return q_mod.QueueEntry(task=lint_mod.AgentTask(
        task_id=task_id, files=tuple(files), weight=int(weight)))


def board_with(*events):
    board = dash.Board()
    for event in events:
        board.apply(event)
    return board


def drive(coro_fn):
    """Run one coroutine to completion. No `pytest-asyncio` — the `dev` extra is
    `pytest` alone, deliberately — so a coroutine test body carrying an
    `asyncio` marker would be collected, never awaited, and reported green. The
    rest of this suite drives coroutines the same way."""
    return asyncio.run(coro_fn())


class Stream:
    """A text sink with a chosen `encoding`.

    NOT an `io.StringIO` subclass: `encoding` is a read-only property on
    `_io._TextIOBase`, so a subclass cannot be given one. `icons_for` reads the
    attribute and `Dashboard._say` calls `print(..., file=self)`, which needs
    only `write`."""

    def __init__(self, encoding="utf-8"):
        self.encoding = encoding
        self._parts: list = []

    def write(self, text) -> int:
        self._parts.append(str(text))
        return len(str(text))

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        return "".join(self._parts)


# --- the bus ------------------------------------------------------------------

def test_a_bus_with_no_subscriber_still_counts_and_never_raises():
    """The default in every existing test of the scheduler: a mission running
    with nobody watching. Publishing into the void must be free, not fatal."""
    bus = dash.EventBus()
    bus.emit("task_started", lane=0, task_id="t-1")
    assert bus.published == 1
    assert bus.subscribers == 0
    assert bus.dropped == 0


def test_subscribe_and_unsubscribe_move_the_count():
    bus = dash.EventBus()
    queue = bus.subscribe()
    assert bus.subscribers == 1
    bus.unsubscribe(queue)
    assert bus.subscribers == 0
    bus.unsubscribe(queue)                              # idempotent
    assert bus.subscribers == 0


def test_emit_builds_the_typed_event_the_kind_names():
    bus = dash.EventBus()
    event = bus.emit("work_stolen", lane=3, victim=1, task_id="t-4",
                     target="src/auth.py")
    assert isinstance(event, dash.WorkStolen)
    assert (event.lane, event.victim, event.target) == (3, 1, "src/auth.py")
    assert bus.unknown == ()


@pytest.mark.parametrize("cls", dash.EVENTS)
def test_every_event_class_is_reachable_by_its_own_kind(cls):
    """The registry and the classes cannot disagree. A class added to `EVENTS`
    with a `KIND` nothing publishes is dead weight; one whose `KIND` collides
    with another's would shadow it silently."""
    event, complaint = dash.make_event(cls.KIND)
    assert isinstance(event, cls)
    assert complaint == ""


def test_an_unrecognised_kind_becomes_a_note_and_is_recorded():
    """The typo the string seam invites. It must not raise, must not vanish, and
    must be findable — `bus.unknown` is what the drift tests read."""
    bus = dash.EventBus()
    event = bus.emit("task_startd", lane=1)
    assert isinstance(event, dash.Note)
    assert bus.unknown == ("kind:task_startd",)
    assert "task_startd" in event.text


def test_an_unrecognised_field_costs_the_field_and_not_the_event():
    """A misspelled optional argument should show a row missing one column, not
    lose the row. The complaint still names it, so the drift test still fails."""
    bus = dash.EventBus()
    event = bus.emit("task_started", lane=2, task_id="t-1", wieght=9)
    assert isinstance(event, dash.TaskStarted)
    assert (event.lane, event.task_id) == (2, "t-1")
    assert bus.unknown == ("task_started:wieght",)


@pytest.mark.parametrize("kind", [None, "", 0, 17, object()])
def test_the_event_factory_is_total_over_junk_kinds(kind):
    event, complaint = dash.make_event(kind)
    assert isinstance(event, dash.Note)
    assert complaint


def test_a_subscriber_receives_what_was_published():
    async def go():
        bus = dash.EventBus()
        queue = bus.subscribe()
        bus.emit("brain_phase", phase=dash.PHASE_RUNNING, detail="4 lane(s)")
        return await asyncio.wait_for(queue.get(), 1.0)

    event = drive(go)
    assert isinstance(event, dash.BrainPhase)
    assert event.phase == dash.PHASE_RUNNING


def test_a_full_backlog_drops_the_oldest_and_says_it_did():
    """A board is a view of NOW. A backlog that discarded the present to keep
    the past would be the wrong end to lose — and a drop nobody counted would
    make a board that had silently stopped being about this mission look
    healthy."""
    def go():
        bus = dash.EventBus(capacity=2)
        queue = bus.subscribe()
        for n in range(4):
            bus.emit("task_started", lane=n, task_id=f"t-{n}")
        return bus.dropped, [queue.get_nowait().lane for _ in range(2)]

    dropped, got = go()
    assert dropped == 2
    assert got == [2, 3], "the newest events were not the ones kept"


def test_a_publish_from_a_worker_thread_reaches_the_loop():
    """Not theoretical. `WorkStealingScheduler._run_one` hands a blocking runner
    to `run_in_executor`, so real publishes arrive off the loop's thread — where
    `asyncio.Queue.put_nowait` is not safe to call, because waking a getter
    schedules on the future's loop."""
    async def go():
        bus = dash.EventBus()
        queue = bus.subscribe()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: bus.emit("task_finished", lane=1, task_id="t-1",
                                   state="validated"))
        return await asyncio.wait_for(queue.get(), 1.0)

    event = drive(go)
    assert isinstance(event, dash.TaskFinished)
    assert event.lane == 1


def test_binding_outside_a_loop_answers_none_rather_than_raising():
    bus = dash.EventBus()
    assert bus.bind() is None
    bus.emit("brain_phase", phase=dash.PHASE_IDLE)      # still total
    assert bus.published == 1


# --- the board's fold ---------------------------------------------------------

def test_a_started_task_turns_its_lane_green_with_its_file_and_weight():
    board = board_with(dash.TaskStarted(lane=0, task_id="t-1",
                                        target="src/auth.py", weight=42,
                                        attempt=1))
    view = board.lane(0)
    assert view.status == dash.WORKING
    assert (view.target, view.load) == ("src/auth.py", 42)
    assert "Working" in view.status_text(dash.ASCII_ICONS)


def test_a_task_with_no_file_shows_its_own_id_rather_than_a_blank_cell():
    board = board_with(dash.TaskStarted(lane=0, task_id="t-1", attempt=1))
    assert board.lane(0).target == "t-1"


def test_a_validated_task_returns_its_lane_to_idle_and_counts():
    board = board_with(
        dash.TaskStarted(lane=0, task_id="t-1", target="a.py", attempt=1),
        dash.TaskFinished(lane=0, task_id="t-1", state="validated",
                          elapsed_ms=12.0))
    view = board.lane(0)
    assert view.status == dash.IDLE
    assert (view.done, board.completed) == (1, 1)
    assert view.target == ""
    assert "12 ms" in view.note


def test_a_steal_names_the_lane_it_took_from():
    board = board_with(dash.WorkStolen(lane=3, victim=1, task_id="t-4",
                                       target="src/auth.py"))
    view = board.lane(3)
    assert view.status == dash.STEALING
    assert "from Lane-1" in view.status_text(dash.ASCII_ICONS)
    assert board.steals == 1


def test_the_steal_caption_is_the_directives_own_sentence():
    board = board_with(dash.WorkStolen(lane=3, victim=1, task_id="t-4",
                                       target="auth.py"))
    assert list(board.steal_log) == [
        "Cat 3 is stealing task [auth.py] from Cat 1"]


def test_a_steal_rides_the_row_instead_of_flashing_once():
    """The module docstring's load-bearing claim. A steal and the dispatch that
    follows it are microseconds apart and the board repaints four times a
    second, so a `WorkStolen` that only set a colour would be a frame nobody
    sees. The provenance has to survive the dispatch."""
    board = board_with(
        dash.WorkStolen(lane=3, victim=1, task_id="t-4", target="auth.py"),
        dash.TaskStarted(lane=3, task_id="t-4", target="auth.py", weight=8,
                         attempt=1))
    view = board.lane(3)
    assert view.status == dash.WORKING, "the cat should be working by now"
    assert view.note == "stolen from Lane-1", "the provenance was thrown away"


def test_a_different_task_clears_the_previous_steals_provenance():
    """The other half. A row that kept saying `stolen from Lane-1` about work it
    was handed honestly would be worse than saying nothing."""
    board = board_with(
        dash.WorkStolen(lane=3, victim=1, task_id="t-4", target="auth.py"),
        dash.TaskStarted(lane=3, task_id="t-9", target="pay.py", weight=8,
                         attempt=1))
    view = board.lane(3)
    assert view.stolen_from == -1
    assert view.note == ""


def test_an_injected_fall_back_turns_the_row_red_with_the_directives_caption():
    board = board_with(dash.FallbackTriggered(lane=2, task_id="t-4", attempt=1,
                                              injected=True,
                                              reason="pytest exited 1"))
    view = board.lane(2)
    assert view.status == dash.FALLBACK
    assert view.note == dash.INJECTED_CAPTION
    assert "Fall-back Loop" in view.status_text(dash.ASCII_ICONS)


def test_a_retry_with_no_injected_stderr_says_so_instead():
    """Two different claims. Self-correction means the failure went back into the
    prompt; a plain retry means the same prompt is going out again, and calling
    that self-correcting would be the launder this repository keeps refusing."""
    board = board_with(dash.FallbackTriggered(lane=2, task_id="t-4", attempt=2,
                                              injected=False))
    assert board.lane(2).note == "Self-Correcting (retry 2)"
    assert dash.INJECTED_CAPTION not in board.lane(2).note


def test_a_repair_attempt_stays_red_for_as_long_as_it_runs():
    """Found by a live run. Turning the row green the instant the retry
    dispatched put the self-correcting cycle on screen for a single frame and
    then hid the only part of it an operator can watch."""
    board = board_with(
        dash.TaskFinished(lane=2, task_id="t-4", state="failed"),
        dash.FallbackTriggered(lane=2, task_id="t-4", attempt=1, injected=True),
        dash.TaskStarted(lane=2, task_id="t-4", target="brief.py", weight=77,
                         attempt=2))
    view = board.lane(2)
    assert view.status == dash.FALLBACK, "the repair attempt went green"
    assert view.note == dash.INJECTED_CAPTION
    assert view.target == "brief.py", "the row lost its file"


def test_a_first_attempt_is_green_and_not_red():
    """The negative half of the test above. Without it the rule could be
    `always red` and every assertion there would still pass."""
    board = board_with(dash.TaskStarted(lane=2, task_id="t-4", attempt=1))
    assert board.lane(2).status == dash.WORKING


def test_a_retried_attempt_is_not_counted_as_a_lost_unit():
    """Found by a live run. Four units, one of which failed once and then
    passed, printed `done 4  failed 1` — which an operator reads as five units
    with one lost. The fall-back is the answer to a provisional failure, so it
    MOVES the tally rather than leaving it."""
    board = board_with(
        dash.TaskStarted(lane=0, task_id="t-bad", attempt=1),
        dash.TaskFinished(lane=0, task_id="t-bad", state="failed"),
        dash.FallbackTriggered(lane=0, task_id="t-bad", attempt=1,
                               injected=True),
        dash.TaskStarted(lane=0, task_id="t-bad", attempt=2),
        dash.TaskFinished(lane=0, task_id="t-bad", state="validated"))
    counts = board.counters()
    assert counts["completed"] == 1
    assert counts["failed"] == 0, "a unit that passed on retry was mourned"
    assert counts["retried"] == 1
    assert board.lane(0).failed == 0


def test_a_failure_no_fall_back_answered_stays_counted_as_a_failure():
    """The half that keeps the correction above from being a whitewash. With no
    repair loop configured nothing decides anything, and a lost unit must stay
    lost on the board."""
    board = board_with(
        dash.TaskStarted(lane=0, task_id="t-bad", attempt=1),
        dash.TaskFinished(lane=0, task_id="t-bad", state="abandoned",
                          reason="the breaker refused the next attempt"))
    assert board.counters()["failed"] == 1
    assert board.counters()["retried"] == 0


def test_a_mission_may_announce_itself_twice_and_the_second_only_adds():
    """`execute_mission` announces before it knows its own shape — so the header
    is not blank through the slowest phase — and again once the planner has told
    it. A replacing fold would let the second announcement, which carries the
    counts, blank the intent the first one carried."""
    board = board_with(
        dash.MissionStarted(mission_id="lm-1", intent="fix the writer"),
        dash.MissionStarted(mission_id="lm-1", intent="fix the writer",
                            lanes=3, units=6))
    assert board.intent == "fix the writer"
    assert board.units == 6
    assert sorted(board.lanes) == [0, 1, 2]


def test_a_later_announcement_cannot_erase_a_count():
    board = board_with(
        dash.MissionStarted(mission_id="lm-1", intent="fix it", units=6),
        dash.MissionStarted(mission_id="lm-1"))
    assert board.units == 6
    assert board.intent == "fix it"


def test_an_opened_breaker_is_a_footer_line_and_not_a_lane_row():
    """The directive asks the footer for CRITICAL warnings only, and a breaker
    is keyed by TARGET — it belongs to no lane. Inventing one would put a row on
    the board for a lane that does not exist."""
    board = board_with(dash.CircuitOpened(key="t-5", state="OPEN",
                                          reason="repeated failure"))
    assert board.lanes == {}
    assert len(board.alerts) == 1
    assert "breaker OPEN for t-5" in board.alerts[0].text


def test_the_footer_is_bounded_and_keeps_the_newest_warning():
    """The ring's length is chosen by the pane's height, so a ring longer than
    the pane can draw would hide the newest warning behind a border."""
    board = dash.Board()
    for n in range(dash.MAX_ALERTS + 3):
        board.apply(dash.CircuitOpened(key=f"t-{n}", state="OPEN"))
    assert len(board.alerts) == dash.MAX_ALERTS
    assert f"t-{dash.MAX_ALERTS + 2}" in board.alerts[-1].text


def test_a_finished_mission_stands_every_cat_down():
    board = board_with(
        dash.TaskStarted(lane=0, task_id="t-1", target="a.py", attempt=1),
        dash.MissionFinished(mission_id="lm-1", outcome="ok", completed=1))
    assert board.lane(0).status == dash.IDLE
    assert board.phase == dash.PHASE_DONE
    assert board.outcome == "ok"


@pytest.mark.parametrize("junk", [None, object(), {"lane": 1}, 17, "task_started"])
def test_the_fold_is_total_over_junk(junk):
    """It runs on a feed written by producers it does not own, the same contract
    `telemetry.fold` holds. What it will not do is absorb the failure silently —
    `ignored` counts it and the header prints the count."""
    board = dash.Board()
    assert board.apply(junk) is False
    assert board.ignored == 1
    assert board.applied == 0


def test_a_lane_id_that_is_not_a_number_lands_somewhere_rather_than_raising():
    board = board_with(dash.TaskStarted(lane="nonsense", task_id="t-1"))
    assert -1 in board.lanes


# --- icons --------------------------------------------------------------------

def test_a_utf8_stream_gets_the_directives_emoji():
    icons = dash.icons_for(Stream("utf-8"))
    assert icons == dash.ICONS
    assert icons[dash.WORKING] == "\U0001f7e2"


def test_a_windows_codepage_stream_gets_ascii_instead_of_a_crash():
    """`cp1252` is what a Windows console still defaults to, and this repository
    is developed on Windows. A board that raised `UnicodeEncodeError` on the
    primary machine would not be a board. Same shape as `resilience.fencing`:
    ask what the platform can do, then report which answer you got."""
    icons = dash.icons_for(Stream("cp1252"))
    assert icons == dash.ASCII_ICONS
    assert icons[dash.FALLBACK] == "[!!]"


@pytest.mark.parametrize("encoding", ["", "not-a-real-codec", None])
def test_an_unusable_encoding_degrades_rather_than_raising(encoding):
    assert dash.icons_for(Stream(encoding)) == dash.ASCII_ICONS


def test_every_status_has_an_icon_in_both_sets():
    """A status with no icon renders as a bare label in one set and not the
    other, which is the kind of drift nobody notices until a screenshot."""
    for status in dash.STATUSES:
        assert status in dash.ICONS
        assert status in dash.ASCII_ICONS
        assert status in dash.LABELS


# --- plain rendering ----------------------------------------------------------

def test_the_plain_board_names_the_brain_the_lanes_and_the_alerts():
    board = board_with(
        dash.MissionStarted(mission_id="lm-9f2a", intent="add retry", lanes=2,
                            units=4),
        dash.BrainPhase(phase=dash.PHASE_RUNNING, detail="2 lane(s)"),
        dash.TaskStarted(lane=0, task_id="t-1", target="src/events.py",
                         weight=118, attempt=1),
        dash.FallbackTriggered(lane=1, task_id="t-4", attempt=1, injected=True))
    text = dash.render_plain(board)
    for hook in ("LINTESTER BRAIN", "lm-9f2a", dash.PHASE_RUNNING, "add retry",
                 "src/events.py", "118", dash.INJECTED_CAPTION, "ALERTS"):
        assert hook in text, f"the plain board never mentions {hook!r}"


def test_an_empty_plain_board_says_it_is_empty():
    """A blank table and a table of idle lanes look identical, and only one of
    them means the board is working."""
    text = dash.render_plain(dash.Board())
    assert "no lane has reported yet" in text
    assert "none" in text


def test_the_plain_board_reports_events_it_could_not_read():
    board = dash.Board()
    board.apply(object())
    assert "could not be read" in dash.render_plain(board)


# --- rich rendering -----------------------------------------------------------

def test_rich_availability_is_reported_rather_than_assumed(monkeypatch):
    """CI does not install `rich` — `dev` does not pull it in — so the reason has
    to be a real sentence naming the fix, not a bare False."""
    import builtins

    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "rich" or name.startswith("rich."):
            raise ImportError("no module named 'rich'")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    available, reason = dash.rich_available()
    assert available is False
    assert "rich" in reason and "tui" in reason


def test_a_dashboard_falls_back_when_rich_is_missing(monkeypatch):
    monkeypatch.setattr(dash, "rich_available",
                        lambda: (False, "rich is not installed here"))
    board = dash.Dashboard(dash.EventBus(), stream=Stream())
    assert board.plain is True
    assert "not installed" in board.degraded


def test_the_layout_splits_the_terminal_into_the_directives_three_areas():
    pytest.importorskip("rich")
    layout = dash.build_layout(dash.Board(), dash.ASCII_ICONS)
    names = {child.name for child in layout.children}
    assert names == {"header", "body", "footer"}


def test_the_footer_pane_can_draw_every_alert_the_ring_holds():
    """The two numbers have to agree. A ring longer than the pane hides the
    newest warning behind a border, and the newest is the one that matters."""
    pytest.importorskip("rich")
    layout = dash.build_layout(dash.Board(), dash.ASCII_ICONS)
    footer = {child.name: child for child in layout.children}["footer"]
    assert footer.size >= dash.MAX_ALERTS + 2


def test_the_rendered_board_carries_every_feature_hook():
    """Not an HTTP 200. The rendered TEXT is grepped for the things the directive
    asked to see, because a layout that builds and draws nothing useful would
    pass every structural assertion above."""
    rich = pytest.importorskip("rich")
    from rich.console import Console

    board = board_with(
        dash.MissionStarted(mission_id="lm-9f2a", intent="add retry", lanes=4,
                            units=6),
        dash.BrainPhase(phase=dash.PHASE_MERGING, detail="4 lane(s)"),
        dash.TaskStarted(lane=0, task_id="t-1", target="src/events.py",
                         weight=118, attempt=1),
        dash.WorkStolen(lane=3, victim=0, task_id="t-6", target="src/auth.py"),
        dash.TaskStarted(lane=3, task_id="t-6", target="src/auth.py", weight=41,
                         attempt=1),
        dash.FallbackTriggered(lane=2, task_id="t-4", attempt=1, injected=True,
                               reason="pytest exited 1"),
        dash.CircuitOpened(key="t-5", state="OPEN", reason="repeated failure"))
    console = Console(file=io.StringIO(), width=110, record=True,
                      legacy_windows=False)
    console.print(dash.build_layout(board, dash.ASCII_ICONS))
    text = console.export_text()
    for hook in ("Lintester Brain", "lm-9f2a", "Worker Lanes", "Target File",
                 "Load", "Circuit Breaker & Alerts", "src/events.py",
                 "stolen from Lane-0", "breaker OPEN for t-5",
                 "Fall-back Loop"):
        assert hook in text, f"the rendered board never draws {hook!r}"
    assert dash.INJECTED_CAPTION.split(" (")[0] in text


def test_repainting_keeps_the_same_layout_object():
    """`Live` holds the object it was given. Rebuilding the layout each frame
    would throw away the one it is rendering, and the board would freeze."""
    pytest.importorskip("rich")
    board = dash.Board()
    layout = dash.build_layout(board, dash.ASCII_ICONS)
    before = [id(child) for child in layout.children]
    board.apply(dash.TaskStarted(lane=0, task_id="t-1", attempt=1))
    dash.update_layout(layout, board, dash.ASCII_ICONS)
    assert [id(child) for child in layout.children] == before


# --- the async runner ---------------------------------------------------------

def test_the_runner_drains_the_last_event_before_it_exits():
    """A board that exited on the stop signal alone would lose the last events
    of the mission — which include the ones saying how it ended."""
    board = dash.Board()

    async def go():
        bus = dash.EventBus()
        async with dash.attach(bus, board=board, plain=True,
                               stream=Stream()) as runner:
            bus.emit("mission_started", mission_id="lm-1", intent="go", lanes=1,
                     units=1)
            bus.emit("task_started", lane=0, task_id="t-1", target="a.py",
                     weight=3, attempt=1)
            bus.emit("mission_finished", mission_id="lm-1", outcome="ok",
                     completed=1)
        return runner

    runner = drive(go)
    assert board.outcome == "ok", "the closing event never reached the board"
    assert board.applied == 3
    assert runner.render_errors == 0


def test_the_fallback_writes_one_line_per_event_and_a_closing_snapshot():
    stream = Stream()

    async def go():
        bus = dash.EventBus()
        async with dash.attach(bus, plain=True, stream=stream):
            bus.emit("work_stolen", lane=3, victim=1, task_id="t-4",
                     target="auth.py")
            bus.emit("circuit_opened", key="t-5", state="OPEN",
                     reason="repeated failure")

    drive(go)
    text = stream.getvalue()
    assert "Cat 3 is stealing task [auth.py] from Cat 1" in text
    assert "breaker OPEN for t-5" in text
    assert "LINTESTER BRAIN" in text, "no closing snapshot was written"


def test_the_fallback_says_it_is_a_fallback():
    """`resilience.run_sandboxed` reports `timeout-only` rather than pretending
    it fenced a child it did not. Same bargain: a scrolling log presented as a
    live board would be the launder."""
    stream = Stream()

    async def go():
        async with dash.attach(dash.EventBus(), plain=True,
                               stream=stream) as runner:
            return runner

    runner = drive(go)
    assert runner.degraded
    assert "[board]" in stream.getvalue()


def test_a_failed_repaint_is_counted_rather_than_raised(monkeypatch):
    """A rendering bug must not kill a mission. A frozen board with no
    explanation is its own lie, though, so the failure is counted and named."""
    pytest.importorskip("rich")
    from rich.console import Console

    real = dash.update_layout
    state = {"first": True}

    def explode_after_the_build(*args, **kwargs):
        # The first call is `build_layout`'s, which the runner needs to survive
        # in order to reach the repaint this test is about.
        if state["first"]:
            state["first"] = False
            return real(*args, **kwargs)
        raise RuntimeError("the table is broken")

    console = Console(file=io.StringIO(), width=90, legacy_windows=False)

    async def go():
        bus = dash.EventBus()
        monkeypatch.setattr(dash, "update_layout", explode_after_the_build)
        async with dash.attach(bus, plain=False, console=console,
                               stream=Stream()) as runner:
            bus.emit("task_started", lane=0, task_id="t-1", attempt=1)
            await asyncio.sleep(0.4)
        return runner

    runner = drive(go)
    assert runner.render_errors >= 1
    assert "the table is broken" in runner.last_render_error
    assert runner.plain is False, "a repaint failure should not abandon the board"


def test_a_board_that_cannot_be_built_degrades_instead_of_killing_the_mission(
        monkeypatch):
    """Found by a live run of the test above. `_paint` counted a failed REPAINT,
    but the initial `build_layout` sat outside it — so a raise there propagated
    out of the task and took down the mission the board was only watching."""
    pytest.importorskip("rich")
    from rich.console import Console

    def explode(*_a, **_k):
        raise RuntimeError("the layout is broken")

    console = Console(file=io.StringIO(), width=90, legacy_windows=False)
    stream = Stream()

    async def go():
        bus = dash.EventBus()
        monkeypatch.setattr(dash, "update_layout", explode)
        async with dash.attach(bus, plain=False, console=console,
                               stream=stream) as runner:
            bus.emit("task_started", lane=0, task_id="t-1", target="a.py",
                     attempt=1)
        return runner

    runner = drive(go)
    assert runner.render_errors >= 1
    assert "the layout is broken" in runner.last_render_error
    assert runner.plain is True, "it should have fallen back to the log"
    text = stream.getvalue()
    assert "fell back to a log" in text, "the degradation was silent"
    assert "LINTESTER BRAIN" in text, "the fallback wrote no snapshot"
    assert runner.board.applied == 1, "the event was lost in the fallback"


def test_starting_twice_answers_the_same_task():
    bus = dash.EventBus()

    async def go():
        runner = dash.Dashboard(bus, plain=True, stream=Stream())
        first = runner.start()
        assert runner.start() is first
        await runner.aclose()

    drive(go)
    assert bus.subscribers == 0


def test_closing_a_runner_that_never_started_is_a_no_op():
    async def go():
        runner = dash.Dashboard(dash.EventBus(), plain=True, stream=Stream())
        await runner.aclose()                           # must not raise

    drive(go)


# --- the decoupling, checked as text -----------------------------------------

_PRODUCERS = ("queue.py", "resilience.py", "lintester.py")
# BOTH halves. Splitting `dashboard.py` at its 1000-line cap moved the event
# classes, the bus and the fold into `board.py`, and a scanner that still only
# knew the old name would have left a back door exactly as wide as the front
# one it guards. `\b` keeps `board` from matching inside `dashboard`, so the
# two alternations stay independent rather than one shadowing the other.
_TARGET = r"(?:dashboard|board)"
_IMPORT_RE = re.compile(rf"^\s*(?:from\s+\S*\s+import\s+[^\n]*\b{_TARGET}\b"
                        rf"|import\s+[^\n]*\b{_TARGET}\b"
                        rf"|from\s+\.?{_TARGET}\s+import)", re.MULTILINE)


def _producer_source(name) -> str:
    root = Path(q_mod.__file__).resolve().parent
    return (root / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _PRODUCERS)
def test_no_producer_imports_the_board_it_feeds(name):
    """`dashboard.py`'s first paragraph, enforced. A test that only constructed a
    bus and watched events arrive would pass just as happily with the import
    present, so this reads the source."""
    hits = _IMPORT_RE.findall(_producer_source(name))
    assert hits == [], f"{name} imports the dashboard: {hits}"


@pytest.mark.parametrize("planted", [
    "from . import dashboard as dash_mod",
    "from .dashboard import TaskStarted",
    "import mindhive.orchestrator.dashboard",
    "    from . import dashboard",
    "from . import board as board_mod",
    "from .board import TaskStarted",
    "import mindhive.orchestrator.board",
    "    from . import board",
])
def test_the_import_scanner_catches_a_planted_import(planted):
    """The canary. Without it a passing guard proves only that the scanner found
    nothing — including if it can find nothing."""
    assert _IMPORT_RE.findall(planted), f"scanner missed {planted!r}"


def test_the_import_scanner_leaves_prose_alone():
    ok = ('# see `dashboard.py` for why the arrow points that way\n'
          'from . import queue as q_mod\n'
          '"""The dashboard subscribes; this module only publishes."""\n')
    assert _IMPORT_RE.findall(ok) == []


def test_every_phase_the_mission_publishes_is_a_declared_one():
    """The other half of the string seam. A phase added at the producer and
    never declared would ship as a string one file has heard of."""
    text = _producer_source("lintester.py")
    published = set(re.findall(r'phase="([^"]+)"', text))
    assert published, "the scan found no published phase at all"
    undeclared = sorted(published - set(dash.BRAIN_PHASES))
    assert undeclared == [], (
        f"phases published by lintester and not in BRAIN_PHASES: {undeclared}")


# --- drift, checked against real runs ----------------------------------------

def test_a_real_scheduler_publishes_no_unknown_event():
    """THE drift test. The seam is a string precisely so no producer needs an
    import, and a string seam drifts silently — so a real scheduler, a real
    repair loop and a real bus run together and the drift record must be
    empty."""
    bus = dash.EventBus()
    lanes = [q_mod.WorkerLane(0, [entry("t-1", ("src/a.py",), 42),
                                  entry("t-2", ("src/b.py",), 9),
                                  entry("t-bad", ("src/c.py",), 30)]),
             q_mod.WorkerLane(1, []),
             q_mod.WorkerLane(2, [])]

    def runner(item, lane):
        return Failed() if item.task_id == "t-bad" and item.attempts < 2 else None

    repair = res_mod.RepairLoop(bus=bus, max_attempts=3)
    scheduler = q_mod.WorkStealingScheduler(lanes, runner, repair=repair,
                                            mission_id="lm-drift", bus=bus)
    completed, failed = asyncio.run(scheduler.run())

    assert bus.unknown == (), f"the string seam has drifted: {bus.unknown}"
    assert bus.published > 0
    assert scheduler.observer_drops == 0
    assert sorted(e.task_id for e in completed) == ["t-1", "t-2", "t-bad"]
    assert failed == []


def test_a_real_dry_run_mission_publishes_no_unknown_event(tmp_path):
    """The mission half. `execute_mission` publishes eight phase lines and two
    mission events, and every one of them has to be a kind and a field set the
    board recognises."""
    bus = dash.EventBus()
    mission = lint_mod.LintesterOrchestrator(
        root=str(tmp_path), project="wb7-drift", bus=bus,
        lanes=2).execute_mission("make the ledger writer retry a failed append",
                                 dry_run=True)
    assert mission.outcome == "dry-run"
    assert bus.unknown == (), f"the string seam has drifted: {bus.unknown}"
    assert bus.published >= 8


def test_a_real_dry_run_fills_a_board_end_to_end(tmp_path):
    """The whole pipe, with nothing faked between the mission and the render."""
    recorder = Recorder()
    lint_mod.LintesterOrchestrator(
        root=str(tmp_path), project="wb7-board", bus=recorder,
        lanes=2).execute_mission("make the ledger writer retry", dry_run=True)
    board = dash.Board()
    for kind, fields in recorder.rows:
        event, complaint = dash.make_event(kind, **fields)
        assert complaint == "", complaint
        board.apply(event)
    assert board.ignored == 0
    assert board.mission_id.startswith(lint_mod.MISSION_PREFIX)
    assert board.phase == dash.PHASE_DONE
    text = dash.render_plain(board)
    assert "LINTESTER BRAIN" in text
    assert board.mission_id in text


# --- the subscription window, and the events that fell outside it -------------
#
# The three tests above replay a `Recorder` into an OFFLINE board, and that is
# exactly how the defect below survived them: a replay has no subscription
# window, so it cannot notice that the real one opens too late. A code review
# caught it and a real `--execute` run confirmed it — 19 events published, 7
# delivered, `dropped == 0`, and a header reading `(no mission)  Waiting` over a
# mission that had just completed two units.

def _git(root, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=False)


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


def _two_units(_root_id, _title, _intent):
    return [("t-worktree", "Serialise the worktree add", "worktree add lock"),
            ("t-ledger", "Rotate the ledger segment", "ledger segment append")]


def test_a_publish_with_nobody_listening_is_held_rather_than_discarded():
    """The bus promises it 'counts what it could not deliver rather than hiding
    it'. Before this, a publish with no subscriber returned silently and was
    neither delivered NOR counted."""
    bus = dash.EventBus()
    bus.emit("mission_started", mission_id="m-1", intent="do the thing")
    bus.emit("brain_phase", phase=dash.PHASE_PARSING, detail="two files")
    assert bus.published == 2
    assert bus.dropped == 0

    queue = bus.subscribe()
    assert queue.qsize() == 2, "the backlog did not reach the first subscriber"
    board = dash.Board()
    while not queue.empty():
        board.apply(queue.get_nowait())
    assert board.mission_id == "m-1"
    assert board.phase == dash.PHASE_PARSING


def test_a_backlog_that_overflows_counts_the_loss_rather_than_hiding_it():
    """Bounded for the same reason a subscriber's queue is: a buffer that grows
    without a listener is a leak. The loss is COUNTED, which is the half the
    silent return never did."""
    bus = dash.EventBus(capacity=3)
    for i in range(5):
        bus.emit("note", text=f"n{i}")
    assert bus.published == 5
    assert bus.dropped == 2
    held = bus.drain()
    assert [e.text for e in held] == ["n2", "n3", "n4"], "kept the wrong end"
    assert bus.drain() == (), "drain did not clear what it handed over"


def test_the_header_survives_a_real_execute_mission(repo):
    """THE regression. A real bus, a real Dashboard subscribed through the real
    observer seam, a real mission — and the header pane has to say what mission
    ran, over what intent, and how it ended."""
    bus = dash.EventBus()
    board = dash.Board()
    stream = io.StringIO()

    mission = lint_mod.LintesterOrchestrator(
        root=repo, project="wb7-header", lanes=2, decomposer=_two_units,
        runner=lambda e, ln: True, bus=bus,
        observer=lambda: dash.attach(bus, board=board, plain=True,
                                     stream=stream),
    ).execute_mission("make the ledger writer retry", dry_run=False)
    for event in bus.drain():
        board.apply(event)

    assert mission.outcome == "ok", mission.reason
    assert bus.dropped == 0
    assert board.mission_id.startswith(lint_mod.MISSION_PREFIX)
    assert board.intent == "make the ledger writer retry"
    assert board.units == 2, "the header counted no units for a two-unit mission"
    assert board.completed == 2
    assert board.phase == dash.PHASE_DONE
    assert board.outcome == "ok"
    text = dash.render_plain(board, dash.ASCII_ICONS)
    assert "(no mission)" not in text
    assert board.mission_id in text


# --- what the producers actually publish -------------------------------------

def test_a_scheduler_with_no_bus_behaves_exactly_as_before():
    """The regression that matters most: every existing caller passes no bus."""
    lanes = [q_mod.WorkerLane(0, [entry("t-1"), entry("t-2")])]
    scheduler = q_mod.WorkStealingScheduler(lanes, lambda item, lane: None)
    completed, failed = asyncio.run(scheduler.run())
    assert sorted(e.task_id for e in completed) == ["t-1", "t-2"]
    assert failed == []
    assert scheduler.observer_drops == 0


def test_a_bus_that_raises_on_every_publish_costs_nothing_but_a_count():
    """A display is derived state. Losing it costs frames; it must never be able
    to cost a unit."""
    bus = Exploding()
    lanes = [q_mod.WorkerLane(0, [entry("t-1"), entry("t-2")])]
    scheduler = q_mod.WorkStealingScheduler(lanes, lambda item, lane: None,
                                            bus=bus)
    completed, failed = asyncio.run(scheduler.run())
    assert sorted(e.task_id for e in completed) == ["t-1", "t-2"]
    assert failed == []
    assert scheduler.observer_drops >= 1
    assert "the display is on fire" in scheduler.observer_error


def test_a_steal_is_published_with_its_victim_and_its_file():
    """Five entries on one lane and an empty sibling, which is the shape
    `test_orchestrator_queue.py`'s own steal test uses. Two entries and an
    instant runner is a race rather than a test: lane 0 can finish both before
    lane 1 is ever scheduled."""
    bus = Recorder()
    lanes = [q_mod.WorkerLane(0, [entry(f"t-{n}", (f"src/{n}.py",), n)
                                  for n in range(5)]),
             q_mod.WorkerLane(1, [])]
    scheduler = q_mod.WorkStealingScheduler(lanes, lambda item, lane: None,
                                            bus=bus)
    asyncio.run(scheduler.run())
    steals = bus.all_of("work_stolen")
    assert scheduler.steals >= 1, "the idle lane never stole; this proves nothing"
    assert scheduler.steals == len(steals), (
        "the scheduler counted steals it did not publish")
    row = steals[0]
    assert row["victim"] == 0 and row["lane"] == 1
    assert row["target"].startswith("src/")


def test_a_dispatch_is_published_with_the_weight_the_board_shows():
    bus = Recorder()
    lanes = [q_mod.WorkerLane(0, [entry("t-1", ("src/a.py",), 42)])]
    asyncio.run(q_mod.WorkStealingScheduler(
        lanes, lambda item, lane: None, bus=bus).run())
    row = bus.first("task_started")
    assert row == {"lane": 0, "task_id": "t-1", "target": "src/a.py",
                   "weight": 42, "attempt": 1}


def test_a_lane_announces_its_worktree_before_any_unit_runs():
    bus = Recorder()

    class Tree:
        path = "/tmp/lane-0"

    lanes = [q_mod.WorkerLane(0, [entry("t-1")], worktree=Tree())]
    asyncio.run(q_mod.WorkStealingScheduler(
        lanes, lambda item, lane: None, bus=bus).run())
    assert bus.kinds()[0] == "lane_opened"
    assert bus.first("lane_opened")["worktree"] == "/tmp/lane-0"


def test_the_target_helper_falls_back_to_the_task_id():
    """A unit with no file really is about itself, which is a truer answer than
    a blank cell."""
    assert q_mod.target_of(entry("t-1")) == "t-1"
    assert q_mod.target_of(entry("t-1", ("a.py", "b.py"))) == "a.py"
    assert q_mod.target_of(object()) == ""
    assert q_mod.weight_of(object()) == 0


def test_a_fall_back_on_lane_zero_is_not_reported_as_lane_minus_one():
    """Found by a live run. `int(getattr(lane, "lane_id", -1) or -1)` reads 0 as
    falsy, so every fall-back on the first lane became a phantom `Lane--1` row
    that no lane ever cleared."""
    bus = Recorder()
    lane = q_mod.WorkerLane(0, [])
    item = entry("t-bad")
    item.state = q_mod.FAILED
    item.reason = "the child exited 1"
    item.attempts = 1
    repair = res_mod.RepairLoop(bus=bus, max_attempts=3)
    assert asyncio.run(repair(item, lane)) is True
    row = bus.first("fallback_triggered")
    assert row is not None, "a re-queue published nothing"
    assert row["lane"] == 0, "lane 0 was published as something else"
    assert row["injected"] is True
    assert row["attempt"] == 1


def test_a_retry_with_nothing_to_inject_publishes_injected_false():
    """The negative half. Without it `injected` could be hardcoded True and the
    board would call every plain retry self-correction."""
    bus = Recorder()
    item = entry("t-bad")
    item.state = q_mod.FAILED
    item.reason = ""
    item.attempts = 1
    repair = res_mod.RepairLoop(bus=bus, max_attempts=3)
    assert asyncio.run(repair(item, q_mod.WorkerLane(1, []))) is True
    row = bus.first("fallback_triggered")
    assert row["lane"] == 1
    assert row["injected"] is False


def test_a_breaker_is_announced_once_per_opening_and_not_per_refusal():
    """On the TRANSITION only, for the same reason the reflection is: an alert
    repeated on every refused retry scrolls the other alerts away, and the ring
    holds five."""
    bus = Recorder()
    repair = res_mod.RepairLoop(bus=bus, max_attempts=99)
    item = entry("t-bad")
    item.reason = "boom"
    for _ in range(8):
        item.state = q_mod.FAILED
        repair.observe(item)
    opened = bus.all_of("circuit_opened")
    assert len(opened) == 1, f"published {len(opened)} openings"
    assert opened[0]["key"] == "t-bad"
    assert opened[0]["state"] == "OPEN"


def test_a_repair_loop_with_no_bus_still_repairs():
    item = entry("t-bad")
    item.state = q_mod.FAILED
    item.reason = "the child exited 1"
    item.attempts = 1
    repair = res_mod.RepairLoop(max_attempts=3)
    assert asyncio.run(repair(item, q_mod.WorkerLane(0, []))) is True
    assert repair.observer_drops == 0


# --- the front door -----------------------------------------------------------

def _args(**kwargs):
    class Args:
        pass

    args = Args()
    for key, value in {"board": False, "json": False, "execute": False,
                       **kwargs}.items():
        setattr(args, key, value)
    return args


def test_the_flag_is_off_by_default_and_costs_nothing():
    from mindhive.orchestrator import cli as cli_mod

    bus, observer, why, board, _stream = cli_mod._observer(_args(), False)
    assert (bus, observer, why, board) == (None, None, "", None)


def test_the_board_refuses_a_plan_and_says_why():
    """A flag that does nothing and says nothing teaches the operator that the
    feature is broken rather than that this run did not qualify."""
    from mindhive.orchestrator import cli as cli_mod

    bus, observer, why, board, _stream = cli_mod._observer(
        _args(board=True), False)
    assert bus is None and observer is None and board is None
    assert "plan" in why and why


def test_the_board_moves_to_stderr_rather_than_losing_to_the_json_report():
    """**This measurement CHANGED, deliberately.** It used to assert the board
    was switched OFF whenever `--json` was set, with "stdout" in the reason.

    The reason was true and the conclusion was not: `Dashboard` has always taken
    `stream=` and `console=`, so the two features never actually conflicted --
    only stdout being hardcoded made them look like they did. The requirement is
    that the JSON report owns stdout WHOLE, and moving the board to stderr meets
    it exactly, while a long run stops being unwatchable the moment a machine
    reads its output."""
    import sys

    from mindhive.orchestrator import cli as cli_mod

    bus, observer, why, board, stream = cli_mod._observer(
        _args(board=True, json=True), True)
    assert bus is not None and board is not None and callable(observer)
    assert stream is sys.stderr, "the board would have written into the report"
    assert why == "" or "rich" in why, why


def test_the_board_keeps_stdout_when_no_json_report_wants_it():
    """The canary. A fix that simply moved the board to stderr always would
    pass the test above and quietly relocate every ordinary run's display."""
    import sys

    from mindhive.orchestrator import cli as cli_mod

    _bus, _observer_fn, _why, _board, stream = cli_mod._observer(
        _args(board=True), True)
    assert stream is sys.stdout


def test_the_json_run_puts_its_closing_board_on_stderr_too():
    """The closing frame follows the live one. Printing this summary to stdout
    would put a human paragraph into the machine surface -- which is the very
    thing the old exclusivity existed to prevent, reintroduced one function
    later."""
    import io

    from mindhive.orchestrator import cli as cli_mod

    bus = dash.EventBus()
    board = dash.Board()
    bus.emit("mission_started", mission_id="m-1", intent="do the thing",
             units=1, lanes=1)
    sink = io.StringIO()
    cli_mod._close_board(bus, board, sink)
    assert "LINTESTER BRAIN" in sink.getvalue()


def test_a_started_run_gets_a_real_bus_and_a_real_observer():
    from mindhive.orchestrator import cli as cli_mod

    bus, observer, _why, board, _stream = cli_mod._observer(
        _args(board=True), True)
    assert isinstance(bus, dash.EventBus)
    assert isinstance(board, dash.Board)
    assert callable(observer)

    async def drive():
        async with observer() as runner:
            assert isinstance(runner, dash.Dashboard)
            assert runner.board is board, "the front door lost its own board"
        return True

    assert asyncio.run(drive()) is True
    assert bus.subscribers == 0, "the observer left a subscriber behind"


def test_the_front_door_draws_a_closing_board_after_the_mission_returns(capsys):
    """`Live` leaves nothing behind when it exits, and the mission's last two
    events are published after the lane phase has torn it down. The closing
    frame is the one the operator can still read."""
    from mindhive.orchestrator import cli as cli_mod

    bus = dash.EventBus()
    board = dash.Board()
    bus.emit("mission_started", mission_id="mn-abc", intent="do it", units=2)
    bus.emit("brain_phase", phase=dash.PHASE_MERGING)
    bus.emit("mission_finished", mission_id="mn-abc", outcome="ok", completed=2)

    cli_mod._close_board(bus, board)

    text = capsys.readouterr().out
    assert "mn-abc" in text, "the closing board never named the mission"
    assert dash.PHASE_DONE in text
    assert board.outcome == "ok"
    assert "dropped" not in text, "a permanent zero teaches the operator to skim"


def test_the_closing_board_reports_what_the_bus_could_not_deliver(capsys):
    """`dropped` and `unknown` had no reader anywhere in `src/`: the process
    exited holding both. Non-zero only, for the reason `hive mindnet brief`
    already prints `failed=` / `dropped=` / `withheld=` only when non-zero."""
    from mindhive.orchestrator import cli as cli_mod

    bus = dash.EventBus(capacity=2)
    for i in range(4):
        bus.emit("note", text=f"n{i}")
    bus.emit("no_such_kind", whatever=1)                # drift — also a Note
    # Five published into a backlog that holds two, so three fell off the old
    # end. The drift is counted separately, on `unknown`.
    assert bus.dropped == 3 and bus.unknown

    cli_mod._close_board(bus, dash.Board())

    text = capsys.readouterr().out
    assert "3 event(s) dropped" in text, text
    assert "unrecognised" in text and "no_such_kind" in text


def test_a_closing_board_that_cannot_be_drawn_costs_the_report_nothing():
    """Same bargain as everywhere else on this path: a display is derived
    state, and losing it must never take the mission report with it."""
    from mindhive.orchestrator import cli as cli_mod

    class Exploding:
        def drain(self):
            raise RuntimeError("no board for you")

    cli_mod._close_board(Exploding(), dash.Board())     # must not raise


# --- the observer must never be able to harm the observed ---------------------
#
# `_paint` and `_run_live` were guarded; the FALLBACK path was not, and it is
# the path CI takes (`rich` is not in `dev`) and the path any machine without
# `rich` takes. One malformed field in one event reached `log_line`, raised out
# of `_drain` -> `_pump` -> `_run` -> the display task -> `asyncio.run`, and
# killed the mission the board was only watching — orphaning its lane
# worktrees on the way out.

def test_a_bad_field_in_the_log_path_costs_a_line_and_not_the_mission():
    """`make_event` filters by field NAME and does not coerce, and the module
    accepts that a string seam can drift. So a drifted value reaches the
    renderer, and the renderer must absorb it."""
    bus = dash.EventBus()
    stream = Stream()

    async def go():
        async with dash.attach(bus, plain=True, stream=stream) as runner:
            # `elapsed_ms` is a number everywhere else; `log_line` does
            # `float(...)` on it.
            bus.emit("task_finished", task_id="t-1", lane=0,
                     state="validated", elapsed_ms="12ms")
            bus.emit("task_finished", task_id="t-2", lane=0,
                     state="validated", elapsed_ms=8)
            await asyncio.sleep(0)
        return runner

    runner = drive(go)                                  # must not raise
    assert runner.render_errors >= 1, "the bad line was not counted"
    assert "ValueError" in (runner.last_render_error or "")
    # The good event still logged and still folded — one bad line costs one
    # line, and the run carries on.
    assert "t-2" in stream.getvalue()
    assert runner.board.completed >= 1, "a display fault cost a unit"


def test_a_closing_render_that_raises_does_not_escape_the_display_task():
    """The last line `_run` prints is a whole-board render, and it sat outside
    every guard in the file."""
    bus = dash.EventBus()
    stream = Stream()

    async def go():
        async with dash.attach(bus, plain=True, stream=stream) as runner:
            runner.board = None                         # the render will raise
        return runner

    runner = drive(go)                                  # must not raise
    assert runner.render_errors >= 1


def test_the_mission_producer_counts_what_the_bus_lost():
    """The two lane-side producers count a failed publish. This one used a bare
    `pass`, so the module publishing the header the operator reads was the one
    whose losses left no trace."""
    class Exploding:
        def emit(self, kind, **fields):
            raise RuntimeError("no bus for you")

    orch = lint_mod.LintesterOrchestrator(bus=Exploding())
    orch._emit("brain_phase", phase=dash.PHASE_PARSING)
    orch._emit("brain_phase", phase=dash.PHASE_PACKING)
    assert orch.observer_drops == 2
    assert "RuntimeError" in orch.observer_error


def test_the_last_event_repairs_a_tally_the_queue_dropped():
    """A `task_finished` lost to backpressure under-counts for the rest of the
    run. The mission publishes its true totals in its final event, and the fold
    used to read the outcome off it and discard them."""
    board = board_with(
        dash.make_event("mission_started", mission_id="m-1", units=4, lanes=2)[0],
        dash.make_event("task_finished", task_id="t-1", lane=0,
                        state="validated")[0],
    )
    assert board.completed == 1, "precondition: three finishes were dropped"

    board.apply(dash.make_event("mission_finished", mission_id="m-1",
                                outcome="ok", completed=4, steals=3)[0])
    assert board.completed == 4, "the closing totals were thrown away"
    assert board.steals == 3
    assert board.outcome == "ok"


def test_the_closing_totals_never_undo_a_repair_the_board_already_folded():
    """`max`, not assignment. The mission computes its totals after the repair
    loop has spoken, so a fold that already moved a retried unit out of `failed`
    must not have it moved back."""
    board = board_with(
        dash.make_event("task_finished", task_id="t-1", lane=0,
                        state="validated")[0],
        dash.make_event("task_finished", task_id="t-2", lane=0,
                        state="validated")[0],
    )
    board.apply(dash.make_event("mission_finished", outcome="ok",
                                completed=1, failed=0)[0])
    assert board.completed == 2, "a stale closing total overwrote a live one"


def test_the_mission_binds_the_bus_to_the_loop_it_actually_runs_on():
    """A board that subscribed before the loop existed would build its queue
    against the wrong one, and every publish from a worker thread would be handed
    to a loop nothing is running."""
    bus = dash.EventBus()
    assert bus.bind() is None

    seen = {}

    async def drive():
        seen["loop"] = asyncio.get_running_loop()
        bus.bind()

    asyncio.run(drive())
    assert bus._loop is seen["loop"]
# --- the bus's two recorded corners ---------------------------------------------
# Both re-verified in docs/residual-review-findings/ and both latent: no caller
# in `src/` reuses a bus across two loops today, and no kind drifts today. They
# are fixed anyway because "latent" is a statement about the callers, and the
# next caller is the one that finds out.

def test_a_bus_reused_across_two_loops_rebinds_to_the_live_one():
    """THE DEFECT. `subscribe` bound only when `self._loop` was None, so a bus
    that had already served one `asyncio.run` kept that loop after it closed.
    Every later publish then took the `call_soon_threadsafe` path on a dead
    loop, raised, and was counted as dropped."""
    bus = dash.EventBus()

    async def _first():
        bus.subscribe()
        return asyncio.get_running_loop()

    first_loop = asyncio.run(_first())
    assert first_loop.is_closed(), "the first loop outlived its asyncio.run"

    async def _second():
        queue = bus.subscribe()
        bus.emit("task_started", lane=1, task_id="t-1")
        await asyncio.sleep(0)
        return asyncio.get_running_loop(), queue

    second_loop, queue = asyncio.run(_second())
    assert bus._loop is not first_loop, "the bus kept the closed loop"
    assert bus._loop is second_loop
    assert bus.dropped == 0, "a publish was lost to the dead loop"
    assert queue.qsize() >= 1, "the live subscriber received nothing"


def test_a_bus_used_inside_one_loop_does_not_rebind_needlessly():
    """THE CANARY. Re-binding on every `subscribe` would be indistinguishable
    from this test passing, so it pins the other half: inside ONE loop the
    remembered loop is the running one and nothing changes."""
    async def _once():
        bus = dash.EventBus()
        bus.subscribe()
        first = bus._loop
        bus.subscribe()
        return first, bus._loop, asyncio.get_running_loop()

    first, second, running = asyncio.run(_once())
    assert first is second is running


def test_an_explicit_bind_from_a_producer_thread_survives_subscribe():
    """`bind` stays public for a caller that builds the bus before the loop
    exists. With no loop running, `subscribe` must not throw that answer away
    -- only a CLOSED loop is replaced."""
    loop = asyncio.new_event_loop()
    try:
        bus = dash.EventBus()
        bus.bind(loop)
        bus.subscribe()                       # no running loop on this thread
        assert bus._loop is loop
    finally:
        loop.close()
    bus.subscribe()
    assert bus._loop is None, "the closed loop was kept"


def test_one_kind_drifting_forever_cannot_grow_the_drift_record():
    """THE DEFECT, P3. `unknown` was rebuilt as a longer tuple per complaint --
    unbounded, and O(n^2) in copying -- so a single misspelled kind emitted per
    event filled it with thousands of copies of one string."""
    bus = dash.EventBus(capacity=1)          # the record is full after one
    for _ in range(500):
        bus.emit("task_startd", lane=1)
    assert bus.unknown == ("kind:task_startd",), bus.unknown
    # `capacity=1` is what makes this discriminating rather than decorative: a
    # `_note_unknown` that did not recognise the repeat would find the record
    # already full and count 499 overflows for a kind it had already recorded.
    assert bus.unknown_dropped == 0, "a repeat was counted as an overflow"


def test_more_distinct_complaints_than_the_bound_are_counted_not_kept():
    """The bound itself, and the counted overflow. `dropped` makes the same
    bargain for the queue: bounded, never silent."""
    bus = dash.EventBus(capacity=3)
    for i in range(10):
        bus.emit(f"task_start{i}", lane=1)
    assert len(bus.unknown) == 3, bus.unknown
    assert bus.unknown_dropped == 7
    assert bus.unknown[0] == "kind:task_start0", "first-seen order was lost"


def test_a_clean_run_records_no_drift_and_no_overflow():
    """The canary for both counters. A record that is never empty reports drift
    on every run and is therefore worth nothing."""
    bus = dash.EventBus(capacity=3)
    for i in range(10):
        bus.emit("task_started", lane=i, task_id=f"t-{i}")
    assert bus.unknown == ()
    assert bus.unknown_dropped == 0
# --- the fold, the two renderers, and the state nothing drew ---------------------
# Four more findings from docs/residual-review-findings/, fixed together because
# they are one complaint in four places: state this board folds and then does
# not tell anybody about.

def test_an_unreadable_elapsed_time_does_not_half_apply_the_event():
    """THE DEFECT. `_finished` moved the counters and THEN did an inline
    `float(event.elapsed_ms)`. A field that would not convert raised after the
    mutation, `apply`'s guard caught it, and one event was counted as a
    completed unit AND as an event that could not be read -- the header then
    disagreed with itself about the same event."""
    board = dash.Board()
    event = dash.make_event("task_finished", lane=0, task_id="t-1",
                            state="validated", elapsed_ms="not a number")[0]

    assert board.apply(event) is True
    assert board.completed == 1
    assert board.ignored == 0, "the event was counted twice, in two ways"
    assert board.applied == 1
    assert board.lane(0).note == "validated", "an unreadable time cost the state"


def test_a_readable_elapsed_time_is_still_rendered():
    """THE CANARY. A `_float` that always answered its default would pass the
    test above and silently delete the timing from every row."""
    board = dash.Board()
    event = dash.make_event("task_finished", lane=0, task_id="t-1",
                            state="validated", elapsed_ms=1234)[0]
    board.apply(event)
    assert board.lane(0).note == "validated in 1234 ms"


def test_a_drifted_kind_reaches_the_footer_instead_of_vanishing():
    """THE DEFECT. `Note` is what an unrecognised kind becomes -- the one event
    carrying information about a producer and this board disagreeing about the
    string seam -- and it was the one event whose text was dropped, folded into
    `ignored` beside genuinely unreadable input."""
    board = dash.Board()
    event, complaint = dash.make_event("task_startd", lane=1)

    assert complaint, "make_event stopped complaining; this test proves nothing"
    assert board.apply(event) is True
    assert board.ignored == 0
    assert board.applied == 1
    assert any("task_startd" in alert.text for alert in board.alerts), board.alerts
    assert "task_startd" in dash.render_plain(board)


def test_genuinely_unreadable_input_is_still_ignored():
    """THE CANARY, and the reason `ignored` is still worth printing. A fold that
    applied everything would pass the test above and make the header's count a
    permanent zero."""
    board = dash.Board()
    assert board.apply(object()) is False
    assert board.ignored == 1
    assert board.applied == 0


def test_an_empty_note_carries_nothing_and_is_ignored():
    """A `Note` with no text has no drift to report, so it is not an alert. The
    branch is about the TEXT reaching the footer, not about the class."""
    board = dash.Board()
    assert board.apply(dash.Note(text="")) is False
    assert board.ignored == 1
    assert not board.alerts


def test_both_renderers_report_the_unreadable_count():
    """`Board`'s own docstring promises "`ignored` counts it and the header
    prints it". Only `render_plain` kept that promise, so the operator most
    likely to be watching -- the one with a terminal -- was the one not told."""
    rich_ok, _why = dash.rich_available()
    if not rich_ok:
        pytest.skip("rich is not installed on this machine")
    board = dash.Board()
    board.apply(object())
    assert board.counters()["ignored"] == 1

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=120, no_color=True).print(dash._header_panel(board))
    live = buf.getvalue()
    assert "could not be read" in live, live
    assert "could not be read" in dash.render_plain(board)


def test_a_clean_board_says_nothing_about_unreadable_events():
    """The canary for both renderers. A permanent notice is not a notice."""
    rich_ok, _why = dash.rich_available()
    if not rich_ok:
        pytest.skip("rich is not installed on this machine")
    from rich.console import Console

    board = dash.Board()
    buf = io.StringIO()
    Console(file=buf, width=120, no_color=True).print(dash._header_panel(board))
    assert "could not be read" not in buf.getvalue()
    assert "could not be read" not in dash.render_plain(board)


def test_a_frozen_live_board_says_so_when_it_exits():
    """THE DEFECT. The live path `return`ed saying nothing, so a board frozen by
    repeated repaint failure exited looking exactly like one that simply had
    nothing more to show -- while `render_errors` held the count nothing read."""
    bus = dash.EventBus()
    sink = io.StringIO()

    async def drive():
        board = dash.Dashboard(bus, plain=False, stream=sink,
                               refresh_per_second=50)
        # `_run_live` succeeds; the REPAINTS are what fail, which is the shape
        # the finding names -- a board that came up and then froze.
        async def _live():
            board._layout = object()
            board._paint(live=object())
            board._paint(live=object())
        board._run_live = _live
        board._queue = asyncio.Queue()
        board._stop = asyncio.Event()
        board._stop.set()
        await board._run()
        return board

    board = asyncio.run(drive())
    out = sink.getvalue()
    assert board.render_errors == 2, board.render_errors
    assert "repaint(s) failed" in out, out
    assert "LINTESTER BRAIN" in out, "the closing snapshot was not written"


def test_a_healthy_live_run_does_not_dump_a_plain_board_over_it():
    """THE CANARY. Reporting unconditionally would put a full text board over
    every successful run's restored terminal, which is the noise that teaches an
    operator to skim the line that matters."""
    bus = dash.EventBus()
    sink = io.StringIO()

    async def drive():
        board = dash.Dashboard(bus, plain=False, stream=sink)

        async def _live():
            return None

        board._run_live = _live
        board._queue = asyncio.Queue()
        board._stop = asyncio.Event()
        board._stop.set()
        await board._run()
        return board

    board = asyncio.run(drive())
    assert board.render_errors == 0
    assert sink.getvalue() == "", sink.getvalue()


def test_the_closing_frame_draws_the_steals_and_the_worktrees():
    """THE DEFECT. `steal_log` and `LaneView.worktree` were folded, bounded, and
    drawn by nothing at all. Drawn in the PLAIN renderer, which is the frame
    `_close_board` prints after the mission returns and therefore the one that
    persists -- `Live` leaves nothing behind when it exits."""
    board = dash.Board()
    board.apply(dash.make_event("lane_opened", lane=0,
                                worktree="/tmp/wt-0")[0])
    board.apply(dash.make_event("work_stolen", lane=1, victim=0,
                                task_id="t-9", target="src/auth.py")[0])
    out = dash.render_plain(board)
    assert "WORKTREES" in out and "/tmp/wt-0" in out, out
    assert "STEALS" in out and "src/auth.py" in out, out
    assert dash.steal_caption(1, 0, "src/auth.py") in out


def test_a_board_with_neither_draws_neither_heading():
    """The canary. Two permanent empty headings on every closing frame would be
    worse than not drawing the state at all."""
    board = dash.Board()
    board.apply(dash.make_event("mission_started", mission_id="m-1",
                                intent="do it", units=1, lanes=1)[0])
    out = dash.render_plain(board)
    assert "WORKTREES" not in out and "STEALS" not in out, out
