# tests/test_harvest.py
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mindhive import __main__ as cli, harvest, hookcapture, events, paths

REPO = Path(__file__).resolve().parents[1]


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _tooluse(name, inp):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _write_transcript(dirpath, sid, lines):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_harvest_captures_edits_and_commands(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"}),
                                   _tooluse("Bash", {"command": "ls"})])
    r = harvest.harvest(tdir, "test")
    assert r["new_events"] == 2
    types = [e.type for e in events.read_all("test")]
    assert types.count("file_touched") == 1 and types.count("command_run") == 1


def _usermsg(text):
    return json.dumps({"type": "user", "message": {"content": text}})


def test_harvest_stores_full_memo_without_truncation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    long_memo = "x " * 400 + "tail"               # ~800 chars, well past the old 300 cap
    _write_transcript(tdir, "s1", [_usermsg("memo: " + long_memo)])
    harvest.harvest(tdir, "test")
    memos = [e for e in events.read_all("test") if e.type in events.SEMANTIC_TYPES]
    assert len(memos) == 1
    assert memos[0].payload["text"] == long_memo  # full text, nothing cut off


def test_harvest_is_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"})])
    harvest.harvest(tdir, "test")
    r2 = harvest.harvest(tdir, "test")            # cursor at end -> nothing new
    assert r2["new_events"] == 0
    assert len(events.read_all("test")) == 1


def test_skip_captured_avoids_double_counting(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # simulate a LIVE-HOOK capture of session s1 (no 'harvest' tag)
    events.append_event("file_touched", {"text": "a.py"}, session_id="s1", tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"})])   # same action in transcript
    _write_transcript(tdir, "s2", [_tooluse("Bash", {"command": "ls"})])
    r = harvest.harvest(tdir, "test", skip_captured=True)
    assert r["skipped_captured"] == 1
    s1 = [e for e in events.read_all("test") if e.session_id == "s1"]
    assert len(s1) == 1 and "harvest" not in (s1[0].tags or [])   # not re-harvested
    s2 = [e for e in events.read_all("test") if e.session_id == "s2"]
    assert len(s2) == 1 and "harvest" in s2[0].tags               # the missed one IS harvested


def test_partial_capture_backfills_missed_events(tmp_path, monkeypatch):
    """A session the hooks captured only PARTIALLY (e.g. hook crashed after the
    first tool call) must still get its missed events backfilled — not be
    fast-forwarded past forever."""
    _isolate(tmp_path, monkeypatch)
    # Hook captured only the FIRST of two tool calls in session s1.
    events.append_event("file_touched", {"text": "a.py"}, session_id="s1",
                        tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"}),   # already captured
                                   _tooluse("Edit", {"file_path": "b.py"})])  # missed by the hook
    harvest.harvest(tdir, "test", skip_captured=True)
    s1 = [e for e in events.read_all("test") if e.session_id == "s1"]
    paths_touched = sorted(e.payload["text"] for e in s1)
    assert paths_touched == ["a.py", "b.py"]          # a.py once (not doubled), b.py backfilled
    # The backfilled one carries the harvest tag; the original hook one does not.
    b = next(e for e in s1 if e.payload["text"] == "b.py")
    assert "harvest" in b.tags


def test_partial_capture_backfill_is_idempotent(tmp_path, monkeypatch):
    """Re-running harvest after a partial-capture backfill adds nothing more."""
    _isolate(tmp_path, monkeypatch)
    events.append_event("file_touched", {"text": "a.py"}, session_id="s1",
                        tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"}),
                                   _tooluse("Edit", {"file_path": "b.py"})])
    harvest.harvest(tdir, "test", skip_captured=True)
    before = len(events.read_all("test"))
    r2 = harvest.harvest(tdir, "test", skip_captured=True)
    assert r2["new_events"] == 0
    assert len(events.read_all("test")) == before


# --- user_prompt capture (every typed prompt enters memory) ---

def test_every_plain_prompt_becomes_user_prompt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("how do I run the tests?")])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert len(prompts) == 1
    assert prompts[0].payload["text"] == "how do I run the tests?"
    assert "prompt" in prompts[0].tags


def test_marker_prompt_yields_both_memo_and_prompt(tmp_path, monkeypatch):
    """A prompt with a marker still produces its typed-decision memo AND the
    full prompt event — every prompt enters, markers add the curated memo."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("decision: ship the queue")])
    harvest.harvest(tdir, "test")
    types = sorted(e.type for e in events.read_all("test"))
    assert types == ["decision", "user_prompt"]


def test_slash_command_args_captured_as_prompt(tmp_path, monkeypatch):
    """For a slash command, the user's real input lives in <command-args>."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    msg = ("<command-message>x</command-message>\n"
           "<command-name>/foo</command-name>\n"
           "<command-args>please refactor the parser</command-args>")
    _write_transcript(tdir, "s1", [_usermsg(msg)])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert len(prompts) == 1
    assert prompts[0].payload["text"] == "please refactor the parser"


def test_injected_skill_body_not_captured_as_prompt(tmp_path, monkeypatch):
    """Skill bodies are loaded as user-role messages but are not prompts."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("Base directory for this skill: C:/x\n\nsome skill body text")])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert prompts == []


def test_system_reminder_stripped_from_prompt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1",
                      [_usermsg("real question here\n<system-reminder>injected noise</system-reminder>")])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert len(prompts) == 1
    assert prompts[0].payload["text"] == "real question here"


def test_tool_result_user_message_not_captured(tmp_path, monkeypatch):
    """A user-role message that is a tool_result (no text) is not a prompt."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    line = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "ok"}]}})
    _write_transcript(tdir, "s1", [line])
    harvest.harvest(tdir, "test")
    prompts = [e for e in events.read_all("test") if e.type == "user_prompt"]
    assert prompts == []


