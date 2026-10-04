# tests/test_spend.py
"""`hive spend`: read-only token-spend reports over Claude Code transcripts.

Plan item 0.3 of the Terion program (spec
`docs/superpowers/specs/2026-10-03-terion-distiller-design.md`). The Fase 0.1
snapshot `state/spend_baseline_2026-10-03.json` was written by a one-off script;
these reports replicate its definitions so a later run reads against it number
for number, which is how Fase 0.5 judges the plugin diet:

- context tokens of one API call = input + cache_creation + cache_read, counted
  once per message id, `<synthetic>` replies skipped;
- a session is one transcript; it counts toward the token reports with at least
  two calls; subagent = under `subagents/`, chore = its first prompt opens a
  machine prompt, headless = a MindHive child (MindHive's registered
  SessionStart hook stayed silent while another owner's ran), interactive =
  the rest; `--since` pins the window's start to a moment;
- a hook's owner comes from the command: installed plugins' hook maps, then the
  plugin cache path, then `mindhive`;
- injected hook text and lookup results stay in context until the next
  `compact_boundary`, at about four characters a token.

Every fixture is a synthetic transcript written in Claude Code's own compact
JSON. Nothing here reads `~/.claude`.
"""
import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from mindhive import __main__ as cli_mod
from mindhive import paths, spend

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

SP_CMD = '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd" session-start'
PV_CMD = 'cat "${CLAUDE_PLUGIN_ROOT}/hooks/user-prompt-submit.md"'
MH_CMD = "powershell -NoProfile -File C:/Users/x/mindhive/hooks/session_start.ps1"
CACHE_CMD = "node C:/Users/x/.claude/plugins/cache/protect-mkt/protect-mcp/0.5.5/hook.js"
OTHER_CMD = "npx some-gate evaluate --policy ./policy"

_ids = itertools.count(1)


# --- fixture builders -------------------------------------------------------

def _t(day=3, minute=0, hour=8):
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def _uid():
    return f"u{next(_ids)}"


def _call(mid, ts, fresh=0, read=0, create=0, out=0, model="claude-opus-5-5",
          tools=()):
    content = [{"type": "tool_use", "id": tid, "name": name, "input": inp}
               for tid, name, inp in tools] or [{"type": "text", "text": "ok"}]
    return {"type": "assistant", "timestamp": ts, "uuid": _uid(),
            "requestId": "req_" + mid,
            "message": {"id": mid, "role": "assistant", "model": model,
                        "content": content,
                        "usage": {"input_tokens": fresh,
                                  "cache_creation_input_tokens": create,
                                  "cache_read_input_tokens": read,
                                  "output_tokens": out}}}


def _prompt(text, ts):
    return {"type": "user", "timestamp": ts, "uuid": _uid(),
            "message": {"role": "user", "content": text}}


def _result(tool_id, text, ts):
    return {"type": "user", "timestamp": ts, "uuid": _uid(),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def _boundary(ts):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts,
            "uuid": _uid(), "content": "Conversation compacted"}


def _hook(ts, kind="hook_success", event="SessionStart", name=None, command="",
          content="", stdout="", stderr="", ms=100):
    att = {"type": kind, "hookEvent": event, "hookName": name or event,
           "toolUseID": event}
    if kind == "hook_additional_context":
        att["content"] = content if isinstance(content, list) else [content]
    else:
        att.update(command=command, content=content, stdout=stdout, stderr=stderr,
                   exitCode=0, durationMs=ms)
    return {"type": "attachment", "timestamp": ts, "uuid": _uid(), "attachment": att}


def _context_json(event, text):
    return json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                              "additionalContext": text}})


def _write(claude, rel, rows, mtime=None):
    path = claude / "projects" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(r if isinstance(r, str) else
                              json.dumps(r, separators=(",", ":"))
                              for r in rows) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _meta(transcript, agent_type):
    transcript.with_name(transcript.stem + ".meta.json").write_text(
        json.dumps({"agentType": agent_type, "description": "d"}), encoding="utf-8")


def _plugins(claude):
    """Two installed plugins: one declares its hook in hooks/hooks.json, the
    other through a file its plugin.json names -- the two places the snapshot
    read hook commands from."""
    cache = claude / "plugins" / "cache"
    sp = cache / "superpowers-dev" / "superpowers" / "5.1.0"
    (sp / "hooks").mkdir(parents=True)
    (sp / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": SP_CMD}]}]}}),
        encoding="utf-8")
    pv = cache / "pensyve" / "pensyve" / "1.0.0"
    (pv / ".claude-plugin").mkdir(parents=True)
    (pv / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "pensyve", "hooks": "./extra-hooks.json"}), encoding="utf-8")
    (pv / "extra-hooks.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": PV_CMD}]}]}}), encoding="utf-8")
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {
            "superpowers@superpowers-dev": [{"scope": "user", "installPath": str(sp)}],
            "pensyve@pensyve": [{"scope": "user", "installPath": str(pv)}]}}),
        encoding="utf-8")


def _scan(claude, days=7):
    return spend.scan(claude, days=days, now=NOW)


def _by(rows, **match):
    hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, (match, rows)
    return hits[0]


@pytest.fixture
def claude(tmp_path):
    root = tmp_path / ".claude"
    (root / "projects").mkdir(parents=True)
    return root


# --- 1. the token arithmetic the snapshot used -------------------------------

def _baseline_world(claude):
    """Two interactive sessions, one chore, one subagent, one single-call."""
    _write(claude, "C--work-a/s1.jsonl", [
        _prompt("fix the flaky test", _t(minute=0)),
        _call("m1", _t(minute=1), fresh=10, create=90, read=900, out=5),
        # the same reply written as a second row (one per content block):
        _call("m1", _t(minute=1), fresh=10, create=90, read=900, out=5),
        _call("m2", _t(minute=2), read=2000, out=7),
        _call("m3", _t(minute=3), read=99999, model="<synthetic>"),
    ])
    _write(claude, "C--work-a/s2.jsonl", [
        _prompt("now the docs", _t(minute=10)),
        _call("n1", _t(minute=11), read=3000, out=1),
        _call("n2", _t(minute=12), read=3000, out=1),
        _call("n3", _t(minute=13), read=3000, out=1),
    ])
    _write(claude, "C--work-a/s3.jsonl", [
        _prompt("You are the Reflection Engine. Analyze ONE failure", _t(minute=20)),
        _call("r1", _t(minute=21), read=500),
        _call("r2", _t(minute=22), read=600),
    ])
    sub = _write(claude, "C--work-a/s1/subagents/agent-a1.jsonl", [
        _prompt("explore the repo", _t(minute=30)),
        _call("x1", _t(minute=31), read=700),
        _call("x2", _t(minute=32), read=800),
    ])
    _meta(sub, "Explore")
    _write(claude, "C--work-a/s5.jsonl", [
        _prompt("one call only", _t(minute=40)),
        _call("o1", _t(minute=41), read=9999),
    ])


def test_context_tokens_are_counted_once_per_message_and_skip_synthetic(claude):
    _baseline_world(claude)

    tok = spend.baseline(_scan(claude))["tokens"]["interactive"]

    assert tok["calls"] == 5
    assert tok["ctx_tokens"] == 1000 + 2000 + 3 * 3000
    assert tok["output_tokens"] == 12 + 3


def test_interactive_aggregates_use_the_snapshot_formulas(claude):
    """Two sessions whose first calls are 1,000 and 3,000: the true median is
    2,000 -- an upper median says 3,000 and a lower one 1,000."""
    _baseline_world(claude)

    tok = spend.baseline(_scan(claude))["tokens"]["interactive"]

    assert tok["sessions"] == 2
    assert tok["ctx_per_call_mean"] == 2400
    assert tok["ctx_per_call_median"] == 3000
    assert tok["first_call_ctx_median"] == 2000
    assert tok["first_call_ctx_p90"] == 3000
    assert tok["fixed_baseline_share"] == round((1000 * 2 + 3000 * 3) / 12000, 4)
    assert tok["window"] == [_t(minute=1), _t(minute=13)]


def test_sessions_are_split_into_interactive_chore_and_subagent(claude):
    _baseline_world(claude)

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert tokens["chore"]["sessions"] == 1
    assert tokens["chore"]["ctx_tokens"] == 1100
    assert tokens["subagent"]["sessions"] == 1
    assert tokens["subagent"]["ctx_tokens"] == 1500


