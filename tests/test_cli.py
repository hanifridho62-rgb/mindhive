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


# --- registry contracts at the CLI append surface ---------------------------
# `hive append <registry type>` accepts an arbitrary payload, so a MALFORMED
# lifecycle event could ride the hash chain as dead weight. These prove the
# mindnet_task contract is enforced here. Scope note: shape validation cannot
# refuse a *well-formed* but illegitimate payload (a phase jump is shape-valid);
# that is the phase controller's job, not this gate's.

def test_append_mindnet_task_malformed_is_rejected(capsys):
    bad = '{"action": "declare", "title": "no task_id here"}'
    assert cli_mod.main(["append", "mindnet_task", bad, "--json"]) == 2
    err = capsys.readouterr().err
    assert "task_id" in err                 # the validator's own reason
    assert "hive mindnet" in err            # the corrective hint
    assert events.read_all() == []          # nothing entered the chain


def test_append_mindnet_task_unknown_action_is_rejected(capsys):
    bad = '{"action": "teleport", "task_id": "mn1-u1"}'
    assert cli_mod.main(["append", "mindnet_task", bad, "--json"]) == 2
    assert "hive mindnet" in capsys.readouterr().err
    assert events.read_all() == []


def test_append_mindnet_task_wellformed_appends_and_folds():
    """The discriminating half: the gate refuses malformed rows only — a valid
    declare still lands and still folds into the graph projection."""
    from mindhive.mindnet import tasks as tasks_mod
    good = ('{"action": "declare", "task_id": "mn1-u1", '
            '"title": "Gate both append surfaces"}')
    assert cli_mod.main(["append", "mindnet_task", good, "--json"]) == 0
    evs = events.read_all()
    assert len(evs) == 1 and evs[0].type == "mindnet_task"
    graph = tasks_mod.fold(evs)
    assert graph["mn1-u1"].title == "Gate both append surfaces"


def test_append_free_text_type_is_not_gated_by_any_contract():
    """A type with no registered contract must stay wide open — the dispatch is
    a gate on the registry, not on the store."""
    assert cli_mod.main(["append", "decision", "no contract applies here"]) == 0
    assert len(events.read_all()) == 1


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


def _tracking_answer(monkeypatch, **fields):
    from mindhive import repo
    answer = {"branch": "", "upstream": "", "upstream_missing": True}
    answer.update(fields)
    monkeypatch.setattr(repo, "tracking", lambda **kw: answer)


def test_state_warns_when_the_checkout_cannot_pull(monkeypatch, capsys):
    """The orient path is where this belongs: a branch that tracks nothing is
    invisible to `git status` and reads as `current` from the drift counts, so
    the checkout loses the ability to notice it has fallen behind — silently,
    for as long as nobody thinks to look."""
    _tracking_answer(monkeypatch, branch="master")
    assert cli_mod.main(["state"]) == 0
    err = capsys.readouterr().err
    assert "tracks nothing" in err
    assert "--set-upstream-to=origin/master master" in err


def test_state_says_nothing_when_the_checkout_can_pull(monkeypatch, capsys):
    """A warning on every healthy session is a warning nobody reads."""
    _tracking_answer(monkeypatch, branch="master", upstream="origin/master",
                     upstream_missing=False)
    assert cli_mod.main(["state"]) == 0
    assert "tracks nothing" not in capsys.readouterr().err


def test_state_is_silent_outside_a_checkout_rather_than_naming_no_branch(
        monkeypatch, capsys):
    """Nothing to act on and nothing to name — a detached HEAD or a directory
    that is not a checkout at all. `hive state` is not a git tool."""
    _tracking_answer(monkeypatch)                   # missing upstream, no branch
    assert cli_mod.main(["state"]) == 0
    assert "tracks nothing" not in capsys.readouterr().err


def test_state_still_prints_when_the_tracking_probe_breaks(monkeypatch, capsys):
    """Fail-open, like the projection refresh beside it: printing STATE.md is
    this command's job, and a diagnostic must never be able to stop it."""
    from mindhive import repo

    def boom(**kw):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(repo, "tracking", boom)
    assert cli_mod.main(["state"]) == 0
    captured = capsys.readouterr()
    assert "STATE" in captured.out
    assert "git exploded" not in captured.err


def test_flag_creates_judgment_incident_active_at_one(capsys):
    ret = cli_mod.main(["flag", "committed to master without permission"])
    assert ret == 0
    out = capsys.readouterr().out
    evs = [e for e in events.read_all() if e.type == "tool_failed"]
    assert len(evs) == 1
    sig = evs[0].payload["signature"]
    # readable slug prefix + collision-resistant content hash (sig.slug_id)
    assert sig.startswith("judgment:flag:committed-to-master-without-permission-")
    assert sig in out
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


def test_project_survives_corrupt_log_and_verify_reports_it(tmp_path, capsys):
    """A corrupt/torn event-log line must NOT break `hive` commands: reads are
    total, so `hive project` still succeeds (exit 0) over the good events. The
    corruption is surfaced explicitly by `hive verify` (corrupt_rows), not by
    crashing every command with a framed error. (Regression: a single torn
    `command_run` fragment used to exit-1 every CLI command and 500 every
    dashboard section.)"""
    events.append_event("decision", {"text": "a"}, session_id="s", ts="2026-06-20T10:00:00Z")
    seg = next((tmp_path / "test" / "events").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert cli_mod.main(["project"]) == 0     # projection no longer crashes
    assert cli_mod.main(["verify"]) == 0
    out = capsys.readouterr().out
    assert "corrupt_rows" in out              # verify surfaces the torn row
    assert "unparseable json" in out


def test_cli_consolidate_runs_and_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    from mindhive import events
    events.append_event("caveat", {"text": "repeat me please"},
                        ts="2026-07-07T10:01:00Z", project="test")
    events.append_event("caveat", {"text": "repeat me please"},
                        ts="2026-07-07T10:02:00Z", project="test")
    ret = cli_mod.main(["consolidate", "--project", "test", "--cooldown-hours", "0"])
    assert ret in (None, 0)
    out = capsys.readouterr().out
    assert '"consolidated": 1' in out


def test_hook_post_tool_use_failure_captures_from_stdin(monkeypatch):
    payload = ('{"session_id":"s9","tool_name":"Bash",'
               '"tool_input":{"command":"pytest -q"},'
               '"error":"ENOENT: no such file or directory"}')
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    ret = cli_mod.main(["hook", "post_tool_use_failure"])
    assert ret == 0
    evs = events.read_all()
    assert len(evs) == 1
    assert evs[0].type == "tool_failed"
    assert evs[0].payload["tool"] == "Bash"
    assert evs[0].payload["category"] == "not_found"
