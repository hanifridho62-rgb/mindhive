# tests/test_replenish_estafet.py
"""The relay: what an interrupted loop run hands the next session.

The load-bearing decision here is that the fingerprint cache is a CACHE and the
ledger fold is the authority. `replenish` has no sidecar because a second source
of truth drifts invisibly — work that quietly stops being proposed — so the cache
may only ever ADD to the spent set. A stale or truncated cache then costs a
candidate the ledger would also have skipped; a cache that could REMOVE an entry
would silently re-propose finished work.

The other decision worth pinning: relay fields are rejected when oversized rather
than truncated. A silently shortened fingerprint list is a cache that looks
complete and is not.
"""
import pytest

from mindhive import autoresume, events, replenish, roadmap
from mindhive.loop import config, engine
from mindhive.loop.driver import DriverResult, Usage

COMPLETING = ("Everything is done and verified; no work remains.\n"
              "LOOP_STATUS:\n  summary: done\n  progress: true\n"
              "  STATUS: COMPLETE\n  EXIT_SIGNAL: true\n")


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _loop_dir(tmp_path):
    d = tmp_path / "proj" / ".hive-loop"
    d.mkdir(parents=True)
    (d / "PROMPT.md").write_text("Build it", encoding="utf-8")
    (d / "fix_plan.md").write_text(
        "- [x] done step\n- [ ] (new) mined step\n- [ ] plain step\n"
        "## Future\n- [ ] someday\n", encoding="utf-8")
    return d


class Stopping:
    """A driver that stops the run the way the caller asked for."""

    def __init__(self, rate_limited=False):
        self.rate_limited = rate_limited

    def run(self, prompt, loop_context, **kw):
        return DriverResult(text=COMPLETING, usage=Usage(input_tokens=10),
                            session_id="s1", num_turns=1,
                            rate_limited=self.rate_limited)


def _run_to_api_cap(loop_dir):
    return engine.run_loop(
        project="test", loop_dir=loop_dir,
        cfg=config.LoopConfig(max_iterations=3, replenish_enabled=False),
        driver=Stopping(rate_limited=True), now_fn=_clock(),
        sleep_fn=lambda _s: None, progress_fn=lambda _i: True)


def _clock():
    ticks = iter(range(0, 100000, 10))
    return lambda: next(ticks)


def _interrupt_payload():
    return [e.payload for e in events.read_all()
            if e.type == "roadmap" and e.payload.get("action") == "interrupt"][0]


# --- which endings hand over the baton ----------------------------------------

def test_max_hours_now_snapshots_like_the_two_usage_caps():
    # A run out of wall clock has exactly the same unfinished plan as one out of
    # quota, and the next session needs the baton either way.
    assert set(engine.USAGE_INTERRUPT_REASONS) == {
        "api_cap", "budget_cap", "max_hours"}


def test_an_api_cap_run_carries_the_new_flag_on_its_steps(tmp_path):
    loop_dir = _loop_dir(tmp_path)
    result = _run_to_api_cap(loop_dir)
    assert result["reason"] == "api_cap"
    sets = [e.payload for e in events.read_all()
            if e.type == "roadmap" and e.payload.get("action") == "set"]
    mined = [s for s in sets[0]["steps"] if s["new"]]
    assert [s["text"] for s in mined] == ["mined step"]


def test_the_interrupt_carries_a_relay_context(tmp_path):
    _run_to_api_cap(_loop_dir(tmp_path))
    payload = _interrupt_payload()
    assert payload["relay_context"]
    assert "api_cap" in payload["relay_context"]
    assert len(payload["relay_context"]) <= roadmap.MAX_RELAY_CONTEXT_CHARS


def test_the_relay_context_counts_the_injected_steps(tmp_path):
    _run_to_api_cap(_loop_dir(tmp_path))
    assert "1 of them added by the injector" in _interrupt_payload()["relay_context"]