def test_a_single_call_session_is_left_out_of_the_token_reports(claude):
    _baseline_world(claude)

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert tokens["headless"] is None                  # not measured: no settings.json
    assert sum(t["sessions"] for t in tokens.values() if t is not None) == 4
    assert tokens["interactive"]["first_call_ctx_p90"] != 9999


def test_a_chore_prefix_counts_only_in_the_first_prompt(claude):
    """CANARY for the chore split: the same machine-prompt text typed later in
    a session leaves it interactive."""
    _write(claude, "C--work-b/s1.jsonl", [
        _prompt("hello", _t(minute=0)),
        _call("a1", _t(minute=1), read=100),
        _prompt("You are the Reflection Engine. pretend", _t(minute=2)),
        _call("a2", _t(minute=3), read=100),
    ])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert tokens["interactive"]["sessions"] == 1
    assert tokens["chore"] == {"sessions": 0}


def test_every_machine_prompt_prefix_marks_a_chore(claude):
    for i, prefix in enumerate(spend.CHORE_PREFIXES):
        _write(claude, f"C--work-c/s{i}.jsonl", [
            _prompt("  " + prefix + " and more", _t(minute=i)),
            _call(f"c{i}a", _t(minute=i), read=10),
            _call(f"c{i}b", _t(minute=i), read=10),
        ])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert tokens["chore"]["sessions"] == len(spend.CHORE_PREFIXES) == 5
    assert tokens["interactive"] == {"sessions": 0}


# --- 1b. headless sessions: MindHive's own children ----------------------------

def _register_mindhive(claude, event="SessionStart", command=MH_CMD):
    """MindHive's hooks live in Claude Code's user settings, not in a plugin."""
    (claude / "settings.json").write_text(json.dumps({"hooks": {event: [
        {"hooks": [{"type": "command", "command": command}]}]}}), encoding="utf-8")


def _headless_world(claude, register=True):
    """A typed session, where MindHive's SessionStart hook ran beside another
    owner's, and a child MindHive started through `claude_cli` (an auto-resume
    delivery, say), where only the other one did: `claude_cli` sets
    MINDHIVE_HEADLESS=1 on its children, and MindHive's hooks exit on it
    without a word."""
    if register:
        _register_mindhive(claude)
    _write(claude, "C--hl/typed.jsonl", [
        _hook(_t(minute=0), command=SP_CMD, content="s" * 40),
        _hook(_t(minute=0), command=MH_CMD, content="m" * 40),
        _prompt("fix the flaky test", _t(minute=0)),
        _call("t1", _t(minute=1), read=5000,
              tools=[("tl1", "Bash", {"command": 'hive recall "flaky"'})]),
        _call("t2", _t(minute=2), read=5000),
    ])
    _write(claude, "C--hl/child.jsonl", [
        _hook(_t(minute=10), command=SP_CMD, content="s" * 40),
        _prompt("resume the release notes where the last session stopped", _t(minute=10)),
        _call("c1", _t(minute=11), read=700,
              tools=[("tc1", "Bash", {"command": "hive state"})]),
        _call("c2", _t(minute=12), read=700),
    ])


def test_a_session_whose_mindhive_session_start_stayed_silent_is_headless(claude):
    _headless_world(claude)
    scan = _scan(claude)

    rep = spend.baseline(scan)

    assert rep["headless_split"] == "separate"
    assert rep["tokens"]["interactive"]["sessions"] == 1
    assert rep["tokens"]["interactive"]["first_call_ctx_median"] == 5000
    assert rep["tokens"]["headless"]["sessions"] == 1
    assert rep["tokens"]["headless"]["ctx_tokens"] == 1400
    # Hook health counts every session in the window, the child's runs too.
    assert _by(spend.hooks(scan)["health"], owner=spend.UNATTRIBUTED,
               event="SessionStart")["runs"] == 2


@pytest.mark.parametrize("settings", [
    None, "{not json", '["a", 1]', '{"hooks": null}', '{"hooks": []}',
    '{"model": "opus"}', "other event", "other command"])
def test_without_mindhive_s_session_start_registered_nothing_is_headless(claude, settings):
    """CANARY for the split: MindHive's silence is evidence only where its
    SessionStart hook is registered. Elsewhere every session stays where the
    snapshot put it, and the headless kind is not measured at all -- which is
    not the same as measuring none."""
    _headless_world(claude, register=False)
    if settings == "other event":
        _register_mindhive(claude, event="UserPromptSubmit")
    elif settings == "other command":
        _register_mindhive(claude, command=SP_CMD)
    elif settings is not None:
        (claude / "settings.json").write_text(settings, encoding="utf-8")

    rep = spend.baseline(_scan(claude))

    assert rep["headless_split"] == "unavailable"
    assert rep["tokens"]["interactive"]["sessions"] == 2
    assert rep["tokens"]["headless"] is None


def test_a_settings_json_saved_with_a_byte_order_mark_is_still_read(claude):
    """PowerShell 5.1's `-Encoding utf8` writes one, and `json.loads` refuses
    a string that opens with it."""
    _headless_world(claude)
    settings = claude / "settings.json"
    settings.write_bytes(b"\xef\xbb\xbf" + settings.read_bytes())

    rep = spend.baseline(_scan(claude))

    assert rep["headless_split"] == "separate"
    assert rep["tokens"]["headless"]["sessions"] == 1


@pytest.mark.parametrize("where", ["settings", "plugin"])
def test_hook_maps_nested_past_the_recursion_limit_are_read_not_raised(claude, where):
    """`json.loads` follows nesting far deeper than a recursive walk over its
    result can (thousands of levels on Python 3.14), so a hook map that deep --
    in `settings.json` or in a plugin's `hooks.json` -- must be read, or
    skipped where this interpreter's parser stops first, never raised out of
    the report."""
    _headless_world(claude, register=where == "plugin")
    _plugins(claude)
    depth = 3 * sys.getrecursionlimit()
    command = MH_CMD if where == "settings" else SP_CMD
    nest = "[" * depth + json.dumps({"type": "command", "command": command}) + "]" * depth
    try:
        json.loads(nest)
        parsed = True
    except RecursionError:
        parsed = False
    target = claude / "settings.json" if where == "settings" else (
        claude / "plugins" / "cache" / "superpowers-dev" / "superpowers" / "5.1.0"
        / "hooks" / "hooks.json")
    target.write_text('{"hooks": {"SessionStart": ' + nest + "}}", encoding="utf-8")

    scan = _scan(claude)

    if where == "settings":
        split = spend.baseline(scan)["headless_split"]
        assert split == ("separate" if parsed else "unavailable")
    else:
        owner = "superpowers" if parsed else spend.UNATTRIBUTED
        assert _by(spend.hooks(scan)["health"], owner=owner,
                   event="SessionStart")["runs"] == 2


def test_a_loop_child_whose_mindhive_session_start_ran_stays_interactive(claude):
    """The split's boundary. The loop driver (`hive loop`, `hive fabricate run`,
    a loop-mode auto-resume) and Workspace board agents start `claude` with
    their parent's environment, so MindHive's hooks run in them and nothing in
    the transcript tells them from a typed session."""
    _register_mindhive(claude)
    _write(claude, "C--hl/loop.jsonl", [
        _hook(_t(minute=0), command=SP_CMD, content="s" * 40),
        _hook(_t(minute=0), command=MH_CMD, content="m" * 40),
        _prompt("# Loop goal\nship the release notes", _t(minute=0)),
        _call("l1", _t(minute=1), read=10), _call("l2", _t(minute=2), read=10)])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert (tokens["interactive"]["sessions"], tokens["headless"]) == (1, {"sessions": 0})


def test_hook_runs_of_other_events_say_nothing_about_who_started_a_session(claude):
    """CANARY for the event filter: a session whose only runs belong to other
    events recorded no SessionStart run at all, so MindHive's silence at its
    start is not in evidence."""
    _register_mindhive(claude)
    _write(claude, "C--hl/pre.jsonl", [
        _prompt("hi", _t(minute=0)),
        _hook(_t(minute=0), event="UserPromptSubmit", command=PV_CMD, content="p" * 40),
        _hook(_t(minute=1), kind="hook_non_blocking_error", event="PreToolUse",
              command=CACHE_CMD),
        _call("e1", _t(minute=1), read=10), _call("e2", _t(minute=2), read=10)])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert (tokens["interactive"]["sessions"], tokens["headless"]) == (1, {"sessions": 0})


