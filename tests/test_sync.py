# tests/test_sync.py
from pathlib import Path
from mindhive import events, paths
from mindhive.sync import encode_folder, native_memory_path, sync, USAGE_NOTE


def test_encode_folder_hanif():
    assert encode_folder(r"C:\Users\hanif") == "C--Users-hanif"


def test_encode_folder_fabio():
    assert encode_folder(r"C:\FABIO_HFT_V18") == "C--FABIO-HFT-V18"


def test_encode_folder_forward_slash():
    assert encode_folder("C:/Users/hanif") == "C--Users-hanif"


def test_native_memory_path_structure(tmp_path):
    p = native_memory_path(r"C:\Users\hanif", home=tmp_path)
    assert p == tmp_path / ".claude" / "projects" / "C--Users-hanif" / "memory" / "MEMORY.md"


def test_sync_writes_file(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "Test task", "verified": "ok",
                         "next_step": "continue", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    assert p.exists()
    assert p.name == "MEMORY.md"


def test_sync_contains_state_task(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "My active task", "verified": "green",
                         "next_step": "ship it", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "My active task" in content


def test_sync_contains_usage_note(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "use python"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "START" in content
    assert "DURING" in content
    assert "END" in content


def test_sync_with_briefing(tmp_path):
    """include_briefing keeps BRIEFING.md fresh ON DISK for on-demand reads, but
    MEMORY.md is a compact digest — the full briefing is no longer embedded."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "briefing decision"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "PROJECT BRIEFING" not in content              # not embedded any more
    assert "briefing decision" in content                 # digest carries decisions
    bpath = paths.project_dir() / "BRIEFING.md"
    assert bpath.exists()                                 # still written to disk
    assert "PROJECT BRIEFING" in bpath.read_text(encoding="utf-8")


def test_sync_no_briefing(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "no briefing decision"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=False, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "PROJECT BRIEFING" not in content


def test_sync_does_not_duplicate_state_block(tmp_path):
    """The digest embeds the curated state block exactly once (no briefing
    embedding to duplicate it against)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "Unique-task-token", "verified": "ok",
                         "next_step": "go", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert content.count("Unique-task-token") == 1
    assert "## Current State" in content
    assert "PROJECT BRIEFING" not in content


def test_sync_reads_event_log_once(tmp_path, monkeypatch):
    """sync must read the event log a single time and reuse it for both the
    STATE and briefing projections (was 2+ full read_all calls)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "x"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    calls = {"n": 0}
    real = events.read_all

    def counting(project=None):
        calls["n"] += 1
        return real(project)

    monkeypatch.setattr(events, "read_all", counting)
    sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    assert calls["n"] == 1


def test_sync_runs_recommend_chain_exactly_once(tmp_path, monkeypatch):
    """sync composes the state twice (STATE.md + the embedded digest) but must
    compute the suggested-skills block — the recommend chain — exactly once."""
    from mindhive import plugins
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "x"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    calls = {"n": 0}
    real = plugins.recommend_for

    def counting(project=None, evs=None, top=None):
        calls["n"] += 1
        return real(project=project, evs=evs, top=top)

    monkeypatch.setattr(plugins, "recommend_for", counting)
    sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    assert calls["n"] == 1


def test_sync_path_matches_encode_folder(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "x"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    folder = r"C:\Users\hanif"
    p = sync(folder, project=None, home=fake_home)
    expected = fake_home / ".claude" / "projects" / "C--Users-hanif" / "memory" / "MEMORY.md"
    assert p == expected


def test_usage_note_is_constant():
    """USAGE_NOTE is a module-level constant (DRY)."""
    assert "START" in USAGE_NOTE
    assert "DURING" in USAGE_NOTE
    assert "END" in USAGE_NOTE


# --- compact digest (KTD8): rules block, pinned lessons, size budget ---------

from mindhive import incidents


def _seed_reflected_rules(n, project="test"):
    """n active incidents, each with a reflected prevention rule. Later indices
    have more recent failures, so they rank higher at equal occurrences."""
    for i in range(n):
        sig = f"pymodule:sig{i:02d}"
        for s, ts in (("s1", f"2026-07-01T10:{i:02d}:00Z"),
                      ("s2", f"2026-07-02T10:{i:02d}:00Z")):
            events.append_event("tool_failed",
                                {"text": "boom", "signature": sig, "action": "x"},
                                session_id=s, tags=["auto", "failure"],
                                ts=ts, project=project)
    incidents.project(project=project, threshold=2)
    d = paths.project_dir(project) / "rules" / "incidents"
    for i in range(n):
        path = d / f"{incidents._safe_name(f'pymodule:sig{i:02d}')}.md"
        path.write_text(incidents._replace_section(
            path.read_text(encoding="utf-8"), "Auto Evaluation",
            f"- Prevention rule: avoid failure {i:02d}"), encoding="utf-8")
    incidents.project(project=project, threshold=2)


def test_memory_rules_block_bounded_and_each_rule_once(tmp_path):
    """30 active reflected rules -> capped block (+N more trailer) in BOTH
    STATE.md and MEMORY.md; each shown rule appears exactly once in MEMORY.md
    (no state-body/rules-block duplication)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _seed_reflected_rules(30)
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert content.count("# PREVENTION RULES") == 1
    assert content.count("- [pymodule:sig") == incidents.INJECT_CAP
    assert "+20 more: hive rules list" in content
    # most recent (highest index) shown exactly once; oldest cut entirely
    assert content.count("avoid failure 29") == 1
    assert "avoid failure 00" not in content
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert state.count("- [pymodule:sig") == incidents.INJECT_CAP
    assert "+20 more: hive rules list" in state


def test_pinned_caveat_survives_cap_unpinned_rotate(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("caveat", {"text": "NEVER disturb V17 --live"},
                        session_id="s", tags=["pinned"],
                        ts="2026-06-01T09:00:00Z")
    for i in range(15):
        events.append_event("caveat", {"text": f"ordinary caveat number {i:02d}"},
                            session_id="s", ts=f"2026-06-20T09:{i:02d}:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "NEVER disturb V17 --live" in content          # pinned, despite being oldest
    assert "ordinary caveat number 14" in content         # newest unpinned kept
    assert "ordinary caveat number 00" not in content     # oldest unpinned rotated out
    # pinned entry rides ahead of the recency-filled remainder
    lessons = content.split("## Lessons & caveats", 1)[1]
    assert lessons.index("NEVER disturb V17") < lessons.index("caveat number 14")


def test_long_history_memory_stays_under_budget(tmp_path):
    """MEMORY.md for a long history stays under the ~6 KB character budget
    (≈1,500 tokens × 4) and still carries state + prevention rules."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "Budget-task-token", "verified": "ok",
                         "next_step": "go", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-01T08:00:00Z")
    for i in range(100):
        events.append_event("caveat",
                            {"text": f"lesson {i:03d}: watch for edge case {i} "
                                     f"in module {i % 7} when the cache is cold"},
                            session_id="s", ts=f"2026-06-10T{i // 60:02d}:{i % 60:02d}:00Z")
        events.append_event("decision",
                            {"text": f"decision {i:03d}: keep approach {i % 5} "
                                     f"for subsystem {i % 3}"},
                            session_id="s", ts=f"2026-06-11T{i // 60:02d}:{i % 60:02d}:00Z")
    _seed_reflected_rules(30)
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert len(content) < 6000
    assert "Budget-task-token" in content
    assert "# PREVENTION RULES" in content
    assert "## Lessons & caveats" in content
    assert "## Key decisions" in content
