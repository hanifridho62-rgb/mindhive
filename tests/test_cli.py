# tests/test_cli.py
import io
import sys
from mindhive import __main__ as cli_mod, events


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def test_append_positional_text(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    ret = cli_mod.main(["append", "decision", "hello"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].payload == {"text": "hello"}
    assert evs[0].type == "decision"


def test_append_positional_json(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    ret = cli_mod.main(["append", "state_set", '{"task":"x"}', "--json"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].payload == {"task": "x"}
    assert evs[0].type == "state_set"


def test_append_stdin(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"task":"y"}'))
    ret = cli_mod.main(["append", "state_set", "--stdin", "--json"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].payload == {"task": "y"}


def test_append_no_payload_returns_2(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    ret = cli_mod.main(["append", "decision"])
    assert ret == 2


def test_hook_post_tool_use_captures_from_stdin(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    payload = '{"session_id":"s9","tool_name":"Bash","tool_input":{"command":"pytest -q"}}'
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    ret = cli_mod.main(["hook", "post_tool_use"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].type == "command_run"
    assert evs[0].session_id == "s9"
    assert evs[0].payload.get("text") == "pytest -q"


def test_project_on_corrupt_log_exits_cleanly(tmp_path, monkeypatch, capsys):
    """A corrupt event-log line must surface as a clean CLI error (exit 1), not a
    raw traceback. read_all still fails loud — the CLI just frames it."""
    _setup(tmp_path, monkeypatch)
    events.append_event("decision", {"text": "a"}, session_id="s", ts="2026-06-20T10:00:00Z")
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    ret = cli_mod.main(["project"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "corrupt event log" in err.lower()
    assert "Traceback" not in err
