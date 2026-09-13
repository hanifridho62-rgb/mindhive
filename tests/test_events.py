# tests/test_events.py
import pytest

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


# --- where a ledger row ends --------------------------------------------------
#
# The ledger is NEWLINE-delimited JSON, but every reader here used
# `str.splitlines()` until 2026-09-10. That also breaks on U+0085, U+2028,
# U+2029 and five C0 controls, and `canonical_json` writes with
# `ensure_ascii=False`, so three of them reach a segment RAW through the
# ordinary writer. All three are legal inside a JSON string -- the row on disk
# was intact and only the reader destroyed it: the event vanished from
# `read_all` (and so from every projection, the briefing and the mirror),
# `corrupt_rows` invented two torn rows for it, and every later row's line index
# shifted by one, including `seg_line`, which Postgres stores as part of a
# UNIQUE natural key.
#
# Most of the tests below are written to FAIL on `splitlines()` and pass on the
# shared `_segment_lines` splitter. Verified that way rather than asserted:
# mutating the one `.split("\n")` in `events._segment_parts` -- the single
# place that decides where a ledger row ends -- back to `.splitlines()` fails
# 30 cases: 13 of the 18 here, 2 in test_pgmirror.py and 15 in test_chain.py.
# The chain count is that high because the split is now SHARED, so the seal
# tests defend the readers' rule too; before the shared seam they would all
# have stayed green while these went red, which is the drift
# tests/test_architecture_drift.py now forbids outright. The other 5 here pass
# either way, deliberately:
#
#   * the fixture canary (3 cases) and the canonical_json survey guard the OTHER
#     way this section could go quiet. If a writer ever started escaping these
#     characters, every test above would become vacuous while staying green;
#     these two go red instead.
#   * the blank-line test pins a property, not the defect. Both splitters agree
#     about a blank line -- that is the point. It is the one guarantee
#     `_segment_lines` makes that no exotic character is needed to state.

RAW_SEPARATORS = ["\u2028", "\u2029", "\u0085"]      # and ONLY these three -- see the
ESCAPED_BY_JSON = ["\u000b", "\u000c", "\u001c", "\u001d", "\u001e"]   # test two below


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_the_separator_fixture_is_written_raw_and_fools_splitlines(sep):
    """CANARY for every test below it. They are only discriminating while the
    writer actually puts the character on disk unescaped; flip `ensure_ascii` and
    they all quietly become three-event tests that pass against the defect."""
    events.append_event("decision", {"text": "mid" + sep + "dle"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    text = (paths.events_dir("test") / "s.jsonl").read_text(encoding="utf-8")
    assert sep in text                          # raw, not the six-char escape
    assert len(text.splitlines()) > text.count("\n")


def test_only_three_separators_survive_canonical_json():
    """The other five are below U+0020, which JSON forbids unescaped, so
    `json.dumps` escapes them whatever `ensure_ascii` says -- they can never
    arrive as separators. Pinning that keeps the docstrings honest and stops the
    fixture list above growing on a guess."""
    for ch in RAW_SEPARATORS:
        assert ch in events.canonical_json({"t": ch}), repr(ch)
    for ch in ESCAPED_BY_JSON:
        assert ch not in events.canonical_json({"t": ch}), repr(ch)


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_read_all_returns_a_separator_bearing_row_as_one_event(sep):
    """The defect in its plainest form: three events written, two read back."""
    for i, text in enumerate(["before", "mid" + sep + "dle", "after"]):
        events.append_event("decision", {"text": text}, session_id="s",
                            ts="2026-06-20T10:00:0%d" % i + "Z")
    assert [e.payload["text"] for e in events.read_all("test")] == [
        "before", "mid" + sep + "dle", "after"]


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_read_types_sees_the_same_rows_read_all_does(sep):
    """The `== read_all()` half alone is NOT discriminating -- both readers broke
    identically, which is why the existing typed-read parity test could not catch
    this. The count assertion is what makes it one."""
    for i, text in enumerate(["before", "mid" + sep + "dle", "after"]):
        events.append_event("decision", {"text": text}, session_id="s",
                            ts="2026-06-20T10:00:0%d" % i + "Z")
    typed = events.read_types(["decision"], "test")
    assert typed == events.read_all("test")
    assert len(typed) == 3


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_a_separator_bearing_row_is_not_reported_as_two_torn_rows(sep):
    """`corrupt_rows`' count is a documented regression detector -- if it moves,
    an unserialised writer exists again. A reader that tears intact rows makes it
    move for a reason that has nothing to do with writers."""
    events.append_event("decision", {"text": "mid" + sep + "dle"},
                        session_id="s", ts="2026-06-20T10:00:00Z")
    assert events.corrupt_rows("test") == []


def test_corrupt_rows_line_is_the_physical_newline_index():
    """The reported `line` must be the newline index a human can find with
    `sed -n <n+1>p`, and the same number `pgmirror.scan_segments` stores as
    `seg_line`. With `splitlines()` the intact row above counted as two, so the
    genuinely broken row on newline-line 1 was reported as line 2."""
    events.append_event("decision", {"text": "mid\u2028dle"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")                  # line 0
    with open(paths.events_dir("test") / "s.jsonl", "a", encoding="utf-8") as f:
        f.write("{not json\n")                                        # line 1
    assert [(r["segment"], r["line"]) for r in events.corrupt_rows("test")] == [
        ("s.jsonl", 1)]


def test_a_blank_line_still_advances_the_line_index():
    """The property `_segment_lines` exists to keep, tested without any exotic
    character: a skipped line is skipped from the OUTPUT, never from the count.
    `corrupt_rows` reports this number to a human and `pgmirror.scan_segments`
    stores it as `seg_line`, so numbering only surviving rows would renumber the
    mirror the first time a segment gained a blank line -- a parity failure with
    no obvious cause. Every other test here happens to use blank-free fixtures."""
    events.append_event("decision", {"text": "first"}, session_id="s",
                        ts="2026-06-20T10:00:00Z")                  # line 0
    seg = paths.events_dir("test") / "s.jsonl"
    with open(seg, "a", encoding="utf-8") as f:
        f.write("\n")                                             # line 1: blank
        f.write("   \n")                                          # line 2: whitespace
        f.write("{not json\n")                                    # line 3
    assert [(r["segment"], r["line"]) for r in events.corrupt_rows("test")] == [
        ("s.jsonl", 3)]
    assert [i for i, _ in events._segment_lines(seg)] == [0, 3]


@pytest.mark.parametrize("sep", RAW_SEPARATORS, ids=["LS", "PS", "NEL"])
def test_a_malformed_row_that_also_carries_a_separator_is_reported_once(sep):
    """The case the "corrupt_rows' count is a regression detector" claim rests
    on. A genuinely broken row must be reported ONCE however exotic its bytes:
    under `splitlines()` one bad row carrying a separator became two entries, so
    the count moved for a reason that had nothing to do with a writer."""
    d = paths.events_dir("test")
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "s.jsonl", "w", encoding="utf-8") as f:
        f.write('{"broken": "mid' + sep + 'dle"}\n')
    bad = events.corrupt_rows("test")
    assert [(r["segment"], r["line"]) for r in bad] == [("s.jsonl", 0)]
    assert "missing required field" in bad[0]["reason"]
