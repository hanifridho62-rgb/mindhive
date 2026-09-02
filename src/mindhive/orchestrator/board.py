# src/mindhive/orchestrator/board.py
"""WB7 — the half of the board that needs no terminal.

Split out of `dashboard.py` when that module reached its 1000-line cap
(`tests/test_file_size_cap.py`) with a real defect still to fix. The seam is
the one `dashboard.py`'s own docstring already drew: **`Board` is a pure fold
over events — no clock, no I/O, no `rich`** — so everything up to and including
that fold lives here, and everything that arranges strings for a screen lives
there.

Nothing in this module imports a renderer, and nothing here may learn to. The
arrow that matters runs the other way:

    producers ──emit──> EventBus ──asyncio.Queue──> Board ──> (dashboard) render

`dashboard.py` re-exports every public name below, so `from .dashboard import
Board` keeps working and every existing monkeypatch address is unmoved — the
two names the suite patches, `rich_available` and `update_layout`, are
renderers and stayed put. This repository's ledger records a mechanical split
silently breaking three such addresses; that is why this one names them.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import sys
from dataclasses import dataclass, field


# --- the seam's vocabulary -----------------------------------------------------
# Producers publish these strings. They are constants here so a reader has one
# place to look, and plain strings at the producer so no producer needs an
# import. `EventBus.unknown` is what keeps the two halves honest.

MISSION_STARTED = "mission_started"
BRAIN_PHASE = "brain_phase"
LANE_OPENED = "lane_opened"
TASK_STARTED = "task_started"
TASK_FINISHED = "task_finished"
WORK_STOLEN = "work_stolen"
FALLBACK_TRIGGERED = "fallback_triggered"
CIRCUIT_OPENED = "circuit_opened"
MISSION_FINISHED = "mission_finished"
NOTE = "note"


# --- Event State objects -------------------------------------------------------
# Frozen, because an event is a statement about a moment that has already passed
# and a subscriber must not be able to edit what another subscriber will read.
#
# `KIND` carries no annotation on purpose: an un-annotated class attribute is not
# a dataclass field, so it names the event without becoming a constructor
# argument every producer would then have to pass.


@dataclass(frozen=True)
class MissionStarted:
    KIND = MISSION_STARTED
    mission_id: str = ""
    intent: str = ""
    lanes: int = 0
    units: int = 0


@dataclass(frozen=True)
class BrainPhase:
    """What the central orchestrator is doing right now. The header's only feed."""

    KIND = BRAIN_PHASE
    phase: str = ""
    detail: str = ""


@dataclass(frozen=True)
class LaneOpened:
    KIND = LANE_OPENED
    lane: int = -1
    worktree: str = ""
    depth: int = 0


@dataclass(frozen=True)
class TaskStarted:
    KIND = TASK_STARTED
    lane: int = -1
    task_id: str = ""
    target: str = ""
    weight: int = 0
    attempt: int = 0


@dataclass(frozen=True)
class TaskFinished:
    KIND = TASK_FINISHED
    lane: int = -1
    task_id: str = ""
    state: str = ""
    elapsed_ms: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class WorkStolen:
    """*lane* took *task_id* from *victim*. The mechanic the PRD wanted visible."""

    KIND = WORK_STOLEN
    lane: int = -1
    victim: int = -1
    task_id: str = ""
    target: str = ""


@dataclass(frozen=True)
class FallbackTriggered:
    """A failed unit is going round again. `injected` says whether its own
    defused stderr went into the next prompt, which is the difference between
    self-correction and a plain retry — two different claims, two captions."""

    KIND = FALLBACK_TRIGGERED
    lane: int = -1
    task_id: str = ""
    attempt: int = 0
    injected: bool = False
    reason: str = ""


@dataclass(frozen=True)
class CircuitOpened:
    KIND = CIRCUIT_OPENED
    key: str = ""
    state: str = ""
    reason: str = ""


@dataclass(frozen=True)
class MissionFinished:
    KIND = MISSION_FINISHED
    mission_id: str = ""
    outcome: str = ""
    completed: int = 0
    failed: int = 0
    steals: int = 0
    reason: str = ""


@dataclass(frozen=True)
class Note:
    """What an unrecognised kind becomes. Never raised, always counted."""

    KIND = NOTE
    text: str = ""
    fields: tuple = ()


EVENTS = (MissionStarted, BrainPhase, LaneOpened, TaskStarted, TaskFinished,
          WorkStolen, FallbackTriggered, CircuitOpened, MissionFinished, Note)

