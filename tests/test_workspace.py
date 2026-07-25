# tests/test_workspace.py
"""Workspace backend: profiles, board, stream rendering, and the agent fleet.

The Manager takes an injectable `spawn` and `append_event`, so every test runs
against a FakeProc that replays canned stream-json lines — no real `claude`
subprocess is ever launched.
"""
import json
import threading
import time
from pathlib import Path

import pytest

from mindhive import runlog, workspace


# --- fakes ------------------------------------------------------------------

class _FakeStdin:
    """Records what the Manager feeds the run via stdin (the prompt)."""

    def __init__(self):
        self.data = ""
        self.closed = False

    def write(self, s):
        self.data += s

    def close(self):
        self.closed = True


class FakeProc:
    """Stands in for the claude subprocess: replays lines, then exits."""

    def __init__(self, lines, code=0):
        self.stdout = iter(lines)
        self.stdin = _FakeStdin()
        self._code = code
        self._done = False
        self.killed = False

    def poll(self):
        return self._code if self._done else None

    def wait(self):
        self._done = True
        return self._code

    def kill(self):
        self.killed = True
        self._done = True


def _stream_lines(session_id="sess-1", result="all done"):
    return [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": session_id, "model": "claude-x"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "working on it"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": result,
                    "num_turns": 2, "total_cost_usd": 0.0123,
                    "session_id": session_id}),
    ]


class Harness:
    """A Manager wired to fakes, capturing spawn argv and ledger events."""

    def __init__(self, lines=None, code=0):
        self.calls = []          # one (argv, env, cwd) per spawn
        self.procs = []          # the FakeProc of each spawn (stdin = prompt)
        self.events = []         # one (etype, payload, kwargs) per ledger write
        self._lines = lines if lines is not None else _stream_lines()
        self._code = code

        def spawn(argv, env, cwd):
            self.calls.append((argv, env, cwd))
            proc = FakeProc(list(self._lines), code=self._code)
            self.procs.append(proc)
            return proc

        def append(etype, payload, **kw):
            self.events.append((etype, payload, kw))

        self.mgr = workspace.Manager(spawn=spawn, append_event=append)


def _wait_settled(agent, timeout=5.0):
    """Block until the reader thread finishes the run."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if agent.status != "running":
            return
        time.sleep(0.01)
    raise AssertionError(f"agent never settled (status={agent.status})")


def _wait_stdin(proc, timeout=5.0) -> str:
    """Block until the stdin feeder thread has delivered the prompt."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.stdin.closed:
            return proc.stdin.data
        time.sleep(0.01)
    raise AssertionError("prompt was never fed to stdin")


def _agent_obj(h, agent_id):
    return h.mgr.get(agent_id)


# --- render_stream_event ------------------------------------------------------

def test_render_init_line_shows_session_and_model():
    lines = workspace.render_stream_event(
        {"type": "system", "subtype": "init", "session_id": "abcdef0123",
         "model": "claude-x"})
    assert lines == ["· session abcdef01 started · model claude-x"]


def test_render_assistant_text_and_tool_use():
    lines = workspace.render_stream_event(
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "line one\nline two"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}})
    assert lines[0] == "line one"
    assert lines[1] == "line two"
    assert lines[2].startswith("▶ Bash")


def test_render_tool_error_surfaces():
    lines = workspace.render_stream_event(
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "boom"}]}})
    assert len(lines) == 1
    assert "tool error" in lines[0]


def test_render_result_success_and_error():
    ok = workspace.render_stream_event(
        {"type": "result", "subtype": "success", "num_turns": 3,
         "total_cost_usd": 0.05})
    assert ok == ["■ done — 3 turns · $0.0500"]
    bad = workspace.render_stream_event(
        {"type": "result", "subtype": "error_during_execution"})
    assert "ended (error)" in bad[0]


def test_render_unknown_shape_is_silent():
    assert workspace.render_stream_event({"type": "mystery"}) == []


# --- profiles ------------------------------------------------------------------

def test_profiles_always_include_default():
    profs = workspace.load_profiles()
    assert profs[0] == {"name": "default", "config_dir": ""}


def test_profile_save_load_remove_roundtrip():
    res = workspace.save_profile("work", "C:/claude-work")
    assert res["ok"]
    names = [p["name"] for p in workspace.load_profiles()]
    assert names == ["default", "work"]
    res = workspace.remove_profile("work")
    assert res["ok"]
    assert [p["name"] for p in workspace.load_profiles()] == ["default"]


def test_profile_rejects_empty_and_default_names():
    assert not workspace.save_profile("", "x")["ok"]
    assert not workspace.save_profile("default", "x")["ok"]


def test_profile_rejects_shell_metachar_names():
    # profile names reach a Windows `cmd /k` string in profile_login, so
    # save_profile must reject anything with shell metacharacters
    for bad in ('x & calc', 'a|b', 'p"q', 'a>b', 'a<b', 'a&&b'):
        res = workspace.save_profile(bad, "C:/x")
        assert not res["ok"], f"{bad!r} should be rejected"
    assert workspace.save_profile("work-2.prod acct", "C:/x")["ok"]


def test_profile_env_applies_config_dir():
    workspace.save_profile("alt", "D:/alt-config")
    env = workspace._profile_env("alt")
    assert env["CLAUDE_CONFIG_DIR"] == "D:/alt-config"
    # the implicit default profile never overrides the inherited environment
    base = workspace._profile_env("default")
    assert base.get("CLAUDE_CONFIG_DIR") != "D:/alt-config"