def test_a_session_start_run_without_a_command_proves_nothing(claude):
    """A run whose owner cannot be read cannot show that MindHive's was
    missing: a transcript that records no command for any SessionStart run
    would otherwise file every typed session as headless."""
    _register_mindhive(claude)
    _write(claude, "C--hl/nocmd.jsonl", [
        _hook(_t(minute=0), command="", content="x" * 40),
        _prompt("hi", _t(minute=0)),
        _call("n1", _t(minute=1), read=10), _call("n2", _t(minute=2), read=10)])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert (tokens["interactive"]["sessions"], tokens["headless"]) == (1, {"sessions": 0})


def test_a_session_that_records_no_session_start_run_stays_interactive(claude):
    """Nothing in it shows that hooks ran at all, so MindHive's silence there
    says nothing about who started it."""
    _register_mindhive(claude)
    _write(claude, "C--hl/bare.jsonl", [
        _prompt("hi", _t(minute=0)),
        _call("b1", _t(minute=1), read=10), _call("b2", _t(minute=2), read=10)])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert (tokens["interactive"]["sessions"], tokens["headless"]) == (1, {"sessions": 0})


@pytest.mark.parametrize("kind", ["hook_cancelled", "hook_non_blocking_error"])
def test_a_mindhive_session_start_that_failed_still_ran(claude, kind):
    _register_mindhive(claude)
    _write(claude, "C--hl/s.jsonl", [
        _hook(_t(minute=0), command=SP_CMD, content="s"),
        _hook(_t(minute=0), kind=kind, command=MH_CMD),
        _prompt("hi", _t(minute=0)),
        _call("f1", _t(minute=1), read=10), _call("f2", _t(minute=2), read=10)])

    assert spend.baseline(_scan(claude))["tokens"]["interactive"]["sessions"] == 1


def test_a_mindhive_session_start_that_failed_open_on_stderr_still_ran(claude):
    """`hooks/session_start.*` fails open with one line on stderr and nothing on
    stdout. Claude Code 2.1.198's transcript filter drops a `hook_success` row
    only when its content, stdout AND stderr are all empty, so that row is kept
    -- empty content and stdout, the line in `stderr`, MindHive's command -- and
    the typed session it started stays interactive.

    CANARY in the same world: a session whose MindHive row is gone, which is
    what a failure that printed nothing left, is filed as headless. The two are
    told apart by their calls (10 against 700 context tokens each), so the
    assertion cannot pass with the sessions swapped."""
    _register_mindhive(claude)
    _write(claude, "C--hl/failed-open.jsonl", [
        _hook(_t(minute=0), command=SP_CMD, content="s"),
        _hook(_t(minute=0), command=MH_CMD, content="", stdout="",
              stderr="mindhive session_start: failed open: no Python interpreter resolved"),
        _prompt("hi", _t(minute=0)),
        _call("o1", _t(minute=1), read=10), _call("o2", _t(minute=2), read=10)])
    _write(claude, "C--hl/silent.jsonl", [
        _hook(_t(minute=10), command=SP_CMD, content="s"),
        _prompt("hi", _t(minute=10)),
        _call("q1", _t(minute=11), read=700), _call("q2", _t(minute=12), read=700)])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert tokens["interactive"]["sessions"] == 1
    assert tokens["interactive"]["ctx_tokens"] == 20
    assert tokens["headless"]["sessions"] == 1
    assert tokens["headless"]["ctx_tokens"] == 1400


@pytest.mark.parametrize("kind", ["chore", "subagent"])
def test_a_chore_or_a_subagent_keeps_its_kind_though_its_mindhive_hook_was_silent(
        claude, kind):
    """Chores are MindHive children too, and a subagent is nobody's typed
    session either; both keep the snapshot's own kind."""
    _register_mindhive(claude)
    rel, prompt = {"chore": ("C--hl/chore.jsonl", "You are the Resolution Engine. one"),
                   "subagent": ("C--hl/s1/subagents/agent-a1.jsonl", "explore")}[kind]
    _write(claude, rel, [
        _hook(_t(minute=0), command=SP_CMD, content="s"),
        _prompt(prompt, _t(minute=0)),
        _call("r1", _t(minute=1), read=10), _call("r2", _t(minute=2), read=10)])

    tokens = spend.baseline(_scan(claude))["tokens"]

    assert (tokens[kind]["sessions"], tokens["headless"]) == (1, {"sessions": 0})


def test_include_headless_counts_them_as_interactive_again(claude):
    _headless_world(claude)

    rep = spend.baseline(spend.scan(claude, days=7, now=NOW, include_headless=True))

    assert rep["headless_split"] == "folded"
    assert rep["tokens"]["interactive"]["sessions"] == 2
    assert rep["tokens"]["interactive"]["first_call_ctx_median"] == (5000 + 700) / 2
    assert rep["tokens"]["headless"] is None


@pytest.mark.parametrize("folded", [False, True])
def test_injection_and_lookups_leave_headless_sessions_out_unless_folded(claude, folded):
    _headless_world(claude)
    scan = spend.scan(claude, days=7, now=NOW, include_headless=folded)

    inj, look = spend.hooks(scan)["injection"], spend.lookups(scan)

    assert inj["sessions"] == look["sessions"] == (2 if folded else 1)
    assert look["lookup_calls"] == (2 if folded else 1)
    assert inj["ctx_tokens"] == (11400 if folded else 10000)


# --- 2. the window ------------------------------------------------------------

def test_days_keeps_sessions_that_started_inside_the_window(claude):
    _write(claude, "C--w/old.jsonl", [
        _prompt("old", "2026-09-20T08:00:00.000Z"),
        _call("o1", "2026-09-20T08:01:00.000Z", read=100),
        _call("o2", "2026-09-20T08:02:00.000Z", read=100),
    ])
    _write(claude, "C--w/new.jsonl", [
        _prompt("new", _t(day=2)),
        _call("w1", _t(day=2, minute=1), read=200),
        _call("w2", _t(day=2, minute=2), read=200),
    ])

    assert spend.baseline(_scan(claude, days=7))["tokens"]["interactive"]["ctx_tokens"] == 400
    assert spend.baseline(_scan(claude, days=None))["tokens"]["interactive"]["ctx_tokens"] == 600


def test_a_transcript_last_written_before_the_window_is_not_opened(claude):
    """A file whose mtime predates the cutoff cannot hold a call inside the
    window; skipping it unread is what keeps `--days 2` fast on a 2 GB tree."""
    old = (NOW - timedelta(days=30)).timestamp()
    _write(claude, "C--w/stale.jsonl", [
        _prompt("stale", _t(day=2)),
        _call("s1", _t(day=2, minute=1), read=100),
        _call("s2", _t(day=2, minute=2), read=100),
    ], mtime=old)

    scan = _scan(claude, days=7)

    assert scan.files_read == 0 and scan.files_skipped == 1
    assert spend.baseline(scan)["tokens"]["interactive"] == {"sessions": 0}
    assert _scan(claude, days=None).files_read == 1


SINCE = datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_since_keeps_sessions_that_started_at_or_after_the_moment(claude):
    """Fase 0.5 reads the sessions that started after the plugin diet, and a
    window counted back from now cannot be pinned to that moment. The session
    starting on the boundary is in: a `>` would drop it."""
    for name, ts in (("before", "2026-10-02T23:59:59.999Z"),
                     ("at", "2026-10-03T00:00:00.000Z"), ("after", _t(hour=8))):
        _write(claude, f"C--s/{name}.jsonl", [
            _prompt(name, ts), _call(name + "1", ts, read=100),
            _call(name + "2", ts, read=100)])

    scan = spend.scan(claude, since=SINCE, now=NOW)

    assert scan.since == "2026-10-03T00:00:00.000Z"
    assert spend.baseline(scan)["tokens"]["interactive"]["sessions"] == 2


def test_since_skips_a_transcript_last_written_before_it_unread(claude):
    _write(claude, "C--s/stale.jsonl", [
        _prompt("stale", _t(day=2)), _call("st1", _t(day=2), read=100),
        _call("st2", _t(day=2), read=100)], mtime=(SINCE - timedelta(hours=1)).timestamp())

    scan = spend.scan(claude, since=SINCE, now=NOW)

    assert (scan.files_read, scan.files_skipped) == (0, 1)


@pytest.mark.parametrize("since", [
    datetime(9999, 12, 31, 23, tzinfo=timezone(timedelta(hours=-5))),
    datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=5)))])
def test_a_since_past_the_calendar_s_ends_in_utc_is_refused(claude, since):
    """In UTC these are the years 10000 and 0. Read as no window at all, they
    reported every session on disk as the one asked for."""
    with pytest.raises(ValueError, match="since"):
        spend.scan(claude, since=since, now=NOW)


