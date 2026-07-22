# tests/test_oversee.py — interactive-session Oversee projection.
import types
from datetime import datetime

from mindhive import events, oversee
from mindhive import __main__ as cli


def _epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _ev(etype, payload, minute, sid="sess-abc", second=0):
    events.append_event(etype, payload, session_id=sid,
                        ts=f"2026-07-08T10:{minute:02d}:{second:02d}Z",
                        project="test")


def test_active_session_appears_with_prompt_title_and_online():
    _ev("session_start", {"cwd": "x"}, 0)
    _ev("user_prompt", {"text": "build the parser feature"}, 1)
    _ev("file_touched", {"path": "src/parser.py"}, 2)
    _ev("command_run", {"text": "pytest -q"}, 3)
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:04:00Z"))
    assert len(res) == 1
    s = res[0]
    assert s["kind"] == "chat"
    assert s["title"] == "build the parser feature"
    assert s["online"] is True
    assert s["iterations"] == 1                       # one command_run
    assert any("src/parser.py" in d for d in s["done"])
    stale = oversee.interactive_sessions("test",
                                         now_fn=lambda: _epoch("2026-07-08T10:30:00Z"))
    assert stale[0]["online"] is False                # idle > online window


def test_ended_session_is_removed_automatically():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "quick fix"}, 1)
    _ev("session_end", {}, 2)
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))
    assert res == []


def test_stale_session_ages_out():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "old work"}, 1)
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T17:30:00Z"))
    assert res == []                                   # > 6h idle


def test_manual_dismissal_hides_until_new_activity():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "some work"}, 1)
    events.append_event("loop_run_dismissed", {"run_id": "sess-abc", "cause": "manual"},
                        ts="2026-07-08T10:05:00Z", project="test")
    hidden = oversee.interactive_sessions("test",
                                          now_fn=lambda: _epoch("2026-07-08T10:06:00Z"))
    assert hidden == []
    _ev("file_touched", {"path": "y.py"}, 7)          # session was still alive
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:08:00Z"))
    assert [s["run_id"] for s in res] == ["sess-abc"]


def test_loop_and_manual_events_are_not_chat_sessions():
    events.append_event("loop_run_start", {"run_id": "r1", "goal_dir": "d"},
                        session_id="sess-abc", ts="2026-07-08T10:00:00Z", project="test")
    events.append_event("decision", {"text": "cli append"}, session_id="manual",
                        ts="2026-07-08T10:01:00Z", project="test")
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:02:00Z"))
    assert res == []


def test_semantic_events_and_next_steps_fill_the_checklist():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "refactor the store"}, 1)
    _ev("decision", {"text": "keep the file backend"}, 2)
    _ev("next_step", {"text": "migrate the reader"}, 3)
    _ev("assistant_response", {"text": "migrating the reader now"}, 4)
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    assert any("keep the file backend" in d for d in s["done"])
    assert s["planned"] == ["migrate the reader"]
    assert s["doing"] == "migrating the reader now"


def test_skill_trail_records_name_time_ok_and_why():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "ship the parser"}, 1)
    _ev("skill_used", {"name": "ce-plan", "plugin": "compound-engineering",
                       "ok": True, "text": "planned",
                       "args": "plan the parser feature"}, 2)
    _ev("user_prompt", {"text": "now fix the bug"}, 3)
    _ev("skill_used", {"name": "ce-debug", "ok": False, "text": "boom"}, 4)
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    assert s["skills_total"] == 2
    a, b = s["skills"]
    assert a["name"] == "ce-plan" and a["plugin"] == "compound-engineering"
    assert a["ts"] == "2026-07-08T10:02:00Z"
    assert a["ok"] is True
    assert a["why"] == "plan the parser feature"      # the args = the ask
    assert a["during"] == "ship the parser"           # the prompt it served
    assert b["ok"] is False and b["why"] == ""
    assert b["during"] == "now fix the bug"           # later prompt shifts it


def test_skill_before_any_prompt_and_nameless_payloads():
    _ev("session_start", {}, 0)
    _ev("skill_used", {"name": "verify"}, 1)
    _ev("skill_used", {"text": "no name"}, 2)         # skipped, never raises
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills_total"] == 1
    assert s["skills"][0]["during"] == ""             # nothing preceded it


def test_skill_trail_caps_display_but_counts_all():
    _ev("session_start", {}, 0)
    for i in range(12):
        _ev("skill_used", {"name": f"s{i}"}, 1, second=i)
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:02:00Z"))[0]
    assert s["skills_total"] == 12
    assert [k["name"] for k in s["skills"]] == [f"s{i}" for i in range(4, 12)]


def test_skill_used_keeps_the_session_fresh():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "long think"}, 1)
    _ev("skill_used", {"name": "verify"}, 30)         # only recent activity
    s = oversee.interactive_sessions("test",
                                     now_fn=lambda: _epoch("2026-07-08T10:31:00Z"))[0]
    assert s["online"] is True