# --- board ----------------------------------------------------------------------

def test_board_load_missing_file_is_empty():
    assert workspace.load_board() == {"tasks": []}


def test_board_save_normalizes_tasks():
    res = workspace.save_board({"tasks": [
        {"title": "Build it", "prompt": "do the thing", "status": "running"},
        {"title": "Weird", "status": "not-a-column"},
        {"prompt": "no title — dropped"},
    ]})
    assert res["ok"]
    tasks = workspace.load_board()["tasks"]
    assert len(tasks) == 2
    assert all(t["id"] for t in tasks)
    assert tasks[0]["status"] == "running"
    assert tasks[1]["status"] == "backlog"           # bad column coerced
    assert tasks[1]["prompt"] == "Weird"             # prompt defaults to title


def test_board_dispatch_runs_card_on_idle_agent():
    h = Harness()
    h.mgr.spawn(name="Pane 1")
    workspace.save_board({"tasks": [{"id": "t1", "title": "Card",
                                     "prompt": "run the card"}]})
    res = workspace.board_dispatch(h.mgr, "t1")
    assert res["ok"]
    a = _agent_obj(h, res["agent"]["id"])
    _wait_settled(a)
    # the reader thread releases the card when the run ends
    deadline = time.time() + 5
    while time.time() < deadline:
        task = workspace.load_board()["tasks"][0]
        if task["status"] == "done":
            break
        time.sleep(0.01)
    assert task["status"] == "done"
    assert task["agent_id"] is None


def test_board_dispatch_unknown_task_and_no_agent():
    h = Harness()
    workspace.save_board({"tasks": [{"id": "t1", "title": "Card"}]})
    assert not workspace.board_dispatch(h.mgr, "nope")["ok"]
    assert "no free agent" in workspace.board_dispatch(h.mgr, "t1")["error"]


def test_failed_run_returns_card_to_backlog():
    h = Harness(code=1)
    h.mgr.spawn(name="Pane 1")
    workspace.save_board({"tasks": [{"id": "t1", "title": "Card"}]})
    res = workspace.board_dispatch(h.mgr, "t1")
    assert res["ok"]
    a = _agent_obj(h, res["agent"]["id"])
    _wait_settled(a)
    deadline = time.time() + 5
    while time.time() < deadline:
        task = workspace.load_board()["tasks"][0]
        if task["status"] == "backlog":
            break
        time.sleep(0.01)
    assert task["status"] == "backlog"


# --- Manager fleet ----------------------------------------------------------------

def test_spawn_lists_agent_and_respects_cap():
    h = Harness()
    res = h.mgr.spawn(name="A")
    assert res["ok"]
    assert h.mgr.list()["agents"][0]["name"] == "A"
    for _ in range(workspace.MAX_AGENTS - 1):
        assert h.mgr.spawn()["ok"]
    full = h.mgr.spawn()
    assert not full["ok"]
    assert "cap" in full["error"]


def test_send_runs_and_logs_both_sides_of_the_exchange():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    res = h.mgr.send(aid, "hello agent")
    assert res["ok"]
    a = _agent_obj(h, aid)
    _wait_settled(a)
    assert a.status == "done"
    assert a.session_id == "sess-1"
    joined = "\n".join(a.lines)
    assert "❯ hello agent" in joined
    assert "working on it" in joined
    assert "■ done" in joined
    etypes = [e[0] for e in h.events]
    assert etypes == ["user_prompt", "assistant_response"]
    assert h.events[1][1]["text"] == "all done"


def test_send_feeds_prompt_via_stdin_never_argv():
    """Regression: the researcher's composed programs run to tens of KB, and
    cmd.exe (hosting the Windows .CMD claude shim) rejects command lines past
    ~8k chars — the run died with 'The command line is too long.' The prompt
    must ride stdin; only bounded flags stay in argv."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    big = "x" * 20000
    assert h.mgr.send(aid, big)["ok"]
    argv = h.calls[0][0]
    assert argv[-1] == "-p"
    assert all(big not in str(part) for part in argv)
    assert _wait_stdin(h.procs[0]) == big
    _wait_settled(_agent_obj(h, aid))


def test_send_add_dirs_grants_out_of_cwd_access():
    """Self-Reliant's researcher edits train.py OUTSIDE its working directory
    (a \\\\wsl$ path) — without --add-dir the CLI's workspace boundary silently
    blocks the edit and the agent ends its turn having changed nothing
    (observed in the first real acceptance run)."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "edit it", add_dirs=[r"\\wsl.localhost\Ubuntu\home\u\autoresearch"])
    argv = h.calls[0][0]
    assert argv[argv.index("--add-dir") + 1] == \
        r"\\wsl.localhost\Ubuntu\home\u\autoresearch"
    # and absent when not requested
    _wait_settled(_agent_obj(h, aid))
    h.mgr.send(aid, "again")
    assert "--add-dir" not in h.calls[1][0]


