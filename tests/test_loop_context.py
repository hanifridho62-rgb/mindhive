# tests/test_loop_context.py
"""The smart-zone gate: how much context a session is carrying, and whether
that has left the zone where the model still works well.

Encodes two contracts from
docs/plans/2026-08-03-001-feat-smart-zone-session-rotation-plan.md:

U2 — occupancy is the TRUE prompt size. The API's own identity is
``input + cache_creation + cache_read``; the original formula omitted
``cache_creation``, which is the dominant term on a session's first turns and
therefore undercounts exactly while a session is growing fastest.

U1 — the ceiling is ``min(absolute, ratio * that model's real window)``. The
operator thinks in absolute tokens ("500k"); the code thought in ratios. They
agree only on a 1M-window model. A lane running Haiku 4.5 has a 200K window,
where an absolute 500k ceiling can never fire at all.
"""
import pytest

from mindhive.loop import context


# --- U2: occupancy is the true prompt size ------------------------------------

def test_fill_ratio():
    assert context.fill_ratio(occupancy=100000, window=200000) == 0.5


def test_occupancy_counts_cache_creation():
    # THE discriminating case. On a session's first turns everything lands in
    # cache_creation and cache_read is still 0, so the old two-term formula
    # reported 1 where the real prompt was 1671. A steady-state fixture cannot
    # tell the two formulas apart; this one kills the old formula outright.
    assert context.occupancy_from(
        input_tokens=1,
        cache_creation_input_tokens=1670,
        cache_read_input_tokens=0) == 1671


def test_occupancy_steady_state_shape():
    # Observed verbatim from a real transcript in this repo's own session dir:
    # input=1, cache_creation=1670, cache_read=207389 -> 209060.
    assert context.occupancy_from(
        input_tokens=1,
        cache_creation_input_tokens=1670,
        cache_read_input_tokens=207389) == 209060


def test_occupancy_all_zero():
    assert context.occupancy_from(0, 0, 0) == 0


# --- U1: window resolution ----------------------------------------------------

def test_resolve_window_known_model():
    cfg = context.ContextConfig(context_window=123, smart_zone_ratio=0.5)
    assert context.resolve_window("claude-opus-5", cfg) == (1_000_000, "model")
    assert context.resolve_window("claude-haiku-4-5", cfg) == (200_000, "model")


def test_resolve_window_alias():
    # claude_cli.MODEL is the bare alias "sonnet"; the loop's tier table uses
    # full ids. Both reach this resolver, so both must resolve.
    cfg = context.ContextConfig(context_window=123, smart_zone_ratio=0.5)
    assert context.resolve_window("sonnet", cfg) == (1_000_000, "model")
    assert context.resolve_window("haiku", cfg) == (200_000, "model")
    assert context.resolve_window("OPUS", cfg) == (1_000_000, "model")


def test_resolve_window_strips_provider_prefix():
    cfg = context.ContextConfig(context_window=123, smart_zone_ratio=0.5)
    assert context.resolve_window(
        "anthropic.claude-opus-5", cfg) == (1_000_000, "model")


def test_resolve_window_unknown_falls_back_to_config():
    # R10: a model the table has not learned yet degrades to the configured
    # scalar and SAYS SO. It must never disable the gate.
    cfg = context.ContextConfig(context_window=777_000, smart_zone_ratio=0.5)
    assert context.resolve_window(
        "claude-something-not-shipped-yet", cfg) == (777_000, "config")


def test_resolve_window_none_model_falls_back():
    # cfg.model is None whenever no model is pinned and the CLI's own default
    # decides — we genuinely do not know which model ran.
    cfg = context.ContextConfig(context_window=777_000, smart_zone_ratio=0.5)
    assert context.resolve_window(None, cfg) == (777_000, "config")
    assert context.resolve_window("", cfg) == (777_000, "config")


# --- U1: the ceiling is the minimum of both bounds ----------------------------

def test_ceiling_absolute_binds_on_large_window():
    # 1M window, generous ratio: the operator's absolute 500k is what stops it.
    cfg = context.ContextConfig(context_window=1_000_000, smart_zone_ratio=0.9,
                                absolute_ceiling=500_000)
    assert context.smart_zone_ceiling(cfg, model="claude-opus-5") == 500_000


def test_ceiling_ratio_binds_on_small_window():
    # 200K window: half of it is 100k, far below the absolute 500k, so the
    # ratio is the binding bound. Without this term a Haiku lane would never
    # rotate at all.
    cfg = context.ContextConfig(context_window=1_000_000, smart_zone_ratio=0.5,
                                absolute_ceiling=500_000)
    assert context.smart_zone_ceiling(cfg, model="claude-haiku-4-5") == 100_000