def _tooluse_at(ts, name, inp):
    return json.dumps({"type": "assistant", "timestamp": ts,
                       "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _usermsg_at(ts, text):
    return json.dumps({"type": "user", "timestamp": ts, "message": {"content": text}})


# --- marker classification (item 3) ---

def test_explicit_decision_not_reclassified_as_caveat(tmp_path, monkeypatch):
    """An explicit `decision:` marker must stay a decision even when the text
    contains a caveat-hint word like 'avoid'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("decision: avoid the global lock, use a queue")])
    harvest.harvest(tdir, "test")
    # the marker yields a memo (+ a user_prompt for the raw prompt); the memo
    # must be a decision, NOT reclassified to caveat by the 'avoid' hint word.
    memos = [e for e in events.read_all("test") if e.type in ("decision", "caveat")]
    assert len(memos) == 1
    assert memos[0].type == "decision"


def test_explicit_caveat_marker_is_caveat(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("caveat: the cache can go stale")])
    harvest.harvest(tdir, "test")
    assert events.read_all("test")[0].type == "caveat"


def test_marker_picked_by_textual_order_not_list_order(tmp_path, monkeypatch):
    """When several markers appear, the FIRST one in the text wins — here
    `decision:` precedes `memo:` even though `memo:` is earlier in _MARKERS."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("decision: ship the queue. memo: trivial note")])
    harvest.harvest(tdir, "test")
    ev = events.read_all("test")[0]
    assert ev.type == "decision"
    assert ev.payload["text"].startswith("ship the queue")


def test_ambiguous_memo_with_hint_is_caveat(tmp_path, monkeypatch):
    """remember:/memo: are ambiguous, so a hint word still infers a caveat."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("memo: this broke prod last night")])
    harvest.harvest(tdir, "test")
    assert events.read_all("test")[0].type == "caveat"


def test_ambiguous_memo_without_hint_is_decision(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("memo: we will adopt the new layout")])
    harvest.harvest(tdir, "test")
    assert events.read_all("test")[0].type == "decision"


# --- timestamp integrity (item 9) ---

def test_harvest_uses_transcript_timestamp(tmp_path, monkeypatch):
    """The event ts must come from the transcript line, normalized to second
    resolution — NOT the wall-clock time the harvest script happens to run."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse_at("2026-06-20T09:15:30.500Z", "Edit", {"file_path": "a.py"})])
    harvest.harvest(tdir, "test", now="2099-01-01T00:00:00Z")
    ev = events.read_all("test")[0]
    assert ev.ts == "2026-06-20T09:15:30Z"


def test_harvest_falls_back_to_now_without_timestamp(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Edit", {"file_path": "a.py"})])  # no timestamp
    harvest.harvest(tdir, "test", now="2026-03-03T03:03:03Z")
    assert events.read_all("test")[0].ts == "2026-03-03T03:03:03Z"


def test_catch_up_scans_transcript_dirs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)   # MINDHIVE_PROJECT=test -> dir resolves to 'test'
    root = tmp_path / "projects"
    _write_transcript(root / "encoded-folder", "s9", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s9"}),
        _tooluse("Edit", {"file_path": "z.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)       # don't require an existing namespace
    assert res["total_new"] == 1
    assert "test" in res["projects"]


def test_catch_up_skips_unknown_projects_by_default(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)   # empty store -> 'test' is not a known namespace yet
    root = tmp_path / "projects"
    _write_transcript(root / "encoded-folder", "s9", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s9"}),
        _tooluse("Edit", {"file_path": "z.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up()                        # known_only=True (default)
    assert res["total_new"] == 0                    # nothing harvested into a surprise namespace
    assert "test" in res["skipped_unknown"]


# --- cross-process harvest lock (low-latency loop must not race the task) ---
#
# Two harvesters share every store: the dashboard's background loop (a tick per
# 3 s whenever a transcript moved) and the scheduled `hive autosync` task (every
# 2 minutes). Both stamp each row with the transcript line's OWN timestamp, so a
# line walked twice is a row appended twice, byte for byte -- and the ledger is
# append-only and hash-chained, so the second copy is permanent. The lock is the
# only thing between the two, and these tests hold it to one rule: a live
# harvester is never a second harvester's to take, however long its pass runs.


def _usermsg_at(text, stamp):
    """A typed prompt carrying the transcript's own timestamp, the one harvest
    stamps on every row it mints from the line."""
    return json.dumps({"type": "user", "message": {"content": text},
                       "timestamp": stamp})


def _canonical_rows(project="test"):
    return [events.canonical_json(events.to_dict(e))
            for e in events.read_all(project)]


def test_a_live_harvester_older_than_the_stale_window_is_not_a_second_one_s_to_take(
        tmp_path, monkeypatch):
    """The live-store duplicate, reproduced: one `decision:` line, one
    `decision` row -- not two byte-identical ones on adjacent lines.

    A pass over the live store reads every transcript in every directory end to
    end on every tick, plus three full `read_all`s per namespace, so it can run
    for minutes; the scheduled task fires every two minutes regardless. The lock
    used to call any holder older than 120 s dead, unlink its file and start a
    second pass over the SAME cursor window (the first pass had loaded its
    cursor before the theft and was about to walk it). The seam is exercised
    exactly there: the second harvester arrives after the first has loaded its
    state and taken its dedup snapshot, and before it appends."""
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "3e7af63e-d223-4832-88f0-a57b8022ab1b", [
        json.dumps({"type": "attachment", "cwd": "C:/x"}),
        _usermsg_at("decision: the lock is the kernel's, not a file's age",
                    "2026-08-31T05:36:06.412Z"),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    lock = paths.store_home() / "harvest.lock"
    real = harvest._hook_captured_sessions
    second = []

    def captured_then_a_second_harvester(project):
        out = real(project)
        if not second:
            # The first pass has loaded its cursor and is about to walk the
            # window. Its lock is now older than the stale window, as it is on
            # the live machine two minutes into a long pass -- and the
            # scheduled task's tick lands.
            second.append(None)
            old = time.time() - 10_000
            os.utime(lock, (old, old))
            second[0] = harvest.catch_up(known_only=False)
        return out

    monkeypatch.setattr(harvest, "_hook_captured_sessions",
                        captured_then_a_second_harvester)
    first = harvest.catch_up(known_only=False)

    rows = _canonical_rows()
    assert len(rows) == len(set(rows)), "a row was appended twice, byte for byte"
    assert [e.type for e in events.read_all("test")].count("decision") == 1
    # The two passes are one harvester's worth of work between them: whichever
    # ran, the other was told it did not.
    assert second[0]["locked"] is True
    assert first["total_new"] == 2 and not first.get("locked")


def test_canary_a_second_pass_over_one_line_is_byte_identical(tmp_path, monkeypatch):
    """CANARY for the test above: the duplicate detector can fire. Two passes
    over one line -- the cursor reset between them -- mint two rows the
    canonical serialiser cannot tell apart, because the timestamp is the
    transcript's and not the clock's. That is what makes a double pass a
    permanent duplicate rather than two rows a second apart."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _usermsg_at("decision: stamp the transcript's time", "2026-08-31T05:36:06Z")])
    harvest.harvest(tdir, "test")
    harvest._state_path("test").unlink()             # the cursor, forgotten
    harvest.harvest(tdir, "test")

    rows = [r for r, e in zip(_canonical_rows(), events.read_all("test"))
            if e.type == "decision"]
    assert len(rows) == 2 and len(set(rows)) == 1


def test_catch_up_skips_when_locked(tmp_path, monkeypatch):
    """If another harvester holds the lock, catch_up no-ops (no double-write).
    The holder is a real one -- a second descriptor holding the kernel lock --
    not a file that happens to exist."""
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s1", [
        json.dumps({"type": "attachment", "cwd": "C:/x", "sessionId": "s1"}),
        _tooluse("Edit", {"file_path": "a.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    with harvest.harvest_lock() as held:
        assert held is True
        res = harvest.catch_up(known_only=False)
    assert res.get("locked") is True
    assert res["total_new"] == 0
    assert [e for e in events.read_all("test") if e.type == "file_touched"] == []


@pytest.mark.parametrize("age_s", [10_000, 0], ids=["long-dead", "fresh"])
def test_a_lock_file_without_a_live_holder_never_blocks_whatever_its_age(
        tmp_path, monkeypatch, age_s):
    """A crashed harvester leaves its file behind; the kernel has already
    released its lock. So the file blocks nothing -- and not only once it is
    old: a FRESH file with no holder is the case the old stale window got
    wrong from the other side, blocking every tick for two minutes after a
    crash. The file's age and content mean nothing; only a live holder does."""
    _isolate(tmp_path, monkeypatch)
    lock = paths.store_home() / "harvest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("99999", encoding="utf-8")
    old = time.time() - age_s
    os.utime(lock, (old, old))
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s1", [
        json.dumps({"type": "attachment", "cwd": "C:/x", "sessionId": "s1"}),
        _tooluse("Edit", {"file_path": "a.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)
    assert not res.get("locked")
    assert res["total_new"] == 1


def test_catch_up_releases_lock(tmp_path, monkeypatch):
    """The lock is released after a normal run so the next tick can acquire it
    -- and the FILE stays on disk, by design. A release that unlinked it would
    hand the next opener a different inode: on POSIX `flock` binds to the
    inode, so two harvesters could each hold "the" lock at once, which is the
    exact double-pass this file exists to refuse."""
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    lock = paths.store_home() / "harvest.lock"
    first = harvest.catch_up(known_only=False)
    assert not first.get("locked")
    assert lock.exists()
    second = harvest.catch_up(known_only=False)
    assert not second.get("locked")


def test_a_child_process_is_refused_while_the_parent_holds_the_lock(tmp_path, monkeypatch):
    """The lock is the kernel's, so it holds across PROCESSES -- the dashboard
    and the scheduled task are two -- and not only across descriptors of one.
    A child asks for it while the parent holds it, then again after."""
    _isolate(tmp_path, monkeypatch)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    def child():
        out = subprocess.run(
            [sys.executable, "-c",
             "from mindhive import harvest\n"
             "with harvest.harvest_lock() as held:\n"
             "    print(held)"],
            capture_output=True, text=True, env=env, cwd=str(tmp_path))
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    with harvest.harvest_lock() as held:
        assert held is True
        assert child() == "False"
    assert child() == "True"


def test_a_lock_the_store_cannot_open_skips_the_tick_rather_than_running_unlocked(
        tmp_path, monkeypatch):
    """Fail-CLOSED, and the direction is the point: a skipped tick loses
    nothing, since the cursor does not move, while an unlocked pass can
    duplicate forever. The append lock chooses the other way for the other
    reason (an event refused is an event lost)."""
    _isolate(tmp_path, monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the lock's directory should be", encoding="utf-8")
    monkeypatch.setattr(harvest, "_harvest_lock_path", lambda: blocker / "harvest.lock")
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s1", [
        json.dumps({"type": "attachment", "cwd": "C:/x", "sessionId": "s1"}),
        _tooluse("Edit", {"file_path": "a.py"}),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)
    assert res["locked"] is True
    assert res["total_new"] == 0
    assert events.read_all("test") == []


def test_hive_harvest_direct_path_yields_to_a_running_harvester(
        tmp_path, monkeypatch, capsys):
    """`hive harvest --transcripts D --project P` is the one door to the
    writer that did not pass through the lock at all: an operator running it
    beside the dashboard's loop was a second harvester over the same cursor.
    It now takes the same lock, and says so when refused."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _usermsg_at("decision: one door, one lock", "2026-08-31T05:36:06Z")])
    with harvest.harvest_lock() as held:
        assert held is True
        rc = cli.main(["harvest", "--transcripts", str(tdir), "--project", "test"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["locked"] is True and out["new_events"] == 0
    assert events.read_all("test") == []
    # Released: the same command then does the work.
    assert cli.main(["harvest", "--transcripts", str(tdir), "--project", "test"]) == 0
    assert [e.type for e in events.read_all("test")] == ["decision", "user_prompt"]


# --- per-bubble 5,000-word cap (one memory entry == one bubble) ---

def test_prompt_capped_at_5000_words(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("word " * 6000)])     # 6000 words
    harvest.harvest(tdir, "test")
    p = [e for e in events.read_all("test") if e.type == "user_prompt"][0]
    toks = p.payload["text"].split()
    assert toks[:3] == ["word", "word", "word"]    # kept the leading words
    assert "[truncated]" in p.payload["text"]       # marked as cut
    assert len(toks) == 5001                         # 5000 words + the marker token


def test_normal_prompt_not_capped(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_usermsg("a short normal prompt")])
    harvest.harvest(tdir, "test")
    p = [e for e in events.read_all("test") if e.type == "user_prompt"][0]
    assert p.payload["text"] == "a short normal prompt"
    assert "[truncated]" not in p.payload["text"]


# --- assistant_response capture (Claude's output enters the hive too) ---

def _assistant(text=None, tool=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "name": tool[0], "input": tool[1]})
    return json.dumps({"type": "assistant", "message": {"content": content}})


def test_assistant_text_captured_as_response(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(text="Here is the plan: do X then Y.")])
    harvest.harvest(tdir, "test")
    rs = [e for e in events.read_all("test") if e.type == "assistant_response"]
    assert len(rs) == 1
    assert rs[0].payload["text"] == "Here is the plan: do X then Y."
    assert "response" in rs[0].tags


def test_tool_only_assistant_has_no_response(tmp_path, monkeypatch):
    """An assistant turn that is only a tool call (no prose) yields no response
    event, but the tool action is still captured."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(tool=("Edit", {"file_path": "a.py"}))])
    harvest.harvest(tdir, "test")
    evs = events.read_all("test")
    assert [e for e in evs if e.type == "assistant_response"] == []
    assert any(e.type == "file_touched" for e in evs)


def test_assistant_response_with_text_and_tool(tmp_path, monkeypatch):
    """A turn with both prose and a tool call captures both."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(text="Editing the file now.",
                                              tool=("Edit", {"file_path": "a.py"}))])
    harvest.harvest(tdir, "test")
    types = sorted(e.type for e in events.read_all("test"))
    assert types == ["assistant_response", "file_touched"]


def test_assistant_response_capped_at_5000_words(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_assistant(text="word " * 6000)])
    harvest.harvest(tdir, "test")
    r = [e for e in events.read_all("test") if e.type == "assistant_response"][0]
    assert len(r.payload["text"].split()) == 5001
    assert "[truncated]" in r.payload["text"]


# --- project_for_transcript_dir streams (stops at first cwd, no full-file read) ---

def test_project_for_transcript_dir_extracts_first_cwd(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(harvest.paths, "detect_project", lambda p, **_: p.name)
    d = tmp_path / "enc"
    d.mkdir()
    lines = [json.dumps({"cwd": "C:/ProjX"})] + [json.dumps({"i": i}) for i in range(200)]
    (d / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert harvest.project_for_transcript_dir(d) == "ProjX"


def test_project_for_transcript_dir_none_without_cwd(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    d = tmp_path / "enc"
    d.mkdir()
    (d / "s.jsonl").write_text(json.dumps({"type": "x"}) + "\n", encoding="utf-8")
    assert harvest.project_for_transcript_dir(d) is None


def test_project_for_transcript_dir_skips_malformed_cwd_line(tmp_path, monkeypatch):
    """A malformed line containing '"cwd"' must not abandon the whole file — a
    later valid cwd line still resolves the project."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(harvest.paths, "detect_project", lambda p, **_: p.name)
    d = tmp_path / "enc"
    d.mkdir()
    lines = [
        '{"cwd": "C:/Broken"',                      # truncated JSON, has "cwd"
        json.dumps({"cwd": "C:/ProjY"}),             # valid, later
    ]
    (d / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert harvest.project_for_transcript_dir(d) == "ProjY"


# --- capture enrichment parity: line stats + command descriptions (item: Task 1) ---

def test_harvest_items_carry_line_stats_and_desc():
    o = json.loads(_tooluse("Edit", {
        "file_path": "src/a.py", "old_string": "x", "new_string": "x\ny"}))
    items = harvest._items_from_obj(o)
    assert items == [{"kind": "file_touched", "value": "src/a.py",
                      "tool": "Edit", "plus": 2, "minus": 1}]
    o2 = json.loads(_tooluse("Bash", {
        "command": "pytest -q", "description": "Run tests"}))
    items2 = harvest._items_from_obj(o2)
    assert items2 == [{"kind": "command_run", "value": "pytest -q",
                       "desc": "Run tests"}]


def test_harvest_skips_zero_change_edit():
    o = json.loads(_tooluse("Edit", {
        "file_path": "src/a.py", "old_string": "", "new_string": ""}))
    assert harvest._items_from_obj(o) == []


def test_harvest_drops_whitespace_only_description():
    o = json.loads(_tooluse("Bash", {"command": "ls", "description": "   "}))
    assert harvest._items_from_obj(o) == [{"kind": "command_run", "value": "ls"}]


def test_event_for_item_passes_extra_keys_but_text_is_unchanged():
    etype, payload, tags = harvest._event_for_item(
        {"kind": "file_touched", "value": "src/a.py",
         "tool": "Edit", "plus": 2, "minus": 1})
    assert etype == "file_touched"
    assert payload["text"] == "src/a.py"          # dedup key stability
    assert payload["plus"] == 2 and payload["minus"] == 1 and payload["tool"] == "Edit"
    etype2, payload2, _ = harvest._event_for_item(
        {"kind": "command_run", "value": "pytest -q", "desc": "Run tests"})
    assert payload2 == {"text": "pytest -q", "desc": "Run tests"}


# --- Skill invocation capture (SE Task 2: retroactive back-fill) ---

def test_harvest_captures_skill_invocations():
    """With no `pending` dict the caller has opted out of the tool_result
    join, so the call is emitted straight away and says plainly that no
    outcome was read — `ok` is the no-error-seen default, not a finding."""
    o = json.loads(_tooluse("Skill", {"skill": "mindhive:circle"}))
    assert harvest._items_from_obj(o) == [
        {"kind": "skill_used",
         "value": {"name": "circle", "plugin": "mindhive", "ok": True,
                   "text": "harvested Skill tool call",
                   "outcome_observed": False}}]


def test_harvest_captures_skill_args_like_the_live_hook():
    """Back-harvest mirrors the live hook's optional capped ``args`` capture,
    so both paths agree on the skill_used payload shape."""
    o = json.loads(_tooluse("Skill", {"skill": "mindhive:circle",
                                      "args": "  spin the loop  " + "y" * 500}))
    [item] = harvest._items_from_obj(o)
    v = item["value"]
    assert v["args"].startswith("spin the loop")
    assert len(v["args"]) == hookcapture.SKILL_ARGS_MAX


def test_event_for_item_maps_skill_used():
    value = {"name": "circle", "plugin": "mindhive", "ok": True,
             "text": "harvested Skill tool call"}
    etype, payload, tags = harvest._event_for_item(
        {"kind": "skill_used", "value": value})
    assert etype == "skill_used"
    assert payload == value
    assert tags == ["harvest", "auto", "skill"]


# --- Slash-command skill capture: the join across TWO user messages ---------
#
# Fixtures below reproduce the SHAPE measured on 25 real transcripts
# (2026-09-03): the <command-name> tag and the injected skill body never share
# a message. They arrive as two adjacent user rows linked by
# ``body.parentUuid == command.uuid``, the body carrying ``isMeta: true``.
# Measured on that sample: 30 command rows, 64 body rows, 0 rows carrying both.

def _row(text, *, uuid, parent=None, meta=False, kind="user", blocks=None):
    """One transcript row in the real recorded shape. ``meta`` marks the
    harness-injected half; ``parent`` is the row it answers.

    Field-for-field against the live transcripts (measured over all 86, on the
    96 real command/body pairs): both halves always carry ``timestamp`` and
    ``message.role``; a COMMAND row's ``message.content`` is always a plain
    str (96/96) while a BODY row's is always a LIST of content blocks (96/96,
    95 a single text block and one mixed image+text). ``blocks=True`` selects
    the list form, so the body half of a fixture is shaped like a real one --
    an earlier version of these fixtures passed a bare str for both halves and
    so never exercised the list path through `_user_text`."""
    content = [{"type": "text", "text": text}] if blocks else text
    o = {"type": kind, "uuid": uuid, "parentUuid": parent,
         "timestamp": "2026-09-03T04:00:00.000Z",
         "isSidechain": False, "userType": "external",
         "message": {"role": "user", "content": content}}
    if meta:
        o["isMeta"] = True
    return json.dumps(o)


def _body_row(text, *, uuid, parent):
    """The harness-injected half, in the exact live shape: isMeta, and a
    content LIST rather than a bare string."""
    return _row(text, uuid=uuid, parent=parent, meta=True, blocks=True)


def _cmd_text(name, args="do the thing"):
    return ("<command-message>" + name + " is running</command-message>\n"
            "<command-name>" + name + "</command-name>\n"
            "<command-args>" + args + "</command-args>")


_SKILL_BODY = ("Base directory for this skill: C:/x/.claude/skills/lfg\n\n"
               "---\nname: lfg\ndescription: ships it\n---\n\nbody text")


def _skill_names(project="test"):
    return sorted(e.payload["name"] for e in events.read_all(project)
                  if e.type == "skill_used")


def test_slash_skill_invocation_spans_two_messages_and_is_captured(tmp_path, monkeypatch):
    """THE regression. A slash-invoked skill is recorded even though neither
    message carries both signals -- the join is command-row + isMeta body row
    linked by parentUuid.

    Kills the single-message conjunction: that implementation reads each row
    alone, finds the body mark and the <command-name> tag in DIFFERENT rows,
    and returns None for both -- which is why it fired zero times on 4127+
    real user rows."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("compound-engineering:lfg"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test")

    [ev] = [e for e in events.read_all("test") if e.type == "skill_used"]
    assert ev.payload["name"] == "lfg"
    assert ev.payload["plugin"] == "compound-engineering"
    assert ev.payload["ok"] is True
    assert ev.tags == ["harvest", "auto", "skill"]


def test_neither_half_alone_mints_a_skill(tmp_path, monkeypatch):
    """CANARY for the test above: proves the fixture's two rows are each
    individually inert, so the positive result comes from the JOIN and not
    from either row being sufficient on its own."""
    _isolate(tmp_path, monkeypatch)

    cmd_only = json.loads(_row(_cmd_text("autoplan"), uuid="u1", parent="u0"))
    body_only = json.loads(_body_row(_SKILL_BODY, uuid="u2", parent="u1"))

    assert [i for i in harvest._items_from_obj(cmd_only)
            if i["kind"] == "skill_used"] == []
    assert [i for i in harvest._items_from_obj(body_only)
            if i["kind"] == "skill_used"] == []
    # ...and joined, the same two rows DO mint -- the canary is armed.
    assert [i for i in harvest._items_from_obj(body_only, cmd_only)
            if i["kind"] == "skill_used"] != []


def test_skill_tool_body_does_not_double_mint(tmp_path, monkeypatch):
    """A body row whose parent carries NO <command-name> is the Skill-TOOL
    path, already captured by _skill_from_tool_use. Minting on the body alone
    would double-count it -- 43 of 64 body rows in the measured sample.

    Kills 'mint whenever the skill-body mark appears'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _tooluse("Skill", {"skill": "compound-engineering:lfg"}),
        _row("just a normal typed prompt", uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test")

    assert _skill_names() == ["lfg"]          # exactly one, from the tool_use


def test_builtin_slash_command_mints_no_skill(tmp_path, monkeypatch):
    """/model and /autocompact carry <command-name> but inject no skill body --
    their reply row is not isMeta. They are commands, not skills.

    Kills 'mint whenever <command-name> appears'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("model", args="opus"), uuid="u1", parent="u0"),
        _row("Set model to opus", uuid="u2", parent="u1"),          # not isMeta
    ])
    harvest.harvest(tdir, "test")

    assert _skill_names() == []
    # The typed command is still captured as a prompt -- only the skill claim
    # is withheld.
    assert [e.payload["text"] for e in events.read_all("test")
            if e.type == "user_prompt"] == ["opus", "Set model to opus"]


def test_an_isMeta_command_row_still_counts_as_an_invocation(tmp_path, monkeypatch):
    """A command row that is itself isMeta is STILL a real invocation.

    An earlier version of the fix rejected these, reasoning that a
    machine-written command row is the harness announcing a Skill-tool load
    that `_skill_from_tool_use` already records. That reasoning was checked
    and is false: over all 86 transcripts the guard rejected exactly 5 rows,
    every one `workflow-authoring`, and `workflow-authoring` appears as a
    Skill tool_use ZERO times -- so nothing recorded it and the guard left a
    genuinely-invoked skill reading 'never called'.

    Kills 'reject the pair when the command row is isMeta'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("workflow-authoring"), uuid="u1", parent="u0", meta=True),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test")

    assert _skill_names() == ["workflow-authoring"]


def test_skill_tool_body_whose_parent_is_an_attachment_row(tmp_path, monkeypatch):
    """The real parent of a Skill-TOOL injected body is not a user row at all
    -- it is an ``attachment``/``assistant`` row. Covers the
    `prev.get("type") != "user"` guard, which no other test reaches.

    Kills 'accept any parent row type'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        # an attachment row that happens to carry command-name-looking text
        _row(_cmd_text("workflow-authoring"), uuid="u1", parent="u0",
             kind="attachment"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test")

    assert _skill_names() == []


def test_adjacent_body_without_parent_link_is_not_joined(tmp_path, monkeypatch):
    """Adjacency alone is not the join. A body row whose parentUuid points
    somewhere else is a different branch of the conversation tree.

    Kills 'take the previous row unconditionally, ignore parentUuid'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="SOMEWHERE-ELSE"),
    ])
    harvest.harvest(tdir, "test")

    assert _skill_names() == []


def test_injected_body_without_the_base_directory_line_still_counts(tmp_path, monkeypatch):
    """ONE row in the measured sample (full-stack-feature, a 31KB body)
    injects a skill body that does NOT open with 'Base directory for this
    skill:'. The join is structural -- the operator typed a slash command and
    the harness answered with an injected body -- so it must not depend on
    that preamble string.

    Kills 'require _SKILL_BODY_MARK in the body'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("full-stack-orchestration:full-stack-feature"),
             uuid="u1", parent="u0"),
        _body_row("---\nname: full-stack-feature\ndescription: orchestrate\n---\nbody",
                  uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test")

    [ev] = [e for e in events.read_all("test") if e.type == "skill_used"]
    assert ev.payload["name"] == "full-stack-feature"
    assert ev.payload["plugin"] == "full-stack-orchestration"
    # …and the injected body is NOT also filed as something the operator
    # typed. This body carries no _SKILL_BODY_MARK, so only the structural
    # isMeta check can suppress it; without that the same row lands as a
    # skill_used AND a user_prompt holding machine-written text, and
    # oversee.skill_trails reads the body as the ask behind the skill.
    assert [e.payload["text"] for e in events.read_all("test")
            if e.type == "user_prompt"] == ["do the thing"]


def test_slash_skill_survives_a_cursor_split_between_the_two_rows(tmp_path, monkeypatch):
    """The harvest cursor can land BETWEEN the command row and its body -- the
    dashboard harvests on a fast loop and the two rows are separate lines.
    The second pass must still see the command row it needs.

    Kills 'seed prev=None at the cursor', which silently drops any invocation
    whose two halves straddle a tick."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    cmd = _row(_cmd_text("compound-engineering:lfg"), uuid="u1", parent="u0")
    body = _body_row(_SKILL_BODY, uuid="u2", parent="u1")

    _write_transcript(tdir, "s1", [cmd])
    harvest.harvest(tdir, "test")
    assert _skill_names() == []               # nothing to join yet

    _write_transcript(tdir, "s1", [cmd, body])
    harvest.harvest(tdir, "test")
    assert _skill_names() == ["lfg"]


def test_cursor_seed_walks_back_past_an_unreadable_line(tmp_path, monkeypatch):
    """The seed must mean the same thing as the in-loop ``prev``, which
    survives blank and unparseable lines via its ``continue`` paths. If the
    seed inspects exactly ONE line it gives up whenever a blank or torn line
    sits at the boundary, and the capture then depends on where the tick
    landed -- the very thing the seed exists to remove.

    Kills 'seed from all_lines[cursor - 1] without walking back'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    cmd = _row(_cmd_text("autoplan"), uuid="u1", parent="u0")
    body = _body_row(_SKILL_BODY, uuid="u2", parent="u1")

    # cursor stops after the torn line; the command row is two rows back.
    _write_transcript(tdir, "s1", [cmd, '{"type": "user", "message":'])
    harvest.harvest(tdir, "test")
    assert _skill_names() == []

    _write_transcript(tdir, "s1", [cmd, '{"type": "user", "message":', body])
    harvest.harvest(tdir, "test")
    assert _skill_names() == ["autoplan"]


def test_backfill_skills_joins_across_the_two_messages(tmp_path, monkeypatch):
    """backfill_skills walks whole transcripts ignoring the cursor; it reads
    the same items, so the join must hold on that path too."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    r = harvest.backfill_skills(tdir, "test")

    assert r["new_skill_used"] == 1
    assert _skill_names() == ["autoplan"]


def test_iter_transcript_events_joins_across_the_two_messages(tmp_path):
    """iter_transcript_events is the third reader of _items_from_obj; it must
    carry the previous row too, or the join works in harvest and not here."""
    p = tmp_path / "s1.jsonl"
    p.write_text("\n".join([
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ]) + "\n", encoding="utf-8")

    skills = [i for i in harvest.iter_transcript_events(p)
              if i["kind"] == "skill_used"]
    assert [s["value"]["name"] for s in skills] == ["autoplan"]


# --- Slash capture identity: repeats and coexistence with the tool path -----
#
# `_event_key` keys skill_used on the skill NAME so that ONE invocation seen
# twice (the live hook and harvest both reading the same Skill tool_use)
# collapses. The slash join is a SECOND, disjoint source the hook never sees,
# so sharing a name-only key turned "collapse a double observation" into
# "discard a different real invocation". Every production caller passes
# skip_captured=True and every real session carries hook events, so this is
# the normal path, not an edge case.

def _hook_event(session="s1"):
    """A live-hook (non-harvest) event, which is what makes a session
    `captured` and switches on harvest's event-level dedup."""
    events.append_event("file_touched", {"text": "a.py"}, session_id=session,
                        tags=["auto"], project="test")


def test_repeat_slash_invocations_in_one_session_all_count(tmp_path, monkeypatch):
    """Typing the same slash command twice in a session is two invocations.

    Kills the name-only dedup key, which collapsed them to one and undercounted
    the very statistic this capture path exists to produce."""
    _isolate(tmp_path, monkeypatch)
    _hook_event()
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
        _row(_cmd_text("autoplan"), uuid="u3", parent="u2"),
        _body_row(_SKILL_BODY, uuid="u4", parent="u3"),
    ])
    harvest.harvest(tdir, "test", skip_captured=True)

    assert _skill_names() == ["autoplan", "autoplan"]


def test_a_slash_invocation_survives_an_existing_row_for_the_same_skill(tmp_path, monkeypatch):
    """The Skill TOOL already recorded `lfg` in this session; a later SLASH
    invocation of the same skill is a different, real invocation and must not
    be swallowed by the tool row.

    Kills 'one skill_used per (session, name)'."""
    _isolate(tmp_path, monkeypatch)
    events.append_event("skill_used",
                        {"name": "lfg", "plugin": "compound-engineering",
                         "ok": True, "text": "hook excerpt"},
                        session_id="s1", tags=["auto"], project="test")
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("compound-engineering:lfg"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test", skip_captured=True)

    assert _skill_names() == ["lfg", "lfg"]


def test_reprocessing_the_same_slash_row_does_not_double_count(tmp_path, monkeypatch):
    """CANARY for the two tests above. A looser key must STILL collapse the
    same invocation read twice, or those tests only prove dedup is broken.
    Rewinding the cursor replays the identical rows."""
    _isolate(tmp_path, monkeypatch)
    _hook_event()
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test", skip_captured=True)
    harvest._save_state("test", {})            # rewind: read the same rows again
    harvest.harvest(tdir, "test", skip_captured=True)

    assert _skill_names() == ["autoplan"]


def test_backfill_skills_counts_repeat_slash_invocations(tmp_path, monkeypatch):
    """backfill_skills keyed its seen-set on (session, name) too, so it
    collapsed repeats the same way.

    Kills '(sid, name) seen-set in backfill_skills'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
        _row(_cmd_text("autoplan"), uuid="u3", parent="u2"),
        _body_row(_SKILL_BODY, uuid="u4", parent="u3"),
    ])
    r = harvest.backfill_skills(tdir, "test")

    assert r["new_skill_used"] == 2
    r2 = harvest.backfill_skills(tdir, "test")
    assert r2["new_skill_used"] == 0           # still idempotent


def test_slash_capture_records_that_no_outcome_was_observed(tmp_path, monkeypatch):
    """A slash row proves the command was typed and the harness expanded the
    body. It carries NO outcome -- no tool response, so nothing can ever mark
    it failed. Saying ``ok: True`` and nothing else would make every slash
    invocation a recorded success and inflate the completion rate that
    `recommend` promotes a skill to 'measured' on.

    Kills 'record a slash invocation as an observed success'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test")

    [ev] = [e for e in events.read_all("test") if e.type == "skill_used"]
    assert ev.payload["outcome_observed"] is False
    # A Skill TOOL call whose result IS in the transcript keeps its outcome —
    # that is the distinction this flag draws. An earlier version of this
    # assertion used a bare tool_use with no result and claimed the tool path
    # "DID observe a response": it had not. Nothing read a result, and the
    # payload said ok anyway, which is the defect the join now closes.
    _write_transcript(tdir, "s2", [_skill_call("circle", tid="t9"),
                                   _skill_result(tid="t9")])
    harvest.harvest(tdir, "test")
    tool = [e for e in events.read_all("test")
            if e.type == "skill_used" and e.payload["name"] == "circle"]
    assert [e.payload["outcome_observed"] for e in tool] == [True]
    assert [e.payload["ok"] for e in tool] == [True]


# --- The Skill TOOL path: observe the outcome instead of asserting it -------
#
# `_skill_from_tool_use` read the CALL and stamped ok: True. The result was
# never opened, so harvest could not tell a completed skill from a failed one
# and recorded every invocation as a success — the same defect as the slash
# path's hardcoded ok, in the path that produces most of the measured rates.
#
# The join is by id, not position. Measured over all 86 real transcripts
# (2026-09-03): 151 Skill tool_use blocks, every one carrying an `id`; 151
# matching tool_result blocks, every one keyed `tool_use_id` and carried by a
# row of type `user`; ZERO unmatched. The result is NOT adjacent — rows
# between call and result were 3 (101 cases), 5+ (31), 4 (12), 1 (7) — so a
# one-row lookback of the kind the slash join uses cannot reach it.
#
# The outcome signals, also measured: `is_error` is TRUE on 502 results across
# the corpus, so the field is real and used; on all 151 Skill results it is
# absent, and the row-level `toolUseResult` is a dict carrying success=True
# (151/151). Where is_error IS true, `toolUseResult` is a plain string
# (502/502) — so the two signals never contradict, and both are read.

_TS_CALL = "2026-09-03T04:00:00.000Z"
_TS_RESULT = "2026-09-03T04:00:07.000Z"        # measured median gap: 6.6s


def _skill_call(skill, *, tid, uuid="a1", ts=_TS_CALL, args=None):
    """An assistant row invoking the Skill tool, with the `id` every real one
    carries."""
    inp = {"skill": skill}
    if args:
        inp["args"] = args
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "id": tid,
                                 "name": "Skill", "input": inp}]}})


def _skill_result(*, tid, uuid="r1", parent="a1", ts=_TS_RESULT,
                  is_error=None, success=True, tool_use_result=True,
                  text="Skill body loaded."):
    """The `user` row that carries the tool_result, shaped as measured: the
    block keyed `tool_use_id` with string content, and a row-level
    `toolUseResult` dict."""
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if is_error is not None:
        block["is_error"] = is_error
    o = {"type": "user", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
         "isSidechain": False, "userType": "external",
         "message": {"role": "user", "content": [block]}}
    if tool_use_result:
        # A real failure carries this as the error STRING, not a dict.
        o["toolUseResult"] = ({"success": success, "commandName": "x"}
                              if is_error is not True else "Error: it failed")
    return json.dumps(o)


def _filler(n, *, uuid_prefix="f"):
    """The rows that really sit between a call and its result."""
    return [json.dumps({"type": "assistant", "uuid": f"{uuid_prefix}{i}",
                        "timestamp": _TS_CALL,
                        "message": {"content": [{"type": "text",
                                                 "text": f"thinking {i}"}]}})
            for i in range(n)]


def _skills(project="test"):
    return [e for e in events.read_all(project) if e.type == "skill_used"]


def test_a_skill_tool_call_records_the_outcome_its_result_reports(tmp_path, monkeypatch):
    """The success case, but OBSERVED: the result was opened and carried no
    error. Same `ok` as before, now with evidence behind it."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   *_filler(3),
                                   _skill_result(tid="t1")])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["name"] == "circle"
    assert ev.payload["ok"] is True
    assert ev.payload["outcome_observed"] is True


def test_a_failed_skill_tool_call_is_recorded_as_a_failure(tmp_path, monkeypatch):
    """THE DEFECT. `is_error: true` on the result — and harvest recorded
    ok: True regardless, because it never opened the result. A failing skill
    counted as a completion and lifted its own completion rate.

    Kills 'stamp ok: True on a Skill tool_use'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   *_filler(3),
                                   _skill_result(tid="t1", is_error=True)])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["ok"] is False
    assert ev.payload["outcome_observed"] is True


def test_a_result_reporting_success_false_is_a_failure_too(tmp_path, monkeypatch):
    """The row-level `toolUseResult.success` is the other signal, and it is
    the one every real Skill result actually carries."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   _skill_result(tid="t1", success=False)])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["ok"] is False
    assert ev.payload["outcome_observed"] is True


def test_a_call_whose_result_never_arrived_is_recorded_as_unobserved(tmp_path, monkeypatch):
    """A transcript that ends mid-call (session killed, harvest tick landing
    inside the call/result window). The invocation still counts — losing it
    would reinstate the 'never called' bug — but it claims no outcome."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   *_filler(2)])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["name"] == "circle"
    assert ev.payload["outcome_observed"] is False


def test_the_result_does_not_have_to_be_adjacent_to_the_call(tmp_path, monkeypatch):
    """CANARY against a one-row lookback. Measured: only 7 of 151 real results
    sat one row after their call; 31 sat five or more rows away."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   *_filler(9),
                                   _skill_result(tid="t1", is_error=True)])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["ok"] is False, "a distant result was not joined"


def test_a_result_for_another_tool_use_does_not_close_the_call(tmp_path, monkeypatch):
    """The join is by id. A Bash result landing between the call and its own
    result must not be mistaken for it.

    Kills 'take the next tool_result you see'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _skill_call("circle", tid="t1"),
        _skill_result(tid="OTHER", uuid="r0", is_error=True),
    ])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["outcome_observed"] is False
    assert ev.payload["ok"] is True, "an unrelated result set the outcome"