def test_send_tool_restriction_renders_allow_and_deny_lists():
    """allowed_tools/disallowed_tools become --allowedTools/--disallowedTools so
    Self-Reliant's learn mode can spawn a read-only agent that cannot reach
    git/gh — the enforced no-GitHub boundary. Absent by default (back-compat)."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "analyze", allowed_tools=["Read", "Grep", "Glob"],
               disallowed_tools=["Bash(git:*)", "Bash(gh:*)"])
    argv = h.calls[0][0]
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert argv[argv.index("--disallowedTools") + 1] == "Bash(git:*),Bash(gh:*)"
    # CRITICAL: an allowlist DROPS bypassPermissions — otherwise the CLI would
    # auto-approve every tool and the allowlist (the boundary) would be inert
    assert "bypassPermissions" not in argv
    # absent when not requested
    _wait_settled(_agent_obj(h, aid))
    h.mgr.send(aid, "again")
    assert "--allowedTools" not in h.calls[1][0]
    assert "--disallowedTools" not in h.calls[1][0]


def test_send_without_allowlist_keeps_bypass_permissions():
    """No allowlist (improve mode / ordinary workspace agents) keeps the
    bypassPermissions posture so out-of-cwd edits are auto-approved."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "edit train.py")
    argv = h.calls[0][0]
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_reader_captures_last_result_text():
    """The reader exposes the run's final result text as agent.last_result so a
    caller (Self-Reliant learn mode) can persist it as a learning."""
    h = Harness(lines=_stream_lines(result="lower lr to 0.03 next"))
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    assert a.last_result == "lower lr to 0.03 next"


def test_fresh_agent_has_empty_last_result():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    assert _agent_obj(h, aid).last_result == ""


def test_send_rejects_empty_unknown_and_busy():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    assert not h.mgr.send(aid, "   ")["ok"]
    assert not h.mgr.send("nope", "hi")["ok"]
    _agent_obj(h, aid).status = "running"        # simulate an in-flight run
    assert "busy" in h.mgr.send(aid, "hi")["error"]


def test_second_send_resumes_the_same_session():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "first")
    _wait_settled(_agent_obj(h, aid))
    h.mgr.send(aid, "second")
    _wait_settled(_agent_obj(h, aid))
    argv_first, argv_second = h.calls[0][0], h.calls[1][0]
    assert "--resume" not in argv_first
    assert argv_second[argv_second.index("--resume") + 1] == "sess-1"


def test_swarm_role_gets_lens_and_shared_memory_prompt():
    h = Harness()
    aid = h.mgr.spawn(name="C", role="coordinator", swarm=True)["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    argv = h.calls[0][0]
    lens = argv[argv.index("--append-system-prompt") + 1]
    assert "COORDINATOR" in lens
    assert "hive" in lens or "Mind Hive" in lens


def test_plain_agent_has_no_system_prompt_lens():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    assert "--append-system-prompt" not in h.calls[0][0]


def test_failed_run_logs_tool_failed_with_signature():
    h = Harness(code=2)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "boom")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    assert a.status == "error"
    etypes = [e[0] for e in h.events]
    assert "tool_failed" in etypes
    payload = h.events[etypes.index("tool_failed")][1]
    assert payload["signature"] == "workspace:agent"


def test_output_cursor_survives_scrollback_trim():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    out = h.mgr.output(aid, since=0)
    assert out["ok"] and out["lines"]
    # a cursor far past the buffer end clamps instead of erroring
    out = h.mgr.output(aid, since=10_000)
    assert out["ok"] and out["lines"] == []
    assert not h.mgr.output("nope")["ok"]


def test_stop_and_remove():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    assert h.mgr.stop(aid)["ok"]                 # no live proc: still ok
    assert not h.mgr.stop("nope")["ok"]
    assert h.mgr.remove(aid)["ok"]
    assert h.mgr.list()["agents"] == []


def test_clear_idle_removes_only_settled_non_swarm_agents():
    h = Harness()
    running = h.mgr.spawn(name="Busy")["agent"]["id"]
    done = h.mgr.spawn(name="Done")["agent"]["id"]
    idle = h.mgr.spawn(name="Fresh")["agent"]["id"]
    swarm = h.mgr.spawn(name="Crew", role="builder", swarm=True)["agent"]["id"]
    h.mgr.send(done, "work")
    _wait_settled(_agent_obj(h, done))
    _agent_obj(h, running).status = "running"    # simulate an in-flight run
    res = h.mgr.clear_idle()
    assert res["ok"] and res["removed"] == 2     # done + fresh idle
    left = {a["id"] for a in h.mgr.list()["agents"]}
    assert left == {running, swarm}              # running and swarm survive


def test_api_post_clear_idle_dispatches():
    res = workspace.api_post("clear_idle")
    assert res["ok"] and res["removed"] == 0     # empty singleton fleet


def test_reader_records_skills_used_and_run_stats():
    lines = [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "s1", "model": "m"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "ce-plan"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "Skill", "input": {}},
            {"type": "tool_use", "name": "Skill", "input": "not-a-dict"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "ce-plan"}},
            {"type": "tool_use", "name": "Skill", "input": {"skill": "ce-work"}}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "ok",
                    "num_turns": 4, "total_cost_usd": 0.5, "session_id": "s1"}),
    ]
    h = Harness(lines=lines)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    # deduped, in first-use order; malformed and non-Skill blocks ignored
    assert a.skills_used == ["ce-plan", "ce-work"]
    assert a.last_turns == 4
    assert a.last_cost == 0.5
    snap = a.snapshot()
    assert snap["skills_used"] == ["ce-plan", "ce-work"]
    assert snap["last_turns"] == 4 and snap["last_cost"] == 0.5


def test_fresh_agent_exposes_empty_run_stats():
    h = Harness()
    snap = h.mgr.spawn(name="A")["agent"]
    assert snap["skills_used"] == []
    assert snap["last_turns"] is None and snap["last_cost"] is None