def test_self_reliant_run_is_labeled_and_titled():
    # a Self-Reliant run's research-<id> stream carries the provenance label
    # and a human title (it has no user_prompt to title from)
    _ev("experiment_result", {"text": "Self-Reliant experiment 1: ok"}, 0,
        sid="research-ab12cd34")
    _ev("user_prompt", {"text": "ordinary chat"}, 1, sid="sess-plain")
    res = oversee.interactive_sessions("test",
                                       now_fn=lambda: _epoch("2026-07-08T10:02:00Z"))
    by_id = {r["run_id"]: r for r in res}
    sr = by_id["research-ab12cd34"]
    assert sr["origin"] == "self-reliant"
    assert sr["title"] == "Self-Reliant run ab12cd34"
    assert by_id["sess-plain"]["origin"] is None


# --- richer why: causal direction + intended-use fallback ----------------------

def test_skill_directed_stamps_causal_why():
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "make it better"}, 1)
    _ev("skill_directed", {"skills": [{"name": "ce-plan",
                                       "plugin": "compound-engineering",
                                       "measured": {"ok": 12, "calls": 12}}]}, 2)
    _ev("skill_used", {"name": "ce-plan", "plugin": "compound-engineering"}, 3)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    k = s["skills"][0]
    assert k["why"] == "directed by prompt-gate (measured 12/12)"
    assert k["during"] == "make it better"


def test_skill_directed_without_measured_stat():
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": [{"name": "verify"}]}, 1)   # unproven -> no stat
    _ev("skill_used", {"name": "verify"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills"][0]["why"] == "directed by prompt-gate"


def test_args_why_beats_direction():
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": [{"name": "ce-plan"}]}, 1)
    _ev("skill_used", {"name": "ce-plan", "args": "plan the parser"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills"][0]["why"] == "plan the parser"   # args (the ask) wins


def test_intended_use_fills_bare_why_labelled_as_purpose():
    _ev("session_start", {}, 0)
    _ev("skill_used", {"name": "verify"}, 1)            # no args, no direction, no prompt
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"),
        intended_use={"verify": "exercise the real flow end to end"})[0]
    # labelled as the skill's PURPOSE, never presented as this call's motive
    assert s["skills"][0]["why"] == "purpose: exercise the real flow end to end"


def test_direction_is_consumed_once_not_over_attributed():
    # one direction must not stamp every later same-skill call — only the next
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": [{"name": "ce-plan"}]}, 1)
    _ev("skill_used", {"name": "ce-plan"}, 2)           # consumes the direction
    _ev("skill_used", {"name": "ce-plan"}, 3)           # independent later call
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))[0]
    whys = [k["why"] for k in s["skills"]]
    assert whys == ["directed by prompt-gate", ""]      # not both stamped


def test_non_list_skills_directed_stays_total():
    # a present-but-non-list `skills` value must not raise (total-read invariant)
    for bad in (None, 7, "ce-plan"):
        events.append_event("skill_directed", {"skills": bad}, session_id="s-bad",
                            ts="2026-07-08T10:01:00Z", project="test")
        events.append_event("skill_used", {"name": "ce-plan"}, session_id="s-bad",
                            ts="2026-07-08T10:02:00Z", project="test")
        trails = oversee.skill_trails(project="test")   # must not raise
        assert trails["s-bad"][-1]["why"] == ""         # nothing stamped from junk


def test_direction_is_scoped_to_its_session():
    _ev("session_start", {}, 0, sid="A")
    _ev("skill_directed", {"skills": [{"name": "ce-plan"}]}, 1, sid="A")
    _ev("session_start", {}, 0, sid="B")
    _ev("skill_used", {"name": "ce-plan"}, 2, sid="B")
    res = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:05:00Z"))
    b = {r["run_id"]: r for r in res}["B"]
    assert b["skills"][0]["why"] == ""                  # A's direction must not leak


def test_malformed_skill_directed_never_raises():
    _ev("session_start", {}, 0)
    _ev("skill_directed", {"skills": ["not-a-dict", {"noname": 1}]}, 1)
    _ev("skill_used", {"name": "ce-plan"}, 2)
    s = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z"))[0]
    assert s["skills"][0]["why"] == ""                  # junk skipped, total read


# --- skill_trails: the durable fold behind the CLI / API -----------------------

def test_skill_trails_includes_ended_sessions():
    _ev("session_start", {}, 0)
    _ev("skill_used", {"name": "verify"}, 1)
    _ev("session_end", {}, 2)
    # the live board drops the ended session...
    assert oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:03:00Z")) == []
    # ...but the durable fold keeps its trail
    trails = oversee.skill_trails(project="test")
    assert [k["name"] for k in trails["sess-abc"]] == ["verify"]


def test_cli_skills_trail_prints_trail(capsys, monkeypatch):
    monkeypatch.setattr(oversee, "intended_use_map", lambda *a, **k: {})
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "ship it"}, 1)
    _ev("skill_used", {"name": "ce-plan", "args": "plan the thing"}, 2)
    args = types.SimpleNamespace(session=None, all=False, json=False, project="test")
    cli.cmd_skills_trail(args)
    out = capsys.readouterr().out
    assert "ce-plan" in out and "plan the thing" in out and "sess-abc" in out


