import json
from mindhive import __main__ as cli, events, harvest


def _write_transcript(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash",
             "input": {"command": "python mindhive.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True, "content": ""}]},
         "toolUseResult": {"stderr": "No module named 'mindhive'", "interrupted": False}},
    ]
    (tdir / "s.jsonl").write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    return tdir


def test_failures_capture_then_list(tmp_path, capsys):
    tdir = _write_transcript(tmp_path)
    rc = cli.main(["failures", "capture", "--transcripts", str(tdir), "--project", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"new_failures": 1' in out

    rc = cli.main(["failures", "list", "--project", "test"])
    assert rc == 0
    listing = capsys.readouterr().out
    assert "pymodule:mindhive" in listing
    assert "candidate" in listing        # 1 occurrence < threshold


def test_capture_cli_stamps_activation_for_recurring(tmp_path):
    from mindhive import incidents
    tdir = tmp_path / "tx"
    tdir.mkdir()

    def pair(tu, cmd, err):
        return [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tu, "name": "Bash", "input": {"command": cmd}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tu, "is_error": True, "content": ""}]},
             "toolUseResult": {"stderr": err, "interrupted": False}},
        ]
    lines = (pair("a", "python x", "No module named 'mindhive'")
             + pair("b", "python y", "No module named 'mindhive.paths'"))
    (tdir / "s.jsonl").write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")

    assert cli.main(["failures", "capture", "--transcripts", str(tdir), "--project", "test"]) == 0
    # capture alone (no manual `rules audit`) must stamp activation, or leak-based
    # auto-demotion is unreachable via the natural flow.
    assert incidents._activation_ts("test", "pymodule:mindhive") is not None


def _fail_pair(tu, cmd, err, ts):
    return [
        {"timestamp": ts, "type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tu, "name": "Bash", "input": {"command": cmd}}]}},
        {"timestamp": ts, "type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tu, "is_error": True, "content": ""}]},
         "toolUseResult": {"stderr": err, "interrupted": False}},
    ]


def test_capture_flow_reaches_auto_demotion(tmp_path):
    """End-to-end: the natural CLI capture flow (no manual audit) must reach
    leak-based auto-demotion once a rule keeps leaking. Regression for the bug
    where capture skipped audit AND discarded transcript timestamps."""
    from mindhive import incidents, paths
    ERR = "No module named 'zzz'"

    # Round 1: two occurrences with real, ordered timestamps -> activates at 10:01.
    t1 = tmp_path / "tx1"
    t1.mkdir()
    r1 = (_fail_pair("a", "python x", ERR, "2026-06-30T10:00:00Z")
          + _fail_pair("b", "python y", ERR, "2026-06-30T10:01:00Z"))
    (t1 / "s1.jsonl").write_text("\n".join(json.dumps(o) for o in r1), encoding="utf-8")
    assert cli.main(["failures", "capture", "--transcripts", str(t1), "--project", "test"]) == 0

    # Give it an auto-sourced rule (what reflection would write).
    p = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-zzz.md")
    p.write_text(incidents._replace_section(p.read_text(encoding="utf-8"),
                 "Auto Evaluation", "- Prevention rule: install zzz"), encoding="utf-8")
    incidents.project(project="test")

    # Round 2: two LATER occurrences (leaks after activation) -> auto-demote.
    t2 = tmp_path / "tx2"
    t2.mkdir()
    r2 = (_fail_pair("c", "python p", ERR, "2026-06-30T11:00:00Z")
          + _fail_pair("d", "python q", ERR, "2026-06-30T11:01:00Z"))
    (t2 / "s2.jsonl").write_text("\n".join(json.dumps(o) for o in r2), encoding="utf-8")
    assert cli.main(["failures", "capture", "--transcripts", str(t2), "--project", "test"]) == 0

    assert incidents.leaks(project="test", signature="pymodule:zzz") == 2
    rec = incidents.load("pymodule:zzz", project="test")
    assert rec["status"] == "demoted"


def test_capture_refused_while_harvest_lock_held_then_captures_after_release(tmp_path, capsys):
    """`hive failures capture` is a second writer over the transcript directory
    the dashboard's auto-harvest loop sweeps under `harvest.harvest_lock`.
    Both stamp `tool_failed` rows with the transcript line's own timestamp and
    dedup against a snapshot taken BEFORE the walk, so two concurrent passes
    mint byte-identical rows the append-only, hash-chained ledger keeps for
    good. The CLI must therefore take the same lock: like `hive harvest
    --catch-up`, answer `locked: true`, append nothing and exit 0 while a live
    harvester holds it, then capture normally once it is released."""
    tdir = _write_transcript(tmp_path)
    argv = ["failures", "capture", "--transcripts", str(tdir), "--project", "test"]

    with harvest.harvest_lock() as held:
        # The test process really holds the mutex, so the refusal asserted
        # below cannot be vacuous.
        assert held is True
        rc = cli.main(argv)
        refused = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert refused.get("locked") is True
    assert refused.get("new_failures") == 0      # a caller reading the count must not KeyError
    assert [e for e in events.read_all("test") if e.type == "tool_failed"] == []

    rc = cli.main(argv)                          # lock released: same command, real capture
    captured = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert not captured.get("locked")
    assert captured["new_failures"] == 1
    assert len([e for e in events.read_all("test") if e.type == "tool_failed"]) == 1
