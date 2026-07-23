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


class _HoldProc:
    """A run that stays open until stop()/kill() releases it, so a test can drive
    the user-stop terminal path deterministically — the instant FakeProc finishes
    before stop() can flip the agent to 'stopped'."""

    def __init__(self):
        self.stdin = _FakeStdin()
        self._released = threading.Event()
        self._code = 0
        self._done = False
        self.killed = False

    @property
    def stdout(self):
        def gen():
            self._released.wait(timeout=5)
            return
            yield            # unreachable — makes this a generator
        return gen()

    def poll(self):
        return self._code if self._done else None

    def wait(self):
        self._released.wait(timeout=5)
        self._done = True
        return self._code

    def kill(self):
        self.killed = True
        self._code = -9
        self._done = True
        self._released.set()


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


def test_result_longer_than_2000_chars_is_not_truncated():
    """Regression (measured 2026-07-22): the persisted result was capped at
    [:2000], silently cutting answers mid-word. 13 of 22 real workspace
    responses in the live ledger sat at exactly 2000 chars — a 59% silent
    data-loss rate that no test covered."""
    body = "word " * 1000                      # 5000 chars, well past the old cap
    h = Harness(lines=_stream_lines(result=body))
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    stored = h.events[1][1]["text"]
    assert stored == body.strip()
    assert len(stored) > 2000
    assert "truncated" not in stored


def test_result_past_the_cap_is_truncated_and_says_so():
    """Truncation must never be silent again: past RESULT_TEXT_CAP the payload
    is bounded AND carries the same visible marker harvest._cap_words uses."""
    body = "y" * (workspace.RESULT_TEXT_CAP + 500)
    h = Harness(lines=_stream_lines(result=body))
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    stored = h.events[1][1]["text"]
    assert stored.endswith("…[truncated]")
    assert len(stored) <= workspace.RESULT_TEXT_CAP + len(" …[truncated]")
    assert stored.startswith("y" * 100)


def test_empty_result_still_logs_the_no_text_placeholder():
    """The '(no text)' fallback is unrelated to the cap and must survive it."""
    h = Harness(lines=_stream_lines(result=""))
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    assert h.events[1][1]["text"] == "(no text)"


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


def test_clear_idle_culls_rehydrated_swarm_record_but_not_live_swarm():
    # a LIVE swarm agent belongs to the mission lifecycle and must survive, but a
    # REHYDRATED swarm record (rebuilt from the ledger, no mission to own it) has
    # to be cullable or it holds a cap slot forever.
    mgr = _bare_manager()
    live = workspace.Agent("Crew", role="builder", swarm=True)   # spawned live
    mgr.agents[live.id] = live
    rec = workspace.Agent.rehydrated("sw01", "Builder 1", "builder",
                                     swarm=True, status="done")   # from ledger
    mgr.agents[rec.id] = rec
    res = mgr.clear_idle()
    assert res["removed"] == 1
    assert {a.id for a in mgr.agents.values()} == {live.id}


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


# --- one run, one ledger identity (U1) ----------------------------------------
# Before this, Manager._log resolved `a.session_id or f"ws-{a.id}"`. a.session_id
# is None at send() time and only set later, when the stream's `system` init
# event arrives — so a run's prompt landed under ws-<id> and its outcome under
# the claude uuid. Measured in the live ledger: 14 ws-<id> sessions holding ONLY
# a user_prompt, and all 22 assistant_response + 6 tool_failed events under
# separate uuids.

def _sids(h):
    """The ledger session id of every event this harness captured."""
    return [kw.get("session_id") for _, _, kw in h.events]


def test_prompt_and_outcome_share_one_ledger_session_id():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    etypes = [e[0] for e in h.events]
    assert etypes == ["user_prompt", "assistant_response"]
    assert len(set(_sids(h))) == 1, "one run must write under one identity"
    assert _sids(h)[0] == f"ws-{a.id}"


def test_failed_run_logs_its_failure_under_the_same_id_as_its_prompt():
    h = Harness(code=1)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    # WS1 adds a machine-readable agent_disconnect after the coarse tool_failed;
    # both land under the run's one ledger identity (U1).
    assert [e[0] for e in h.events] == \
        ["user_prompt", "tool_failed", "agent_disconnect"]
    assert len(set(_sids(h))) == 1