def test_two_calls_in_flight_are_matched_by_id_not_by_order(tmp_path, monkeypatch):
    """Results can come back out of order. A queue or a stack would pin the
    failure on the wrong skill.

    Kills 'match calls to results positionally'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _skill_call("first", tid="t1", uuid="a1"),
        _skill_call("second", tid="t2", uuid="a2"),
        _skill_result(tid="t2", uuid="r2", is_error=True),   # B fails first
        _skill_result(tid="t1", uuid="r1"),                  # A succeeds later
    ])
    harvest.harvest(tdir, "test")

    got = {e.payload["name"]: e.payload["ok"] for e in _skills()}
    assert got == {"first": True, "second": False}


def test_the_event_is_stamped_at_the_call_not_at_the_result(tmp_path, monkeypatch):
    """`last_used` should say when the skill ran, not when its output landed.
    Measured gap: median 6.6s, max 76.7s."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   *_filler(3),
                                   _skill_result(tid="t1")])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.ts.startswith("2026-09-03T04:00:00"), ev.ts


def test_a_call_with_no_id_is_recorded_but_unobserved(tmp_path, monkeypatch):
    """No id means nothing can join the call to a result. Every real block
    carries one (151/151), so this is the synthetic/legacy shape — it must
    still record the call, and must not claim an outcome."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_tooluse("Skill", {"skill": "circle"})])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["name"] == "circle"
    assert ev.payload["outcome_observed"] is False


def test_a_non_skill_tool_result_mints_nothing(tmp_path, monkeypatch):
    """CANARY: 18583 tool_result blocks exist across the corpus and only 151
    answer a Skill call. The other 18432 must stay invisible here."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _tooluse("Bash", {"command": "ls"}),
        _skill_result(tid="whatever", is_error=True),
    ])
    harvest.harvest(tdir, "test")

    assert _skills() == []


