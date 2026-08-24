# tests/test_events.py
from mindhive import events, paths


def test_read_all_skips_malformed_rows_and_stays_total(tmp_path):
    # Ledger-schema SOP: reads stay TOTAL. A torn/interleaved write from a past
    # session — a line of unparseable JSON, or a JSON object missing a required
    # Event field — must be SKIPPED, never raised. Every dashboard endpoint and
    # every `hive` CLI command reads via read_all; one corrupt byte-run must not
    # take down the whole hive (it did: a torn `command_run` fragment 500'd every
    # dashboard section and crashed every CLI command).
    events.append_event("decision", {"text": "before"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-20T11:00:00Z","type":"decision"}\n')  # missing fields
        f.write(':"command_run"}\n')                                   # torn fragment
    events.append_event("decision", {"text": "after"}, session_id="s",
                        ts="2026-06-20T12:00:00Z")
    evs = events.read_all("test")
    assert [e.payload.get("text") for e in evs] == ["before", "after"]


def test_corrupt_rows_surfaces_skipped_rows(tmp_path):
    # The strict companion to the total read: read_all() drops malformed rows so
    # folds never crash; corrupt_rows() SURFACES them so `hive verify` and the
    # dashboard can report corruption instead of it passing silently.
    events.append_event("decision", {"text": "ok"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "a", encoding="utf-8") as f:
        f.write('{"ts":"2026-06-20T11:00:00Z","type":"decision"}\n')  # missing fields
        f.write(':"command_run"}\n')                                   # torn fragment
    bad = events.corrupt_rows("test")
    assert len(bad) == 2
    joined = " ".join(r["reason"] for r in bad)
    assert "missing required field" in joined
    assert "unparseable json" in joined
    assert all(r["segment"] == seg.name for r in bad)


def test_read_stays_total_when_a_torn_write_splits_a_utf8_character(tmp_path):
    # The total-read contract at FILE-DECODE granularity, not just per line. A
    # crash or unserialised append mid-character leaves invalid UTF-8 bytes, and
    # `read_text(encoding="utf-8")` without errors= raises UnicodeDecodeError
    # BEFORE splitlines — so the per-line JSON guard never runs and read_all /
    # corrupt_rows take down every dashboard section and CLI command on one torn
    # byte-run, the exact outage the line-level guard exists to prevent. And it
    # is reachable: canonical_json writes non-ASCII as UTF-8 (ensure_ascii=False),
    # so a tear can land mid-character; `verify` itself reads through corrupt_rows.
    events.append_event("decision", {"text": "before"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")
    seg = next(paths.events_dir("test").glob("*.jsonl"))
    with open(seg, "ab") as f:              # a dangling UTF-8 lead byte: the start
        f.write(b"\xc3")                    # of a 2-byte sequence, no continuation
    # read_all must skip the torn byte-run and still return the good row.
    assert [e.payload.get("text") for e in events.read_all("test")] == ["before"]
    # corrupt_rows must SURFACE it rather than crashing verify().
    bad = events.corrupt_rows("test")
    assert any(r["segment"] == seg.name and "unparseable" in r["reason"]
               for r in bad)


def test_canonical_json_is_stable():
    a = events.canonical_json({"b": 1, "a": 2})
    b = events.canonical_json({"a": 2, "b": 1})
    assert a == b == '{"a":2,"b":1}'

def test_append_rejects_unknown_type():
    import pytest
    with pytest.raises(ValueError):
        events.append_event("bogus", {"text": "x"})

def test_user_prompt_is_valid_and_in_memory_view():
    ev = events.append_event("user_prompt", {"text": "hello"}, session_id="s",
                             ts="2026-06-23T10:00:00Z")
    assert ev.type == "user_prompt"
    assert "user_prompt" in events.VALID_TYPES
    # shows in the dashboard Memory view, but is NOT a curated semantic type
    assert "user_prompt" in events.MEMORY_VIEW_TYPES
    assert "user_prompt" not in events.SEMANTIC_TYPES

def test_append_and_read_orders_by_ts():
    events.append_event("decision", {"text": "second"}, session_id="s1",
                        ts="2026-06-20T10:00:00Z")
    events.append_event("decision", {"text": "first"}, session_id="s2",
                        ts="2026-06-20T09:00:00Z")
    evs = events.read_all()
    assert [e.payload["text"] for e in evs] == ["first", "second"]

def test_segments_are_per_session():
    events.append_event("caveat", {"text": "x"}, session_id="abc",
                        ts="2026-06-20T10:00:00Z")
    assert (paths.events_dir() / "abc.jsonl").exists()


def test_assistant_response_is_valid_and_in_memory_view():
    ev = events.append_event("assistant_response", {"text": "ok"}, session_id="s",
                             ts="2026-06-23T10:00:00Z")
    assert ev.type == "assistant_response"
    assert "assistant_response" in events.MEMORY_VIEW_TYPES
    assert "assistant_response" not in events.SEMANTIC_TYPES


def test_tool_failed_is_a_valid_deterministic_type():
    assert "tool_failed" in events.VALID_TYPES
    assert "tool_failed" in events.DETERMINISTIC_TYPES
    # deterministic, not semantic: must stay out of the LEDGER/graph intent layer
    assert "tool_failed" not in events.SEMANTIC_TYPES
    ev = events.append_event("tool_failed", {"text": "boom", "signature": "pymodule:x"},
                             session_id="s1", tags=["auto", "failure"], project="test")
    assert ev.type == "tool_failed"


def test_rule_revised_is_a_valid_deterministic_type():
    assert "rule_revised" in events.DETERMINISTIC_TYPES
    assert "rule_revised" not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_revised",
                             {"signature": "pymodule:x", "text": "do Y", "source": "human"},
                             session_id="s1", tags=["reconcile"], project="test")
    assert ev.type == "rule_revised"


def test_skill_used_is_a_valid_deterministic_type():
    # U5: without this registration append_event raises and every Skill-tool
    # capture would be silently dropped to hooks_errors.log
    assert "skill_used" in events.DETERMINISTIC_TYPES
    assert "skill_used" not in events.SEMANTIC_TYPES
    ev = events.append_event("skill_used",
                             {"name": "ce-plan", "plugin": "compound-engineering",
                              "ok": True, "text": "done"},
                             session_id="s1", tags=["auto"], project="test")
    assert ev.type == "skill_used"


def test_lifecycle_event_types_are_valid():
    for t in ("rule_activated", "rule_promoted", "rule_demoted"):
        assert t in events.DETERMINISTIC_TYPES
        assert t not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_promoted", {"signature": "pymodule:x"},
                             session_id="s", tags=["promote"], project="test")
    assert ev.type == "rule_promoted"


def test_loop_run_dismissed_is_valid_deterministic_type():
    assert "loop_run_dismissed" in events.DETERMINISTIC_TYPES
    assert "loop_run_dismissed" not in events.SEMANTIC_TYPES
    ev = events.append_event("loop_run_dismissed",
                             {"run_id": "abc123", "cause": "manual"},
                             session_id="s", project="test")
    assert ev.type == "loop_run_dismissed"


def test_learning_is_a_valid_semantic_type():
    # regression guard: research._record_learning appends type "learning";
    # before it was registered, append_event rejected it and the controller's
    # crash guard silently dropped every PRD from the ledger
    assert "learning" in events.SEMANTIC_TYPES
    assert "learning" in events.MEMORY_VIEW_TYPES
    ev = events.append_event("learning", {"text": "Self-Reliant learning 1",
                                          "report": "PRD body"},
                             session_id="research-ab12cd34",
                             tags=["self-reliant", "learning"], project="test")
    assert ev.type == "learning"


def test_origin_of_marks_self_reliant_output():
    # tag mark (append-time) and session mark (research-<run_id> stream) both
    # resolve to the one provenance label; everything else stays unlabeled
    assert events.origin_of("research-ab12cd34") == "self-reliant"
    assert events.origin_of("sess-1", ["self-reliant"]) == "self-reliant"
    assert events.origin_of("sess-1", ["swarm"]) is None
    assert events.origin_of("sess-1") is None
    assert events.origin_of(None) is None
    assert events.origin_of("researchy-session") is None   # prefix, not substring


def test_concurrent_appends_never_lose_or_tear_a_row(tmp_path):
    """The ledger is the one file in this system that may never lose a write.

    Measured before the lock landed, six processes x sixty appends into one
    segment: 72 of 360 rows simply GONE, 5 runs out of 5, worst case 606 of 2400
    at twelve-way. The loss is a Windows story -- `open(seg, "a")` there is
    seek-to-end THEN write, two syscalls, so two processes seek to the same
    offset and the second overwrites the first -- but the same race also tears
    rows, leaving exactly the `:"command_run"}` fragment that this file's
    total-read test above was written to TOLERATE. Reads were made total years
    before the write was made atomic; this test is the other half.

    Processes, not threads, and that is not incidental: threads share one
    interpreter and would serialise on the GIL for a write this small, so a
    threaded version of this test passes against the broken code and proves
    nothing.

    `os.write` on an O_APPEND descriptor was measured too and does NOT fix it
    (328 lost vs 322 for the plain write) -- Windows has no atomic-append
    syscall to reach for, so the serialisation has to be explicit.
    """
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    nproc, nrows = 6, 60
    src = str(Path(events.__file__).resolve().parents[1])
    child = tmp_path / "appender.py"
    child.write_text(
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from mindhive import events\n"
        "tag, n = sys.argv[1], int(sys.argv[2])\n"
        "for i in range(n):\n"
        "    events.append_event('command_run',\n"
        "                        {'desc': '%%s-%%d' %% (tag, i),\n"
        "                         'text': 'x' * 120},\n"
        "                        session_id='race', tags=['auto'],\n"
        "                        project='test')\n" % src,
        encoding="utf-8")

    env = dict(os.environ, MINDHIVE_HOME=str(tmp_path), MINDHIVE_PROJECT="test",
               MINDHIVE_AUTORESUME_TIMER="off")
    procs = [subprocess.Popen([sys.executable, str(child), "p%d" % i, str(nrows)],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
             for i in range(nproc)]
    for p in procs:
        _out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode("utf-8", "replace")[-800:]

    seg = paths.events_dir("test") / "race.jsonl"
    survived = set()
    for line in seg.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            survived.add(json.loads(line)["payload"]["desc"])
        except Exception:                                       # noqa: BLE001
            pass

    expected = {"p%d-%d" % (p, i) for p in range(nproc) for i in range(nrows)}
    missing = expected - survived
    assert not missing, (
        "%d of %d appended rows never reached the ledger — the append raced"
        % (len(missing), len(expected)))
    assert events.corrupt_rows("test") == [], "concurrent appends tore a row"
