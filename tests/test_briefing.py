# tests/test_briefing.py
from mindhive import events, paths
from mindhive.briefing import headline, build_briefing, briefing


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_headline_strips_frontmatter(tmp_path, monkeypatch):
    text = "---\nts: 2026-01-01\ntype: decision\n---\n\nReal content here"
    assert headline(text) == "Real content here"


def test_headline_truncates_at_140():
    long = "x" * 200
    result = headline(long)
    assert result.endswith("…")
    assert len(result) == 141  # 140 chars + ellipsis


def test_headline_collapses_whitespace():
    text = "  hello   world  "
    assert headline(text) == "hello world"


def test_headline_no_frontmatter():
    text = "just a plain line"
    assert headline(text) == "just a plain line"


def test_headline_frontmatter_multiline_body():
    text = "---\nts: 2026-01-01\n---\n\nFirst line\nSecond line"
    assert headline(text) == "First line"


def test_headline_skips_blank_lines_after_frontmatter():
    text = "---\nts: x\n---\n\n\nActual content"
    assert headline(text) == "Actual content"


def test_build_briefing_current_state(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="state_set",
                     payload={"task": "Build MIND HIVE", "verified": "tests green",
                              "next_step": "add briefing", "open_questions": ["Q1?"],
                              "deferred": ["D1"]}, tags=[]),
        events.Event(ts="2026-06-20T10:00:00Z", session_id="s", type="decision",
                     payload={"text": "Use TDD"}, tags=["arch"]),
    ]
    out = build_briefing(evs)
    assert "# PROJECT BRIEFING" in out
    assert "## Current state" in out
    assert "Build MIND HIVE" in out
    assert "add briefing" in out
    assert "Q1?" in out
    assert "D1" in out


def test_build_briefing_no_state_set(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = [
        events.Event(ts="2026-06-20T10:00:00Z", session_id="s", type="decision",
                     payload={"text": "Use Python"}, tags=[]),
    ]
    out = build_briefing(evs)
    assert "(no state_set yet)" in out


def test_build_briefing_timeline_lists_distinct_semantic_events(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="decision",
                     payload={"text": "Decision A"}, tags=[]),
        events.Event(ts="2026-06-20T10:00:00Z", session_id="s", type="caveat",
                     payload={"text": "Caveat B"}, tags=[]),
        events.Event(ts="2026-06-20T11:00:00Z", session_id="s", type="experiment_result",
                     payload={"text": "Experiment C"}, tags=[]),
        events.Event(ts="2026-06-20T12:00:00Z", session_id="s", type="state_set",
                     payload={"task": "x"}, tags=[]),
    ]
    out = build_briefing(evs)
    # Distinct (non-duplicate) semantic events all survive dedup; state_set is
    # excluded from the timeline.
    assert "## Timeline" in out
    assert "Decision A" in out
    assert "Caveat B" in out
    assert "Experiment C" in out
    timeline = out[out.index("## Timeline"):out.index("## Key decisions")]
    lines = [l for l in timeline.splitlines() if l.startswith("- ") and "[" in l]
    assert len(lines) == 3


def test_build_briefing_dedups_repeated_caveats(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # Eight verbatim repeats of the same caveat — the real-world bloat case.
    evs = [
        events.Event(ts=f"2026-06-20T09:0{i}:00Z", session_id="s", type="caveat",
                     payload={"text": "Decide per question: browser or terminal"},
                     tags=[])
        for i in range(8)
    ]
    out = build_briefing(evs)
    # A caveat legitimately appears in both the Timeline and the Lessons
    # sections — but within EACH it collapses from 8 verbatim repeats to a
    # single representative carrying the occurrence count.
    tl_idx = out.index("## Timeline")
    lc_idx = out.index("## Lessons")
    timeline = out[tl_idx:lc_idx]
    lessons = out[lc_idx:]
    assert timeline.count("Decide per question: browser or terminal") == 1
    assert lessons.count("Decide per question: browser or terminal") == 1
    assert timeline.count("(×8)") == 1
    assert lessons.count("(×8)") == 1


def test_build_briefing_timeline_capped(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from mindhive.briefing import _TIMELINE_CAP
    evs = [
        events.Event(ts=f"2026-06-20T09:{i:02d}:00Z", session_id="s",
                     type="decision", payload={"text": f"Distinct decision {i}"},
                     tags=[])
        for i in range(_TIMELINE_CAP + 10)
    ]
    out = build_briefing(evs)
    timeline_lines = [l for l in out.splitlines()
                      if l.startswith("- ") and "[decision]" in l]
    assert len(timeline_lines) == _TIMELINE_CAP
    # The cap keeps the most recent entries.
    assert f"Distinct decision {_TIMELINE_CAP + 9}" in out
    assert "Distinct decision 0" not in out


def test_build_briefing_key_decisions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="decision",
                     payload={"text": "Use TDD"}, tags=[]),
        events.Event(ts="2026-06-20T10:00:00Z", session_id="s", type="caveat",
                     payload={"text": "Fragile"}, tags=[]),
    ]
    out = build_briefing(evs)
    assert "## Key decisions" in out
    assert "Use TDD" in out
    # Key decisions section should not show caveats
    kd_idx = out.index("## Key decisions")
    lc_idx = out.index("## Lessons")
    kd_section = out[kd_idx:lc_idx]
    assert "Fragile" not in kd_section


def test_build_briefing_caveats(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="caveat",
                     payload={"text": "Watch out"}, tags=[]),
    ]
    out = build_briefing(evs)
    assert "## Lessons & caveats" in out
    assert "Watch out" in out


def test_build_briefing_activity_counts(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="session_start",
                     payload={}, tags=[]),
        events.Event(ts="2026-06-20T09:01:00Z", session_id="s", type="file_touched",
                     payload={"path": "src/foo.py"}, tags=[]),
        events.Event(ts="2026-06-20T09:02:00Z", session_id="s", type="file_touched",
                     payload={"text": "src/bar.py"}, tags=[]),
        events.Event(ts="2026-06-20T09:03:00Z", session_id="s", type="command_run",
                     payload={"text": "pytest"}, tags=[]),
    ]
    out = build_briefing(evs)
    assert "## Activity" in out
    assert "file_touched: 2" in out
    assert "command_run: 1" in out
    assert "session_start: 1" in out
    assert "src/foo.py" in out
    assert "src/bar.py" in out


def test_build_briefing_event_count_footer(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    evs = [
        events.Event(ts="2026-06-20T09:00:00Z", session_id="s", type="decision",
                     payload={"text": "x"}, tags=[]),
    ]
    out = build_briefing(evs)
    assert "_events: 1_" in out


def test_briefing_function_writes_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "wrote a file"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = briefing()
    assert p.exists()
    assert p.name == "BRIEFING.md"
    content = p.read_text(encoding="utf-8")
    assert "# PROJECT BRIEFING" in content


def test_briefing_deterministic(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "deterministic"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p1 = briefing()
    c1 = p1.read_text(encoding="utf-8")
    p2 = briefing()
    c2 = p2.read_text(encoding="utf-8")
    assert c1 == c2