def test_a_failed_skill_lands_in_the_completion_rate_as_a_failure(tmp_path, monkeypatch):
    """End to end, through the statistic this exists to make honest: one
    failed call and one clean call is 50%, not 100%."""
    _isolate(tmp_path, monkeypatch)
    from mindhive import plugins
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1", uuid="a1"),
                                   _skill_result(tid="t1", uuid="r1",
                                                 is_error=True)])
    _write_transcript(tdir, "s2", [_skill_call("circle", tid="t2", uuid="a2"),
                                   _skill_result(tid="t2", uuid="r2")])
    harvest.harvest(tdir, "test")

    st = plugins.skill_stats(project="test")["circle"]
    assert (st["calls"], st["ok"], st["fail"], st["unobserved"]) == (2, 1, 1, 0)
    assert st["completion_rate"] == 0.5


def test_the_split_cursor_case_records_the_call_and_says_it_is_unobserved(tmp_path, monkeypatch):
    """A harvest tick landing between the call and its result cannot see an
    outcome that is not written yet. The call must still be recorded, once,
    and a later tick must not mint a second event for it."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1")])
    harvest.harvest(tdir, "test")
    [ev] = _skills()
    assert ev.payload["outcome_observed"] is False

    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   _skill_result(tid="t1")])
    harvest.harvest(tdir, "test")
    assert len(_skills()) == 1, "the second tick minted a duplicate call"


def test_iter_transcript_events_joins_the_result(tmp_path):
    """iter_transcript_events is the second reader of the join."""
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   *_filler(3),
                                   _skill_result(tid="t1", is_error=True)])
    items = harvest.iter_transcript_events(tdir / "s1.jsonl")
    skills = [i for i in items if i["kind"] == "skill_used"]
    assert [i["value"]["ok"] for i in skills] == [False]
    assert [i["value"]["outcome_observed"] for i in skills] == [True]


def test_backfill_skills_joins_the_result(tmp_path, monkeypatch):
    """backfill_skills is the third reader, and it ignores the cursor."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1"),
                                   *_filler(3),
                                   _skill_result(tid="t1", is_error=True)])
    r = harvest.backfill_skills(tdir, "test")
    assert r["new_skill_used"] == 1
    [ev] = _skills()
    assert ev.payload["ok"] is False and ev.payload["outcome_observed"] is True


