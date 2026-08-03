# tests/test_loop_context.py
from mindhive.loop import context


def test_fill_ratio():
    assert context.fill_ratio(occupancy=100000, window=200000) == 0.5


def test_needs_reset_at_or_above_ratio():
    cfg = context.ContextConfig(context_window=200000, smart_zone_ratio=0.5)
    assert context.needs_reset(occupancy=100000, cfg=cfg) is True   # exactly 0.5
    assert context.needs_reset(occupancy=120000, cfg=cfg) is True
    assert context.needs_reset(occupancy=99999, cfg=cfg) is False


def test_occupancy_from_usage():
    # occupancy = input + cache_creation + cache_read. Cache-WRITE tokens are part
    # of the prompt the model just read; leaving them out undercounts by exactly
    # the span newly written to cache each turn.
    assert context.occupancy_from(input_tokens=8000,
                                  cache_creation_input_tokens=0,
                                  cache_read_input_tokens=42000) == 50000


def test_occupancy_counts_cache_creation():
    """The real measured shape of a cached turn (taken from a live transcript):
    input_tokens is 1-2 and nearly all context sits in the two cache fields.

    Discriminating: the old two-field formula returns 198_994 for this input, so
    a regression to `input + cache_read` fails here rather than passing quietly."""
    assert context.occupancy_from(input_tokens=2,
                                  cache_creation_input_tokens=4826,
                                  cache_read_input_tokens=198992) == 203820


def test_occupancy_from_usage_all_zero():
    assert context.occupancy_from(0, 0, 0) == 0
