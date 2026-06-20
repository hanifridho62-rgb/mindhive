# tests/test_migrate.py
from mindhive import migrate, events, projector, paths

def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")

SAMPLE = """\
# FABIO V18

2026-06-11 progress: built V18 tree, sim e2e green.

2026-06-13 progress: cost-ladder shows negative gross edge.

INSIDEN 2026-06-15: V17 live died from resource exhaustion. Lesson: pid-file teardown.
"""

def test_import_splits_into_events(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    src = tmp_path / "fabio.md"
    src.write_text(SAMPLE, encoding="utf-8")
    n = migrate.import_memory_file(str(src))
    assert n == 3
    evs = events.read_all()
    texts = " ".join(e.payload.get("text", "") for e in evs)
    assert "resource exhaustion" in texts

def test_pensyve_disable_writes_flags(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    cfg = tmp_path / "pensyve-plugin.local.md"
    migrate.disable_pensyve(str(cfg))
    body = cfg.read_text(encoding="utf-8")
    assert "context_loading: off" in body
    assert "prompt_enrichment: false" in body