def test_ledger_id_is_unaffected_by_the_claude_session_arriving_mid_run():
    """The regression itself: a.session_id is assigned while the stream is being
    drained. That must no longer move the ledger identity underneath the run."""
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    a = _agent_obj(h, aid)
    before = a.ledger_sid
    h.mgr.send(aid, "go")
    _wait_settled(a)
    assert a.session_id == "sess-1"          # the resume token DID arrive...
    assert a.ledger_sid == before            # ...and the ledger id did not move
    assert set(_sids(h)) == {before}


def test_ledger_id_is_stable_across_runs_and_unique_per_agent():
    h = Harness()
    a1 = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(a1, "first")
    _wait_settled(_agent_obj(h, a1))
    h.mgr.send(a1, "second")
    _wait_settled(_agent_obj(h, a1))
    assert len(set(_sids(h))) == 1, "same agent keeps one card across runs"
    a2 = h.mgr.spawn(name="B")["agent"]["id"]
    h.mgr.send(a2, "other")
    _wait_settled(_agent_obj(h, a2))
    assert len(set(_sids(h))) == 2, "different agents are different cards"


def test_claude_session_id_rides_in_the_payload_once_known():
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    prompt_payload, done_payload = h.events[0][1], h.events[1][1]
    # unknown at send() time, known by run-end — correlation is preserved
    assert "claude_session_id" not in prompt_payload
    assert done_payload["claude_session_id"] == "sess-1"


def test_run_still_logs_when_the_claude_session_id_never_arrives():
    """A stream with no `system` init event must not break logging — the ledger
    may never crash a pane."""
    lines = [json.dumps({"type": "result", "subtype": "success",
                         "result": "done", "num_turns": 1})]
    h = Harness(lines=lines)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    a = _agent_obj(h, aid)
    _wait_settled(a)
    assert a.session_id is None
    assert [e[0] for e in h.events] == ["user_prompt", "assistant_response"]
    assert set(_sids(h)) == {f"ws-{a.id}"}
    assert all("claude_session_id" not in p for _, p, _ in h.events)


# --- WS1: agent_disconnect journal (terminal signals) --------------------------
# A run had exactly three terminal paths (workspace.py:697-714): a clean
# assistant_response, a coarse tool_failed, and — for a user stop — a bare `pass`
# that wrote NOTHING, orphaning the user_prompt. WS1 journals every non-clean
# ending as a machine-readable agent_disconnect(reason=...), keying the failure
# class so the Improvements loop (WS4) can tell a stop/timeout/budget death from
# a genuine tool failure. The clean path stays a single assistant_response.


def test_user_stopped_run_journals_agent_disconnect_stopped():
    proc = _HoldProc()
    events = []
    mgr = workspace.Manager(
        spawn=lambda argv, env, cwd: proc,
        append_event=lambda et, pl, **kw: events.append((et, pl, kw)))
    aid = mgr.spawn(name="A")["agent"]["id"]
    mgr.send(aid, "long job")
    a = mgr.get(aid)

    deadline = time.time() + 5
    while time.time() < deadline and a.status != "running":
        time.sleep(0.01)
    assert a.status == "running"            # the run is genuinely in flight

    mgr.stop(aid)
    deadline = time.time() + 5              # wait for the LEDGER write, not status
    while time.time() < deadline and not any(
            e[0] == "agent_disconnect" for e in events):
        time.sleep(0.01)

    assert a.status == "stopped"
    kinds = [e[0] for e in events]
    assert "agent_disconnect" in kinds      # the orphan is closed
    i = kinds.index("agent_disconnect")
    assert events[i][1]["reason"] == "stopped"
    assert events[i][2].get("session_id") == a.ledger_sid   # one run, one identity


def test_failed_run_also_journals_agent_disconnect():
    h = Harness(code=1)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    # the coarse tool_failed stays (Memory/Improvements), the disconnect is added
    assert [e[0] for e in h.events] == \
        ["user_prompt", "tool_failed", "agent_disconnect"]
    dc = h.events[-1][1]
    assert dc["reason"] == "exit" and dc["exit_code"] == 1
    assert dc["agent"] == "A" and dc["role"] == "agent"


def test_completed_run_journals_no_agent_disconnect():
    # a clean run's assistant_response IS its terminal record — adding a disconnect
    # would double every happy-path run (22 of 29 in history) for no gain
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    assert [e[0] for e in h.events] == ["user_prompt", "assistant_response"]


def test_timeout_kill_marks_reason_timeout():
    # a timeout kill must be DISTINGUISHABLE from a generic failure (both hit the
    # reader's else branch) — _timeout_kill stamps the reason the reader journals
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    a = _agent_obj(h, aid)

    class _P:
        def poll(self):
            return None

        def kill(self):
            pass

    h.mgr._timeout_kill(a, _P())
    assert a._disconnect_reason == "timeout"


