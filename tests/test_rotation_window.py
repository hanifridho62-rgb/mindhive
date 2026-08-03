# tests/test_rotation_window.py
"""Contract for the model-window table and the smart-zone threshold.

Encodes docs/plans/2026-08-03-002-feat-smart-zone-interactive-rotation-plan.md
units U2. The window table is static by design (the Stop hook runs on every turn
and must not make a network call); these tests are what stop it drifting silently.
"""
from mindhive.rotation import window


# --- window_for ------------------------------------------------------------

def test_window_for_opus_5_is_one_million():
    assert window.window_for("claude-opus-5") == 1_000_000


def test_window_for_haiku_is_two_hundred_thousand():
    """Discriminating: Haiku is the only current model that is NOT 1M, so a table
    that returned a single constant for everything passes the Opus test above and
    fails here."""
    assert window.window_for("claude-haiku-4-5") == 200_000


def test_window_for_resolves_dated_snapshot_by_prefix():
    # The catalog ships `claude-haiku-4-5-20251001`; matching must not require the
    # bare alias, or every dated id silently falls through to the default.
    assert window.window_for("claude-haiku-4-5-20251001") == 200_000
    assert window.window_for("claude-opus-5-20260101") == 1_000_000


def test_window_for_current_sonnet_and_fable_are_one_million():
    assert window.window_for("claude-sonnet-5") == 1_000_000
    assert window.window_for("claude-sonnet-4-6") == 1_000_000
    assert window.window_for("claude-fable-5") == 1_000_000


def test_window_for_unknown_model_degrades_to_default():
    """Unknown ids must rotate EARLY (small window), never never-rotate."""
    assert window.window_for("some-future-model") == window.DEFAULT_WINDOW
    assert window.DEFAULT_WINDOW == 200_000


def test_window_for_missing_model_is_total():
    # A transcript with no model field, or a malformed one, must not raise here.
    assert window.window_for(None) == window.DEFAULT_WINDOW
    assert window.window_for("") == window.DEFAULT_WINDOW
    assert window.window_for(12345) == window.DEFAULT_WINDOW


def test_window_for_prefers_longest_prefix():
    """`claude-opus-5` and a hypothetical longer entry must not collide: the
    longest matching prefix wins, so ordering in the table is not load-bearing."""
    hits = [p for p, _ in window.WINDOWS if "claude-opus-4-8".startswith(p)]
    assert hits, "expected at least one prefix to match claude-opus-4-8"
    assert window.window_for("claude-opus-4-8") == 1_000_000


# --- threshold -------------------------------------------------------------

def test_threshold_cap_binds_on_a_large_window():
    """0.9 x 1M = 900k, above the 500k cap -> the cap wins."""
    assert window.threshold("claude-opus-5", 0.9) == 500_000


def test_threshold_ratio_binds_on_a_small_window():
    """0.5 x 200k = 100k, below the cap -> the ratio wins.

    Paired with the test above this is the discriminating pair: an implementation
    that only ever applies the cap, or only ever applies the ratio, fails one of
    the two. Either alone passes half the contract."""
    assert window.threshold("claude-haiku-4-5", 0.5) == 100_000


def test_threshold_default_cap_is_five_hundred_thousand():
    assert window.DEFAULT_CAP == 500_000
    assert window.threshold("claude-opus-5", 1.0) == 500_000


def test_threshold_cap_is_overridable():
    assert window.threshold("claude-opus-5", 0.5, cap=250_000) == 250_000


def test_threshold_ratio_bounds():
    assert window.threshold("claude-opus-5", 0.0) == 0
    # Never negative, never unbounded, whatever nonsense arrives.
    assert window.threshold("claude-opus-5", -1.0) == 0
    assert window.threshold("claude-opus-5", 99.0) == window.DEFAULT_CAP


def test_threshold_returns_int_not_float():
    # Occupancy is an integer token count; comparing it against a float threshold
    # works but reads wrong in logs and events.
    assert isinstance(window.threshold("claude-opus-5", 0.33), int)
