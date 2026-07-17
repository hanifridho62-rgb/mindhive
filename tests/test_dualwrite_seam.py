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


def _fake_host_read(monkeypatch, returns=None, raises=False):
    scripts = types.ModuleType("scripts")
    scripts.__path__ = []
    core = types.ModuleType("scripts.core")
    core.__path__ = []
    ew = types.ModuleType("scripts.core.event_write")

    def read_pg(project):
        if raises:
            raise RuntimeError("pg down")
        return returns

    ew.read_pg = read_pg
    core.event_write = ew
    monkeypatch.setitem(sys.modules, "scripts", scripts)
    monkeypatch.setitem(sys.modules, "scripts.core", core)
    monkeypatch.setitem(sys.modules, "scripts.core.event_write", ew)


def test_read_default_and_dual_read_files(tmp_path, monkeypatch):
    # 'file' and 'dual' both read the file store (file stays canonical-read).
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "rtest")
    _fake_host_read(monkeypatch, returns=["SENTINEL_PG"])
    events.append_event("decision", {"text": "from file"}, session_id="s1")
    for backend in ("file", "dual"):
        monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", backend)
        evs = events.read_all()
        assert evs and evs[0].payload["text"] == "from file"  # file, not the PG sentinel


def test_read_pg_backend_reads_from_postgres(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "rtest")
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")
    _fake_host_read(monkeypatch, returns=["PG_EVENTS"])
    assert events.read_all() == ["PG_EVENTS"]


def test_read_pg_falls_back_to_file_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "rtest")
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")
    _fake_host_read(monkeypatch, raises=True)
    # seed a file event with backend temporarily 'file' so the write isn't mirrored
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "file")
    events.append_event("decision", {"text": "file fallback"}, session_id="s1")
    monkeypatch.setenv("MINDHIVE_EVENT_BACKEND", "pg")
    evs = events.read_all()  # pg read raises -> falls back to file
    assert evs and evs[0].payload["text"] == "file fallback"
