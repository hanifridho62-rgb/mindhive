# tests/test_autosync.py
import json
from mindhive import autosync, events


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _tooluse(name, inp):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _write_transcript(dirpath, sid, lines):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_register_and_load_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    autosync.register("proj-a", "C:/tx/a", "C:/code/a")
    autosync.register("proj-b", "C:/tx/b", "C:/code/b")
    reg = autosync.load_registry()
    by_proj = {e["project"]: e for e in reg}
    assert by_proj["proj-a"]["transcripts"] == "C:/tx/a"
    assert by_proj["proj-b"]["folder"] == "C:/code/b"


def test_register_replaces_existing_entry(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    autosync.register("proj-a", "C:/old", "C:/old")
    autosync.register("proj-a", "C:/new", "C:/new")
    reg = autosync.load_registry()
    assert len([e for e in reg if e["project"] == "proj-a"]) == 1
    assert reg[0]["transcripts"] == "C:/new"


# --- run_all: auto-discover capture (new behavior) ---

def test_run_all_discovers_all_via_catch_up(tmp_path, monkeypatch):
    """run_all must harvest via catch_up with skip_captured=True (no double-count)
    and known_only=False (discover brand-new projects, not just registered ones)."""
    _isolate(tmp_path, monkeypatch)
    seen = {}

    def spy_catch_up(now=None, skip_captured=True, known_only=True):
        seen["skip_captured"] = skip_captured
        seen["known_only"] = known_only
        return {"projects": {}, "total_new": 0, "skipped_unknown": []}

    monkeypatch.setattr(autosync.harvest, "catch_up", spy_catch_up)
    monkeypatch.setattr(autosync, "load_registry", lambda: [])
    autosync.run_all()
    assert seen["skip_captured"] is True
    assert seen["known_only"] is False


def test_run_all_rebuilds_every_touched_project(tmp_path, monkeypatch):
    """Every project that gained events gets projector.project + graph.build.
    With nothing registered, NO native sync happens."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(autosync.harvest, "catch_up",
                        lambda **k: {"projects": {"alpha": {"new_events": 2},
                                                  "beta": {"new_events": 1}},
                                     "total_new": 3, "skipped_unknown": []})
    monkeypatch.setattr(autosync, "load_registry", lambda: [])
    calls = []
    monkeypatch.setattr(autosync.projector, "project", lambda p: calls.append(("project", p)))
    monkeypatch.setattr(autosync.graph, "build", lambda p, semantic=False: calls.append(("graph", p)))
    monkeypatch.setattr(autosync.sync, "sync", lambda folder, project=None: calls.append(("sync", project)))
    autosync.run_all()
    assert ("project", "alpha") in calls and ("graph", "alpha") in calls
    assert ("project", "beta") in calls and ("graph", "beta") in calls
    assert not any(c[0] == "sync" for c in calls)   # nothing registered -> no MEMORY.md


def test_run_all_native_syncs_only_registered(tmp_path, monkeypatch):
    """Registered projects get a native MEMORY.md (via sync.sync); unregistered
    auto-discovered projects do NOT (store capture only)."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(autosync.harvest, "catch_up",
                        lambda **k: {"projects": {"mindhive": {"new_events": 1},
                                                  "randoproj": {"new_events": 1}},
                                     "total_new": 2, "skipped_unknown": []})
    monkeypatch.setattr(autosync, "load_registry",
                        lambda: [{"project": "mindhive", "transcripts": "T",
                                  "folder": "C:/Users/hanif/mindhive"}])
    synced, projected, graphed = [], [], []
    monkeypatch.setattr(autosync.sync, "sync",
                        lambda folder, project=None: synced.append((project, folder)))
    monkeypatch.setattr(autosync.projector, "project", lambda p: projected.append(p))
    monkeypatch.setattr(autosync.graph, "build", lambda p, semantic=False: graphed.append(p))
    autosync.run_all()
    # registered -> sync (not a direct projector call, sync does that internally)
    assert synced == [("mindhive", "C:/Users/hanif/mindhive")]
    # unregistered -> projector.project, never sync
    assert projected == ["randoproj"]
    assert set(graphed) == {"mindhive", "randoproj"}


def test_run_all_isolates_per_project_errors(tmp_path, monkeypatch):
    """One failing project must not abort the others."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(autosync.harvest, "catch_up",
                        lambda **k: {"projects": {"good": {"new_events": 1},
                                                  "bad": {"new_events": 1}},
                                     "total_new": 2, "skipped_unknown": []})
    monkeypatch.setattr(autosync, "load_registry", lambda: [])

    def proj(p):
        if p == "bad":
            raise RuntimeError("boom")

    monkeypatch.setattr(autosync.projector, "project", proj)
    monkeypatch.setattr(autosync.graph, "build", lambda p, semantic=False: None)
    results = autosync.run_all()
    by = {r["project"]: r for r in results}
    assert "error" in by["bad"]
    assert "error" not in by["good"]


def test_run_all_end_to_end_captures_unregistered_without_memory_md(tmp_path, monkeypatch):
    """Real path: a transcript session for an unregistered project is captured in
    the store, but no native MEMORY.md is written for it."""
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s1", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s1"}),
        _tooluse("Edit", {"file_path": "a.py"}),
    ])
    monkeypatch.setattr(autosync.harvest, "claude_projects_root", lambda: root)
    monkeypatch.setattr(autosync, "load_registry", lambda: [])   # nothing registered
    synced = []
    monkeypatch.setattr(autosync.sync, "sync",
                        lambda folder, project=None: synced.append(project))
    autosync.run_all()
    # MINDHIVE_PROJECT=test forces the namespace to "test" regardless of cwd
    evs = events.read_all("test")
    assert any(e.type == "file_touched" for e in evs)   # captured in the store
    assert synced == []                                  # unregistered -> no MEMORY.md