def test_ceiling_uses_config_window_when_model_unknown():
    cfg = context.ContextConfig(context_window=400_000, smart_zone_ratio=0.5,
                                absolute_ceiling=500_000)
    assert context.smart_zone_ceiling(cfg, model="who-knows") == 200_000


# --- U1: needs_reset ----------------------------------------------------------

def test_needs_reset_boundary_is_inclusive():
    # >= not >. An off-by-one here means a session sits exactly on the ceiling
    # forever without rotating.
    cfg = context.ContextConfig(context_window=1_000_000, smart_zone_ratio=0.5,
                                absolute_ceiling=500_000)
    assert context.needs_reset(499_999, cfg, model="claude-opus-5") is False
    assert context.needs_reset(500_000, cfg, model="claude-opus-5") is True
    assert context.needs_reset(500_001, cfg, model="claude-opus-5") is True


def test_needs_reset_haiku_rotates_where_absolute_alone_would_not():
    cfg = context.ContextConfig(context_window=1_000_000, smart_zone_ratio=0.5,
                                absolute_ceiling=500_000)
    # The ratio bound fires at 120k on a 200K-window model.
    assert context.needs_reset(120_000, cfg, model="claude-haiku-4-5") is True

    # CANARY: the same occupancy against the absolute bound alone does NOT
    # fire. Without this, the assertion above could pass for the wrong reason
    # (e.g. an implementation that rotates on everything) and we would not know.
    assert 120_000 < cfg.absolute_ceiling


def test_needs_reset_without_model_uses_config_window():
    # Backward compatibility: the pre-existing two-argument call shape still
    # works and still means "measure against the configured scalar".
    cfg = context.ContextConfig(context_window=200_000, smart_zone_ratio=0.5,
                                absolute_ceiling=500_000)
    assert context.needs_reset(occupancy=100_000, cfg=cfg) is True
    assert context.needs_reset(occupancy=99_999, cfg=cfg) is False


@pytest.mark.parametrize("window", [0, -1])
def test_degenerate_window_drops_only_the_ratio_bound(window):
    # Totality without failing open (R10). A nonsense window must not raise
    # inside a loop driving real subprocesses — but it must not silently
    # DISABLE the gate either. The absolute bound needs no window to compute,
    # so it keeps governing while the ratio bound is simply unavailable.
    cfg = context.ContextConfig(context_window=window, smart_zone_ratio=0.5,
                                absolute_ceiling=500_000)
    assert context.fill_ratio(10_000, window) == 0.0
    assert context.smart_zone_ceiling(cfg) == 500_000
    assert context.needs_reset(10_000_000, cfg) is True
    assert context.needs_reset(1_000, cfg) is False


def test_no_usable_bound_at_all_never_rotates():
    # Both bounds unusable: there is no ceiling to compare against, so the
    # honest answer is "cannot say" -> do not rotate. Asserted explicitly so a
    # future change cannot make this raise or rotate-always by accident.
    cfg = context.ContextConfig(context_window=0, smart_zone_ratio=0.5,
                                absolute_ceiling=0)
    assert context.smart_zone_ceiling(cfg) is None
    assert context.needs_reset(10_000_000, cfg) is False


def test_absolute_ceiling_of_zero_disables_only_that_bound():
    # 0 means "no absolute bound", not "rotate immediately" — the ratio bound
    # must still govern. The opposite reading would rotate every single turn.
    cfg = context.ContextConfig(context_window=1_000_000, smart_zone_ratio=0.5,
                                absolute_ceiling=0)
    assert context.smart_zone_ceiling(cfg, model="claude-opus-5") == 500_000
    assert context.needs_reset(1_000, cfg, model="claude-opus-5") is False


def test_occupancy_from_usage():
    # From master: a plain cached turn where cache_creation is 0. Kept alongside
    # the cache_creation-dominant case above because the two pin different
    # halves of the same identity.
    assert context.occupancy_from(input_tokens=8000,
                                  cache_creation_input_tokens=0,
                                  cache_read_input_tokens=42000) == 50000


def test_occupancy_real_cached_turn_shape():
    """From master, renamed to avoid shadowing the case above.

    The real measured shape of a cached turn: input_tokens is 1-2 and nearly all
    context sits in the two cache fields. Discriminating — the old two-field
    formula returns 198_994 here, so a regression fails rather than passing."""
    assert context.occupancy_from(input_tokens=2,
                                  cache_creation_input_tokens=4826,
                                  cache_read_input_tokens=198992) == 203820
