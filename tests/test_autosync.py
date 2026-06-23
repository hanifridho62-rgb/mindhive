# tests/test_autosync.py
from mindhive import autosync


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _stub_downstream(monkeypatch, calls):
    """Replace the heavy projection stages with no-ops that record their calls."""
    monkeypatch.setattr(autosync.projector, "project", lambda p: calls.append(("project", p)))
    monkeypatch.setattr(autosync.briefing, "briefing", lambda p: calls.append(("briefing", p)))
    monkeypatch.setattr(autosync.graph, "build", lambda p, semantic=False: calls.append(("graph", p)))
    monkeypatch.setattr(autosync.sync, "sync", lambda folder, project=None: calls.append(("sync", folder, project)))


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


def test_run_all_passes_skip_captured_true(tmp_path, monkeypatch):
    """The invariant: autosync must NOT re-harvest sessions the live hooks already
    recorded, or it double-counts. run_all must call harvest with skip_captured=True."""
    _isolate(tmp_path, monkeypatch)
    autosync.register("test", "C:/tx", "C:/code")
    seen = {}

    def spy_harvest(transcripts, project, **kwargs):
        seen["kwargs"] = kwargs
        return {"new_events": 0}

    monkeypatch.setattr(autosync.harvest, "harvest", spy_harvest)
    _stub_downstream(monkeypatch, [])
    autosync.run_all()
    assert seen["kwargs"].get("skip_captured") is True


def test_run_all_runs_full_pipeline_per_project(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    autosync.register("test", "C:/tx", "C:/code")
    monkeypatch.setattr(autosync.harvest, "harvest", lambda t, p, **k: {"new_events": 1})
    calls = []
    _stub_downstream(monkeypatch, calls)
    results = autosync.run_all()
    stages = [c[0] for c in calls]
    assert stages == ["project", "briefing", "graph", "sync"]
    assert results[0]["project"] == "test"


def test_run_all_isolates_per_entry_errors(tmp_path, monkeypatch):
    """A single failing entry must not abort the rest of the registry."""
    _isolate(tmp_path, monkeypatch)
    autosync.register("bad", "C:/tx/bad", "C:/code/bad")
    autosync.register("good", "C:/tx/good", "C:/code/good")

    def harvest_maybe_fail(transcripts, project, **kwargs):
        if project == "bad":
            raise RuntimeError("boom")
        return {"new_events": 0}

    monkeypatch.setattr(autosync.harvest, "harvest", harvest_maybe_fail)
    _stub_downstream(monkeypatch, [])
    results = autosync.run_all()
    by_proj = {r["project"]: r for r in results}
    assert "error" in by_proj["bad"]
    assert "harvest" in by_proj["good"]
