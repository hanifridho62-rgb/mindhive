# tests/test_workspace.py
"""Workspace backend: profiles, board, stream rendering, and the agent fleet.

The Manager takes an injectable `spawn` and `append_event`, so every test runs
against a FakeProc that replays canned stream-json lines — no real `claude`
subprocess is ever launched.
"""
import json
import threading
import time

import pytest

from mindhive import workspace


# --- fakes ------------------------------------------------------------------

class FakeProc:
    """Stands in for the claude subprocess: replays lines, then exits."""

    def __init__(self, lines, code=0):
        self.stdout = iter(lines)
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
        self.events = []         # one (etype, payload, kwargs) per ledger write
        self._lines = lines if lines is not None else _stream_lines()
        self._code = code

        def spawn(argv, env, cwd):
            self.calls.append((argv, env, cwd))
            return FakeProc(list(self._lines), code=self._code)

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
    # every swarm agent got the mission as its first prompt
    for argv, _env, _cwd in h.calls:
        assert "MISSION: ship the feature" in argv[argv.index("-p") + 1]


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
    prompt = argv[argv.index("-p") + 1]
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
