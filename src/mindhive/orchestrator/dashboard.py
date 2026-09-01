# src/mindhive/orchestrator/dashboard.py
"""WB7 — the worker-cat board: a terminal view the mission publishes TO.

## The one rule this module exists to keep

The scheduler, the repair loop and the mission pipeline do not import this
module, and must never learn to. They are handed a *bus* — anything carrying an
`emit(kind, **fields)` method — and they call it. Every arrow points one way:

    queue.WorkStealingScheduler ──emit──┐
    resilience.RepairLoop       ──emit──┼─> EventBus ─asyncio.Queue─> Board ─> render
    lintester.LintesterOrchestrator ────┘

`tests/test_orchestrator_dashboard.py` asserts the absence of that import as
TEXT, because a decoupling resting on nobody happening to type
`from . import dashboard` is a convention, not an architecture.

Passing a bus rather than importing an event class is also why the seam is a
*string* kind: a producer that had to import `TaskStarted` in order to publish
one would have the dependency straight back.

A string seam can drift silently, so it is not allowed to. `EventBus.emit`
records every kind it does not recognise and every field it could not place, on
`bus.unknown` — and a test drives a REAL scheduler through a real bus and
asserts that tuple is empty. The typo a string seam invites fails a test rather
than a rendering.

## Three layers, and only the last one needs a terminal

`Board` is a pure fold over events: no clock, no I/O, no `rich`. Every decision
the display makes — which lane is stealing from which, what turns a row red,
which alerts are worth the footer — is made there, and is testable with no
terminal at all. The renderers hold no logic beyond arranging strings.

## `rich` is optional, and its absence is reported rather than crashed

`pyproject.toml`'s runtime `dependencies` list is empty and this module does not
change it; `rich` lives in a new `tui` extra, on the same bargain the `pg` extra
already documents. Without it the board degrades to a scrolling one-line-per-
event log and says so — the way `resilience.run_sandboxed` reports
`timeout-only` rather than pretending it fenced a child it did not.

Be precise about what that fallback is NOT. It is a log, not a board. Redrawing
a whole table into a dumb terminal four times a second would be worse than the
log, and calling it a live view would be exactly the launder this repository
keeps refusing everywhere else.

## Why a steal is carried, not flashed

A steal and the dispatch that follows it are microseconds apart, and this board
repaints four times a second. A `WorkStolen` that only set a colour would be a
frame nobody ever sees, and a board whose most interesting event is invisible is
a board that lies by omission. So the stolen-from lane rides the ROW for as long
as that unit runs: the cat goes green and still says where it got its work.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import sys
from dataclasses import dataclass

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
        self._loop = None

    def __repr__(self) -> str:                          # pragma: no cover - debug aid
        return (f"<EventBus subs={len(self._subs)} published={self.published} "
                f"dropped={self.dropped}>")

    def bind(self, loop=None):
        """Remember which loop the subscribers' queues belong to.

        Called by `subscribe`, and separately by the mission just before it
        starts its lanes, so a bus built before the loop existed still knows
        where to hand work published from a worker thread."""
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
        return queue

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


# --- plain rendering -----------------------------------------------------------

def render_plain(board, icons=None, width=110) -> str:
    """The whole board as text. No `rich`, no terminal, no colour.

    Used for the fallback's closing snapshot and by every test that asserts what
    the board SAYS, which is why it is a pure function of the board."""
    table = icons if icons is not None else ASCII_ICONS
    counts = board.counters()
    head = [
        f"LINTESTER BRAIN  {board.mission_id or '(no mission)'}  {board.phase}"
        + (f" — {board.detail}" if board.detail else ""),
    ]
    if board.intent:
        head.append(f"  intent: {board.intent[:width - 10]}")
    head.append(f"  lanes {counts['lanes']}  units {counts['units']}  "
                f"done {counts['completed']}  failed {counts['failed']}  "
                f"retried {counts['retried']}  steals {counts['steals']}")
    if counts["ignored"]:
        head.append(f"  {counts['ignored']} event(s) could not be read")

    rows = ["", f"{'LANE':<6}{'STATUS':<34}{'TARGET':<28}{'LOAD':>6}  NOTE"]
    for view in board.rows():
        rows.append(f"{view.lane_id:<6}{view.status_text(table):<34}"
                    f"{view.target[:27]:<28}{view.load:>6}  {view.note}")
    if not board.rows():
        rows.append("(no lane has reported yet)")

    foot = ["", "ALERTS"]
    for alert in board.alerts:
        foot.append(f"  ! {alert.text}")
    if not board.alerts:
        foot.append("  (none — no breaker has opened and nothing has been "
                    "re-queued)")
    return "\n".join(head + rows + foot)


# --- rich rendering ------------------------------------------------------------

REFRESH_PER_SECOND = 4


def rich_available() -> tuple:
    """``(available, reason)``, so a caller can report the degradation without
    spending a mission finding out. `resilience.limits_available`'s shape."""
    try:
        import rich                                     # noqa: F401
    except ImportError as exc:
        return False, (f"the `rich` package is not installed "
                       f"({type(exc).__name__}: {exc}); install it with "
                       f"`pip install -e .[tui]` for the live board")
    return True, ""