# --- workspace run outcome on the card (WS0.2) ---------------------------------
# A MindParallel/MindSwarm run logs into the ledger under the agent's claude
# session id with tags ["workspace", "parallel"|"swarm", role] (workspace._log),
# so those runs surface here as chat cards. Before this, `status` was assigned
# "running" once at card creation and never updated, so a run that had already
# failed was indistinguishable from a healthy idle session for up to 6 hours.

def _wsev(etype, payload, minute, sid="ws-run-1", second=0):
    """A workspace-tagged event, exactly as workspace._log writes it."""
    events.append_event(etype, payload, session_id=sid,
                        tags=["workspace", "parallel", "agent"],
                        ts=f"2026-07-08T10:{minute:02d}:{second:02d}Z",
                        project="test")


def _only(sid="ws-run-1", at="2026-07-08T10:10:00Z"):
    res = oversee.interactive_sessions("test", now_fn=lambda: _epoch(at))
    return next(s for s in res if s["run_id"] == sid)


def test_failed_workspace_run_reports_failed_not_running():
    _wsev("user_prompt", {"text": "audit the ledger", "agent": "A"}, 1)
    _wsev("tool_failed", {"text": "workspace run failed (exit 1)",
                          "signature": "workspace:agent"}, 2)
    assert _only()["status"] == "failed"


def test_completed_workspace_run_reports_done():
    _wsev("user_prompt", {"text": "audit the ledger", "agent": "A"}, 1)
    _wsev("assistant_response", {"text": "here is what I found", "agent": "A"}, 2)
    assert _only()["status"] == "done"


def test_ordinary_tool_failure_does_not_mark_the_session_failed():
    """Guard against the obvious wrong fix: tool_failed is written by five
    different producers (failures.py transcript capture, hookcapture, invoker,
    __main__, workspace). One failed tool call inside a normal session must NOT
    paint the whole session as a failed run."""
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "build the parser"}, 1)
    _ev("tool_failed", {"text": "Edit failed: string not found",
                        "signature": "edit:not-read"}, 2)
    assert _only(sid="sess-abc")["status"] == "running"


def test_workspace_agent_re_sent_after_a_failure_is_running_again():
    """An agent is reusable: send() can run again on the same session id. The
    LAST outcome wins, so a new prompt after a failure returns it to running."""
    _wsev("user_prompt", {"text": "first go", "agent": "A"}, 1)
    _wsev("tool_failed", {"text": "workspace run failed (exit 1)",
                          "signature": "workspace:agent"}, 2)
    _wsev("user_prompt", {"text": "second go", "agent": "A"}, 3)
    assert _only()["status"] == "running"


# --- one fleet run, one whole card (U2, end-to-end) ----------------------------
# These drive a REAL Manager (fake subprocess, real append_event) and then read
# back through oversee.interactive_sessions. The bug being guarded was two
# modules disagreeing about a run's identity, so a fold-level test that fakes
# the ledger rows cannot catch a regression here — the write path has to be real.

import json as _json
import time as _time

from mindhive import workspace as _ws


class _Stdin:
    def __init__(self):
        self.data, self.closed = "", False

    def write(self, s):
        self.data += s

    def close(self):
        self.closed = True


class _Proc:
    def __init__(self, lines, code=0):
        self.stdout, self.stdin, self._code, self._done = (
            iter(lines), _Stdin(), code, False)

    def poll(self):
        return self._code if self._done else None

    def wait(self):
        self._done = True
        return self._code

    def kill(self):
        self._done = True


_CLAUDE_SID = "claude-uuid-1"


def _stream(result="the answer"):
    return [
        _json.dumps({"type": "system", "subtype": "init",
                     "session_id": _CLAUDE_SID, "model": "m"}),
        _json.dumps({"type": "result", "subtype": "success", "result": result,
                     "num_turns": 1, "session_id": _CLAUDE_SID}),
    ]