_BY_KIND = {cls.KIND: cls for cls in EVENTS}


def make_event(kind, **fields) -> tuple:
    """``(event, complaint)`` for one published *kind*.

    *complaint* is empty when the kind and every field were recognised, and is
    the string `EventBus.unknown` collects otherwise.

    An unplaceable FIELD does not cost the whole event: the recognised fields
    still build a real event and the extras are merely reported. A producer that
    misspells one optional argument should show a row missing one column, not
    lose the row. An unrecognised KIND is different — there is nothing to build
    — and becomes a `Note` carrying what it was given."""
    cls = _BY_KIND.get(str(kind or ""))
    if cls is None:
        return (Note(text=f"unrecognised event kind {kind!r}",
                     fields=tuple(sorted(fields))), f"kind:{kind}")
    names = {f.name for f in dataclasses.fields(cls)}
    extra = sorted(set(fields) - names)
    try:
        event = cls(**{k: v for k, v in fields.items() if k in names})
    except (TypeError, ValueError) as exc:              # pragma: no cover - guard
        return (Note(text=f"{kind} could not be built: {exc}"), f"{kind}:build")
    return event, (f"{kind}:{','.join(extra)}" if extra else "")


# --- the bus -------------------------------------------------------------------

# One subscriber's backlog. Bounded because the producer is a scheduler that
# never awaits the consumer: an unbounded queue behind a stalled renderer is a
# memory leak wearing a feature's clothes.
DEFAULT_CAPACITY = 512


def _elsewhere(loop) -> bool:
    """Is this call happening off *loop*'s own thread?

    A blocking runner reaches `WorkStealingScheduler` through
    `run_in_executor`, so some publishes really do arrive on a worker thread —
    where `asyncio.Queue.put_nowait` is not safe to call, because waking a
    getter schedules on the future's loop."""
    try:
        return asyncio.get_running_loop() is not loop
    except RuntimeError:
        return True


class EventBus:
    """Publish-subscribe over `asyncio.Queue`. One queue per subscriber.

    Producers only ever call `emit`. It is TOTAL — no publish can raise into a
    mission — and it counts what it could not deliver rather than hiding it:
    `dropped` is the overflow count and `unknown` is the drift record."""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        self.capacity = max(1, int(capacity))
        self.published = 0
        self.dropped = 0
        self.unknown: tuple = ()
        self._subs: list = []
        self._held: collections.deque = collections.deque()
        self._loop = None

    def __repr__(self) -> str:                          # pragma: no cover - debug aid
        return (f"<EventBus subs={len(self._subs)} published={self.published} "
                f"dropped={self.dropped}>")

    def bind(self, loop=None):
        """Remember which loop the subscribers' queues belong to.

        `subscribe` is the only caller in `src` today — checked, because the
        line that stood here claimed the mission called it too, just before
        starting its lanes, and nothing does. It stays public for a caller
        that builds a bus before the loop exists and publishes from a worker
        thread, and because `subscribe` will not re-bind a loop it already
        remembers: a bus reused across two `asyncio.run` calls keeps the
        first, closed loop, and every later publish is counted as dropped."""
        if loop is not None:
            self._loop = loop
            return self._loop
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        return self._loop

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self):
        if self._loop is None:
            self.bind()
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.capacity)
        self._subs.append(queue)
        for event in self.drain():
            queue.put_nowait(event)
        return queue

    def drain(self) -> tuple:
        """Take what was published while nobody was listening, and clear it.

        `subscribe` calls it, and so does the front door once the mission has
        returned — the closing phase and the outcome are published after the
        lane phase has torn the only subscriber down."""
        held = tuple(self._held)
        self._held.clear()
        return held

    def unsubscribe(self, queue) -> None:
        try:
            self._subs.remove(queue)
        except ValueError:
            pass

    def emit(self, kind, **fields):
        """The producers' whole surface. Answers the event it published."""
        event, complaint = make_event(kind, **fields)
        if complaint:
            self.unknown = self.unknown + (complaint,)
        self.publish(event)
        return event

    def publish(self, event) -> None:
        self.published += 1
        if not self._subs:
            # HELD, not discarded. The mission announces itself and names six
            # phases before the lane phase opens the only subscription, so a
            # bare `return` here left the header reading `(no mission)
            # Waiting` over a finished run — and said `dropped == 0` while it
            # did, which is the one thing this class promises never to do.
            if len(self._held) >= self.capacity:
                self._held.popleft()
                self.dropped += 1
            self._held.append(event)
            return
        loop = self._loop
        if loop is not None and _elsewhere(loop):
            try:
                loop.call_soon_threadsafe(self._fanout, event)
            except RuntimeError:
                # The loop closed under us — the mission is already ending.
                self.dropped += 1
            return
        self._fanout(event)

    def _fanout(self, event) -> None:
        for queue in tuple(self._subs):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the OLDEST, keep the newest. A board is a view of NOW; a
                # backlog that discards the present to preserve the past is the
                # wrong end to lose.
                self.dropped += 1
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass


# --- lane state ----------------------------------------------------------------

WORKING = "working"
IDLE = "idle"
STEALING = "stealing"
FALLBACK = "fallback"

STATUSES = (WORKING, IDLE, STEALING, FALLBACK)

# The directive's four colours.
ICONS = {WORKING: "\U0001f7e2", IDLE: "\U0001f7e1",
         STEALING: "\U0001f7e3", FALLBACK: "\U0001f534"}

# Windows consoles still default to a codepage that cannot encode those, and a
# board that raises `UnicodeEncodeError` on the machine this repository is
# primarily developed on is not a board. Same shape as `resilience.fencing`:
# find out what the platform can actually do, then say which one you got.
ASCII_ICONS = {WORKING: "[on]", IDLE: "[--]", STEALING: "[<<]", FALLBACK: "[!!]"}

LABELS = {WORKING: "Working", IDLE: "Idle", STEALING: "Stealing",
          FALLBACK: "Fall-back Loop"}

# The caption a self-correcting cycle earns, and only when stderr really went
# back into the prompt.
INJECTED_CAPTION = "Self-Correcting (Stderr Injected)"

# The header's phases. Spaces in every one of them, which also keeps them clear
# of invariant 10's single-token literal scan.
PHASE_IDLE = "Waiting"
PHASE_DECOMPOSING = "Decomposing Intent"
PHASE_PARSING = "Parsing AST"
PHASE_WEIGHING = "Calculating Weights"
PHASE_PACKING = "Packing Lanes"
PHASE_ISOLATING = "Isolating Worktrees"
PHASE_BRIEFING = "Composing Briefs"
PHASE_RUNNING = "Running Lanes"
PHASE_MERGING = "Merging Worktrees"
PHASE_DONE = "Mission Complete"

# The declared vocabulary. `phase` is free display prose as far as the board is
# concerned — it is printed, never keyed on — so nothing here VALIDATES it at
# runtime. What this tuple buys is a test: the suite scans `lintester.py` for the
# phase literals it publishes and asserts every one appears below, so a phase
# added at the producer and never documented here fails rather than shipping as
# a string only one file has ever heard of. `PHASE_IDLE` and `PHASE_DONE` are the
# board's own two, set by `Board` rather than published.
BRAIN_PHASES = (PHASE_IDLE, PHASE_DECOMPOSING, PHASE_PARSING, PHASE_WEIGHING,
                PHASE_PACKING, PHASE_ISOLATING, PHASE_BRIEFING, PHASE_RUNNING,
                PHASE_MERGING, PHASE_DONE)

# Five, and the number is chosen by the footer's HEIGHT rather than by
# taste: `build_layout` gives the alert pane a fixed size, and a ring that
# held more than the pane can draw would silently hide the newest warning
# behind a border. A deque keeps the newest, which is the right end to keep.
MAX_ALERTS = 5
MAX_STEALS = 6


def icons_for(stream=None) -> dict:
    """The icon set *stream* can actually encode, emoji where it can."""
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or ""
    try:
        "".join(ICONS.values()).encode(encoding or "ascii", errors="strict")
    except (LookupError, UnicodeEncodeError, TypeError, AttributeError):
        return dict(ASCII_ICONS)
    return dict(ICONS)


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value) -> str:
    try:
        return str(value or "")
    except (TypeError, ValueError):                     # pragma: no cover - guard
        return ""


@dataclass
class LaneView:
    """One cat's row. Mutable, because it IS the lane's current state."""

    lane_id: int = -1
    status: str = IDLE
    task_id: str = ""
    target: str = ""
    load: int = 0
    note: str = ""
    worktree: str = ""
    stolen_from: int = -1
    attempt: int = 0
    done: int = 0
    failed: int = 0
    steals: int = 0

    @property
    def name(self) -> str:
        return f"Lane-{self.lane_id}"

    def status_text(self, icons=None) -> str:
        """The Active Status column: icon, label, and — while stealing — from whom."""
        table = icons if icons is not None else ICONS
        label = LABELS.get(self.status, self.status)
        if self.status == STEALING and self.stolen_from >= 0:
            label = f"{label} from Lane-{self.stolen_from}"
        return f"{table.get(self.status, '')} {label}".strip()