def test_a_days_window_reaching_past_the_calendar_reads_everything(claude):
    """CANARY beside the refused `since`: a window that long holds every
    session there is, so reading them all is the answer, not a stand-in."""
    _write(claude, "C--w/s.jsonl", [_prompt("hi", _t()), _call("y1", _t(), read=10),
                                    _call("y2", _t(), read=10)])

    for days in (1e12, 800000):                 # past timedelta; past year 1
        scan = spend.scan(claude, days=days, now=NOW)
        assert scan.since is None
        assert spend.baseline(scan)["tokens"]["interactive"]["sessions"] == 1


@pytest.mark.parametrize("since, start", [
    (datetime(2026, 10, 3), "2026-10-03T00:00:00.000Z"),
    (datetime(2026, 10, 3, 15, 0, 0, 999999, tzinfo=timezone(timedelta(hours=7))),
     "2026-10-03T08:00:00.999Z"),
    (datetime(1, 1, 1), "0001-01-01T00:00:00.000Z")])
def test_since_is_stamped_the_way_transcripts_are(claude, since, start):
    """A naive moment is UTC already, the clock transcripts are stamped in, and
    the start is written in their own shape so a string comparison orders it."""
    assert spend.scan(claude, since=since, now=NOW).since == start


def test_a_missing_claude_dir_is_an_empty_scan_not_a_raise(tmp_path):
    scan = spend.scan(tmp_path / "nowhere", days=7, now=NOW)

    assert scan.sessions == [] and scan.files_read == 0
    assert spend.baseline(scan)["tokens"]["interactive"] == {"sessions": 0}


def test_malformed_lines_are_skipped(claude):
    _write(claude, "C--m/s1.jsonl", [
        _prompt("hi", _t(minute=0)),
        '{"type":"assistant","message":{"id":"broken"',
        "not json at all",
        _call("k1", _t(minute=1), read=100),
        _call("k2", _t(minute=2), read=100),
    ])

    assert spend.baseline(_scan(claude))["tokens"]["interactive"]["ctx_tokens"] == 200


def test_rows_with_wrong_typed_ids_or_numbers_are_read_not_raised(claude):
    """Totality past the JSON parse: a row Claude Code would never write -- a
    list or an object where an id belongs, Infinity in a usage count -- is
    measured as far as it can be, and the rest of the transcript still counts."""
    odd_uuid = {**_call("w1", _t(minute=1), read=100), "uuid": ["not", "a", "str"]}
    odd_mid = _call("w2", _t(minute=2), read=200)
    odd_mid["message"]["id"] = {"odd": 1}             # falls back to the requestId
    odd_tool = _call("w3", _t(minute=3), read=300,
                     tools=[(["t", "x"], "Bash", {"command": 'hive recall "x"'})])
    odd_result = _result("t", "r", _t(minute=3))
    odd_result["message"]["content"][0]["tool_use_id"] = ["t", "x"]
    endless = _call("w4", _t(minute=4), read=400)
    endless["message"]["usage"]["input_tokens"] = float("inf")   # written as Infinity
    huge = json.dumps(_call("w5", _t(minute=5), read=500), separators=(",", ":")
                      ).replace('"output_tokens":0', '"output_tokens":1e999')
    _write(claude, "C--odd/s1.jsonl", [_prompt("hi", _t(minute=0)), odd_uuid, odd_mid,
                                       odd_tool, odd_result, endless, huge])

    scan = _scan(claude)

    tok = spend.baseline(scan)["tokens"]["interactive"]
    assert (tok["calls"], tok["ctx_tokens"]) == (5, 100 + 200 + 300 + 400 + 500)
    assert spend.lookups(scan)["lookup_calls"] == 1


def test_json_nested_past_the_parser_s_depth_is_skipped_not_raised(claude):
    """Past its depth limit `json.loads` raises RecursionError, which is not a
    ValueError: a transcript line, a hook's JSON answer and the plugin
    registry each meet it here."""
    deep = "[" * 100000 + "]" * 100000
    (claude / "plugins").mkdir()
    (claude / "plugins" / "installed_plugins.json").write_text(
        '{"plugins":' + deep + "}", encoding="utf-8")
    _write(claude, "C--d/s1.jsonl", [
        deep,
        _hook(_t(minute=0), command=MH_CMD, stdout='{"a":' * 100000 + "1" + "}" * 100000),
        _prompt("hi", _t(minute=0)),
        _call("d1", _t(minute=1), read=10), _call("d2", _t(minute=2), read=10)])

    scan = _scan(claude)

    assert scan.files_read == 1
    assert spend.baseline(scan)["tokens"]["interactive"]["ctx_tokens"] == 20
    assert _by(spend.hooks(scan)["health"], owner="mindhive")["runs"] == 1


class _FailingFile:
    """Yields one line, then fails the way a read can on Windows when another
    process truncates or locks the transcript mid-scan."""

    def __init__(self, line):
        self.line = line

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield self.line
        raise OSError(22, "Invalid argument")


def test_a_transcript_that_fails_mid_read_is_counted_unreadable(claude, monkeypatch):
    from mindhive import spend_scan
    path = _write(claude, "C--r/s1.jsonl", [
        _prompt("hi", _t(minute=0)),
        _call("f1", _t(minute=1), read=10), _call("f2", _t(minute=2), read=10)])
    first = path.read_text(encoding="utf-8").splitlines(keepends=True)[0]
    monkeypatch.setattr(spend_scan, "open", lambda *a, **k: _FailingFile(first),
                        raising=False)

    scan = _scan(claude)

    assert (scan.files_read, scan.files_unreadable, scan.sessions) == (0, 1, [])
    assert spend.baseline(scan)["source"]["transcripts_unreadable"] == 1


def test_a_transcript_that_cannot_be_opened_is_unreadable_not_read(claude, monkeypatch):
    """CANARY beside the mid-read case: the open itself failing was already
    total, but it was counted as a transcript read."""
    from mindhive import spend_scan
    _write(claude, "C--r/s1.jsonl", [_prompt("hi", _t(minute=0))])

    def refuse(*args, **kwargs):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(spend_scan, "open", refuse, raising=False)

    scan = _scan(claude)

    assert (scan.files_read, scan.files_unreadable) == (0, 1)


# --- 3. hooks: owner, health, injection ---------------------------------------

def _hooks_world(claude):
    _plugins(claude)
    sp_json = _context_json("SessionStart", "SUPERPOWERS-CONTEXT")
    _write(claude, "C--h/s1.jsonl", [
        _hook(_t(minute=0), command=SP_CMD, content="", stdout=sp_json, ms=400,
              name="SessionStart:startup"),
        _hook(_t(minute=0), kind="hook_additional_context",
              content=["SUPERPOWERS-CONTEXT"]),
        _hook(_t(minute=0), command=MH_CMD, ms=4000, name="SessionStart:startup",
              content="<persisted-output>\nOutput too large (31.5KB). Full output saved"),
        _prompt("go", _t(minute=1)),
        _hook(_t(minute=1), event="UserPromptSubmit", command=PV_CMD,
              content="p" * 40, ms=100),
        _hook(_t(minute=1), kind="hook_additional_context", event="UserPromptSubmit",
              content=["MINDHIVE-GATE-CONTEXT"]),
        _call("h1", _t(minute=2), read=1000),
        _hook(_t(minute=2), kind="hook_non_blocking_error", event="PreToolUse",
              name="PreToolUse:Bash", command=CACHE_CMD, ms=6000),
        _hook(_t(minute=2), kind="hook_cancelled", event="Stop", command=OTHER_CMD,
              ms=50),
        _call("h2", _t(minute=3), read=1000),
    ])


def test_hook_owner_comes_from_the_plugin_hook_maps_then_the_command(claude):
    _hooks_world(claude)

    health = spend.hooks(_scan(claude))["health"]

    assert _by(health, event="SessionStart", owner="superpowers")["runs"] == 1
    assert _by(health, event="UserPromptSubmit", owner="pensyve")["runs"] == 1
    assert _by(health, event="SessionStart", owner="mindhive")["runs"] == 1
    assert _by(health, event="PreToolUse", owner="protect-mcp")["errors"] == 1
    assert _by(health, event="Stop", owner="(unattributed)")["cancelled"] == 1


def test_hook_health_flags_output_too_large_and_times_the_runs(claude):
    _hooks_world(claude)

    health = spend.hooks(_scan(claude))["health"]

    mh = _by(health, event="SessionStart", owner="mindhive")
    assert (mh["output_too_large"], mh["ms_median"], mh["sessions"]) == (1, 4000, 1)
    assert _by(health, event="SessionStart", owner="superpowers")["output_too_large"] == 0


