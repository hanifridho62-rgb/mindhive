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