def test_disconnect_reason_is_per_run_state():
    # agents are reusable: a prior run's timeout must not mislabel the next run's
    # disconnect. send() clears the marker before the run starts.
    h = Harness()
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    a = _agent_obj(h, aid)
    a._disconnect_reason = "timeout"           # stale from a prior run
    h.mgr.send(aid, "go")
    _wait_settled(a)
    assert a._disconnect_reason is None


# --- WS1: process-table rehydration (restart survival, G3) ---------------------
# Manager.__init__ used to restore nothing, so a dashboard restart wiped every
# agent's identity, status, session id and cost. rehydrate() folds recent
# ws-<id> fleet sessions back into a RECORD of the fleet — never a live run: no
# subprocess is re-attached, and a run interrupted by the restart is 'interrupted'
# (not a phantom 'running' that clear_idle, keyed on status != 'running', could
# never remove).

from mindhive import events as _evmod


def _wsrow(sid, etype, payload, second, *, swarm=False):
    role = payload.get("role", "agent")
    tags = ["workspace", "swarm" if swarm else "parallel", role]
    return _evmod.Event(ts=f"2026-07-08T10:00:{second:02d}Z", session_id=sid,
                        type=etype, payload=payload, tags=tags)


def _wsrun(sid, *pairs, swarm=False):
    """(etype, payload) pairs -> workspace-tagged Event rows for one ws-<id> run."""
    return [_wsrow(sid, et, pl, i, swarm=swarm) for i, (et, pl) in enumerate(pairs)]


def _bare_manager():
    # a Manager that never touches the real ledger — rehydrate() is fed events
    return workspace.Manager(spawn=lambda *a: None,
                             append_event=lambda *a, **k: None,
                             read_events=lambda project=None: [])


def test_rehydrate_reconstructs_a_completed_agent():
    evs = _wsrun("ws-abc123",
                 ("user_prompt", {"text": "audit", "agent": "Fleet 1",
                                  "role": "agent"}),
                 ("assistant_response", {"text": "done", "agent": "Fleet 1",
                                         "role": "agent",
                                         "claude_session_id": "cuid-1"}))
    mgr = _bare_manager()
    res = mgr.rehydrate(evs=evs)
    assert res["rehydrated"] == 1
    a = mgr.get("abc123")
    assert a is not None
    assert a.ledger_sid == "ws-abc123"
    assert a.name == "Fleet 1" and a.role == "agent"
    assert a.status == "done"
    assert a.session_id == "cuid-1"        # claude session recovered for --resume
    assert a.proc is None                  # a record, never a live subprocess


def test_rehydrate_maps_every_terminal_status():
    evs = (
        _wsrun("ws-done", ("user_prompt", {"agent": "D"}),
                          ("assistant_response", {"agent": "D"}))
        + _wsrun("ws-fail", ("user_prompt", {"agent": "F"}),
                            ("tool_failed", {"agent": "F",
                                             "signature": "workspace:agent"}))
        + _wsrun("ws-stop", ("user_prompt", {"agent": "S"}),
                            ("agent_disconnect", {"agent": "S", "reason": "stopped"}))
        + _wsrun("ws-timeout", ("user_prompt", {"agent": "T"}),
                               ("agent_disconnect", {"agent": "T",
                                                     "reason": "timeout"}))
        + _wsrun("ws-live", ("user_prompt", {"agent": "L"}))   # no terminal event
    )
    mgr = _bare_manager()
    mgr.rehydrate(evs=evs)
    st = {aid: mgr.get(aid).status
          for aid in ("done", "fail", "stop", "timeout", "live")}
    assert st == {"done": "done", "fail": "error", "stop": "stopped",
                  "timeout": "error", "live": "interrupted"}


def test_rehydrate_recovers_cost_and_turns_from_disconnect():
    evs = _wsrun("ws-c",
                 ("user_prompt", {"agent": "C"}),
                 ("tool_failed", {"agent": "C", "signature": "workspace:agent"}),
                 ("agent_disconnect", {"agent": "C", "reason": "exit",
                                       "cost": 0.42, "turns": 7}))
    mgr = _bare_manager()
    mgr.rehydrate(evs=evs)
    a = mgr.get("c")
    assert a.status == "error"
    assert a.last_cost == 0.42 and a.last_turns == 7