def _header_panel(board):
    from rich.panel import Panel
    from rich.text import Text

    counts = board.counters()
    body = Text()
    body.append(board.phase or PHASE_IDLE, style="bold")
    if board.detail:
        body.append(f"  {board.detail}", style="dim")
    body.append("\n")
    body.append(f"{counts['lanes']} lane(s)  {counts['units']} unit(s)  "
                f"done {counts['completed']}  failed {counts['failed']}  "
                f"retried {counts['retried']}  steals {counts['steals']}",
                style="dim")
    if board.intent:
        body.append(f"\n{board.intent[:120]}", style="italic dim")
    return Panel(body, title=f"Lintester Brain — {board.mission_id or '...'}",
                 border_style="cyan")


_ROW_STYLES = {WORKING: "green", IDLE: "yellow", STEALING: "magenta",
               FALLBACK: "red"}


def _lane_table(board, icons=None):
    from rich.table import Table

    table = Table(expand=True, header_style="bold", title="Worker Lanes",
                  title_style="bold")
    table.add_column("Lane", width=6)
    table.add_column("Status", width=30)
    table.add_column("Target File", overflow="fold")
    table.add_column("Load", justify="right", width=6)
    table.add_column("Note", overflow="fold")
    for view in board.rows():
        table.add_row(view.name, view.status_text(icons), view.target or "-",
                      str(view.load), view.note,
                      style=_ROW_STYLES.get(view.status, ""))
    if not board.rows():
        table.add_row("-", "(no lane has reported yet)", "", "", "")
    return table


def _footer_panel(board):
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    if not board.alerts:
        body.append("no breaker has opened and nothing has been re-queued",
                    style="dim")
    for alert in board.alerts:
        style = "red" if alert.kind == CIRCUIT_OPENED else "yellow"
        body.append(f"! {alert.text}\n", style=style)
    return Panel(body, title="Circuit Breaker & Alerts", border_style="red")


def build_layout(board, icons=None):
    """Header / Worker Lanes / Circuit Breaker, as the directive splits them."""
    from rich.layout import Layout

    layout = Layout()
    # Fixed header and footer, elastic body: the lane table is the part that
    # grows with the mission, and a footer sized to `MAX_ALERTS + 2` is what
    # keeps the ring bound and the pane in agreement.
    layout.split_column(Layout(name="header", size=6),
                        Layout(name="body", ratio=1),
                        Layout(name="footer", size=MAX_ALERTS + 2))
    update_layout(layout, board, icons)
    return layout


def update_layout(layout, board, icons=None) -> None:
    """Repaint an existing layout in place. `Live` holds the same object, so a
    rebuilt layout each frame would throw away what it is already rendering."""
    layout["header"].update(_header_panel(board))
    layout["body"].update(_lane_table(board, icons))
    layout["footer"].update(_footer_panel(board))


# --- the async runner ----------------------------------------------------------

def log_line(event, icons=None) -> str:
    """One event as one line. The fallback's whole vocabulary."""
    table = icons if icons is not None else ASCII_ICONS
    if isinstance(event, WorkStolen):
        return (f"{table.get(STEALING, '')} "
                f"{steal_caption(event.lane, event.victim, event.target)}")
    if isinstance(event, FallbackTriggered):
        caption = (INJECTED_CAPTION if event.injected
                   else f"Self-Correcting (retry {_int(event.attempt)})")
        return f"{table.get(FALLBACK, '')} Lane-{event.lane} {event.task_id}: {caption}"
    if isinstance(event, TaskStarted):
        return (f"{table.get(WORKING, '')} Lane-{event.lane} started "
                f"{event.task_id} [{event.target}] weight {event.weight}")
    if isinstance(event, TaskFinished):
        return (f"{table.get(IDLE, '')} Lane-{event.lane} {event.task_id} "
                f"{event.state} in {float(event.elapsed_ms or 0):.0f} ms")
    if isinstance(event, CircuitOpened):
        return f"! breaker OPEN for {event.key} — {event.reason}"
    if isinstance(event, BrainPhase):
        return f"* {event.phase}" + (f" — {event.detail}" if event.detail else "")
    if isinstance(event, MissionStarted):
        return (f"* mission {event.mission_id}: {event.units} unit(s) across "
                f"{event.lanes} lane(s)")
    if isinstance(event, MissionFinished):
        return (f"* mission {event.mission_id} {event.outcome}: "
                f"{event.completed} done, {event.failed} failed, "
                f"{event.steals} steal(s)")
    if isinstance(event, LaneOpened):
        return f"* Lane-{event.lane} -> {event.worktree or '(no worktree)'}"
    return f"? {getattr(event, 'text', event)!r}"


