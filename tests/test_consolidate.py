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
    assert cons[0].payload["absorbed_ts"] == ["2026-07-07T10:01:00Z"]
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
