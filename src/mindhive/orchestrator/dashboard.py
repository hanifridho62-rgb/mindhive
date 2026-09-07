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

# --- the pure half, re-exported ------------------------------------------------
# `board.py` holds the event vocabulary, the bus and the `Board` fold — split
# out when this file hit its 1000-line cap. Imported here by NAME rather than
# with a star so the surface is readable, and so `from .dashboard import Board`
# keeps resolving for every existing caller and test.
from .board import (                                     # noqa: F401
    ASCII_ICONS, BRAIN_PHASE, BRAIN_PHASES, CIRCUIT_OPENED, DEFAULT_CAPACITY,
    FALLBACK, FALLBACK_TRIGGERED, ICONS, IDLE, INJECTED_CAPTION, LABELS,
    LANE_OPENED, MAX_ALERTS, MAX_STEALS, MISSION_FINISHED, MISSION_STARTED,
    NOTE, PHASE_BRIEFING, PHASE_DECOMPOSING, PHASE_DONE, PHASE_IDLE,
    PHASE_ISOLATING, PHASE_MERGING, PHASE_PACKING, PHASE_PARSING,
    PHASE_RUNNING, PHASE_WEIGHING, STATUSES, STEALING, TASK_FINISHED,
    TASK_STARTED, WORK_STOLEN, WORKING,
    Alert, Board, BrainPhase, CircuitOpened, EventBus, EVENTS,
    FallbackTriggered, LaneOpened, LaneView, MissionFinished, MissionStarted,
    Note, TaskFinished, TaskStarted, WorkStolen,
    icons_for, make_event, steal_caption, _int, _text,
)


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

    # `steal_log` and `LaneView.worktree` were folded, bounded, and drawn by
    # NOTHING. Recorded as "either draw them or delete them, and say which";
    # this is the which. They are drawn HERE, in the plain renderer, and
    # deliberately not in the live one:
    #
    #   - The live view has a fixed-height layout and shows a steal WHILE it
    #     happens, as the lane's own STEALING status and note. The log's value
    #     is afterwards, and `Live` leaves nothing behind when it exits.
    #   - `_close_board` prints this renderer after the mission returns, so the
    #     plain frame is the one that persists and the one an operator reads.
    #
    # Both blocks are omitted entirely when empty: a permanent "(none)" is the
    # noise that teaches an operator to skim.
    extra: list = []
    worktrees = [view for view in board.rows() if view.worktree]
    if worktrees:
        extra.append("")
        extra.append("WORKTREES")
        for view in worktrees:
            extra.append(f"  {view.name:<10}{view.worktree}")
    if board.steal_log:
        extra.append("")
        extra.append("STEALS")
        for caption in board.steal_log:
            extra.append(f"  {caption}")

    foot = ["", "ALERTS"]
    for alert in board.alerts:
        foot.append(f"  ! {alert.text}")
    if not board.alerts:
        foot.append("  (none — no breaker has opened and nothing has been "
                    "re-queued)")
    return "\n".join(head + rows + extra + foot)


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
    # The same line `render_plain` draws, in the same words. `Board`'s own
    # docstring promises "`ignored` counts it and the header prints it", and
    # only the plain renderer kept that promise -- so the operator most likely
    # to be watching, the one with a terminal, was the one not told.
    if counts["ignored"]:
        body.append(f"\n{counts['ignored']} event(s) could not be read",
                    style="yellow")
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
            else:
                # The live path used to `return` here and say nothing, so a
                # board frozen by repeated repaint failure exited looking like a
                # board that simply had nothing more to show. `_paint` counted
                # every one of those failures into `render_errors` and nothing
                # ever read it back.
                #
                # Reported only when non-zero, and AFTER `Live` has restored the
                # terminal so the snapshot is not painted over. A healthy run
                # still ends with the frame it drew: dumping a plain board over
                # every successful run would be the noise that trains an
                # operator to skim the line that matters.
                if self.render_errors:
                    self._say(f"[board] {self.render_errors} repaint(s) failed "
                              f"and the frame above is stale — "
                              f"{self.last_render_error}")
                    self._tolerate(
                        lambda: self._say(render_plain(self.board, self.icons)))
                return
        self._say(f"[board] {self.degraded}")
        await self._pump(None)
        self._tolerate(lambda: self._say(render_plain(self.board, self.icons)))

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
                self._tolerate(lambda: self._say(log_line(event, self.icons)))
            got += 1
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                event = None
        return got

    def _tolerate(self, step):
        """Run one render step; absorb its failure into a COUNT.

        `_paint` always did this for the LIVE path. The fallback path did not,
        and it is the path CI takes (`rich` is not in `dev`) and the path any
        machine without `rich` takes — so one drifted field reaching `log_line`
        raised out of `_drain`, out of the display task, and killed the mission
        the board was only watching, orphaning its lane worktrees on the way.

        `make_event` filters by field NAME and does not coerce; this module
        accepts that a string seam can drift. Absorbing the consequence here is
        the other half of accepting it.

        `CancelledError` is a BaseException and so is deliberately NOT caught —
        a cancelled board must stay cancelled."""
        try:
            return step()
        except Exception as exc:                        # noqa: BLE001
            # A frozen board with no explanation is its own lie, so the
            # failure is counted and named rather than merely survived.
            self.render_errors += 1
            self.last_render_error = f"{type(exc).__name__}: {exc}"
            return None

    def _paint(self, live) -> None:
        if live is None or self._layout is None:
            return
        self._tolerate(
            lambda: update_layout(self._layout, self.board, self.icons))

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
