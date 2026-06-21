# tests/test_sync.py
from pathlib import Path
from mindhive import events, paths
from mindhive.sync import encode_folder, native_memory_path, sync, USAGE_NOTE


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_encode_folder_hanif():
    assert encode_folder(r"C:\Users\hanif") == "C--Users-hanif"


def test_encode_folder_fabio():
    assert encode_folder(r"C:\FABIO_HFT_V18") == "C--FABIO-HFT-V18"


def test_encode_folder_forward_slash():
    assert encode_folder("C:/Users/hanif") == "C--Users-hanif"


def test_native_memory_path_structure(tmp_path):
    p = native_memory_path(r"C:\Users\hanif", home=tmp_path)
    assert p == tmp_path / ".claude" / "projects" / "C--Users-hanif" / "memory" / "MEMORY.md"


def test_sync_writes_file(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "Test task", "verified": "ok",
                         "next_step": "continue", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    assert p.exists()
    assert p.name == "MEMORY.md"


def test_sync_contains_state_task(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("state_set",
                        {"task": "My active task", "verified": "green",
                         "next_step": "ship it", "open_questions": [], "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "My active task" in content


def test_sync_contains_usage_note(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "use python"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "START" in content
    assert "DURING" in content
    assert "END" in content


def test_sync_with_briefing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "briefing decision"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=True, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "PROJECT BRIEFING" in content


def test_sync_no_briefing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    events.append_event("decision", {"text": "no briefing decision"},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    p = sync(r"C:\TEMP\demo", project=None, include_briefing=False, home=fake_home)
    content = p.read_text(encoding="utf-8")
    assert "PROJECT BRIEFING" not in content


def test_sync_path_matches_encode_folder(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
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
