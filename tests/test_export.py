# tests/test_export.py
from mindhive import events, projector, export, paths


def test_export_bundles_state_and_ledger():
    events.append_event("state_set", {"task": "OFI verdict"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "default OFF"},
                        session_id="s", ts="2026-06-20T11:00:00Z")
    projector.project()
    out = export.export()
    text = out.read_text(encoding="utf-8")
    assert out == paths.project_dir() / "handoff.md"
    assert "HANDOFF" in text
    assert "OFI verdict" in text
    assert "default OFF" in text

def test_export_custom_out_path(tmp_path):
    events.append_event("decision", {"text": "x"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    projector.project()
    dest = tmp_path / "myhandoff.md"
    out = export.export(out=str(dest))
    assert out == dest and dest.exists()
