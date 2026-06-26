"""Phase B U3/U7 — MindHive dual-write seam.

The default backend ('file') must be a no-op: behavior identical to before, no
host import. 'dual' mirrors through the CC-v3 host bridge. The bridge is faked
via sys.modules so this test needs neither CC-v3 on the path nor a database.
"""
import sys
import types

from mindhive import events


def _fake_host_bridge(monkeypatch):
    calls = []
    scripts = types.ModuleType("scripts")
    scripts.__path__ = []  # mark as package
    core = types.ModuleType("scripts.core")
    core.__path__ = []
    ew = types.ModuleType("scripts.core.event_write")
    ew.append_pg = lambda *a, **k: calls.append((a, k)) or {"ok": True, "id": 1}
    core.event_write = ew
    monkeypatch.setitem(sys.modules, "scripts", scripts)
    monkeypatch.setitem(sys.modules, "scripts.core", core)
    monkeypatch.setitem(sys.modules, "scripts.core.event_write", ew)
    return calls


def test_default_backend_is_file_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "seamtest")
    monkeypatch.delenv("MINDHIVE_EVENT_BACKEND", raising=False)
    calls = _fake_host_bridge(monkeypatch)

    ev = events.append_event("decision", {"text": "hi"}, session_id="s1")

    seg = tmp_path / "seamtest" / "events" / "s1.jsonl"
    assert seg.exists() and "hi" in seg.read_text(encoding="utf-8")
    assert calls == []  # default 'file' never touches the host bridge
    assert ev.type == "decision"


def test_dual_backend_mirrors_to_pg(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "seamtest")
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "dual")
    calls = _fake_host_bridge(monkeypatch)

    events.append_event("caveat", {"text": "watch out"}, session_id="s2")

    # File still written (source of truth) AND mirrored to PG.
    seg = tmp_path / "seamtest" / "events" / "s2.jsonl"
    assert seg.exists()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "seamtest" and args[1] == "caveat"  # project, type
    assert kwargs.get("ts")  # ts forwarded for parity ordering