def test_the_interrupt_carries_the_dry_round_count(tmp_path):
    _run_to_api_cap(_loop_dir(tmp_path))
    assert _interrupt_payload()["dry_rounds"] == 0


def test_fix_plan_is_byte_identical_after_the_snapshot(tmp_path):
    loop_dir = _loop_dir(tmp_path)
    before = (loop_dir / "fix_plan.md").read_bytes()
    _run_to_api_cap(loop_dir)
    assert (loop_dir / "fix_plan.md").read_bytes() == before


def test_a_relay_failure_still_lets_the_run_close(tmp_path, monkeypatch):
    # A run must never be stranded as "running" in Oversee.
    monkeypatch.setattr(engine, "_relay_state",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    loop_dir = _loop_dir(tmp_path)
    result = _run_to_api_cap(loop_dir)
    assert result["reason"] == "api_cap"
    assert [e for e in events.read_all() if e.type == "loop_run_end"]


# --- the contract -------------------------------------------------------------

def _interrupt(**over):
    payload = {"action": "interrupt", "plan_id": "p-1", "cause": "api_cap"}
    payload.update(over)
    return payload


def test_a_bare_interrupt_still_validates():
    roadmap.validate_payload(_interrupt())            # must not raise


def test_an_oversized_relay_context_is_rejected_not_truncated():
    with pytest.raises(ValueError, match="relay_context exceeds"):
        roadmap.validate_payload(_interrupt(
            relay_context="x" * (roadmap.MAX_RELAY_CONTEXT_CHARS + 1)))


def test_a_non_text_relay_context_is_rejected():
    with pytest.raises(ValueError, match="relay_context must be text"):
        roadmap.validate_payload(_interrupt(relay_context=42))


def test_too_many_cached_fingerprints_are_rejected():
    with pytest.raises(ValueError, match="spent_fingerprints exceeds"):
        roadmap.validate_payload(_interrupt(spent_fingerprints=[
            f"fp-{i}" for i in range(roadmap.MAX_RELAY_FINGERPRINTS + 1)]))


def test_a_non_list_fingerprint_cache_is_rejected():
    with pytest.raises(ValueError, match="must be a list"):
        roadmap.validate_payload(_interrupt(spent_fingerprints="fp-1"))


def test_a_blank_fingerprint_entry_is_rejected():
    with pytest.raises(ValueError, match="non-empty text"):
        roadmap.validate_payload(_interrupt(spent_fingerprints=["ok", "  "]))


def test_a_negative_dry_round_count_is_rejected():
    with pytest.raises(ValueError, match="non-negative integer"):
        roadmap.validate_payload(_interrupt(dry_rounds=-1))


def test_a_boolean_dry_round_count_is_rejected():
    # bool is an int subclass; True would otherwise pass as 1.
    with pytest.raises(ValueError, match="non-negative integer"):
        roadmap.validate_payload(_interrupt(dry_rounds=True))


# --- the fold -----------------------------------------------------------------

def _plan_with_relay(**relay):
    events.append_event("roadmap", {
        "action": "set", "plan_id": "p-1", "prompt": "build it",
        "title": "build it",
        "steps": [{"text": "a step", "status": "active", "new": True}]},
        session_id="p-1", tags=["roadmap", roadmap.AUTO_TAG])
    events.append_event("roadmap", _interrupt(**relay), session_id="p-1",
                        tags=["roadmap"])
    return roadmap.fold(events.read_all())["p-1"]


def test_the_fold_carries_the_relay_onto_the_record():
    rec = _plan_with_relay(relay_context="stopped on api_cap",
                           spent_fingerprints=["fp-a", "fp-b"], dry_rounds=1)
    assert rec["relay_context"] == "stopped on api_cap"
    assert rec["spent_fingerprints"] == ["fp-a", "fp-b"]
    assert rec["dry_rounds"] == 1


def test_the_fold_invents_nothing_for_a_bare_interrupt():
    rec = _plan_with_relay()
    assert "relay_context" not in rec
    assert "spent_fingerprints" not in rec
    assert "dry_rounds" not in rec


def test_a_resume_consumes_the_relay():
    _plan_with_relay(relay_context="stopped", spent_fingerprints=["fp-a"],
                     dry_rounds=2)
    events.append_event("roadmap", {"action": "resume", "plan_id": "p-1"},
                        session_id="p-1", tags=["roadmap"])
    rec = roadmap.fold(events.read_all())["p-1"]
    assert "relay_context" not in rec
    assert "spent_fingerprints" not in rec
    assert rec["status"] == "active"


# --- rehydration: the fold wins ----------------------------------------------

def _landed(fp):
    events.append_event("work_injected",
                        {"outcome": "landed", "round": 1, "surface": "loop",
                         "lens": "probe", "title": "t", "fingerprint": fp,
                         "evidence": ["p"]})


def test_the_cache_and_the_fold_are_unioned():
    _landed("from-the-ledger")
    rec = {"spent_fingerprints": ["from-the-cache"]}
    spent = autoresume.rehydrate_spent(rec, events.read_all())
    assert "from-the-ledger" in spent
    assert "from-the-cache" in spent


def test_the_fold_survives_a_cache_that_omits_it():
    # This is the invariant. A cache that could REMOVE an entry would silently
    # re-propose finished work.
    _landed("from-the-ledger")
    spent = autoresume.rehydrate_spent({"spent_fingerprints": []},
                                       events.read_all())
    assert "from-the-ledger" in spent


def test_a_corrupt_cache_is_ignored_and_the_fold_is_used():
    _landed("from-the-ledger")
    for bad in ("not a list", 42, None, {"a": 1}):
        spent = autoresume.rehydrate_spent({"spent_fingerprints": bad},
                                           events.read_all())
        assert spent == frozenset({"from-the-ledger"})


def test_blank_cache_entries_are_dropped():
    spent = autoresume.rehydrate_spent(
        {"spent_fingerprints": ["real", "   ", ""]}, [])
    assert spent == frozenset({"real"})


def test_a_record_that_is_not_a_mapping_degrades_to_the_fold():
    _landed("from-the-ledger")
    assert autoresume.rehydrate_spent(None, events.read_all()) == frozenset(
        {"from-the-ledger"})


def test_a_rehydrated_candidate_is_refused_as_a_duplicate():
    # The point of the cache: after a five-hour cooldown the resumed run must not
    # re-mine and re-reject what it already evaluated.
    spent = autoresume.rehydrate_spent({"spent_fingerprints": ["fp-spent"]}, [])
    candidate = replenish.Candidate(lens="probe", title="t",
                                    fingerprint="fp-spent",
                                    evidence=("pointer",))
    kept, refused = replenish.gate([candidate], spent,
                                   replenish.ReplenishConfig())
    assert kept == []
    assert refused[0][1] == replenish.DUPLICATE_REASON


# --- the resumed prompt -------------------------------------------------------

def test_a_loop_origin_prompt_names_the_stop_cause_and_the_marker():
    rec = _plan_with_relay(relay_context="loop run abc stopped on api_cap")
    prompt = autoresume.resume_prompt(rec)
    assert "resuming an autonomous loop run" in prompt
    assert "loop run abc stopped on api_cap" in prompt
    assert "`(new)` marker" in prompt


def test_the_marker_itself_still_renders_on_the_step():
    rec = _plan_with_relay(relay_context="stopped")
    assert roadmap.NEW_MARKER.strip() in autoresume.resume_prompt(rec)


def test_a_session_origin_prompt_is_unchanged():
    # No relay means no extra section. A session-origin plan reads exactly as it
    # did before the estafet existed.
    rec = _plan_with_relay()
    prompt = autoresume.resume_prompt(rec)
    assert "resuming an autonomous loop run" not in prompt
    assert "The original ask" in prompt
