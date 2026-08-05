# tests/test_replenish.py
"""The `work_injected` audit row: registration and payload contract.

These rows are the injector's only persistent state — the dedup set, the round
counter, and the dry-round count are folded back out of them. A malformed row
therefore does not merely look wrong, it changes what gets proposed, so the
contract is tested at the registry dispatch (`events.validate_typed_payload`)
rather than only against the module function: registering the contract and
writing it are two separate mistakes.
"""
import pytest

from mindhive import events, replenish


def _landed(**over):
    payload = {"outcome": "landed", "round": 1, "surface": "session",
               "lens": "evidence-gap", "title": "run the suite",
               "fingerprint": "evidence-gap:2026-08-05T00:00:00Z",
               "evidence": ["code_change @ 2026-08-05T00:00:00Z"],
               "score": 1.0}
    payload.update(over)
    return payload


def _check(payload):
    """Through the registry, not the module — this is what the append surfaces
    actually call."""
    events.validate_typed_payload("work_injected", payload)


# --- registration ------------------------------------------------------------

def test_work_injected_is_a_registered_event_type():
    assert "work_injected" in events.VALID_TYPES
    assert "work_injected" in events.DETERMINISTIC_TYPES
    # Deterministic membership is the whole point: semantic would put a LEDGER
    # file and a recall entry on disk per refusal.
    assert "work_injected" not in events.SEMANTIC_TYPES
    assert "work_injected" not in events.MEMORY_VIEW_TYPES


def test_work_injected_round_trips_through_the_ledger():
    events.append_event("work_injected", _landed())
    rows = [e for e in events.read_all() if e.type == "work_injected"]
    assert len(rows) == 1
    assert rows[0].payload["fingerprint"] == "evidence-gap:2026-08-05T00:00:00Z"


def test_the_registry_dispatches_to_this_module():
    # A contract registered against the wrong module accepts everything.
    with pytest.raises(ValueError) as exc:
        _check({"outcome": "nonsense", "round": 1, "surface": "session"})
    assert "hive replenish run" in str(exc.value)


# --- the contract ------------------------------------------------------------

def test_valid_landed_payload_passes():
    _check(_landed())


def test_unknown_outcome_is_refused():
    with pytest.raises(ValueError, match="outcome"):
        _check(_landed(outcome="shipped"))


def test_landed_without_a_lens_is_refused():
    payload = _landed()
    del payload["lens"]
    with pytest.raises(ValueError, match="lens"):
        _check(payload)


def test_landed_without_a_fingerprint_is_refused():
    payload = _landed()
    del payload["fingerprint"]
    with pytest.raises(ValueError, match="fingerprint"):
        _check(payload)


def test_landed_with_non_list_evidence_is_refused():
    # `.get("evidence", [])` would default only on a MISSING key; a present
    # non-list is the shape that gets through a lazy check.
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence="code_change @ ts"))


def test_landed_with_empty_evidence_is_refused():
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence=[]))


def test_landed_with_a_blank_evidence_item_is_refused():
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence=["   "]))


def test_oversized_evidence_item_is_refused():
    big = "x" * (replenish.MAX_EVIDENCE_CHARS + 1)
    with pytest.raises(ValueError, match="evidence"):
        _check(_landed(evidence=[big]))


def test_refused_row_requires_a_reason():
    payload = _landed(outcome="refused")
    payload.pop("evidence")
    with pytest.raises(ValueError, match="reason"):
        _check(payload)


def test_refused_row_with_a_reason_passes_without_evidence():
    payload = _landed(outcome="refused", reason="no evidence")
    payload.pop("evidence")
    _check(payload)


def test_truncated_row_requires_a_dropped_count():
    with pytest.raises(ValueError, match="dropped"):
        _check({"outcome": "truncated", "round": 1, "surface": "session"})


def test_truncated_row_passes_with_a_dropped_count():
    _check({"outcome": "truncated", "round": 1, "surface": "session",
            "dropped": 4})


def test_dry_row_needs_nothing_beyond_round_and_surface():
    _check({"outcome": "dry", "round": 2, "surface": "session"})


def test_round_must_be_a_positive_integer():
    with pytest.raises(ValueError, match="round"):
        _check(_landed(round=0))


def test_boolean_round_is_refused():
    # bool subclasses int: `True` would pass a bare isinstance(x, int) check and
    # make a malformed row read as round 1.
    with pytest.raises(ValueError, match="round"):
        _check(_landed(round=True))


def test_surface_is_required():
    payload = _landed()
    del payload["surface"]
    with pytest.raises(ValueError, match="surface"):
        _check(payload)


def test_non_object_payload_is_refused():
    with pytest.raises(ValueError, match="object"):
        _check(["outcome", "landed"])


def test_score_must_be_a_number_when_present():
    with pytest.raises(ValueError, match="score"):
        _check(_landed(score="high"))


def test_score_is_optional():
    payload = _landed()
    del payload["score"]
    _check(payload)