def test_agent_scrollback_is_capped():
    a = workspace.Agent("A")
    for i in range(workspace.MAX_LINES + 50):
        a.emit(f"line {i}")
    assert len(a.lines) == workspace.MAX_LINES
    assert a.lines[0] == "line 50"


# --- MindSwarm -----------------------------------------------------------------

def test_mission_spawns_coordinator_and_crew():
    h = Harness()
    res = h.mgr.start_mission("ship the feature", builders=2, scouts=1,
                              reviewers=1)
    assert res["ok"]
    roles = sorted(a["role"] for a in res["agents"])
    assert roles == ["builder", "builder", "coordinator", "reviewer", "scout"]
    assert h.mgr.mission["mission"] == "ship the feature"
    assert h.mgr.mission["coordinator"] == res["agents"][0]["id"]
    # every swarm agent got the mission as its first prompt (fed via stdin)
    for proc in h.procs:
        assert "MISSION: ship the feature" in _wait_stdin(proc)


def test_mission_rejects_empty_and_overflow():
    h = Harness()
    assert not h.mgr.start_mission("  ")["ok"]
    res = h.mgr.start_mission("x", builders=workspace.MAX_AGENTS)
    assert not res["ok"]
    assert "slots" in res["error"]


def test_stop_mission_clears_state():
    h = Harness()
    h.mgr.start_mission("m", builders=1, scouts=0, reviewers=0)
    for a in list(h.mgr.agents.values()):
        _wait_settled(a)
    res = h.mgr.stop_mission()
    assert res["ok"]
    assert h.mgr.mission is None


def test_command_targets_one_agent_or_whole_swarm():
    h = Harness()
    h.mgr.start_mission("m", builders=1, scouts=0, reviewers=0)
    for a in list(h.mgr.agents.values()):
        _wait_settled(a)
    pane = h.mgr.spawn(name="Solo")["agent"]["id"]   # non-swarm pane
    n_calls = len(h.calls)
    res = h.mgr.command("all", "status report")
    assert res["ok"] and res["sent"] == 2            # coordinator + builder only
    for a in list(h.mgr.agents.values()):
        _wait_settled(a)
    res = h.mgr.command(pane, "just you")
    assert res["ok"]
    assert len(h.calls) == n_calls + 3
    assert not h.mgr.command("all", " ")["ok"]


# --- model picker -----------------------------------------------------------------

def test_spawn_with_model_passes_model_flag():
    h = Harness()
    res = h.mgr.spawn(name="A", model="claude-opus-4-8")
    assert res["ok"] and res["agent"]["model"] == "claude-opus-4-8"
    aid = res["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    argv = h.calls[0][0]
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


def test_default_model_omits_model_flag():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    assert "--model" not in h.calls[0][0]


def test_set_model_updates_idle_agent_and_rejects_busy(monkeypatch):
    h = Harness()
    monkeypatch.setattr(workspace, "MANAGER", h.mgr)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    res = workspace.api_post("set_model",
                             body={"id": aid, "model": "claude-sonnet-5"})
    assert res["ok"] and res["agent"]["model"] == "claude-sonnet-5"
    _agent_obj(h, aid).status = "running"
    res = workspace.api_post("set_model", body={"id": aid, "model": ""})
    assert not res["ok"] and "busy" in res["error"]
    assert not workspace.api_post("set_model", body={"id": "nope"})["ok"]


def test_profiles_api_ships_model_choices():
    res = workspace.api_get("profiles")
    assert res["ok"]
    ids = [m["id"] for m in res["models"]]
    assert "" in ids and "claude-fable-5" in ids and "claude-opus-4-8" in ids


# --- auth / login / trust -----------------------------------------------------------

def _auth_profile(tmp_path, expires_in_s):
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"expiresAt": (time.time() + expires_in_s) * 1000,
                           "subscriptionType": "max"}}), encoding="utf-8")
    workspace.save_profile("authp", str(cfg))
    return cfg


def test_auth_status_ok_expired_and_none(tmp_path):
    cfg = _auth_profile(tmp_path, expires_in_s=3600)
    st = workspace.profile_auth_status("authp")
    assert st["state"] == "ok" and "max" in st["detail"]
    (cfg / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"expiresAt": 1000}}), encoding="utf-8")
    assert workspace.profile_auth_status("authp")["state"] == "expired"
    (cfg / ".credentials.json").unlink()
    assert workspace.profile_auth_status("authp")["state"] == "none"
    # state file with an account but no readable token store -> unknown
    (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"e": "x"}}),
                                      encoding="utf-8")
    assert workspace.profile_auth_status("authp")["state"] == "unknown"


def test_auth_api_reports_every_profile(tmp_path):
    _auth_profile(tmp_path, expires_in_s=3600)
    res = workspace.api_get("auth")
    assert res["ok"]
    by_name = {a["profile"]: a for a in res["auth"]}
    assert "default" in by_name
    assert by_name["authp"]["state"] == "ok"


def test_profile_trust_writes_both_slash_spellings(tmp_path):
    cfg = _auth_profile(tmp_path, expires_in_s=3600)
    res = workspace.profile_trust("authp", r"C:\proj\demo")
    assert res["ok"]
    st = json.loads((cfg / ".claude.json").read_text(encoding="utf-8"))
    assert st["projects"]["C:\\proj\\demo"]["hasTrustDialogAccepted"] is True
    assert st["projects"]["C:/proj/demo"]["hasTrustDialogAccepted"] is True
    # existing entries keep their other keys
    res = workspace.profile_trust("authp", r"C:\proj\demo")
    assert res["ok"]