def test_additional_context_rows_are_not_counted_as_hook_runs(claude):
    """A hook that answers with JSON leaves a run row AND a context row; only
    the run is an execution."""
    _hooks_world(claude)

    health = spend.hooks(_scan(claude))["health"]

    assert sum(r["runs"] for r in health) == 5
    assert not [r for r in health if r["owner"] == "(no command)"]


def test_a_row_written_twice_under_one_uuid_is_one_hook_run(claude):
    """One transcript on this machine repeats 945 hook rows under uuids it
    already holds; the Fase 0.1 snapshot counted every copy."""
    run = _hook(_t(minute=0), event="UserPromptSubmit", command=PV_CMD,
                content="# prompt notes")
    twin = {**run, "uuid": _uid()}   # CANARY: the same body under its own uuid
    _write(claude, "C--h/s1.jsonl", [
        _prompt("hi", _t(minute=0)), run, run, twin,
        _call("d1", _t(minute=1), read=10), _call("d2", _t(minute=2), read=10)])

    rep = spend.hooks(_scan(claude))

    assert _by(rep["health"], event="UserPromptSubmit")["runs"] == 2
    assert _by(rep["injection"]["rows"], event="UserPromptSubmit")["fires"] == 2


def test_additional_context_takes_the_owner_of_the_run_that_returned_it(claude):
    _hooks_world(claude)

    rows = spend.hooks(_scan(claude))["injection"]["rows"]

    sp = _by(rows, event="SessionStart", owner="superpowers")
    assert sp["fires"] == 1 and sp["chars_median"] == len("SUPERPOWERS-CONTEXT")
    gate = _by(rows, event="UserPromptSubmit", owner="(no command)")
    assert gate["chars_median"] == len("MINDHIVE-GATE-CONTEXT")


def test_each_piece_of_one_context_row_goes_to_its_own_owner(claude):
    """61 real SessionStart rows hold two pieces: the harness's worktree note
    beside superpowers' context. Charging both to superpowers would bill a
    plugin for text it never wrote."""
    _plugins(claude)
    note = "You are operating in a git worktree."
    _write(claude, "C--h/s1.jsonl", [
        _hook(_t(minute=0), command=SP_CMD, content="",
              stdout=_context_json("SessionStart", "SUPERPOWERS-CONTEXT")),
        _hook(_t(minute=0), kind="hook_additional_context",
              content=[note, "SUPERPOWERS-CONTEXT"]),
        _prompt("go", _t(minute=1)),
        _call("w1", _t(minute=2), read=10), _call("w2", _t(minute=3), read=10),
    ])

    rows = spend.hooks(_scan(claude))["injection"]["rows"]

    assert _by(rows, owner="superpowers")["chars_median"] == len("SUPERPOWERS-CONTEXT")
    assert _by(rows, owner="(no command)")["chars_median"] == len(note)


def test_a_json_answer_is_not_counted_as_injected_text_twice(claude):
    """The run row of a JSON-answering hook has empty `content`; counting its
    stdout as well would charge the same context twice."""
    _hooks_world(claude)

    rows = spend.hooks(_scan(claude))["injection"]["rows"]

    assert sum(r["fires"] for r in rows if r["owner"] == "superpowers") == 1


def test_injected_text_is_charged_until_the_next_compaction(claude):
    """400 chars (100 tokens) before three calls, then compaction; 800 chars
    (200 tokens) after it, before two more calls: 100*3 + 200*2 = 700."""
    _write(claude, "C--i/s1.jsonl", [
        _hook(_t(minute=0), command=MH_CMD, content="a" * 400),
        _prompt("go", _t(minute=0)),
        _call("i1", _t(minute=1), read=1000),
        _hook(_t(minute=1), event="UserPromptSubmit", command=MH_CMD, content="b" * 40),
        _call("i2", _t(minute=2), read=1000),
        _call("i3", _t(minute=3), read=1000),
        _boundary(_t(minute=4)),
        _hook(_t(minute=4), command=MH_CMD, content="c" * 800,
              name="SessionStart:compact"),
        _call("i4", _t(minute=5), read=1000),
        _call("i5", _t(minute=6), read=1000),
    ])

    inj = spend.hooks(_scan(claude))["injection"]

    ss = _by(inj["rows"], event="SessionStart", owner="mindhive")
    assert (ss["fires"], ss["tokens"]) == (2, 700)
    ups = _by(inj["rows"], event="UserPromptSubmit", owner="mindhive")
    assert ups["tokens"] == 10 * 2
    assert inj["ctx_tokens"] == 5000
    assert inj["total_share"] == round(720 / 5000, 4)


def test_injection_is_measured_on_interactive_sessions_only(claude):
    _write(claude, "C--i/chore.jsonl", [
        _hook(_t(minute=0), command=MH_CMD, content="z" * 4000),
        _prompt("Break one software task into parts", _t(minute=0)),
        _call("q1", _t(minute=1), read=10),
        _call("q2", _t(minute=2), read=10),
    ])

    inj = spend.hooks(_scan(claude))["injection"]

    assert inj["rows"] == [] and inj["sessions"] == 0


def test_without_plugin_maps_the_command_rules_still_attribute(claude):
    """Totality: no installed_plugins.json at all. The cache-path and
    `mindhive` rules need no plugin map; an exact-match-only command falls to
    unattributed rather than raising."""
    _write(claude, "C--h/s1.jsonl", [
        _hook(_t(minute=0), command=MH_CMD, content="m"),
        _hook(_t(minute=0), command=SP_CMD, content="s"),
        _hook(_t(minute=0), kind="hook_non_blocking_error", event="PreToolUse",
              command=CACHE_CMD),
        _call("p1", _t(minute=1), read=10),
        _call("p2", _t(minute=2), read=10),
    ])
    (claude / "plugins").mkdir()
    (claude / "plugins" / "installed_plugins.json").write_text("{not json", encoding="utf-8")

    health = spend.hooks(_scan(claude))["health"]

    assert {r["owner"] for r in health} == {"mindhive", "(unattributed)", "protect-mcp"}


def test_an_install_path_that_is_not_a_string_is_ignored(claude):
    from mindhive import spend_scan
    _plugins(claude)
    registry = claude / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["plugins"]["odd@m"] = [{"installPath": ["a", "list"]}, {"installPath": 7},
                                {"installPath": ""}, "not a dict"]
    registry.write_text(json.dumps(data), encoding="utf-8")

    assert spend_scan.hook_owners(claude / "plugins") == {SP_CMD: "superpowers",
                                                         PV_CMD: "pensyve"}


def test_a_manifest_hooks_path_outside_the_plugin_is_not_followed(claude, tmp_path,
                                                                  monkeypatch):
    """A plugin.json `hooks` string names a file relative to the plugin root.
    An absolute path or a `..` step would read whatever the manifest names, and
    a UNC path would make `hive spend` open a connection to that share."""
    from mindhive import spend_scan
    elsewhere = tmp_path / "ELSEWHERE.json"
    root = claude / "plugins" / "cache" / "m" / "p" / "1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    for path, command in ((elsewhere, "ELSEWHERE-CMD"), (root / "inside.json", "INSIDE-CMD")):
        path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": command}]}]}}), encoding="utf-8")
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"hooks": [
        "./inside.json", str(elsewhere), "../" * 6 + "ELSEWHERE.json",
        "//attacker.invalid/share/h.json"]}), encoding="utf-8")
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"p@m": [{"installPath": str(root)}]}}), encoding="utf-8")
    asked, real = [], spend_scan._read_json

    def spy(path):
        asked.append(str(path).replace("\\", "/"))
        # The test itself must never reach for a share, whatever the code does.
        return None if asked[-1].startswith("//") else real(path)
    monkeypatch.setattr(spend_scan, "_read_json", spy)

    owners = spend_scan.hook_owners(claude / "plugins")

    assert owners == {"INSIDE-CMD": "p"}            # the relative file still counts
    assert not [a for a in asked if "ELSEWHERE" in a or "attacker.invalid" in a]