@dataclass(frozen=True)
class Alert:
    """A footer line. Only two producers can make one — see `Board._alert`."""

    kind: str = ""
    lane: int = -1
    text: str = ""


class Board:
    """The fold. Events in, a renderable state out, and nothing else in between.

    `apply` is TOTAL for the same reason `telemetry.fold` is: it runs on a feed
    written by producers this class does not own, and one malformed row must not
    take out the display. What it will not do is silently absorb the failure —
    `ignored` counts it and the header prints it."""

    def __init__(self, max_alerts=MAX_ALERTS, max_steals=MAX_STEALS):
        self.mission_id = ""
        self.intent = ""
        self.phase = PHASE_IDLE
        self.detail = ""
        self.outcome = ""
        self.reason = ""
        self.units = 0
        self.completed = 0
        self.failed = 0
        # An attempt that failed and was re-queued is not a lost unit, and the
        # header must not read as though it were. See `_fell_back`.
        self.retried = 0
        self.steals = 0
        self.applied = 0
        self.ignored = 0
        self.lanes: dict = {}
        # task_id -> the lane view that counted its failure, for attempts the
        # repair loop has not answered yet. Bounded by the units in flight, which
        # is bounded by the lane count.
        self._unanswered: dict = {}
        self.alerts: collections.deque = collections.deque(
            maxlen=max(1, int(max_alerts)))
        self.steal_log: collections.deque = collections.deque(
            maxlen=max(1, int(max_steals)))

    # ---- access -------------------------------------------------------------

    def lane(self, lane_id) -> LaneView:
        key = _int(lane_id, -1)
        view = self.lanes.get(key)
        if view is None:
            view = LaneView(lane_id=key)
            self.lanes[key] = view
        return view

    def rows(self) -> list:
        return [self.lanes[key] for key in sorted(self.lanes)]

    def counters(self) -> dict:
        return {"lanes": len(self.lanes), "units": self.units,
                "completed": self.completed, "failed": self.failed,
                "retried": self.retried, "steals": self.steals,
                "alerts": len(self.alerts), "applied": self.applied,
                "ignored": self.ignored}

    # ---- the fold -----------------------------------------------------------

    def apply(self, event) -> bool:
        """Fold one event in. ``True`` when it changed something."""
        try:
            return self._apply(event)
        except Exception:                               # noqa: BLE001
            self.ignored += 1
            return False

    def _apply(self, event) -> bool:
        if isinstance(event, MissionStarted):
            # REFINING, not replacing, and that is what lets a mission announce
            # itself twice: once when it knows its own name, and again when the
            # planner has told it how many units and lanes it actually has. A
            # later announcement may only add — otherwise the second one, sent
            # with the counts, would blank the intent the first one carried.
            self.mission_id = _text(event.mission_id) or self.mission_id
            self.intent = _text(event.intent) or self.intent
            self.units = max(self.units, _int(event.units))
            for lane_id in range(_int(event.lanes)):
                self.lane(lane_id)
        elif isinstance(event, BrainPhase):
            self.phase = _text(event.phase) or self.phase
            self.detail = _text(event.detail)
        elif isinstance(event, LaneOpened):
            view = self.lane(event.lane)
            view.worktree = _text(event.worktree)
            view.status = IDLE
        elif isinstance(event, TaskStarted):
            self._started(event)
        elif isinstance(event, TaskFinished):
            self._finished(event)
        elif isinstance(event, WorkStolen):
            self._stolen(event)
        elif isinstance(event, FallbackTriggered):
            self._fell_back(event)
        elif isinstance(event, CircuitOpened):
            self._alert(CIRCUIT_OPENED, -1,
                        f"breaker OPEN for {_text(event.key)}"
                        + (f" — {_text(event.reason)}" if event.reason else ""))
        elif isinstance(event, MissionFinished):
            self.outcome = _text(event.outcome)
            self.reason = _text(event.reason)
            self.phase = PHASE_DONE
            self.detail = self.outcome
            # RECONCILE. The queue drops under backpressure and a lost
            # `task_finished` under-counts for the rest of the run with no way
            # back — except that the mission publishes its true totals in its
            # last event, and this branch used to read the outcome and throw
            # them away. `max` because the mission computes these AFTER the
            # repair loop has had its say, so it cannot undo `_fell_back`'s
            # retried-not-failed correction.
            self.completed = max(self.completed, _int(event.completed))
            self.failed = max(self.failed, _int(event.failed))
            self.steals = max(self.steals, _int(event.steals))
            for view in self.lanes.values():
                view.status = IDLE
                view.task_id = ""
                view.target = ""
        else:
            self.ignored += 1
            return False
        self.applied += 1
        return True

    def _started(self, event) -> None:
        view = self.lane(event.lane)
        task_id = _text(event.task_id)
        attempt = _int(event.attempt)
        # A steal's provenance survives the dispatch that follows it — see the
        # module docstring. It is cleared only when a DIFFERENT unit starts.
        if view.stolen_from >= 0 and view.task_id and view.task_id != task_id:
            view.stolen_from = -1
            view.note = ""
        # A repair attempt stays RED for as long as it runs. Turning the row
        # green the instant the retry dispatched would put the self-correcting
        # cycle on screen for a single frame and then hide the only part of it an
        # operator can actually watch — the same argument the module docstring
        # makes about a steal.
        view.status = FALLBACK if attempt > 1 else WORKING
        view.task_id = task_id
        view.target = _text(event.target) or task_id
        view.load = _int(event.weight)
        view.attempt = attempt
        if view.stolen_from >= 0:
            view.note = f"stolen from Lane-{view.stolen_from}"
        elif attempt > 1 and not view.note:
            view.note = f"Self-Correcting (retry {attempt})"

    def _finished(self, event) -> None:
        view = self.lane(event.lane)
        state = _text(event.state)
        if state == "validated":
            view.done += 1
            self.completed += 1
            self._unanswered.pop(_text(event.task_id), None)
        else:
            view.failed += 1
            self.failed += 1
            # Provisional. The repair loop gets the next word, and if it
            # re-queues this unit the count is MOVED rather than kept — see
            # `_fell_back`.
            self._unanswered[_text(event.task_id)] = view
        view.status = IDLE
        view.task_id = ""
        view.target = ""
        view.load = 0
        view.stolen_from = -1
        view.note = state + (f" in {float(event.elapsed_ms):.0f} ms"
                             if event.elapsed_ms else "")

    def _stolen(self, event) -> None:
        view = self.lane(event.lane)
        victim = _int(event.victim, -1)
        target = _text(event.target) or _text(event.task_id)
        view.status = STEALING
        view.stolen_from = victim
        view.task_id = _text(event.task_id)
        view.target = target
        view.note = f"stolen from Lane-{victim}"
        view.steals += 1
        self.steals += 1
        self.steal_log.append(steal_caption(event.lane, victim, target))

    def _fell_back(self, event) -> None:
        view = self.lane(event.lane)
        caption = (INJECTED_CAPTION if event.injected
                   else f"Self-Correcting (retry {_int(event.attempt)})")
        view.status = FALLBACK
        view.task_id = _text(event.task_id) or view.task_id
        view.attempt = _int(event.attempt)
        view.note = caption
        # The correction the header depends on. `_finished` already counted this
        # attempt as a failure, because at that moment nothing knew whether it
        # would be retried. A fall-back is the answer, and it moves the tally
        # rather than leaving a header that reads `done 4  failed 1` for a
        # mission of four units that lost none of them.
        loser = self._unanswered.pop(_text(event.task_id), None)
        if loser is not None:
            self.failed = max(0, self.failed - 1)
            loser.failed = max(0, loser.failed - 1)
            self.retried += 1
        self._alert(FALLBACK_TRIGGERED, view.lane_id,
                    f"{view.name} {view.task_id}: {caption}"
                    + (f" — {_text(event.reason)}" if event.reason else ""))

    def _alert(self, kind, lane, text) -> None:
        """The footer's only door.

        Deliberately narrow: the directive asks for CRITICAL warnings only, and
        a footer that also carried ordinary progress would be a second event log
        nobody reads — which is the same as no footer at all."""
        self.alerts.append(Alert(kind=kind, lane=_int(lane, -1),
                                 text=_text(text)))


def steal_caption(lane, victim, target) -> str:
    """The directive's own sentence, in one place so both renderers agree."""
    return (f"Cat {_int(lane, -1)} is stealing task [{_text(target)}] "
            f"from Cat {_int(victim, -1)}")