def test_profile_trust_concurrent_writes_do_not_clobber(tmp_path):
    # two folders trusted at once must both survive — the read-modify-write is
    # serialized under _TRUST_LOCK so neither overwrites the other's key
    _auth_profile(tmp_path, expires_in_s=3600)
    cfg = tmp_path / "cfg"
    folders = [f"C:/proj/demo{i}" for i in range(12)]
    threads = [threading.Thread(target=workspace.profile_trust,
                                args=("authp", f)) for f in folders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = json.loads((cfg / ".claude.json").read_text(encoding="utf-8"))
    for f in folders:
        assert st["projects"][f]["hasTrustDialogAccepted"] is True


def test_profile_login_rejects_unknown_and_unsafe_profiles(monkeypatch):
    # never launch a console for a name that isn't a saved profile — the name
    # is interpolated into the cmd string, so an unchecked value is injection
    launched = []
    monkeypatch.setattr(workspace.subprocess, "Popen",
                        lambda *a, **k: launched.append(a) or object())
    assert not workspace.profile_login("no-such-profile")["ok"]
    assert not workspace.profile_login('x & calc')["ok"]
    assert launched == []                    # nothing was spawned


def test_profile_login_passes_config_dir_via_env_not_shell(tmp_path, monkeypatch):
    workspace.save_profile("envp", r"C:\cfg\envp")
    captured = {}

    def fake_popen(argv, *a, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env") or {}
        return object()
    monkeypatch.setattr(workspace.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(workspace.subprocess, "CREATE_NEW_CONSOLE", 16,
                        raising=False)
    res = workspace.profile_login("envp")
    assert res["ok"]
    # config dir rides in the child env, never as a shell `set` command
    assert captured["env"].get("CLAUDE_CONFIG_DIR") == r"C:\cfg\envp"
    assert "set " not in " ".join(captured["argv"])


def test_profile_login_launches_browser_oauth_not_repl(tmp_path, monkeypatch):
    # zero-typing sign-in: the console runs `claude auth login` (the CLI opens
    # the browser OAuth itself) — never the bare REPL that needs "/login" typed
    workspace.save_profile("oauthp", r"C:\cfg\oauthp")
    captured = {}
    monkeypatch.setattr(workspace.subprocess, "Popen",
                        lambda argv, *a, **kw: captured.setdefault("argv", argv)
                        or object())
    monkeypatch.setattr(workspace.subprocess, "CREATE_NEW_CONSOLE", 16,
                        raising=False)
    res = workspace.profile_login("oauthp")
    assert res["ok"] and "nothing to type" in res["detail"]
    cmd = " ".join(captured["argv"])
    assert "auth login" in cmd
    assert "/login" not in cmd


def test_cli_auth_status_parses_logged_in(monkeypatch):
    payload = json.dumps({"loggedIn": True, "email": "u@example.com",
                          "subscriptionType": "pro"})

    def fake_run(argv, **kw):
        assert argv[1:] == ["auth", "status", "--json"]
        # hooks must never fire off a status probe
        assert kw["env"].get("MINDHIVE_HEADLESS") == "1"

        class P:
            stdout = payload
        return P()

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    st = workspace.cli_auth_status("default")
    assert st["state"] == "ok" and st["source"] == "cli"
    assert st["email"] == "u@example.com" and st["subscription"] == "pro"
    assert "u@example.com" in st["detail"]


def test_cli_auth_status_not_logged_in_and_error(monkeypatch):
    class P:
        stdout = json.dumps({"loggedIn": False})
    monkeypatch.setattr(workspace.subprocess, "run", lambda *a, **k: P())
    st = workspace.cli_auth_status()
    assert st["state"] == "none" and "Connect" in st["detail"]

    def boom(*a, **k):
        raise OSError("cli missing")
    monkeypatch.setattr(workspace.subprocess, "run", boom)
    st = workspace.cli_auth_status()
    assert st["state"] == "error" and "cli missing" in st["detail"]


def test_verify_auth_always_answers_with_identity(tmp_path, monkeypatch):
    # the CLI is the single source of truth even when the credential file
    # looks fresh — a file fast path would drop the email and the identity
    # line ("connected as <email>") would silently never render
    workspace._AUTH_CACHE.clear()
    _auth_profile(tmp_path, expires_in_s=3600)     # file heuristic says ok
    monkeypatch.setattr(workspace, "cli_auth_status",
                        lambda profile, timeout_s=None:
                        {"profile": profile, "state": "ok", "source": "cli",
                         "email": "u@example.com", "subscription": "pro",
                         "detail": "connected as u@example.com (pro)"})
    st = workspace.verify_auth("authp")
    assert st["state"] == "ok" and st["source"] == "cli"
    assert st["email"] == "u@example.com"


def test_verify_auth_settles_with_cli_and_caches(tmp_path, monkeypatch):
    workspace._AUTH_CACHE.clear()
    workspace.save_profile("clip", str(tmp_path / "empty"))   # no credentials
    calls = {"n": 0}

    def fake_cli(profile, timeout_s=workspace.AUTH_VERIFY_TIMEOUT_S):
        calls["n"] += 1
        return {"profile": profile, "state": "ok", "source": "cli",
                "email": "u@example.com", "subscription": "pro",
                "detail": "connected as u@example.com (pro)"}

    monkeypatch.setattr(workspace, "cli_auth_status", fake_cli)
    st = workspace.verify_auth("clip")
    assert st["state"] == "ok" and st["email"] == "u@example.com"
    assert workspace.verify_auth("clip")["state"] == "ok"
    assert calls["n"] == 1                          # second call was cached
    workspace.verify_auth("clip", force=True)
    assert calls["n"] == 2                          # force skips the cache


def test_verify_auth_cli_error_falls_back_to_file(tmp_path, monkeypatch):
    workspace._AUTH_CACHE.clear()
    cfg = _auth_profile(tmp_path, expires_in_s=3600)
    (cfg / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"expiresAt": 1000}}), encoding="utf-8")   # expired
    monkeypatch.setattr(workspace, "cli_auth_status",
                        lambda profile, timeout_s=None:
                        {"profile": profile, "state": "error", "source": "cli",
                         "detail": "cli missing"})
    st = workspace.verify_auth("authp", force=True)
    assert st["state"] == "expired" and st["source"] == "file"
    assert "cli missing" in st["cli_error"]