def test_rehydrate_recovers_cost_and_turns_from_done_path():
    # WS2 persists cost/turns on the done path's assistant_response too — a
    # cleanly-completed run must rehydrate WITH its cost, not a blank.
    evs = _wsrun("ws-d",
                 ("user_prompt", {"agent": "D"}),
                 ("assistant_response", {"agent": "D", "cost": 0.19, "turns": 4}))
    mgr = _bare_manager()
    mgr.rehydrate(evs=evs)
    a = mgr.get("d")
    assert a.status == "done"
    assert a.last_cost == 0.19 and a.last_turns == 4


def test_rehydrated_agents_are_settled_not_phantom_running():
    # a restart-interrupted run must be clearable, never a phantom 'running' that
    # clear_idle (status != 'running') could never remove
    evs = _wsrun("ws-x", ("user_prompt", {"agent": "X"}))
    mgr = _bare_manager()
    mgr.rehydrate(evs=evs)
    assert mgr.get("x").status != "running"
    assert mgr.clear_idle()["removed"] == 1
    assert mgr.list()["agents"] == []


def test_rehydrate_caps_at_horizon_newest_first():
    # 20 sessions, distinct minutes; only the newest REHYDRATE_HORIZON survive
    evs = [_evmod.Event(ts=f"2026-07-08T10:{i:02d}:00Z", session_id=f"ws-a{i:02d}",
                        type="user_prompt", payload={"agent": f"A{i}"},
                        tags=["workspace", "parallel", "agent"]) for i in range(20)]
    mgr = _bare_manager()
    res = mgr.rehydrate(evs=evs)
    assert res["rehydrated"] == workspace.REHYDRATE_HORIZON
    ids = {a["id"] for a in mgr.list()["agents"]}
    assert "a19" in ids and "a04" in ids           # newest HORIZON survive
    assert "a03" not in ids and "a00" not in ids   # oldest dropped


def test_rehydrate_never_exceeds_max_agents_with_live_agents():
    # live agents plus newest-horizon records must not overflow MAX_AGENTS —
    # an over-cap fleet wedges every future spawn (which refuses at the cap).
    mgr = _bare_manager()
    for i in range(workspace.MAX_AGENTS - 2):       # nearly-full live fleet
        live = workspace.Agent(f"L{i}")
        mgr.agents[live.id] = live
    evs = [_evmod.Event(ts=f"2026-07-08T10:{i:02d}:00Z", session_id=f"ws-r{i:02d}",
                        type="user_prompt", payload={"agent": f"R{i}"},
                        tags=["workspace", "parallel", "agent"])
           for i in range(10)]
    res = mgr.rehydrate(evs=evs)
    assert res["rehydrated"] == 2                    # only 2 free slots were left
    assert len(mgr.agents) == workspace.MAX_AGENTS


def test_rehydrate_is_idempotent_and_skips_live_agents():
    evs = _wsrun("ws-dup", ("user_prompt", {"agent": "Dup"}),
                           ("assistant_response", {"agent": "Dup"}))
    mgr = _bare_manager()
    assert mgr.rehydrate(evs=evs)["rehydrated"] == 1
    assert mgr.rehydrate(evs=evs)["rehydrated"] == 0   # no duplicate on re-fold
    assert len(mgr.list()["agents"]) == 1


def test_rehydrate_skips_non_workspace_and_junk_rows():
    import types as _t
    evs = [
        _wsrow("ws-ok", "user_prompt", {"agent": "OK"}, 0),
        _evmod.Event(ts="2026-07-08T10:00:01Z", session_id="sess-plain",
                     type="user_prompt", payload={"text": "hi"}, tags=[]),
        _evmod.Event(ts="2026-07-08T10:00:02Z", session_id="research-xyz",
                     type="assistant_response", payload={}, tags=["workspace"]),
        _t.SimpleNamespace(session_id="ws-bad", type="user_prompt",
                           payload="not-a-dict", tags="not-a-list", ts="z"),
        _t.SimpleNamespace(session_id=None, type=None, payload=None,
                           tags=None, ts=None),
    ]
    mgr = _bare_manager()
    res = mgr.rehydrate(evs=evs)                    # must not raise (total read)
    assert res["rehydrated"] == 1
    assert [a["id"] for a in mgr.list()["agents"]] == ["ok"]


def test_rehydrate_uses_injected_reader_when_no_events_passed():
    golden = _wsrun("ws-rdr", ("user_prompt", {"agent": "R"}),
                              ("assistant_response", {"agent": "R"}))
    mgr = workspace.Manager(spawn=lambda *a: None,
                            append_event=lambda *a, **k: None,
                            read_events=lambda project=None: golden)
    res = mgr.rehydrate()                # no evs -> pulls from the injected reader
    assert res["rehydrated"] == 1
    assert mgr.get("rdr").status == "done"