def test_the_slash_path_still_reports_no_observed_outcome(tmp_path, monkeypatch):
    """CANARY: a slash invocation fires no tool, so it has no result to open
    and must stay unobserved. The tool-path join must not accidentally mark
    it observed."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _row(_cmd_text("autoplan"), uuid="u1", parent="u0"),
        _body_row(_SKILL_BODY, uuid="u2", parent="u1"),
    ])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["outcome_observed"] is False
    assert "row" in ev.payload           # the slash per-invocation identity


def test_a_result_with_no_success_envelope_is_still_a_clean_run(tmp_path, monkeypatch):
    """Absence is not failure — the rule `hookcapture._response_ok` already
    follows. Measured: 7429 of 18583 results carry no ``is_error`` key at all
    and 17475 carry no ``success``, so reading absence as failure would
    invent failures wholesale.

    Kills 'require an explicit success marker'."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [
        _skill_call("circle", tid="t1"),
        _skill_result(tid="t1", tool_use_result=False),   # bare result block
    ])
    harvest.harvest(tdir, "test")

    [ev] = _skills()
    assert ev.payload["ok"] is True
    assert ev.payload["outcome_observed"] is True


def test_harvest_reports_how_many_calls_it_could_not_close(tmp_path, monkeypatch):
    """A silent zero and a silent hundred look identical, so the count is
    returned rather than swallowed."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1", uuid="a1"),
                                   _skill_result(tid="t1", uuid="r1"),
                                   _skill_call("other", tid="t2", uuid="a2")])
    r = harvest.harvest(tdir, "test")
    assert r["unclosed_skill_calls"] == 1

    _isolate(tmp_path / "second", monkeypatch)
    tdir2 = tmp_path / "tx2"
    _write_transcript(tdir2, "s1", [_skill_call("circle", tid="t1"),
                                    _skill_result(tid="t1")])
    r2 = harvest.harvest(tdir2, "test")
    assert r2["unclosed_skill_calls"] == 0


def test_iter_transcript_events_still_reports_an_unclosed_call(tmp_path):
    """Each of the three readers owns its own flush; a reader that forgets it
    loses the invocation entirely, which is worse than losing the outcome."""
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1")])
    items = harvest.iter_transcript_events(tdir / "s1.jsonl")
    skills = [i for i in items if i["kind"] == "skill_used"]
    assert [i["value"]["name"] for i in skills] == ["circle"]
    assert [i["value"]["outcome_observed"] for i in skills] == [False]


def test_backfill_skills_still_reports_an_unclosed_call(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    _write_transcript(tdir, "s1", [_skill_call("circle", tid="t1")])
    r = harvest.backfill_skills(tdir, "test")
    assert r["new_skill_used"] == 1
    [ev] = _skills()
    assert ev.payload["outcome_observed"] is False


# --- the unclosed count has to reach a reader ---------------------------------
#
# `harvest()` is not what runs in production: autosync and the dashboard's
# background sweep both go through `catch_up`, which summarises. It drops
# `processed_files` and `skipped_captured` on purpose — neither is a signal
# anyone acts on. An unclosed call IS one: it says an invocation was recorded
# with no outcome, so a rate built from it is thinner than it looks. A count
# that stops at `harvest()` is a count no operator ever sees.

def test_catch_up_surfaces_the_calls_it_could_not_close(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s9", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s9"}),
        _skill_call("circle", tid="t1", uuid="a1"),
        _skill_result(tid="t1", uuid="r1"),
        _skill_call("other", tid="t2", uuid="a2"),      # no result in the file
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)
    assert res["unclosed_skill_calls"] == 1
    assert res["projects"]["test"]["unclosed_skill_calls"] == 1


def test_catch_up_reports_zero_when_every_call_was_closed(tmp_path, monkeypatch):
    """The other half of the pair: without it, a propagator hardwired to 1 —
    or to `len(pending)` of the wrong dict — would read as working."""
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s9", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s9"}),
        _skill_call("circle", tid="t1", uuid="a1"),
        _skill_result(tid="t1", uuid="r1"),
    ])
    monkeypatch.setattr(harvest, "claude_projects_root", lambda: root)
    res = harvest.catch_up(known_only=False)
    assert res["unclosed_skill_calls"] == 0
    assert res["projects"]["test"]["unclosed_skill_calls"] == 0


def test_a_locked_catch_up_still_carries_the_count(tmp_path, monkeypatch):
    """The early return when another harvester holds the lock is a different
    dict literal, so it needs the key too — a caller reading it must not
    KeyError on the one tick that did no work."""
    _isolate(tmp_path, monkeypatch)
    with harvest.harvest_lock() as held:                # held by "someone"
        assert held is True
        res = harvest.catch_up(known_only=False)
    assert res["locked"] is True
    assert res["unclosed_skill_calls"] == 0
# --- injected non-user-authored bodies must not mint memos (harvest defect) ---
#
# Claude Code delivers skill bodies, workflow references and other injected
# documents as user-ROLE messages. The envelope distinguishes them: an injected
# body carries `isMeta: True`; a really-typed prompt instead carries `origin`,
# `permissionMode` and `promptSource` and has no `isMeta`. The prefix guard
# `_is_injected_prompt` only recognises bodies that OPEN with "Base directory
# for this skill:", so a reference document that merely CONTAINS the word
# "Caveat:" mid-body slipped through and was mined for a memo. This shape is
# taken from the real transcript message that minted the junk caveat.

_INJECTED_REFERENCE = (
    "# Workflow authoring reference\n"
    "\n"
    "A workflow structures work across many agents.\n"
    "\n"
    "Caveat: interactively-authenticated MCP servers may be absent in "
    "headless runs.\n"
    "\n"
    "Use phases to sequence the lanes.\n"
)


def _user(text, **envelope):
    o = {"type": "user", "message": {"content": text}}
    o.update(envelope)
    return o


def _kinds(items):
    return [i["kind"] for i in items]


def test_isMeta_injected_body_yields_no_memo():
    items = harvest._items_from_obj(_user(_INJECTED_REFERENCE, isMeta=True))

    assert "memo" not in _kinds(items)


def test_same_text_typed_by_the_user_still_yields_a_memo():
    """CANARY for the test above: the fixture text really is memo-bearing, so
    the silence there comes from the envelope check and not from a fixture
    that could never have tripped the extractor in the first place."""
    items = harvest._items_from_obj(
        _user(_INJECTED_REFERENCE, origin="terminal", promptSource="user"))

    [memo] = [i for i in items if i["kind"] == "memo"]
    assert memo["memo_type"] == "caveat"
    assert memo["value"].startswith("interactively-authenticated MCP servers")


def test_isMeta_injected_body_yields_no_user_prompt():
    """The prefix guard already skipped bodies opening with the skill marker;
    this body has no such marker, so only the envelope can reject it."""
    items = harvest._items_from_obj(_user(_INJECTED_REFERENCE, isMeta=True))

    assert "user_prompt" not in _kinds(items)


def test_isMeta_does_not_suppress_slash_command_skill_capture():
    """Kills the plausible over-broad fix (an early return for every isMeta
    message). skill_used is a structural fact about the session, not an
    assertion attributed to the user, so the envelope must not gate it: a
    slash invocation injects its skill body as an isMeta message, and that
    body is the only place the invocation is recorded.

    Written against the TWO-row shape the format really uses (see the slash
    block above), and against a body carrying no skill marker at all, so the
    envelope is the only thing that can classify it. An earlier version of
    this test pinned the same contract on a SINGLE message holding both the
    <command-name> tag and the body marker, a pair that occurs zero times in
    4127 real user rows because `_skill_from_command` demanded it and so
    fired on nothing. The contract survived the fix; the fixture did not."""
    cmd = json.loads(_row(_cmd_text("workflow-authoring"), uuid="c1"))
    body = json.loads(_body_row(_INJECTED_REFERENCE, uuid="b1", parent="c1"))

    items = harvest._items_from_obj(body, cmd)

    [skill] = [i for i in items if i["kind"] == "skill_used"]
    assert skill["value"]["name"] == "workflow-authoring"
    assert "memo" not in _kinds(items)


# --- compaction summaries must not mint memos either (harvest defect) ---
#
# When Claude Code compacts a conversation it writes the summary back into the
# transcript as a user-ROLE row. That row is stamped `isCompactSummary: True`
# and `isVisibleInTranscriptOnly: True`, never `isMeta`, so the envelope check
# above let it through: the summary was filed as something the operator typed
# and mined for a memo at its first "decision:"/"caveat:". Measured 2026-10-03
# over the 80 top-level transcripts harvest reads: 221 summary rows, every one
# flagged and opening with the sentence below, 51 minting a memo and all 221
# filed as user_prompt; all 51 memos had reached a project ledger. The fixture
# keeps the live values of every field that bears on the classification: a
# plain-str content (221/221), a role, a timestamp, and no isMeta, origin,
# promptSource or permissionMode.

_COMPACT_SUMMARY = (
    "This session is being continued from a previous conversation that ran "
    "out of context. The summary below covers the earlier portion of the "
    "conversation.\n"
    "\n"
    "Summary:\n"
    "1. Primary Request and Intent: ship the fold.\n"
    "2. Key decision: keep the cursor per segment.\n"
    "3. Caveat: the mirror lags the ledger by one tick.\n"
)


def _compact_summary_row(text=_COMPACT_SUMMARY):
    return {"type": "user", "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True, "userType": "external",
            "timestamp": "2026-10-03T05:32:57.000Z",
            "message": {"role": "user", "content": text}}


def test_a_compaction_summary_yields_no_memo():
    items = harvest._items_from_obj(_compact_summary_row())

    assert "memo" not in _kinds(items)


def test_a_compaction_summary_is_not_filed_as_a_typed_prompt():
    """promptgate counts user_prompt events as human-typed, and roadmap_auto
    takes a session's first one as its original ask; a summary is neither."""
    items = harvest._items_from_obj(_compact_summary_row())

    assert "user_prompt" not in _kinds(items)