def test_api_post_auth_verify_roundtrip(monkeypatch):
    monkeypatch.setattr(workspace, "verify_auth",
                        lambda name="default", force=False:
                        {"profile": name, "state": "ok",
                         "detail": "connected", "forced": force})
    res = workspace.api_post("auth_verify",
                             body={"name": "default", "force": True})
    assert res["ok"] and res["state"] == "ok" and res["forced"] is True


# --- recipe runs ---------------------------------------------------------------------

def _stub_history(monkeypatch, status=None, lessons=None, hits=None):
    from mindhive import plugins as plugins_mod, incidents as inc, recall as rec
    monkeypatch.setattr(plugins_mod, "call_status",
                        lambda project=None: status or {})
    monkeypatch.setattr(inc, "summaries", lambda project=None: lessons or [])
    monkeypatch.setattr(rec, "recall",
                        lambda q, project=None, limit=10, **kw: hits or [])


def test_compose_recipe_prompt_orders_skills_and_injects_history(monkeypatch):
    _stub_history(
        monkeypatch,
        status={"ce-plan": "ok", "ce-debug": "fail"},
        lessons=[{"signature": "skill:ce-debug",
                  "lesson": "always reproduce before fixing"}],
        hits=[{"snippet": "decision: panes stage skills until Run"}])
    prompt = workspace.compose_recipe_prompt(
        [{"name": "ce-plan", "plugin": "compound-engineering"},
         {"name": "ce-debug", "plugin": None}],
        "fix the login flow")
    assert prompt.index("1. Use the ce-plan skill (from the "
                        "compound-engineering plugin).") \
        < prompt.index("2. Use the ce-debug skill.")
    assert "exactly once, in the order listed" in prompt
    assert "COMMAND: fix the login flow" in prompt
    assert "ce-debug: last outcome fail" in prompt
    assert "always reproduce before fixing" in prompt
    assert "panes stage skills until Run" in prompt


def test_compose_recipe_survives_broken_history_sources(monkeypatch):
    from mindhive import plugins as plugins_mod, incidents as inc, recall as rec

    def boom(*a, **kw):
        raise RuntimeError("subsystem down")
    monkeypatch.setattr(plugins_mod, "call_status", boom)
    monkeypatch.setattr(inc, "summaries", boom)
    monkeypatch.setattr(rec, "recall", boom)
    prompt = workspace.compose_recipe_prompt([{"name": "ce-plan"}], "do it")
    assert "Use the ce-plan skill" in prompt
    assert "COMMAND: do it" in prompt


def test_run_recipe_sends_enriched_prompt_with_primer(monkeypatch):
    _stub_history(monkeypatch)
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    res = workspace.run_recipe(h.mgr, aid,
                               [{"name": "ce-plan"}], "build the thing")
    assert res["ok"]
    _wait_settled(_agent_obj(h, aid))
    argv = h.calls[0][0]
    prompt = _wait_stdin(h.procs[0])
    assert "Use the ce-plan skill" in prompt
    assert "COMMAND: build the thing" in prompt
    sysp = argv[argv.index("--append-system-prompt") + 1]
    assert "MindHive integration" in sysp
    assert "rules summaries" in sysp


def test_run_recipe_rejects_empty_and_unknown(monkeypatch):
    _stub_history(monkeypatch)
    h = Harness()
    assert not workspace.run_recipe(h.mgr, "nope", [], "")["ok"]
    res = workspace.run_recipe(h.mgr, "nope", [{"name": "x"}], "go")
    assert not res["ok"] and "unknown agent" in res["error"]


def test_run_recipe_api_action(monkeypatch):
    _stub_history(monkeypatch)
    h = Harness()
    monkeypatch.setattr(workspace, "MANAGER", h.mgr)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    res = workspace.api_post("run_recipe",
                             body={"id": aid, "skills": [{"name": "ce-plan"}],
                                   "command": "go"})
    assert res["ok"]
    _wait_settled(_agent_obj(h, aid))


# --- dashboard API dispatch ------------------------------------------------------

def test_api_get_dispatch():
    assert workspace.api_get("profiles")["ok"]
    assert workspace.api_get("board") == {"tasks": []}
    assert not workspace.api_get("nope")["ok"]


def test_api_post_unknown_action():
    assert not workspace.api_post("nope")["ok"]