@pytest.mark.parametrize("command, owner", [
    (CACHE_CMD, "protect-mcp"),
    ('node "C:\\Users\\x\\.claude\\plugins\\cache\\m\\my-plugin_2.0\\h.js"', "my-plugin_2.0"),
    ("cd ~/.claude/plugins/cache/m/tail-end", "tail-end"),
    # A plugin segment that runs on into command text is not a plugin name:
    ("PLUGIN_DIR=~/.claude/plugins/cache/m/plug;KEY=TOPSECRET;node x.js", "(unattributed)"),
    ("X=~/.claude/plugins/cache/m/plug&&echo TOPSECRET", "(unattributed)"),
])
def test_the_cache_path_rule_takes_only_a_whole_plugin_name(command, owner):
    """The cache-path owner is printed, so it must be a name, never a slice of
    the command after it."""
    from mindhive import spend_scan
    assert spend_scan.hook_owner(command, {}) == owner


def test_output_too_large_is_read_off_the_persisted_output_wrapper(claude):
    """Claude Code swaps an oversized hook output for a `<persisted-output>`
    preview. Searching the whole raw row also counted a hook that merely
    quoted the phrase, or an error whose stderr carried it."""
    quoting = _hook(_t(minute=0), command=MH_CMD,
                    content="notes: the Output too large (31.5KB) bug is fixed")
    failing = _hook(_t(minute=0), kind="hook_non_blocking_error", command=MH_CMD)
    failing["attachment"]["stderr"] = "Output too large"
    _write(claude, "C--t/s1.jsonl", [
        quoting, failing,
        _hook(_t(minute=0), command=MH_CMD,
              content="<persisted-output>\nOutput too large (31.5KB). Full output saved"),
        _prompt("go", _t(minute=1)),
        _call("t1", _t(minute=2), read=10), _call("t2", _t(minute=3), read=10)])

    mh = _by(spend.hooks(_scan(claude))["health"], owner="mindhive", event="SessionStart")

    assert (mh["runs"], mh["output_too_large"]) == (3, 1)


# --- 4. lookups ---------------------------------------------------------------

@pytest.mark.parametrize("name, inp, kind", [
    ("Bash", {"command": 'hive recall "budget" --semantic'}, "hive recall"),
    ("PowerShell", {"command": "& C:/v/hive.exe pg tool get_current_state"}, "hive pg"),
    ("Bash", {"command": "python -m mindhive sop show execution-discipline"}, "hive sop"),
    ("Read", {"file_path": "C:\\Users\\x\\.mindhive\\mindhive\\STATE.md"}, "read store"),
    ("Read", {"file_path": "C:/repo/MEMORY.md"}, "read state/memory md"),
    ("Grep", {"path": "C:/Users/x/.mindhive/mindhive/LEDGER", "pattern": "x"}, "grep store"),
    ("Bash", {"command": "git status"}, None),
    ("Read", {"file_path": "C:/repo/src/mindhive/spend.py"}, None),
    ("Bash", {"command": "echo archive recall"}, None),
])
def test_classify_lookup(name, inp, kind):
    assert spend.classify_lookup({"type": "tool_use", "name": name, "input": inp}) == kind


def _lookup_world(claude):
    _write(claude, "C--l/s1.jsonl", [
        _prompt("where were we", _t(minute=0)),
        _call("l1", _t(minute=1), read=1000,
              tools=[("t1", "Bash", {"command": 'hive recall "budget"'})]),
        _result("t1", "r" * 400, _t(minute=1)),
        _call("l2", _t(minute=2), read=2000,
              tools=[("t2", "Read", {"file_path": "C:/Users/x/.mindhive/mindhive/STATE.md"}),
                     ("t3", "Bash", {"command": "git status"})]),
        _result("t2", "s" * 800, _t(minute=2)),
        _result("t3", "g" * 4000, _t(minute=2)),
        _call("l3", _t(minute=3), read=3000),
        _boundary(_t(minute=4)),
        _call("l4", _t(minute=5), read=4000,
              tools=[("t4", "Grep", {"path": "C:/Users/x/.mindhive/mindhive/LEDGER"}),
                     ("t5", "PowerShell", {"command": "hive.exe pg tool get_current_state"})]),
        _call("l5", _t(minute=6), read=1000),
    ])


def test_lookup_calls_and_their_share_of_context(claude):
    _lookup_world(claude)

    rep = spend.lookups(_scan(claude))

    assert (rep["sessions"], rep["calls"], rep["ctx_tokens"]) == (1, 5, 11000)
    assert rep["lookup_calls"] == 3
    assert rep["lookup_call_pct"] == 60.0
    assert rep["lookup_ctx_share"] == round(7000 / 11000, 4)
    assert rep["by_kind"] == {"hive recall": 1, "read store": 1, "grep store": 1,
                              "hive pg": 1}
    assert rep["per_session_median"] == 3


def test_lookup_results_are_charged_until_the_next_compaction(claude):
    """400 chars after call 1 and 800 after call 2, compaction after call 3:
    100*2 + 200*1 = 400 tokens. The non-lookup result is not charged."""
    _lookup_world(claude)

    rep = spend.lookups(_scan(claude))

    assert rep["result_tokens"] == 400
    assert rep["result_share"] == round(400 / 11000, 4)


# --- 5. agents ----------------------------------------------------------------

def test_agents_group_subagent_spend_by_the_recorded_agent_type(claude):
    a = _write(claude, "C--g/s1/subagents/agent-a1.jsonl", [
        _call("a1", _t(minute=1), read=700), _call("a2", _t(minute=2), read=800)])
    _meta(a, "Explore")
    b = _write(claude, "C--g/s1/subagents/workflows/wf_1/agent-b1.jsonl", [
        _call("b1", _t(minute=1), read=100), _call("b2", _t(minute=2), read=200),
        _call("b3", _t(minute=3), read=300)])
    _meta(b, "workflow-subagent")
    _write(claude, "C--g/s1/subagents/agent-c1.jsonl", [
        _call("c1", _t(minute=1), read=50), _call("c2", _t(minute=2), read=50)])
    d = _write(claude, "C--g/s1/subagents/agent-d1.jsonl", [
        _call("d1", _t(minute=1), read=10), _call("d2", _t(minute=2), read=10)])
    d.with_name(d.stem + ".meta.json").write_text("{broken", encoding="utf-8")

    rep = spend.agents(_scan(claude))

    assert [r["agent_type"] for r in rep["rows"]] == [
        "Explore", "workflow-subagent", "(unknown)"]
    unknown = rep["rows"][-1]
    assert (unknown["sessions"], unknown["ctx_tokens"]) == (2, 120)
    assert _by(rep["rows"], agent_type="workflow-subagent")["first_call_ctx_median"] == 100
    assert rep["total"]["ctx_tokens"] == 1500 + 600 + 120


# --- 6. the snapshot comparison -----------------------------------------------

def _snapshot(name="spend_baseline_2026-10-03.json", median=108037.0):
    state = paths.project_dir() / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / name
    path.write_text(json.dumps({
        "schema": "mindhive.spend_baseline/1", "created": "2026-10-03T08:55:56+00:00",
        "tokens": {"interactive": {"sessions": 44, "first_call_ctx_median": median,
                                   "first_call_ctx_p90": 133346,
                                   "ctx_per_call_mean": 252239,
                                   "fixed_baseline_share": 0.3528},
                   "chore": {"sessions": 5}, "subagent": {"sessions": 1379}},
        "hooks": [{"owner": "mindhive(settings.json)", "event": "SessionStart",
                   "runs": 336, "errors": 0, "cancelled": 0, "ms_median": 4351.0,
                   "output_too_large": 336, "sessions": 84},
                  {"owner": "protect-mcp", "event": "PreToolUse", "runs": 57888,
                   "errors": 57887, "cancelled": 1, "ms_median": 5982.0,
                   "output_too_large": 0, "sessions": 1242}]}), encoding="utf-8")
    return path


def test_the_newest_snapshot_in_the_state_dir_is_found_by_default():
    _snapshot("spend_baseline_2026-09-01.json")
    newest = _snapshot("spend_baseline_2026-10-03.json")

    assert spend.find_snapshot() == newest


def test_find_snapshot_answers_none_when_there_is_none():
    assert spend.find_snapshot() is None
    assert not (paths.project_dir() / "state").exists(), "finding must not mkdir"


def test_baseline_reads_against_the_snapshot(claude):
    _baseline_world(claude)
    snap = spend.load_snapshot(_snapshot())

    cmp = spend.compare_baseline(spend.baseline(_scan(claude)), snap)

    row = cmp["interactive"]["first_call_ctx_median"]
    assert row == {"snapshot": 108037.0, "now": 2000, "delta": 2000 - 108037.0}


@pytest.mark.parametrize("held, noted", [("absent", True), (None, True),
                                         ({"sessions": 3}, False)])
