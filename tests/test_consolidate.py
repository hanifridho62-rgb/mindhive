# tests/test_consolidate.py — memory consolidation "dreaming" pass (U10).
from mindhive import chain, consolidate, events
from mindhive.briefing import build_briefing


def _caveat(text, minute):
    events.append_event("caveat", {"text": text},
                        ts=f"2026-07-07T10:{minute:02d}:00Z", project="test")


def test_consolidate_merges_near_duplicates_only():
    _caveat("never push straight to master without review", 1)
    _caveat("never push straight to master without a review", 2)
    events.append_event("decision", {"text": "use postgres for the backbone"},
                        ts="2026-07-07T10:03:00Z", project="test")
    res = consolidate.consolidate(project="test", cooldown_hours=0)
    assert res == {"consolidated": 1, "absorbed": 1}
    cons = [e for e in events.read_all("test") if e.type == "memory_consolidated"]
    assert len(cons) == 1
    assert cons[0].payload["type"] == "caveat"
    absorbed = cons[0].payload["absorbed"]
    assert len(absorbed) == 1
    assert absorbed[0][0] == "2026-07-07T10:01:00Z"     # (ts, type, fingerprint)
    assert absorbed[0][1] == "caveat"
    assert cons[0].payload["survivor_ts"] == "2026-07-07T10:02:00Z"


def test_briefing_excludes_absorbed_events():
    _caveat("watch out for the flaky websocket reconnect", 1)
    _caveat("watch out for a flaky websocket reconnect", 2)
    consolidate.consolidate(project="test", cooldown_hours=0)
    text = build_briefing(events.read_all("test"))
    # survivor appears once per section (Timeline + Lessons & caveats) with no
    # (×2) merge suffix — the absorbed duplicate is gone from the digest
    assert text.count("flaky websocket") == 2
    assert "×2" not in text


def test_consolidate_respects_limit_and_is_idempotent():
    for i, base in enumerate(["alpha rule one", "beta rule two", "gamma rule three"]):
        _caveat(base + " applies here", 2 * i + 1)
        _caveat(base + " applies here!", 2 * i + 2)
    res = consolidate.consolidate(project="test", limit=2, cooldown_hours=0)
    assert res["consolidated"] == 2
    res = consolidate.consolidate(project="test", limit=5, cooldown_hours=0)
    assert res["consolidated"] == 1          # only the remaining cluster
    res = consolidate.consolidate(project="test", limit=5, cooldown_hours=0)
    assert res == {"consolidated": 0, "absorbed": 0}


def test_cooldown_marker_skips_second_run():
    _caveat("dup text here", 1)
    _caveat("dup text here", 2)
    t = {"v": 1000.0}
    res = consolidate.consolidate(project="test", cooldown_hours=6,
                                  now_fn=lambda: t["v"])
    assert res["consolidated"] == 1
    _caveat("another dup line", 3)
    _caveat("another dup line", 4)
    t["v"] += 60.0                            # one minute later — inside cooldown
    res = consolidate.consolidate(project="test", cooldown_hours=6,
                                  now_fn=lambda: t["v"])
    assert res.get("skipped") == "cooldown"
    t["v"] += 7 * 3600                        # past the window
    res = consolidate.consolidate(project="test", cooldown_hours=6,
                                  now_fn=lambda: t["v"])
    assert res["consolidated"] == 1


def test_live_lock_refuses_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "LIVE_LOCK").write_text("trading", encoding="utf-8")
    _caveat("dup", 1)
    _caveat("dup", 2)
    import pytest
    with pytest.raises(RuntimeError):
        consolidate.consolidate(project="test", cooldown_hours=0)
    res = consolidate.consolidate(project="test", cooldown_hours=0, force=True)
    assert res["consolidated"] == 1


def test_chain_verifies_after_consolidation():
    _caveat("same note twice", 1)
    _caveat("same note twice", 2)
    consolidate.consolidate(project="test", cooldown_hours=0)
    from mindhive import projector
    projector.project("test")
    assert chain.verify("test")["ok"] is True


def test_same_second_events_are_not_collaterally_hidden():
    # two DIFFERENT memos sharing one second: absorbing one must not hide the
    # other (identity is ts+type+fingerprint, not bare ts)
    events.append_event("caveat", {"text": "duplicate memo body"},
                        ts="2026-07-07T10:01:00Z", project="test")
    events.append_event("caveat", {"text": "totally unrelated other note"},
                        ts="2026-07-07T10:01:00Z", project="test")
    events.append_event("caveat", {"text": "duplicate memo body"},
                        ts="2026-07-07T10:02:00Z", project="test")
    consolidate.consolidate(project="test", cooldown_hours=0)
    text = build_briefing(events.read_all("test"))
    assert "totally unrelated other note" in text
    assert text.count("duplicate memo body") == 2      # survivor: timeline+lessons


def test_pinned_events_never_enter_a_cluster():
    events.append_event("caveat", {"text": "never touch the live trading lock"},
                        ts="2026-07-07T10:01:00Z", tags=["pinned"], project="test")
    events.append_event("caveat", {"text": "never touch the live trading lock"},
                        ts="2026-07-07T10:02:00Z", project="test")
    res = consolidate.consolidate(project="test", cooldown_hours=0)
    # the pinned original is protected content: it is neither survivor nor
    # absorbed, so the only cluster candidate is a lone unpinned twin -> no-op
    assert res == {"consolidated": 0, "absorbed": 0}


def test_opposite_polarity_memos_never_merge():
    events.append_event("caveat", {"text": "deploy the parser to production"},
                        ts="2026-07-07T10:01:00Z", project="test")
    events.append_event("caveat", {"text": "do not deploy the parser to production"},
                        ts="2026-07-07T10:02:00Z", project="test")
    res = consolidate.consolidate(project="test", cooldown_hours=0)
    assert res == {"consolidated": 0, "absorbed": 0}
    text = build_briefing(events.read_all("test"))
    assert "do not deploy" in text


def test_audit_artifact_types_are_exempt_from_consolidation():
    # change-log entries and Self-Reliant PRDs are audit trails: two
    # similar-sounding entries are genuinely distinct and must never merge
    assert "code_change" not in consolidate.CONSOLIDATE_TYPES
    assert "learning" not in consolidate.CONSOLIDATE_TYPES
    assert "learning" in events.SEMANTIC_TYPES


def test_memory_md_digest_excludes_absorbed(tmp_path):
    from mindhive import sync as sync_mod
    events.append_event("caveat", {"text": "watch the flaky reconnect path"},
                        ts="2026-07-07T10:01:00Z", project="test")
    events.append_event("caveat", {"text": "watch a flaky reconnect path"},
                        ts="2026-07-07T10:02:00Z", project="test")
    consolidate.consolidate(project="test", cooldown_hours=0)
    dest = sync_mod.sync("C:/proj", project="test", home=tmp_path / "home")
    content = dest.read_text(encoding="utf-8")
    assert content.count("flaky reconnect") == 1       # digest shows the survivor once