def test_api_post_profile_roundtrip():
    res = workspace.api_post("profile_add",
                             body={"name": "p1", "config_dir": "C:/x"})
    assert res["ok"]
    assert workspace.api_post("profile_remove", body={"name": "p1"})["ok"]


# --- MN-0: output durability ----------------------------------------------------
# Every test below pins one of the four defects read in the code on 2026-07-25
# (docs/superpowers/specs/2026-07-25-mn0-parallel-output-durability-design.md).


class _GatedProc(FakeProc):
    """A FakeProc that streams its lines and then blocks, so a test can act on
    a run that is genuinely mid-flight (the user pressing Stop, a timeout kill)
    instead of racing an instantly-finished fake."""

    def __init__(self, lines, code=0):
        super().__init__(lines, code=code)
        self.streamed = threading.Event()
        self.release = threading.Event()
        self.stdout = self._gen(list(lines))

    def _gen(self, lines):
        for ln in lines:
            yield ln
        self.streamed.set()
        self.release.wait(5)


def _gated(h, lines, code=0):
    """Re-point a Harness at gated procs; returns the list they land in."""
    procs = []

    def spawn(argv, env, cwd):
        p = _GatedProc(list(lines), code=code)
        h.calls.append((argv, env, cwd))
        h.procs.append(p)
        procs.append(p)
        return p

    h.mgr._spawn = spawn
    return procs


def _artifact_of(payload) -> str:
    """The artifact body a ledger event points at."""
    path = payload["artifact"]
    assert path, "event carries no artifact pointer"
    return Path(path).read_text(encoding="utf-8")


def _events_of(h, etype) -> list:
    return [p for (t, p, _kw) in h.events if t == etype]


def _wait_event(h, etype, timeout=5.0) -> list:
    """Block until the reader thread has logged `etype`.

    `_wait_settled` is not enough on the stop path: stop() flips the status to
    "stopped" immediately, so it returns while the reader is still finalising —
    the ledger write is the event to wait for, not the status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = _events_of(h, etype)
        if found:
            return found
        time.sleep(0.01)
    raise AssertionError(f"{etype} was never logged")


# --- D3: the failure path used to discard 100% of the work's record -----------

def test_failed_run_persists_the_output_it_produced():
    """D3 regression (workspace.py:676-682 before MN-0): on a non-zero exit with
    no `result` event, the accumulated output was never written anywhere — only
    a bare tool_failed naming the exit code. A run that did real work and then
    died lost the entire record of that work. This is the reported "stops
    midway" symptom."""
    lines = [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "s1", "model": "m"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "migrated the parser"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "41 of 44 tests pass"}]}}),
        # NB: no result event — the run dies before the CLI emits one
    ]
    h = Harness(lines=lines, code=1)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "do the migration")
    a = _agent_obj(h, aid)
    _wait_settled(a)

    assert a.status == "error"
    # the produced output is durably recorded BEFORE the failure is logged:
    # no work is discarded on the way to reporting the error
    etypes = [e[0] for e in h.events]
    assert etypes == ["user_prompt", "assistant_response", "tool_failed"]
    resp = h.events[1][1]
    assert resp["partial"] is True
    assert "migrated the parser" in resp["text"]
    body = _artifact_of(resp)
    assert "migrated the parser" in body
    assert "41 of 44 tests pass" in body
    # and the failure record itself is unchanged (Improvements still sees it)
    assert h.events[2][1]["signature"] == "workspace:agent"


def test_stopped_run_persists_what_it_had_produced():
    """The user-stop path recorded nothing at all before MN-0. Work the operator
    interrupted is still work they paid for."""
    lines = [
        json.dumps({"type": "system", "subtype": "init",
                    "session_id": "s1", "model": "m"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "found the root cause"}]}}),
    ]
    h = Harness()
    procs = _gated(h, lines)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "investigate")
    assert procs[0].streamed.wait(5)
    h.mgr.stop(aid)                       # user presses Stop mid-run
    procs[0].release.set()
    a = _agent_obj(h, aid)
    resps = _wait_event(h, "assistant_response")

    assert a.status == "stopped"
    assert len(resps) == 1
    assert resps[0]["partial"] is True
    assert "found the root cause" in _artifact_of(resps[0])


# --- D2: truncation and volatility ---------------------------------------------

def test_clean_run_keeps_its_event_shape_and_gains_an_artifact_pointer():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    resp = _events_of(h, "assistant_response")[0]
    assert resp["text"] == "all done"          # unchanged shape for consumers
    assert resp["partial"] is False
    assert "working on it" in _artifact_of(resp)


def test_long_run_is_complete_on_disk_while_ring_and_excerpt_stay_bounded():
    """D2: the durable record was capped at 2000 chars over a volatile 4000-line
    in-memory ring, so a long run lost its BEGINNING from the live view and lost
    everything but the excerpt on a dashboard restart."""
    n = workspace.MAX_LINES + 200
    lines = [json.dumps({"type": "system", "subtype": "init",
                         "session_id": "s1", "model": "m"})]
    lines += [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": f"chunk {i}"}]}}) for i in range(n)]
    lines.append(json.dumps({"type": "result", "subtype": "success",
                             "result": "R" * 5000, "num_turns": 1,
                             "total_cost_usd": 0.1, "session_id": "s1"}))
    h = Harness(lines=lines)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "long job")
    a = _agent_obj(h, aid)
    _wait_settled(a)

    # the live ring did lose the beginning — that part is by design
    assert len(a.lines) == workspace.MAX_LINES
    assert "chunk 0" not in a.lines
    # the excerpt in the event stays bounded, exactly as before MN-0
    resp = _events_of(h, "assistant_response")[0]
    assert len(resp["text"]) == 2000
    # ...but nothing is lost: the artifact holds the whole run, head included
    body = _artifact_of(resp)
    assert "chunk 0" in body
    assert f"chunk {n - 1}" in body