def test_the_split_note_asks_whether_the_snapshot_measured_headless_sessions(
        claude, held, noted):
    """A snapshot whose headless row is missing or not measured (null) counted
    those sessions as interactive, so a split run does not read like for like."""
    _headless_world(claude)
    snap = spend.load_snapshot(_snapshot())
    if held != "absent":
        snap["tokens"]["headless"] = held

    note = spend.baseline_note(spend.baseline(_scan(claude)), snap)

    assert (note is not None) == noted


def test_hooks_read_against_the_snapshot_by_rate(claude):
    _hooks_world(claude)
    snap = spend.load_snapshot(_snapshot())

    cmp = spend.compare_hooks(spend.hooks(_scan(claude)), snap)

    mh = _by(cmp, owner="mindhive", event="SessionStart")
    assert mh["too_large_rate"] == {"snapshot": 1.0, "now": 1.0}
    pm = _by(cmp, owner="protect-mcp", event="PreToolUse")
    assert pm["error_rate"] == {"snapshot": round(57887 / 57888, 4), "now": 1.0}


def test_a_hook_family_that_stopped_firing_reads_as_zero_runs(claude):
    """The diet's own success case: protect-mcp is in the snapshot and absent
    from the new window, so the comparison must say `0 runs`, not drop it."""
    _write(claude, "C--h/s1.jsonl", [
        _call("z1", _t(minute=1), read=10), _call("z2", _t(minute=2), read=10)])
    snap = spend.load_snapshot(_snapshot())

    cmp = spend.compare_hooks(spend.hooks(_scan(claude)), snap)

    pm = _by(cmp, owner="protect-mcp", event="PreToolUse")
    assert pm["runs"] == {"snapshot": 57888, "now": 0}


@pytest.mark.parametrize("body", ["{not json", json.dumps({"schema": "other/1"}),
                                  json.dumps(["a", "list"])])
def test_load_snapshot_refuses_what_is_not_a_spend_baseline(tmp_path, body):
    path = tmp_path / "s.json"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(spend.SnapshotError):
        spend.load_snapshot(path)


def test_load_snapshot_refuses_a_missing_file(tmp_path):
    with pytest.raises(spend.SnapshotError):
        spend.load_snapshot(tmp_path / "absent.json")


# --- 7. namespaces --------------------------------------------------------------

@pytest.mark.parametrize("pinned", [None, "pinned"])
def test_baseline_attributes_sessions_to_mindhive_namespaces(claude, monkeypatch, pinned):
    """Each folder is placed by its own recorded cwd. MINDHIVE_PROJECT pins the
    namespace this process writes to; read as a placement it folded every
    folder on the machine into that one row."""
    if pinned:
        monkeypatch.setenv("MINDHIVE_PROJECT", pinned)
    else:
        monkeypatch.delenv("MINDHIVE_PROJECT", raising=False)
    for d, cwd in (("C--alpha", "C:/nowhere/alpha"), ("C--beta", "C:/nowhere/beta")):
        _write(claude, f"{d}/s1.jsonl", [
            {**_prompt("hi", _t(minute=0)), "cwd": cwd},
            _call(d + "1", _t(minute=1), read=100),
            _call(d + "2", _t(minute=2), read=300 if d == "C--beta" else 100),
        ])

    rows = spend.baseline(_scan(claude))["by_namespace"]

    assert [(r["namespace"], r["ctx_tokens"]) for r in rows] == [
        ("beta", 400), ("alpha", 200)]


# --- 8. the CLI -------------------------------------------------------------------

def _cli(*argv):
    return cli_mod.main(["spend", *argv])


def test_cli_baseline_json_carries_the_comparison(claude, capsys):
    _baseline_world(claude)
    _snapshot()

    code = _cli("baseline", "--claude-dir", str(claude), "--all", "--json")

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["schema"] == spend.SCHEMA and out["report"] == "baseline"
    assert out["tokens"]["interactive"]["sessions"] == 2
    assert out["against"]["path"].endswith("spend_baseline_2026-10-03.json")
    assert "first_call_ctx_median" in out["against"]["deltas"]["interactive"]


def test_cli_text_comparison_prints_shares_as_percentages_and_counts_whole(claude, capsys):
    """The first run on the real store printed the share at one decimal
    (`0.4 -> 0.5`) and the token deltas in exponent form (`+1.149e+04`)."""
    _baseline_world(claude)
    _snapshot()

    assert _cli("baseline", "--claude-dir", str(claude), "--all") == 0

    text = capsys.readouterr().out
    assert "fixed_baseline_share 35.28% -> 91.67% (+56.39 pp)" in text
    assert "first_call_ctx_median 108,037 -> 2,000 (-106,037)" in text
    assert "e+0" not in text and "e-0" not in text


def _garble(path, how):
    """The snapshot's schema name is right; what sits under it is not."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if how == "fields":
        data["tokens"]["interactive"].update(first_call_ctx_median={"odd": 1},
                                             fixed_baseline_share="n/a")
        data["tokens"]["chore"] = ["not", "a", "dict"]
        data["hooks"] += [{"owner": ["a", "list"], "event": "PreToolUse", "runs": 3},
                          {"owner": "protect-mcp", "event": "PostToolUse",
                           "runs": "many", "errors": None},
                          "not a row"]
    else:
        data.update(tokens=["a", "list"], hooks=7)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("how", ["fields", "containers"])
@pytest.mark.parametrize("report", ["baseline", "hooks"])
def test_cli_a_snapshot_of_the_wrong_shape_reads_as_absent_not_a_crash(
        claude, capsys, report, how):
    """`load_snapshot` checks the schema name only, and a snapshot is a
    hand-written file: a field of the wrong shape reads as absent."""
    _baseline_world(claude)
    _hooks_world(claude)
    _garble(_snapshot(), how)

    for extra in ((), ("--json",)):
        assert _cli(report, "--claude-dir", str(claude), "--all", *extra) == 0
        assert "against" in capsys.readouterr().out


def test_cli_no_compare_leaves_the_snapshot_alone(claude, capsys):
    _baseline_world(claude)
    _snapshot()

    assert _cli("baseline", "--claude-dir", str(claude), "--all", "--json",
                "--no-compare") == 0
    assert "against" not in json.loads(capsys.readouterr().out)


def test_cli_an_unreadable_explicit_snapshot_is_exit_2(claude, tmp_path, capsys):
    _baseline_world(claude)

    code = _cli("baseline", "--claude-dir", str(claude), "--against",
                str(tmp_path / "absent.json"))

    assert code == 2
    assert "absent.json" in capsys.readouterr().err


def test_cli_an_empty_against_is_exit_2_not_the_default_snapshot(claude, capsys):
    """`--against ""` used to fall through to the newest snapshot and print a
    comparison the caller never asked for."""
    _baseline_world(claude)
    _snapshot()

    code = _cli("baseline", "--claude-dir", str(claude), "--all", "--against", "")

    captured = capsys.readouterr()
    assert code == 2
    assert "--against" in captured.err and "against" not in captured.out


def test_cli_an_unreadable_default_snapshot_is_warned_about_not_fatal(claude, capsys):
    """CANARY beside the explicit case: the same unreadable file, found rather
    than named, warns and prints the report without a comparison."""
    _baseline_world(claude)
    _snapshot().write_text("{not json", encoding="utf-8")

    code = _cli("baseline", "--claude-dir", str(claude), "--all")

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err.startswith("hive spend: warning:")
    assert "against" not in captured.out and "interactive" in captured.out


@pytest.mark.parametrize("given", ["missing", "a file", "empty"])
def test_cli_an_explicit_claude_dir_without_transcripts_is_exit_2(tmp_path, capsys,
                                                                 monkeypatch, given):
    """A mistyped --claude-dir printed an all-zero report and exit 0, and an
    empty one silently read the default home instead."""
    from mindhive import spend_scan
    default = tmp_path / "default-home"
    (default / "projects").mkdir(parents=True)
    monkeypatch.setattr(spend_scan, "default_claude_dir", lambda: default)
    target = tmp_path / "typo"
    if given == "a file":
        target.write_text("x", encoding="utf-8")

    code = _cli("baseline", "--claude-dir", "" if given == "empty" else str(target),
                "--all", "--no-compare")

    assert code == 2
    assert "no Claude Code transcripts" in capsys.readouterr().err


def test_cli_days_must_be_positive(claude):
    assert _cli("hooks", "--claude-dir", str(claude), "--days", "0") == 2


@pytest.mark.parametrize("given, since, sessions", [
    ("2026-10-03", "2026-10-03T00:00:00.000Z", 2),
    ("2026-10-03T08:00:00Z", "2026-10-03T08:00:00.000Z", 1),
    ("2026-10-03T15:00:00+07:00", "2026-10-03T08:00:00.000Z", 1),
    ("2026-10-03T08:00", "2026-10-03T08:00:00.000Z", 1),
])
def test_cli_since_takes_a_utc_date_or_an_iso_time(claude, capsys, given, since, sessions):
    """A bare date is midnight UTC, the clock transcripts are stamped in; a time
    without an offset is read as UTC too."""
    for name, ts in (("early", _t(hour=7, minute=59)), ("late", _t(hour=8))):
        _write(claude, f"C--s/{name}.jsonl", [
            _prompt(name, ts), _call(name + "1", ts, read=100),
            _call(name + "2", ts, read=100)])

    code = _cli("baseline", "--claude-dir", str(claude), "--since", given, "--json",
                "--no-compare")

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["since"] == since
    assert out["tokens"]["interactive"]["sessions"] == sessions


@pytest.mark.parametrize("given", ["yesterday", "", "2026-13-01", "03-10-2026",
                                   "9999-12-31T23:00:00-05:00"])
def test_cli_a_since_that_is_not_a_date_is_exit_2(claude, capsys, given):
    """The last one parses, then overflows: in UTC it is the year 10000."""
    _baseline_world(claude)

    code = _cli("baseline", "--claude-dir", str(claude), "--since", given, "--no-compare")

    captured = capsys.readouterr()
    assert code == 2
    assert "--since" in captured.err and captured.out == ""


def test_cli_baseline_notes_that_the_snapshot_predates_the_headless_split(claude, capsys):
    """The snapshot's interactive row held MindHive's children too. Read
    against it, a typed-only row is not like for like, and the report says so;
    --include-headless restores the snapshot's own split."""
    _headless_world(claude)
    _snapshot()

    assert _cli("baseline", "--claude-dir", str(claude), "--all") == 0
    text = capsys.readouterr().out
    assert "headless" in text and "predates the headless split" in text

    assert _cli("baseline", "--claude-dir", str(claude), "--all", "--json",
                "--include-headless") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["headless_split"] == "folded" and "note" not in out["against"]
    assert out["tokens"]["interactive"]["sessions"] == 2
    assert _cli("baseline", "--claude-dir", str(claude), "--all", "--json") == 0
    assert "predates the headless split" in json.loads(capsys.readouterr().out)[
        "against"]["note"]