def test_the_same_summary_text_typed_by_the_user_still_yields_a_memo_and_a_prompt():
    """CANARY for the two tests above: the fixture text really mints a memo,
    so their silence comes from the envelope. It also kills a text-prefix
    fix: this text opens with the real summary sentence, and a user who
    pastes a summary in has still typed it."""
    items = harvest._items_from_obj(
        _user(_COMPACT_SUMMARY, origin="terminal", promptSource="user"))

    [memo] = [i for i in items if i["kind"] == "memo"]
    assert memo["memo_type"] == "decision"
    assert memo["value"].startswith("keep the cursor per segment.")
    assert "user_prompt" in _kinds(items)


def test_harvest_files_nothing_from_a_compaction_summary(tmp_path, monkeypatch):
    """End to end in the recorded order: the compact_boundary system row, the
    summary, then the first prompt typed after it. Only that prompt and its
    memo reach the ledger."""
    _isolate(tmp_path, monkeypatch)
    tdir = tmp_path / "tx"
    boundary = {"type": "system", "subtype": "compact_boundary",
                "content": "Conversation compacted", "level": "info",
                "compactMetadata": {"trigger": "auto", "preTokens": 369739}}
    typed = _user("decision: ship the brief first",
                  origin="terminal", promptSource="user")
    _write_transcript(tdir, "s1", [json.dumps(r) for r in
                                   (boundary, _compact_summary_row(), typed)])

    harvest.harvest(tdir, "test")

    evs = events.read_all("test")
    assert [e.payload["text"] for e in evs
            if e.type in events.SEMANTIC_TYPES] == ["ship the brief first"]
    assert [e.payload["text"] for e in evs
            if e.type == "user_prompt"] == ["decision: ship the brief first"]