class Dashboard:
    """Drains a bus into a `Board` and paints it, as its own task.

    Independent of the work by construction: the mission's coroutines and this
    one share a loop and nothing else, so a slow paint costs frames, never a
    stalled lane. The queue's bound is what makes that true rather than hoped —
    a renderer that fell far enough behind drops frames and the bus counts them.

    `plain` forces the fallback. It defaults to "whatever this machine can do",
    which is what makes the CI leg — where `rich` is not installed — exercise
    the same path an operator without the extra gets."""

    def __init__(self, bus, board=None, refresh_per_second=REFRESH_PER_SECOND,
                 console=None, plain=None, stream=None):
        self.bus = bus
        self.board = board if board is not None else Board()
        self.refresh_per_second = max(1, _int(refresh_per_second, 1) or 1)
        self.stream = stream if stream is not None else sys.stdout
        self.icons = icons_for(self.stream)
        available, why = rich_available()
        self.plain = (not available) if plain is None else bool(plain)
        self.degraded = ("" if not self.plain
                         else (why or "a plain log was asked for"))
        self.console = console
        self.render_errors = 0
        self.last_render_error = ""
        self.lines = 0
        self._layout = None
        self._queue = None
        self._task = None
        self._stop = None

    @property
    def tick(self) -> float:
        return 1.0 / float(self.refresh_per_second)

    def start(self):
        """Subscribe and launch the update loop. Answers the `asyncio.Task`."""
        if self._task is not None:
            return self._task
        self._queue = self.bus.subscribe()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="lintester-dashboard")
        return self._task

    async def aclose(self) -> None:
        """Stop after draining. A board that exits on the stop signal alone
        would lose the last events of the mission — which include the ones
        saying how it ended."""
        if self._task is None:
            return
        self._stop.set()
        task, self._task = self._task, None
        try:
            await task
        except asyncio.CancelledError:                  # pragma: no cover - teardown
            pass
        finally:
            self.bus.unsubscribe(self._queue)
            self._queue = None

    async def _run(self) -> None:
        if not self.plain:
            try:
                await self._run_live()
                return
            except Exception as exc:                    # noqa: BLE001
                # The live board could not be BUILT (or could not be torn
                # down). `_paint` already counts a failed repaint, but the
                # initial `build_layout` sits outside it, and a raise there
                # propagated out of the task and killed the mission the board
                # was only supposed to be watching. So the whole terminal path
                # degrades the way a missing `rich` does: to the log that needs
                # no terminal, naming itself.
                #
                # `CancelledError` is a BaseException and is deliberately NOT
                # caught here — a cancelled board must stay cancelled.
                self.render_errors += 1
                self.last_render_error = f"{type(exc).__name__}: {exc}"
                self.plain = True
                self.degraded = (f"the live board failed and this run fell back "
                                 f"to a log — {self.last_render_error}")
        self._say(f"[board] {self.degraded}")
        await self._pump(None)
        self._say(render_plain(self.board, self.icons))

    async def _run_live(self) -> None:
        """The terminal path. `Live` as a context manager, so an interrupt or a
        cancellation still restores the screen it took over."""
        from rich.live import Live

        self._layout = build_layout(self.board, self.icons)
        with Live(self._layout, refresh_per_second=self.refresh_per_second,
                  console=self.console) as live:
            await self._pump(live)

    async def _pump(self, live) -> None:
        while True:
            drained = await self._drain()
            self._paint(live)
            if self._stop.is_set() and not drained:
                return

    async def _drain(self) -> int:
        """Wait up to one frame for an event, then take everything queued."""
        try:
            event = await asyncio.wait_for(self._queue.get(), self.tick)
        except (asyncio.TimeoutError, TimeoutError):
            return 0
        got = 0
        while event is not None:
            self.board.apply(event)
            if self.plain:
                self._say(log_line(event, self.icons))
            got += 1
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                event = None
        return got

    def _paint(self, live) -> None:
        if live is None or self._layout is None:
            return
        try:
            update_layout(self._layout, self.board, self.icons)
        except Exception as exc:                        # noqa: BLE001
            # A render bug must not kill a mission — but a frozen board with no
            # explanation is its own lie, so the failure is counted and named.
            self.render_errors += 1
            self.last_render_error = f"{type(exc).__name__}: {exc}"

    def _say(self, text) -> None:
        self.lines += 1
        try:
            print(text, file=self.stream, flush=True)
        except (OSError, ValueError, UnicodeEncodeError):  # pragma: no cover
            pass


@contextlib.asynccontextmanager
async def attach(bus, **kwargs):
    """`async with attach(bus):` — the board runs for the block's lifetime.

    This is the factory `LintesterOrchestrator(observer=...)` is handed, which
    is how the mission gets a board without importing one."""
    dashboard = Dashboard(bus, **kwargs)
    dashboard.start()
    try:
        yield dashboard
    finally:
        await dashboard.aclose()