def test_run_artifact_outlives_the_manager():
    """Restart durability: the fleet object is discarded (a dashboard restart)
    and the run's output is still retrievable from disk."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    key = runlog.workspace_key(a.id, 1)
    path = a.artifact
    del h
    assert runlog.read(key) is not None
    assert "working on it" in Path(path).read_text(encoding="utf-8")


def test_each_send_gets_its_own_artifact():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "first")
    _wait_settled(_agent_obj(h, aid))
    h.mgr.send(aid, "second")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    paths_seen = [p["artifact"] for p in _events_of(h, "assistant_response")]
    assert len(set(paths_seen)) == 2           # one run, one artifact
    assert "❯ second" in Path(paths_seen[1]).read_text(encoding="utf-8")
    assert "❯ first" not in Path(paths_seen[1]).read_text(encoding="utf-8")


def test_snapshot_carries_the_artifact_pointer():
    h = Harness()
    snap = h.mgr.spawn(name="A")["agent"]
    assert snap["artifact"] is None             # nothing run yet
    aid = snap["id"]
    h.mgr.send(aid, "go")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    assert a.snapshot()["artifact"] == a.artifact
    assert a.artifact


# --- D2 (cursor honesty): a bounded view must announce its bound ---------------

def test_output_reports_lines_a_stale_cursor_can_never_see():
    """`since = min(since, len(a.lines))` kept polling alive but silently
    resumed at the wrong offset once the ring had trimmed past the cursor. The
    gap is now reported instead of hidden."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    a = _agent_obj(h, aid)
    first = h.mgr.output(aid, since=0)
    assert first["dropped"] == 0
    overflow = 50
    for i in range(workspace.MAX_LINES + overflow):
        a.emit(f"line {i}")

    out = h.mgr.output(aid, since=first["next"])
    # spawn banner + overflow lines fell out of the ring; the poller's cursor
    # had only consumed the banner, so exactly `overflow` lines are unreachable
    assert out["dropped"] == overflow
    assert len(out["lines"]) == workspace.MAX_LINES
    assert out["next"] == workspace.MAX_LINES + overflow + 1
    # a cursor that is up to date reports no loss — the canary proving the
    # count above tracks real trimming rather than always being nonzero
    fresh = h.mgr.output(aid, since=out["next"])
    assert fresh["dropped"] == 0 and fresh["lines"] == []


# --- D4: a timed-out run orphaned the agent and killed the next turn ----------

def test_send_settles_an_agent_whose_run_died_without_settling():
    """D4 (research.py:742-754 documents the cascade): a turn crossing its
    wall-clock budget left the agent's status stuck at "running"; the NEXT
    send() rejected with "is busy", which the loop treated as terminal — "and
    the whole run dies." The guard now lives in Manager, so the swarm and
    MindParallel paths inherit it, not just ResearchController."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "first")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    a.status = "running"                  # the orphan: process gone, status stuck

    res = h.mgr.send(aid, "second")
    assert res["ok"], "an orphaned agent must be settled, not declared busy"
    _wait_settled(a)
    assert a.status == "done"
    assert len(h.calls) == 2              # the second run really was dispatched


def test_genuinely_busy_agent_returns_a_structured_retryable_error():
    """SOP 2.1: failures carry isError/errorCategory/isRetryable so a caller can
    reroute instead of treating a bare string as terminal."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    a = _agent_obj(h, aid)
    a.proc, a.status = None, "running"    # live run, nothing to settle
    res = h.mgr.send(aid, "hi")
    assert not res["ok"]
    assert "busy" in res["error"]         # unchanged operator-facing message
    assert res["isError"] is True
    assert res["errorCategory"] == "agent_busy"
    assert res["isRetryable"] is True


def test_settle_releases_an_orphan_and_leaves_a_live_run_alone():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    a.status = "running"
    assert h.mgr.settle(aid)["settled"] is True
    assert a.status != "running"
    # a genuinely live run is never settled out from under itself
    a.proc, a.status = None, "running"
    assert h.mgr.settle(aid)["settled"] is False
    assert a.status == "running"
    assert not h.mgr.settle("nope")["ok"]


# --- payload-extension canary (spec §3.4, risk 3) ------------------------------

def test_historical_assistant_response_without_an_artifact_still_folds():
    """Events written before MN-0 carry no `artifact` / `partial` keys. The
    ledger is never migrated, so every reader must keep folding them — and be
    seen to actually PROCESS the row rather than skip it."""
    from mindhive import events as ev_mod, oversee
    ev_mod.append_event("assistant_response",
                        {"text": "an answer from before MN-0", "agent": "A",
                         "role": "agent"},
                        session_id="ws-legacy", tags=["workspace", "parallel"])
    evs = ev_mod.read_all()
    rows = oversee.interactive_sessions(evs=evs)
    row = next(s for s in rows if s["run_id"] == "ws-legacy")
    assert "an answer from before MN-0" in row["doing"]   # folded, not skipped
