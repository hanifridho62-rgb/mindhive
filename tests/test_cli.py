# tests/test_cli.py
import io
import sys
from mindhive import __main__ as cli_mod, events, projector, paths


def test_append_positional_text():
    ret = cli_mod.main(["append", "decision", "hello"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].payload == {"text": "hello"}
    assert evs[0].type == "decision"


def test_append_positional_json():
    ret = cli_mod.main(["append", "state_set", '{"task":"x"}', "--json"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].payload == {"task": "x"}
    assert evs[0].type == "state_set"


def test_append_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"task":"y"}'))
    ret = cli_mod.main(["append", "state_set", "--stdin", "--json"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].payload == {"task": "y"}


def test_append_no_payload_returns_2():
    ret = cli_mod.main(["append", "decision"])
    assert ret == 2


def test_hook_post_tool_use_captures_from_stdin(monkeypatch):
    payload = '{"session_id":"s9","tool_name":"Bash","tool_input":{"command":"pytest -q"}}'
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    ret = cli_mod.main(["hook", "post_tool_use"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].type == "command_run"
    assert evs[0].session_id == "s9"
    assert evs[0].payload.get("text") == "pytest -q"


def test_recall_project_override_targets_named_namespace(capsys):
    """`recall --project X` searches X's ledger regardless of the detected
    (env) namespace — the fix for 'wrong project from the wrong directory'."""
    events.append_event("decision", {"text": "fee tier maker taker in fabio"},
                        session_id="s", ts="2026-06-20T10:00:00Z", project="fabio")
    projector.project("fabio")
    ret = cli_mod.main(["recall", "fee tier maker taker", "--project", "fabio"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "fee tier maker taker" in out


def test_state_project_override(capsys):
    events.append_event("state_set", {"task": "fabio task"},
                        session_id="s", ts="2026-06-20T10:00:00Z", project="fabio")
    projector.project("fabio")
    ret = cli_mod.main(["state", "--project", "fabio"])
    assert ret == 0
    assert "fabio task" in capsys.readouterr().out


def test_projects_lists_registered_and_unregistered(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    (tmp_path / "autosync.json").write_text(
        '[{"project": "fabio"}]', encoding="utf-8")
    (tmp_path / "fabio" / "events").mkdir(parents=True)
    (tmp_path / "brave-brattain-28ae65").mkdir()
    (tmp_path / "brave-brattain-28ae65" / "STATE.md").write_text("# STATE\n", encoding="utf-8")
    ret = cli_mod.main(["projects"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "registered" in out and "fabio" in out
    assert "unregistered" in out and "brave-brattain-28ae65" in out


def test_state_warns_on_freshly_minted_namespace(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    monkeypatch.setattr(paths, "detect_project", lambda cwd=None: "brave-brattain-28ae65")
    ret = cli_mod.main(["state"])
    assert ret == 0
    err = capsys.readouterr().err
    assert "no memory for detected project" in err.lower()


def test_flag_creates_judgment_incident_active_at_one(capsys):
    ret = cli_mod.main(["flag", "committed to master without permission"])
    assert ret == 0
    sig = "judgment:flag:committed-to-master-without-permission"
    assert sig in capsys.readouterr().out
    evs = [e for e in events.read_all() if e.type == "tool_failed"]
    assert len(evs) == 1
    assert evs[0].payload["signature"] == sig
    assert evs[0].payload["text"] == "committed to master without permission"
    assert "judgment" in evs[0].tags
    from mindhive import incidents
    rec = incidents.load(sig, project="test")
    assert rec["status"] == "active"           # flags activate at 1 occurrence
    assert rec["occurrences"] == 1


def test_flag_category_adds_tag():
    ret = cli_mod.main(["flag", "merged without approval", "--category", "git"])
    assert ret == 0
    ev = [e for e in events.read_all() if e.type == "tool_failed"][0]
    assert "judgment" in ev.tags and "git" in ev.tags


def test_project_on_corrupt_log_exits_cleanly(tmp_path, capsys):
    """A corrupt event-log line must surface as a clean CLI error (exit 1), not a
    raw traceback. read_all still fails loud — the CLI just frames it."""
    events.append_event("decision", {"text": "a"}, session_id="s", ts="2026-06-20T10:00:00Z")
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    ret = cli_mod.main(["project"])
    assert ret == 1
    err = capsys.readouterr().err
    assert "corrupt event log" in err.lower()
    assert "Traceback" not in err