def test_rehydrated_swarm_agent_keeps_role_and_swarm_flag():
    evs = _wsrun("ws-sw",
                 ("user_prompt", {"agent": "Builder 1", "role": "builder"}),
                 ("assistant_response", {"agent": "Builder 1", "role": "builder"}),
                 swarm=True)
    mgr = _bare_manager()
    mgr.rehydrate(evs=evs)
    a = mgr.get("sw")
    assert a.role == "builder" and a.swarm is True


# --- WS2: per-run cost accounting (the entry source) ---------------------------
# Every run's cost reached Agent.last_cost in memory and was then discarded — 0
# of 29 historical runs persisted a cost. WS2 persists it on the terminal events
# so loop/budget.py can fold entries: the done path's assistant_response gains
# cost/turns; the failure path's agent_disconnect already carries them (WS1a).


def test_completed_run_persists_its_cost_on_the_done_path():
    h = Harness(lines=_stream_lines(result="ok"))   # envelope: $0.0123, 2 turns
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    done = h.events[1]
    assert done[0] == "assistant_response"
    assert done[1]["cost"] == 0.0123
    assert done[1]["turns"] == 2


def test_done_path_persists_none_cost_when_envelope_omits_it():
    # distinguish "no cost reported" (None) from "$0" so the fold can skip it
    lines = [json.dumps({"type": "result", "subtype": "success",
                         "result": "done", "num_turns": 1})]     # no total_cost_usd
    h = Harness(lines=lines)
    aid = h.mgr.spawn(name="A")["agent"]["id"]
    h.mgr.send(aid, "go")
    _wait_settled(_agent_obj(h, aid))
    assert h.events[1][0] == "assistant_response"
    assert h.events[1][1]["cost"] is None
    assert h.events[1][1]["turns"] == 1


# --- WS2: pre-flight budget gate -----------------------------------------------
# Manager.spawn / start_mission check a rolling estimate against the user-set
# ceiling before launching subprocesses; the gate fails OPEN on no ceiling, no
# history, or any budget-subsystem error (a broken gate must never block work).


def test_spawn_refused_when_gate_blocks_launches_nothing(monkeypatch):
    h = Harness()
    monkeypatch.setattr(workspace.wsbudget, "gate",
                        lambda project=None, n=1, **k:
                        {"ok": False, "reason": "over ceiling",
                         "wait_seconds": 42.0})
    res = h.mgr.spawn(name="A", prompt="go")
    assert not res["ok"]
    assert "over ceiling" in res["error"] and res["wait_seconds"] == 42.0
    assert h.calls == []                       # no subprocess launched
    assert h.mgr.list()["agents"] == []        # no agent created


def test_spawn_allowed_when_gate_passes(monkeypatch):
    h = Harness()
    monkeypatch.setattr(workspace.wsbudget, "gate",
                        lambda project=None, n=1, **k: {"ok": True, "estimate": 0.01})
    assert h.mgr.spawn(name="A")["ok"]
    assert len(h.mgr.list()["agents"]) == 1


def test_spawn_gate_fails_open_on_subsystem_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("budget subsystem down")
    h = Harness()
    monkeypatch.setattr(workspace.wsbudget, "gate", boom)
    assert h.mgr.spawn(name="A")["ok"]         # R5: fail open, never block
    assert len(h.mgr.list()["agents"]) == 1


def test_mission_refused_pre_spawn_when_gate_blocks(monkeypatch):
    h = Harness()
    monkeypatch.setattr(workspace.wsbudget, "gate",
                        lambda project=None, n=1, **k: {"ok": False, "reason": "over"})
    res = h.mgr.start_mission("do it", builders=1, scouts=0, reviewers=0)
    assert not res["ok"] and "over" in res["error"]
    assert h.mgr.list()["agents"] == []        # whole mission gated, nothing spawned


def test_default_spawn_is_unlimited_and_allowed():
    # no ceiling set in the isolated store -> the real gate fails open
    h = Harness()
    assert h.mgr.spawn(name="A")["ok"]


def test_api_budget_get_and_set_roundtrip():
    res = workspace.api_post("set_budget", proj="test",
                             body={"cost_budget_usd": 7.5, "window_hours": 4})
    assert res["ok"] and res["cost_budget_usd"] == 7.5
    got = workspace.api_get("budget", proj="test")
    assert got["ok"]
    assert got["cost_budget_usd"] == 7.5 and got["window_hours"] == 4.0
    assert got["remaining_usd"] == 7.5         # no spend yet