def _run_fleet_agent(prompt="audit the ledger", code=0, lines=None):
    """Run one fleet agent to completion against the isolated test store."""
    mgr = _ws.Manager(spawn=lambda argv, env, cwd: _Proc(
        list(_stream() if lines is None else lines), code=code))
    aid = mgr.spawn(name="Fleet", project="test")["agent"]["id"]
    mgr.send(aid, prompt)
    a = mgr.get(aid)
    # Wait for the LEDGER write, not just the status flip: _reader assigns
    # a.status and only then calls _log, so polling status alone races the
    # append and reads a card that has no terminal event yet.
    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        if a.status != "running" and any(
                e.type in ("assistant_response", "tool_failed")
                for e in events.read_all("test")
                if e.session_id == a.ledger_sid):
            return a
        _time.sleep(0.01)
    raise AssertionError(
        f"run never reached the ledger (status={a.status})")


def _cards():
    return oversee.interactive_sessions("test")


def test_completed_fleet_run_is_one_card_carrying_prompt_and_outcome():
    a = _run_fleet_agent(prompt="audit the ledger")
    fleet = [c for c in _cards() if c["run_id"] == a.ledger_sid]
    assert len(fleet) == 1, "one run must not render as two cards"
    assert fleet[0]["title"] == "audit the ledger"
    assert fleet[0]["status"] == "done"


def test_failed_fleet_run_is_one_card_reporting_failed():
    a = _run_fleet_agent(code=1)
    fleet = [c for c in _cards() if c["run_id"] == a.ledger_sid]
    assert len(fleet) == 1
    assert fleet[0]["status"] == "failed"


def test_harvested_session_end_does_not_delete_the_fleet_card():
    """R3 / defect 0.6. harvest imports the subprocess's own transcript —
    including session_end — under the CLAUDE uuid. That must not remove the
    fleet card, which is what silently deleted every completed run's result."""
    a = _run_fleet_agent()
    events.append_event("session_end", {}, session_id=_CLAUDE_SID,
                        tags=["harvest"], project="test")
    assert [c["run_id"] for c in _cards() if c["run_id"] == a.ledger_sid] \
        == [a.ledger_sid]


def test_a_sessions_own_session_end_still_removes_it():
    """R5 guard: the fix above must not disable legitimate lifecycle removal."""
    _ev("session_start", {}, 0)
    _ev("user_prompt", {"text": "ordinary work"}, 1)
    _ev("session_end", {}, 2)
    assert not [c for c in _cards() if c["run_id"] == "sess-abc"]


def test_fleet_run_re_sent_after_finishing_returns_to_running():
    a = _run_fleet_agent()
    assert [c for c in _cards() if c["run_id"] == a.ledger_sid][0]["status"] \
        == "done"
    events.append_event("user_prompt", {"text": "second go"},
                        session_id=a.ledger_sid,
                        tags=["workspace", "parallel", "agent"], project="test")
    assert [c for c in _cards() if c["run_id"] == a.ledger_sid][0]["status"] \
        == "running"


# --- legacy split sessions stay readable (U3 / KTD2) ---------------------------
# 14 runs were recorded before the identity fix, split across two session ids.
# The ledger is append-only and is never migrated (SOP 7.1), so those rows stay
# on disk forever. KTD2: they are left as legacy, NOT repaired at read time —
# these tests pin that decision and the total-read invariant that protects it.

def test_legacy_split_run_folds_without_raising():
    """The historical shape: prompt under ws-<id>, outcome under the uuid."""
    _wsev("user_prompt", {"text": "legacy work", "agent": "A"}, 1,
          sid="ws-legacy-1")
    _wsev("assistant_response", {"text": "legacy answer", "agent": "A"}, 2,
          sid="claude-legacy-1")
    res = oversee.interactive_sessions(
        "test", now_fn=lambda: _epoch("2026-07-08T10:10:00Z"))
    by_id = {c["run_id"]: c for c in res}
    # the orphaned prompt has no terminal event, so "running" is correct here —
    # it is the absence of an outcome, not a mis-read
    assert by_id["ws-legacy-1"]["status"] == "running"
    # and the already-recorded outcome is still reported, not lost
    assert by_id["claude-legacy-1"]["status"] == "done"


def test_legacy_split_failure_still_reports_failed():
    _wsev("user_prompt", {"text": "legacy work", "agent": "A"}, 1,
          sid="ws-legacy-2")
    _wsev("tool_failed", {"text": "workspace run failed (exit 1)",
                          "signature": "workspace:agent"}, 2,
          sid="claude-legacy-2")
    assert _only(sid="claude-legacy-2")["status"] == "failed"


def test_workspace_detection_survives_malformed_tags():
    """Reads stay total: the store holds rows written by every past version of
    the code, and a hand-edited or future-shaped `tags` must never raise."""
    for tags in (None, "workspace", 42, {"workspace": True}):
        e = types.SimpleNamespace(type="tool_failed", tags=tags,
                                  session_id="s", payload={}, ts="")
        assert oversee._is_workspace(e) is False
    assert oversee._is_workspace(
        types.SimpleNamespace(type="tool_failed", tags=["workspace"],
                              session_id="s", payload={}, ts="")) is True
