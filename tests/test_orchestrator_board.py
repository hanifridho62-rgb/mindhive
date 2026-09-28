# tests/test_orchestrator_board.py
"""`orchestrator/board.py` -- the half of the worker-cat board that needs no
terminal: the event vocabulary, the `EventBus`, and the `Board` fold.

Most of this module's behaviour is pinned in `tests/test_orchestrator_dashboard.py`,
which predates the split and still reaches every name through `dashboard`'s
re-exports. This file holds what belongs to `board.py` alone.

Blueprint §2.11 (Bug 11) reports a board that subscribed AFTER the mission's
opening phases and so lost `mission_started` and `brain_phase` -- a header
reading ``(no mission)  Waiting`` over a live run -- and asks the bus to buffer
"the first 50 events". The backlog already existed (`EventBus._held`, bounded
by `DEFAULT_CAPACITY`, replayed by `subscribe`), and the plan's decision was to
keep that capacity at 512 rather than cut it to the blueprint's 50. That
settled the SIZE and left the END: over capacity the backlog kept the NEWEST
events, so a burst past 512 lost `mission_started` first -- the exact symptom
§2.11 names. It now keeps the spec's first `BACKLOG_HEAD` (50) whatever
arrives after them and lets the loss fall in the middle, behind the opening
and ahead of the present. The canaries hold both ends: a newest-only backlog
fails on the header, a first-only one on the current phase.
"""
import asyncio

from mindhive.orchestrator import board
from mindhive.orchestrator import dashboard as dash

# --- fixture builders -----------------------------------------------------------

# Past the blueprint's floor of 50, so a backlog sized to the letter of §2.11
# would already be losing events here.
BURST = 60


def _publish_header_burst(bus, count=BURST):
    """What a mission says before its lane phase opens the only subscription:
    one `mission_started`, then phase lines. Answers the events, in order."""
    published = [bus.emit(board.MISSION_STARTED, mission_id="m-burst",
                          intent="replay the header", lanes=2,
                          units=count - 1)]
    published += [bus.emit(board.BRAIN_PHASE, phase=board.PHASE_PARSING,
                           detail=f"step {i:02d}") for i in range(1, count)]
    return published


def _late_subscriber(bus, then=None):
    """Subscribe INSIDE a running loop, the way `Dashboard.start` does, publish
    *then* afterwards when given, and answer everything the new queue holds."""
    async def go():
        queue = bus.subscribe()
        if then is not None:
            bus.emit(board.NOTE, text=then)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    return asyncio.run(go())


def _fold(events):
    folded = board.Board()
    for event in events:
        folded.apply(event)
    return folded


# --- the backlog: blueprint §2.11 -----------------------------------------------

def test_the_backlog_capacity_stays_512_by_decision():
    """§2.11 asks for 'at least the first 50'. 512 was kept on purpose: it
    already clears that floor, and it also bounds every live subscriber's
    queue, so shrinking it to the letter of the blueprint would cost a slow
    renderer frames for no gain in the header."""
    assert board.DEFAULT_CAPACITY == 512
    assert board.EventBus().capacity == board.DEFAULT_CAPACITY
    # One authority: `dashboard` re-exports the constant rather than copying
    # it. 512 is outside CPython's small-int cache, so `is` really can fail.
    assert dash.DEFAULT_CAPACITY is board.DEFAULT_CAPACITY


def test_sixty_events_before_a_late_subscriber_all_replay_in_order():
    """THE §2.11 PIN. Sixty events published with nobody listening -- the
    header's own two kinds among them -- reach the first subscriber whole, in
    the order they were published, and ahead of anything published after it
    attached."""
    bus = board.EventBus()
    published = _publish_header_burst(bus)
    assert bus.published == BURST and bus.subscribers == 0

    got = _late_subscriber(bus, then="published after the subscription")

    assert len(got) == BURST + 1
    assert got[:BURST] == published, "the backlog lost or reordered events"
    assert all(a is b for a, b in zip(got, published)), "copies, not the events"
    assert got[BURST].text == "published after the subscription", (
        "the backlog was not replayed ahead of the live stream")
    assert bus.dropped == 0
    assert bus.drain() == (), "the backlog was kept for a second subscriber"
    header = _fold(got)
    assert header.mission_id == "m-burst", "the header lost its mission"
    assert header.detail == "step 59"


def test_the_backlog_keeps_the_specs_first_fifty():
    """§2.11's "the first 50 events", pinned as the number the bus uses."""
    assert board.BACKLOG_HEAD == 50
    assert board.EventBus().head == board.BACKLOG_HEAD
    # Re-exported, like every public name `board` holds.
    assert dash.BACKLOG_HEAD == board.BACKLOG_HEAD


# Past `DEFAULT_CAPACITY`, so a backlog that kept only the newest events would
# lose the header here, and one that kept only the first would lose the phase.
OVERFLOW = board.DEFAULT_CAPACITY + 88


def test_a_burst_past_capacity_still_replays_the_opening_first():
    """THE §2.11 GUARANTEE, past capacity. With nobody listening, 600 events:
    the late subscriber gets the first 50 whole and in order, `mission_started`
    first, then the newest 462, and the 88 between them are COUNTED. An event
    published after the subscription lands behind the replay without evicting
    any of it -- a full replay must not leave the live stream no room but the
    opening's."""
    bus = board.EventBus()
    published = _publish_header_burst(bus, count=OVERFLOW)
    head, cap = board.BACKLOG_HEAD, board.DEFAULT_CAPACITY

    got = _late_subscriber(bus, then="published after the subscription")

    assert len(got) == cap + 1
    assert got[:cap] == published[:head] + published[-(cap - head):]
    assert got[cap].text == "published after the subscription"
    assert bus.dropped == OVERFLOW - cap
    header = _fold(got[:cap])
    assert header.mission_id == "m-burst", "the opening was evicted"
    assert header.detail == f"step {OVERFLOW - 1}", "the present was evicted"


def test_a_small_backlog_loses_the_middle_and_keeps_both_ends():
    """The canary. Without it the tests above prove only that the numbers
    fit. With room for ten the opening takes at most half, five, and the
    present the other five: the late subscriber gets exactly those, the fifty
    between are COUNTED, and the header carries both the mission and its
    current phase. A newest-only backlog loses the mission here -- the §2.11
    symptom -- and a first-only one loses the phase."""
    bus = board.EventBus(capacity=10)
    published = _publish_header_burst(bus)
    assert bus.head == 5

    got = _late_subscriber(bus)

    assert got == published[:5] + published[-5:]
    assert bus.dropped == BURST - 10
    header = _fold(got)
    assert header.mission_id == "m-burst", "the opening was evicted"
    assert header.detail == "step 59", "the present was evicted"


def test_the_opening_never_takes_more_than_half_the_backlog():
    """`min(BACKLOG_HEAD, capacity // 2)`, so the present always keeps at least
    half the room. At capacity 1 there is no room for both and the present
    wins: the one size at which the opening is not kept, and nothing in `src`
    builds a bus that small."""
    assert board.EventBus(capacity=100).head == 50
    assert board.EventBus(capacity=64).head == 32
    assert board.EventBus(capacity=1).head == 0