@pytest.mark.parametrize("mode", ["separate", "folded", "unavailable"])
def test_cli_baseline_says_how_it_counted_headless_sessions(claude, capsys, mode):
    """A kind that was not measured is null, never 0: a measured zero reads as
    `no headless sessions`, and against the snapshot as a change. The head
    says which way the report counted them, and why."""
    _headless_world(claude, register=mode != "unavailable")
    _snapshot()
    argv = ("baseline", "--claude-dir", str(claude), "--all",
            *(("--include-headless",) if mode == "folded" else ()))

    assert _cli(*argv, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert _cli(*argv) == 0
    text = capsys.readouterr().out

    assert out["headless_split"] == mode
    rows = [line.split() for line in text.splitlines() if line.startswith("headless")]
    said = {"separate": "headless children counted apart",
            "folded": "headless children counted as interactive (--include-headless)",
            "unavailable": "settings.json holds no readable MindHive SessionStart hook"}
    assert said[mode] in text
    if mode == "separate":
        assert out["against"]["deltas"]["headless"]["sessions"] == {
            "snapshot": None, "now": 1, "delta": None}
        assert [row[:2] for row in rows] == [["headless", "1"]]
    else:
        assert out["tokens"]["headless"] is None
        assert out["against"]["deltas"]["headless"] == {} and "note" not in out["against"]
        assert rows == []


@pytest.mark.parametrize("report", ["hooks", "lookups"])
def test_cli_include_headless_reaches_the_interactive_only_reports(claude, capsys, report):
    """... and the text says which way they were counted."""
    _headless_world(claude)
    sessions = {"hooks": lambda r: r["injection"]["sessions"],
                "lookups": lambda r: r["sessions"]}[report]

    counts = []
    for extra, said in (((), "counted apart"),
                        (("--include-headless",), "counted as interactive")):
        assert _cli(report, "--claude-dir", str(claude), "--all", "--json", *extra) == 0
        counts.append(sessions(json.loads(capsys.readouterr().out)))
        assert _cli(report, "--claude-dir", str(claude), "--all", *extra) == 0
        assert "headless children " + said in capsys.readouterr().out

    assert counts == [1, 2]


@pytest.mark.parametrize("report", ["baseline", "hooks", "lookups", "agents"])
def test_cli_every_report_runs_in_text_and_json(claude, capsys, report):
    """Every report closes the way the repo's read-only reports do, and the two
    that estimate tokens from characters say so, in both forms. Every one
    takes `--since`; all but `agents`, which counts subagents only, say how
    they counted headless sessions."""
    _baseline_world(claude)
    _hooks_world(claude)
    _lookup_world(claude)
    estimates = report in ("hooks", "lookups")

    assert _cli(report, "--claude-dir", str(claude), "--all") == 0
    text = capsys.readouterr().out
    assert text.rstrip().endswith("This command is read-only and changed nothing.")
    assert ("are estimates (~4 chars/token)" in text) == estimates
    assert _cli(report, "--claude-dir", str(claude), "--all", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["report"] == report
    assert out.get("chars_per_token") == (4 if estimates else None)
    assert ("headless_split" in out) == (report != "agents")
    assert _cli(report, "--claude-dir", str(claude), "--since", "2026-10-03", "--json") == 0
    assert json.loads(capsys.readouterr().out)["since"] == "2026-10-03T00:00:00.000Z"


def test_cli_help_states_the_exit_codes_and_the_json_contract(capsys):
    """An agent reads `--help`, not this repo's README, to learn what a field
    means and what an exit code says."""
    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["spend", "baseline", "--help"])

    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "exit codes" in out and "headless_split" in out and "null" in out


@pytest.mark.parametrize("report", ["baseline", "hooks", "lookups", "agents"])
def test_no_report_prints_a_prompt_a_command_or_hook_text(claude, capsys, report):
    """Aggregates only: what a session typed, ran or was handed by a hook --
    the hook commands `settings.json` registers included -- and what a
    subagent was asked never reaches the output, in either form. Each JSON leg
    is first shown to have measured the planted rows; a report that saw
    nothing would pass this vacuously."""
    _plugins(claude)
    _register_mindhive(claude, command=MH_CMD + " TOPSECRET-SETTINGS")
    leaky = "PLUGIN_DIR=~/.claude/plugins/cache/mkt/plug;KEY=TOPSECRET-CMD2;node x.js"
    _write(claude, "C--p/s1.jsonl", [
        _hook(_t(minute=0), command=OTHER_CMD + " TOPSECRET-HOOK",
              content="TOPSECRET-INJECTED"),
        _hook(_t(minute=0), command=MH_CMD + " TOPSECRET-SETTINGS",
              content="TOPSECRET-INJECTED3"),
        _hook(_t(minute=0), event="UserPromptSubmit", command=leaky,
              content="TOPSECRET-INJECTED2"),
        _prompt("TOPSECRET-PROMPT", _t(minute=0)),
        _call("p1", _t(minute=1), read=10,
              tools=[("t1", "Bash", {"command": "hive recall TOPSECRET-CMD"})]),
        _result("t1", "TOPSECRET-RESULT", _t(minute=1)),
        _call("p2", _t(minute=2), read=10),
    ])
    sub = _write(claude, "C--p/s1/subagents/agent-a1.jsonl", [
        _prompt("TOPSECRET-SUBAGENT-PROMPT", _t(minute=3)),
        _call("q1", _t(minute=4), read=10), _call("q2", _t(minute=5), read=10)])
    sub.with_name(sub.stem + ".meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "TOPSECRET-DESCRIPTION"}), encoding="utf-8")
    measured = {"baseline": lambda r: r["tokens"]["interactive"]["sessions"] == 1,
                "hooks": lambda r: sum(row["runs"] for row in r["health"]) == 3,
                "lookups": lambda r: r["lookup_calls"] == 1,
                "agents": lambda r: r["total"]["sessions"] == 1}

    for extra in (("--json",), ()):
        assert _cli(report, "--claude-dir", str(claude), "--all", *extra) == 0
        out = capsys.readouterr().out
        assert "TOPSECRET" not in out
        if extra:
            assert measured[report](json.loads(out))
