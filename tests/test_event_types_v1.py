# tests/test_event_types_v1.py
"""The `state/` sidecar contract: `paths.state_dir` and the `pending_embed.json`
marker that `append_event` leaves behind every SEMANTIC row.

The marker is the hand-off between the append path and the background vector
watcher. `append_event` writes one row to one segment and returns; something
else has to notice that a new decision or caveat exists and embed it, and it
has to find that row again WITHOUT re-scanning the ledger. So each semantic
append queues one entry -- `{session_id, segment, seg_line, queued_at}` --
into `<namespace>/state/pending_embed.json`, and the watcher consumes the
queue. Three properties of that entry are what this file pins, because each
one closes a real hole:

* `segment` is the basename `events.segment_name(session_id)` actually wrote
  to, carried beside the RAW `session_id`. A quarantined id (`../x`) lands in
  `unsafe-<sha256[:16]>.jsonl`, so a watcher that opened `f"{session_id}.jsonl"`
  raw would walk the same path the quarantine closed, and then raise.
* `seg_line` is the 1-BASED physical line the new row starts on: the count of
  `b"\\n"` in that one segment before the write, plus one. Counted in binary so
  a CRLF segment (Windows text-mode appends write `\\r\\n`) still counts one per
  row. This is deliberately NOT the mirror's zero-based `seg_line` column
  (`pgmirror.scan_segments`, `events._segment_lines`); the two differ by one,
  and the arithmetic in this file is the reference the watcher is held to.
* The marker is a SECONDARY projection; the row is the primary record.
  `post_tool_use` fires on every tool call, so a marker that cannot be read,
  parsed or appended to must never lose or delay the row: a corrupt line is
  skipped by every reader, a refused append and a raising emitter are
  swallowed, and `append_event` returns the `Event` either way. Every fail-open test here carries a positive control --
  the seam was actually reached -- so none of them can pass vacuously against
  a build that emits nothing at all.

`paths.state_dir(project)` is the namespace's directory for sidecars of this
kind: `project_dir(project) / "state"`, made on demand, joined THROUGH
`project_dir` so a hostile project name lands inside the quarantined namespace
like everything else in the store.

All six v1.x event types are now registered, each by the phase that landed
its owning module: `work_session` and `initiative_declared` with
`worksessions.py` and `initiatives.py` (blueprint P1b), `interceptor_signal`
and `merge_conflict_detected` with `orchestrator/interceptor.py` and
`mindnet/worktree.py` (P1f), and `vector_sync_started` /
`vector_sync_completed` with `watcher.py` (P1h). Their registration
assertions close this file.

Arriving together is the rule and not a preference. A `_PAYLOAD_CONTRACTS`
row names its owner by MODULE PATH and `events.validate_typed_payload`
imports it, so a type registered before its module exists makes `hive append
<type>` die with `ModuleNotFoundError` instead of the contract's `ValueError`;
a SEMANTIC type registered early renders an empty LEDGER body until
`ledger_body` lands. The exemption-parity guards in
`tests/test_architecture_drift.py` are satisfied either way as long as the
`NEVER_CONSOLIDATE` / `NEVER_COLLAPSE` entries land together with the type --
which the two watcher types need not do, being deterministic: consolidation
and display dedupe only ever see the semantic half.
"""
import json
import re
import time
from pathlib import Path

import pytest

from mindhive import embedqueue, events, paths


# --- helpers ---------------------------------------------------------------------

# PDF section 8.1: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ").
QUEUED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ENTRY_KEYS = {"session_id", "segment", "seg_line", "queued_at"}

# Every shape the watcher could find on disk that is NOT a queue. The PDF guards
# only the parse exception; the wrong-shape cases are the totality extension --
# `current_queue["entries"].append` on a list or a string is a second crash on
# the hook path, one the exception guard does not cover.
CORRUPT_MARKERS = [
    pytest.param(b"{not json", id="unparseable"),
    pytest.param(b"[]", id="list-not-dict"),
    pytest.param(b'{"entries": "nope"}', id="entries-not-a-list"),
    pytest.param(b'{"queue": []}', id="dict-without-entries"),
    pytest.param(b'"just a string"', id="scalar"),
    pytest.param(b"\xff\xfe\x00\x00garbage", id="not-utf8"),
    pytest.param(b"", id="empty"),
]


def _marker(project=None):
    """The marker path, joined by hand rather than through `paths.state_dir`:
    `state_dir` mkdirs, and several tests below assert the directory was NOT
    created, so the probe itself must not create it."""
    return paths.project_dir(project) / "state" / "pending_embed.json"


def _entries(project=None):
    """The marker's queue, asserting the documented shape on the way: one JSON
    object per line, each carrying exactly the four documented keys."""
    out = []
    for line in _marker(project).read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        assert isinstance(entry, dict), entry
        assert set(entry) == {"session_id", "segment", "seg_line", "queued_at"}, entry
        out.append(entry)
    return out


def _physical_line(seg, seg_line):
    """Row *seg_line* (1-based) of *seg*, parsed. Split IN THE TEST on the
    same byte the counter counts, `b"\\n"`, so the assertion is about the
    physical line and nothing else. A trailing `b"\\r"` left by a Windows
    text-mode append is JSON whitespace and `json.loads` ignores it."""
    return json.loads(seg.read_bytes().split(b"\n")[seg_line - 1])


def _decision(text, session_id="s1", **kw):
    return events.append_event("decision", {"text": text},
                               session_id=session_id, **kw)


def _touch(session_id="s1", **kw):
    return events.append_event("file_touched", {"path": "src/x.py"},
                               session_id=session_id, **kw)


# --- paths.state_dir ---------------------------------------------------------------

def test_state_dir_is_the_namespace_state_subdirectory_made_on_demand():
    expected = paths.project_dir() / "state"
    assert not expected.exists()
    d = paths.state_dir()
    assert d == expected
    assert d.is_dir()


def test_state_dir_takes_an_explicit_project():
    d = paths.state_dir("p")
    assert d == paths.project_dir("p") / "state"
    assert d.is_dir()
    assert d != paths.state_dir()


def test_state_dir_quarantines_a_hostile_project_inside_the_store(tmp_path,
                                                                  monkeypatch):
    """Joined through `project_dir`, so `../../x` cannot climb out of the store:
    it lands under the quarantined namespace `namespace_name` hands back.

    The store sits two levels below `tmp_path`, so the place an UNQUARANTINED
    join would climb to -- `store_home/../../x` -- is `tmp_path / "x"`, inside
    this test's own directory. With the autouse store at `tmp_path` itself the
    probe was `basetemp.parent / "x"`, a directory the test does not own, and a
    pre-existing `%TEMP%\\x` beside the basetemp failed it while the quarantine
    held. The canary pins the probe to the hostile value: change `../../x` and
    the escape moves, and the canary says so before the negative below could
    pass vacuously against a path nothing was ever going to write."""
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "a" / "home"))
    home = paths.store_home()
    escape = tmp_path / "x"                # where `../../x` climbs to, unquarantined
    assert (home / "../../x").resolve() == escape.resolve()
    assert not escape.exists()             # clear beforehand: the negative is armed

    d = paths.state_dir("../../x")

    assert home.resolve() in d.resolve().parents
    assert d.parent == paths.project_dir("../../x")
    assert d.parent.name.startswith(paths.QUARANTINE_PREFIX)
    assert not escape.exists()


# --- one semantic append, one entry --------------------------------------------------

def test_one_semantic_append_leaves_exactly_one_entry():
    ev = _decision("one")
    assert isinstance(ev, events.Event)          # the return type did not change
    (entry,) = _entries()
    assert set(entry) == ENTRY_KEYS
    assert entry["session_id"] == "s1"
    assert entry["segment"] == events.segment_name("s1") == "s1.jsonl"
    assert entry["seg_line"] == 1
    assert type(entry["seg_line"]) is int
    assert (paths.events_dir() / entry["segment"]).is_file()


def test_the_marker_lives_under_state_not_at_the_namespace_root():
    """PDF 1.7 listed it at the namespace root; 8.1 and 4.4.1 put it under
    `state/`. The plan chose `state/` -- the root is for the store's own files
    and the namespace's projections, and a sidecar queue is neither."""
    _decision("one")
    assert (paths.project_dir() / "state" / "pending_embed.json").is_file()
    assert _marker() == paths.state_dir() / "pending_embed.json"
    assert not (paths.project_dir() / "pending_embed.json").exists()


def test_the_marker_lands_in_the_namespace_the_append_targets():
    _decision("one", project="other")
    assert _marker("other").is_file()
    assert _marker("other") == paths.state_dir("other") / "pending_embed.json"
    assert not _marker().exists()
    (entry,) = _entries("other")
    assert (paths.events_dir("other") / entry["segment"]).is_file()


def test_the_marker_line_is_the_documented_shape():
    """One entry, one line: the object compact, its keys sorted, ASCII only,
    newline-terminated -- the watcher parses this, so the shape is a
    contract. (PDF 8.1 spelled a `{"entries": [...]}` document; that shape is
    still READ, see `embedqueue.load_entries`, but no longer written, because
    a document has to be rewritten whole on every append.)"""
    _decision("one")
    raw = _marker().read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1 and raw.isascii()
    entry = json.loads(raw)
    assert set(entry) == ENTRY_KEYS
    assert raw == json.dumps(entry, sort_keys=True,
                             separators=(",", ":")).encode() + b"\n"


# --- entries accumulate; seg_line is the 1-based physical line ------------------------

def test_a_second_semantic_append_to_the_same_session_appends_a_second_entry():
    _decision("one")
    (first,) = _entries()
    _decision("two")
    entries = _entries()
    assert len(entries) == 2
    assert entries[0] == first                   # the queue accumulates in order
    assert [e["seg_line"] for e in entries] == [1, 2]
    assert {e["session_id"] for e in entries} == {"s1"}
    assert {e["segment"] for e in entries} == {"s1.jsonl"}


def test_three_appends_number_their_lines_one_two_three():
    for text in ("one", "two", "three"):
        _decision(text)
    entries = _entries()
    assert [e["seg_line"] for e in entries] == [1, 2, 3]
    seg = paths.events_dir() / "s1.jsonl"
    assert [_physical_line(seg, e["seg_line"])["payload"]["text"]
            for e in entries] == ["one", "two", "three"]


def test_seg_line_is_the_physical_line_even_after_a_deterministic_row_in_between():
    """The deterministic row still occupies a physical line, so the next
    semantic row is line 3, not entry-count-plus-one. The expected value is
    computed the way the contract states it: `b"\\n"` count before the write,
    plus one."""
    _decision("one")
    _touch()                                     # row 2 -- no entry for it
    seg = paths.events_dir() / "s1.jsonl"
    expected = seg.read_bytes().count(b"\n") + 1
    assert expected == 3
    _decision("three")
    entries = _entries()
    assert [e["seg_line"] for e in entries] == [1, expected]
    assert _physical_line(seg, expected)["payload"] == {"text": "three"}
    assert _physical_line(seg, 2)["type"] == "file_touched"


def test_seg_line_counts_only_the_segment_being_written():
    """One segment, never the ledger: a fresh session starts at line 1 no
    matter how many rows the namespace already holds."""
    for text in ("one", "two", "three"):
        _decision(text, session_id="s1")
    _decision("other", session_id="s2")
    entries = _entries()
    assert [(e["session_id"], e["seg_line"]) for e in entries] == [
        ("s1", 1), ("s1", 2), ("s1", 3), ("s2", 1)]
    assert entries[-1]["segment"] == "s2.jsonl"


@pytest.mark.parametrize("ending", [pytest.param(b"\n", id="lf"),
                                    pytest.param(b"\r\n", id="crlf")])
def test_a_preseeded_segment_of_n_rows_yields_seg_line_n_plus_one(ending):
    """Counted in BINARY: a segment written by a Windows text-mode append
    carries `\\r\\n`, and one `b"\\n"` per row is still one row."""
    n = 5
    d = paths.events_dir()
    d.mkdir(parents=True, exist_ok=True)
    seg = d / "s1.jsonl"
    rows = [events.canonical_json(events.to_dict(events.Event(
                ts="2026-09-14T09:00:%02dZ" % i, session_id="s1",
                type="decision", payload={"text": "seed %d" % i})))
            for i in range(n)]
    seg.write_bytes(b"".join(r.encode("utf-8") + ending for r in rows))
    assert seg.read_bytes().count(b"\n") == n
    _decision("new", ts="2026-09-14T09:01:00Z")
    (entry,) = _entries()
    assert entry["seg_line"] == n + 1
    assert _physical_line(seg, n + 1)["payload"] == {"text": "new"}
    assert [e.payload["text"] for e in events.read_all()][-1] == "new"


def test_an_empty_preexisting_segment_starts_at_line_one():
    """The missing-segment case with a file handle: a segment that exists but
    holds no bytes has zero newlines, so the first row is line 1 -- not 0 (a
    count with no `+ 1`) and not 2 (an `exists()` branch that assumes one)."""
    d = paths.events_dir()
    d.mkdir(parents=True, exist_ok=True)
    seg = d / "s1.jsonl"
    seg.write_bytes(b"")
    _decision("first")
    (entry,) = _entries()
    assert entry["seg_line"] == 1
    assert _physical_line(seg, 1)["payload"] == {"text": "first"}


def test_the_newline_count_covers_a_segment_larger_than_one_read_chunk():
    """The counter reads the segment in fixed-size binary chunks rather than
    `readlines()` over the whole file -- the PDF's shape, refused because a live
    segment can run to tens of megabytes and `post_tool_use` appends on every
    tool call. A counter that read ONE chunk and stopped would pass every other
    test in this file, whose segments are a few hundred bytes, so this one seeds
    more than a mebibyte (the example chunk size) and expects every row counted.
    Newlines are single bytes, so no chunk boundary can split one; the only way
    to get this wrong is to stop early."""
    n = 3000
    d = paths.events_dir()
    d.mkdir(parents=True, exist_ok=True)
    seg = d / "s1.jsonl"
    seed = events.canonical_json(events.to_dict(events.Event(
        ts="2026-09-14T09:00:00Z", session_id="s1", type="decision",
        payload={"text": "x" * 400}))).encode("utf-8") + b"\n"
    seg.write_bytes(seed * n)
    assert seg.stat().st_size > (1 << 20), "fixture no longer spans a chunk"
    assert seg.read_bytes().count(b"\n") == n
    _decision("new", ts="2026-09-14T09:01:00Z")
    (entry,) = _entries()
    assert entry["seg_line"] == n + 1
    assert _physical_line(seg, n + 1)["payload"] == {"text": "new"}


# --- a quarantined session ----------------------------------------------------------

def test_a_quarantined_session_gets_a_marker_naming_its_real_segment():
    """[Tinjauan I.1] The entry carries the segment that was WRITTEN, so the
    watcher never opens `f"{session_id}.jsonl"` raw. The id itself stays raw
    in both the row and the entry: sanitize the path, keep the fact."""
    ev = _decision("hostile", session_id="../x")
    assert ev.session_id == "../x"
    (entry,) = _entries()
    assert entry["session_id"] == "../x"
    assert entry["segment"] == events.segment_name("../x")
    assert entry["segment"].startswith(events.QUARANTINE_PREFIX)
    assert entry["segment"].endswith(".jsonl")
    assert entry["seg_line"] == 1
    seg = paths.events_dir() / entry["segment"]
    assert seg.is_file()
    assert [p.name for p in paths.events_dir().glob("*.jsonl")] == [entry["segment"]]
    # Reading it back the way the watcher will: through the entry, no OSError.
    row = _physical_line(seg, entry["seg_line"])
    assert row["session_id"] == "../x" and row["payload"] == {"text": "hostile"}
    assert [e.session_id for e in events.read_all()] == ["../x"]
    assert not (paths.events_dir() / "x.jsonl").exists()
    assert not (paths.events_dir().parent / "x.jsonl").exists()


# --- deterministic types emit nothing --------------------------------------------------

@pytest.mark.parametrize("det_type", ["file_touched", "command_run", "user_prompt"])
def test_a_deterministic_append_adds_no_entry_to_an_existing_marker(det_type):
    assert det_type in events.DETERMINISTIC_TYPES
    _decision("one")
    before = _entries()
    assert len(before) == 1
    events.append_event(det_type, {"text": "x"}, session_id="s1")
    events.append_event(det_type, {"text": "y"}, session_id="s1")
    assert _entries() == before


def test_a_deterministic_append_alone_creates_no_marker_and_no_state_dir():
    """`state_dir` mkdirs, so it is reached only when there is something to
    write; two telemetry rows leave the namespace without a `state/` at all.
    The positive control at the end proves the counter still saw those rows."""
    _touch()
    _touch()
    assert not _marker().exists()
    assert not (paths.project_dir() / "state").exists()
    _decision("three")
    assert [e["seg_line"] for e in _entries()] == [3]


# --- a corrupt or foreign marker is appended to, a valid one is extended ---------------

@pytest.mark.parametrize("junk", CORRUPT_MARKERS)
def test_a_corrupt_marker_is_appended_to_not_raised(junk):
    """A corrupt marker used to be REPLACED on the next append -- which cost
    the read the append no longer makes. It is appended to instead: the junk
    is skipped by every reader, and the new entry, on a line of its own even
    after a tail with no newline, is what they see."""
    _marker().parent.mkdir(parents=True, exist_ok=True)
    _marker().write_bytes(junk)
    ev = _decision("one")
    assert isinstance(ev, events.Event)
    assert _marker().read_bytes().startswith(junk)     # not replaced
    (entry,) = embedqueue.load_entries(_marker())
    assert set(entry) == ENTRY_KEYS
    assert entry["session_id"] == "s1" and entry["seg_line"] == 1
    assert _physical_line(paths.events_dir() / "s1.jsonl", 1)["payload"] == {"text": "one"}


def test_a_valid_existing_queue_is_preserved_and_extended():
    """Entries ACCUMULATE. The watcher is the only consumer and, below the
    bound, the only truncator; the append path never drops what an earlier
    append queued. Seeded in the DOCUMENT shape an older writer left, so this
    is also the upgrade path: the document's rows and the appended lines are
    read together."""
    seed = {"session_id": "older", "segment": "older.jsonl", "seg_line": 7,
            "queued_at": "2026-09-13T00:00:00Z"}
    _marker().parent.mkdir(parents=True, exist_ok=True)
    _marker().write_text(json.dumps({"entries": [seed]}, indent=2), encoding="utf-8")
    _decision("one")
    entries = embedqueue.load_entries(_marker())
    assert entries[0] == seed
    assert len(entries) == 2
    assert entries[1]["session_id"] == "s1" and entries[1]["seg_line"] == 1


# --- queued_at --------------------------------------------------------------------------

def test_queued_at_is_utc_iso8601_ending_in_z():
    """The format sorts lexicographically, so bracketing the append between two
    UTC stamps catches a LOCAL time wearing a `Z` -- on any box not at UTC the
    stamp would fall outside the bracket."""
    before = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _decision("one")
    after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (entry,) = _entries()
    stamp = entry["queued_at"]
    assert QUEUED_AT_RE.match(stamp), stamp
    time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")   # every field is a real one
    assert before <= stamp <= after


# --- the emitter seam: once per semantic append, inside the module global -------------

def test_the_emitter_is_called_once_per_semantic_append_with_the_written_segment(monkeypatch):
    """`append_event` reaches the emitter through the events-module global
    name, so a test (and a later carve-out into its own module) can swap it.
    The spy sees `(project, session_id, segment, seg_line)` -- the segment
    that was written and the 1-based line -- and nothing for telemetry."""
    calls = []

    def spy(project, session_id, segment, seg_line):
        calls.append((project, session_id, segment, seg_line))

    monkeypatch.setattr(events, "_emit_pending_embed_marker", spy)
    _decision("one")
    assert calls == [(None, "s1", "s1.jsonl", 1)]
    _touch()
    assert len(calls) == 1
    _decision("three")
    assert calls[1] == (None, "s1", "s1.jsonl", 3)
    assert type(calls[1][3]) is int
    _decision("elsewhere", project="other")
    assert calls[2] == ("other", "s1", "s1.jsonl", 1)
    _decision("hostile", session_id="../x")
    assert calls[3][1] == "../x"
    assert calls[3][2] == events.segment_name("../x")
    # The spy wrote nothing, and nothing else did either: the seam is the only writer.
    assert not _marker().exists()
    assert not _marker("other").exists()


# --- fail-open: the row is never lost ---------------------------------------------------

def test_a_raising_emitter_never_loses_the_row(monkeypatch):
    calls = []

    def boom(project, session_id, segment, seg_line):
        calls.append(seg_line)
        raise RuntimeError("marker exploded")

    monkeypatch.setattr(events, "_emit_pending_embed_marker", boom)
    ev = _decision("kept", ts="2026-09-14T10:00:00Z")
    assert isinstance(ev, events.Event)
    assert ev.payload == {"text": "kept"}
    assert calls == [1]                          # positive control: it was reached
    seg = paths.events_dir() / "s1.jsonl"
    assert _physical_line(seg, 1)["payload"] == {"text": "kept"}
    assert [e.payload["text"] for e in events.read_all()] == ["kept"]
    # And the lock was released: the next append is not wedged.
    ev2 = _decision("also kept", ts="2026-09-14T10:00:01Z")
    assert ev2.payload == {"text": "also kept"}
    assert calls == [1, 2]


def test_a_marker_append_the_os_refuses_never_loses_the_row(monkeypatch):
    """The OS refusing the queue itself: a directory sits where the file
    should be, so the append's `open(..., "a")` is denied (`IsADirectoryError`
    on POSIX, `PermissionError` on Windows). The row must still land, and the
    lock must still be released."""
    _marker().mkdir(parents=True)
    real = events._emit_pending_embed_marker
    calls = []

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(events, "_emit_pending_embed_marker", spy)
    ev = _decision("kept")
    assert isinstance(ev, events.Event)
    assert calls == [(None, "s1", "s1.jsonl", 1)]   # positive control: attempted
    assert _physical_line(paths.events_dir() / "s1.jsonl", 1)["payload"] == {"text": "kept"}
    assert _marker().is_dir() and list(_marker().iterdir()) == []
    assert [e.payload["text"] for e in events.read_all()] == ["kept"]
    # And the lock was released: the next append is not wedged.
    assert _decision("also kept").payload == {"text": "also kept"}


def test_an_unreadable_segment_skips_the_marker_never_the_row(monkeypatch):
    """The count is the marker's, not the row's. Before the marker existed
    `append_event` never opened the segment for READ; a segment that is
    appendable but unreadable (a deny-read ACE, mode 0o200, an EIO from the
    read) must therefore still take the row -- and the marker is skipped
    rather than filed under line 1, because a wrong line would make the
    watcher embed the wrong row, which is worse than no marker at all.
    `embedqueue.newline_cursor` answers None for such a segment (its own
    tests cover the raise); this pins what `append_event` does with None."""
    calls = []
    emitted = []

    def unreadable(path, prior=None):
        calls.append(Path(path))
        return None                # what `embedqueue.newline_cursor` answers

    monkeypatch.setattr(events, "_newline_cursor", unreadable)
    monkeypatch.setattr(events, "_emit_pending_embed_marker",
                        lambda *a: emitted.append(a))
    ev = _decision("kept")
    assert isinstance(ev, events.Event)
    assert calls == [paths.events_dir() / "s1.jsonl"]   # positive control
    assert _physical_line(paths.events_dir() / "s1.jsonl", 1)["payload"] == {"text": "kept"}
    assert emitted == []                                # skipped, not line 1
    assert not _marker().exists()
    # The lock was released and the next append is not wedged either.
    ev2 = _decision("also kept")
    assert ev2.payload == {"text": "also kept"}
    assert [e.payload["text"] for e in events.read_all()] == ["kept", "also kept"]


# --- cross-platform: atomic publish, retrying read ---------------------------------------

def test_a_semantic_append_adds_one_line_and_rewrites_nothing(monkeypatch):
    """The queue used to be one JSON document, re-serialised and published
    through `write_text_atomic` on every semantic append -- a rewrite that grew
    with the queue, inside the append lock. An append is now one line
    appended: the earlier bytes are untouched and nothing is published."""
    real = paths.write_text_atomic
    calls = []

    def spy(path, text):
        calls.append(Path(path))
        real(path, text)

    monkeypatch.setattr(paths, "write_text_atomic", spy)
    _decision("one")
    first = _marker().read_bytes()
    _decision("two")
    second = _marker().read_bytes()
    assert _marker() not in calls, "the append path re-published the whole queue"
    assert second.startswith(first) and second.count(b"\n") == 2
    assert [e["seg_line"] for e in _entries()] == [1, 2]
    # No private temp name left behind either.
    assert list((paths.project_dir() / "state").glob("*.tmp")) == []


def test_a_semantic_append_reads_nothing_back(monkeypatch):
    """The other half of the old cost: the document had to be READ before it
    could be re-serialised. An append reads no queue at all; the readers that
    remain (compaction, recovery, the watcher) go through `read_text_retry`,
    which `tests/test_embedqueue.py` holds them to."""
    real = paths.read_text_retry
    reads = []

    def spy(path):
        reads.append(Path(path))
        return real(path)

    monkeypatch.setattr(paths, "read_text_retry", spy)
    _decision("one")
    _decision("two")
    assert _marker() not in reads
    assert [e["seg_line"] for e in _entries()] == [1, 2]


# --- the two v1.x types blueprint P1b registers (DATA_MODEL.md section 6) -----------

P1B_TYPES = ("work_session", "initiative_declared")
OWNERS = {"work_session": ".worksessions", "initiative_declared": ".initiatives"}
# Blueprint P1f registered `interceptor_signal` (orchestrator/interceptor.py)
# and `merge_conflict_detected` (mindnet/worktree.py) together with their owner
# modules, the way this file's docstring says each type must arrive. P1h did
# the same for the watcher's pair, below.
P1H_TYPES = ("vector_sync_started", "vector_sync_completed")


@pytest.mark.parametrize("etype", P1B_TYPES)
def test_p1b_types_are_registered_semantic_exempt_and_contracted(etype):
    from mindhive import consolidate, contracts, dedupe, pgschema
    assert etype in events.VALID_TYPES and etype in events.SEMANTIC_TYPES
    assert etype not in events.DETERMINISTIC_TYPES
    assert pgschema.EVENT_TYPE_CLASSES[etype] == "semantic"
    assert etype in consolidate.NEVER_CONSOLIDATE and etype in dedupe.NEVER_COLLAPSE
    module, hint = contracts._PAYLOAD_CONTRACTS[etype]
    assert module == OWNERS[etype] and hint.startswith("hive worksession ")
    with pytest.raises(ValueError, match=r"\(use: hive worksession "):
        contracts.validate_typed_payload(etype, {})


@pytest.mark.parametrize("etype", P1H_TYPES)
def test_p1h_types_are_registered_deterministic_and_contracted(etype):
    from mindhive import contracts, pgschema
    assert etype in events.VALID_TYPES and etype in events.DETERMINISTIC_TYPES
    assert pgschema.EVENT_TYPE_CLASSES[etype] == "deterministic"
    module, hint = contracts._PAYLOAD_CONTRACTS[etype]
    assert module == ".watcher" and "hive watcher run" in hint
    with pytest.raises(ValueError, match=r"needs marker, count, duration_s"):
        contracts.validate_typed_payload(etype, {})


@pytest.mark.parametrize("etype", P1H_TYPES)
def test_a_sync_row_never_queues_its_own_embedding(etype):
    """The watcher indexes SEMANTIC rows. A semantic sync row would queue an
    embed of itself, whose completion row would queue another one, and the
    queue would never be empty again."""
    assert etype not in events.SEMANTIC_TYPES
    assert etype not in events.MEMORY_VIEW_TYPES
    events.append_event(etype, {"marker": "processing_embed_1.json",
                                "count": 1, "duration_s": 0.5},
                        session_id="sync-marker")
    assert not _marker().exists()


@pytest.mark.parametrize("bad, why", [
    ({"count": 1, "duration_s": 0.0}, r"needs marker, count, duration_s"),
    ({"marker": "nope.json", "count": 1, "duration_s": 0.0},
     r"must name a processing_embed_\*\.json claim"),
    ({"marker": "processing_embed_1.json", "count": -1, "duration_s": 0.0},
     r"whole number of rows"),
    ({"marker": "processing_embed_1.json", "count": True, "duration_s": 0.0},
     r"whole number of rows"),
    ({"marker": "processing_embed_1.json", "count": 1, "duration_s": -0.5},
     r"non-negative number of seconds"),
])
def test_a_hand_appended_sync_row_is_refused_with_its_hint(bad, why):
    from mindhive import contracts
    with pytest.raises(ValueError, match=why):
        contracts.validate_typed_payload("vector_sync_completed", bad)
    with pytest.raises(ValueError, match=r"there is no command"):
        contracts.validate_typed_payload("vector_sync_completed", bad)


def test_a_well_formed_sync_row_passes_the_contract():
    from mindhive import contracts
    assert contracts.validate_typed_payload(
        "vector_sync_started",
        {"marker": "processing_embed_17.json", "count": 3,
         "duration_s": 1.25}) is None


def test_p1b_appends_queue_one_marker_entry_per_row():
    from mindhive import initiatives, worksessions as ws
    step = ws.create_work_session_step("execution", "TDD_RED", False)
    ws.record_work_session_step("ws-marker", step)
    initiatives.declare_initiative("ws-marker", "flaky lock")
    entries = [e for e in _entries() if e["session_id"] == "ws-marker"]
    assert [e["seg_line"] for e in entries] == [1, 2]
    assert {e["segment"] for e in entries} == {events.segment_name("ws-marker")}


def test_p1b_types_render_ledger_bodies_carrying_their_tags():
    from mindhive import initiatives, projector, worksessions as ws
    step = ws.create_work_session_step("PRD", "AST_PARSE", True, initiative_reason="audit")
    ws.record_work_session_step("ws-ledger", step)
    tag = initiatives.declare_initiative("ws-ledger", "audit", "finding:3")
    bodies = {ev.type: content
              for _f, content, ev in projector.build_ledger_entries(events.read_all())}
    assert "PRD" in bodies["work_session"] and step.technical_action in bodies["work_session"]
    assert tag in bodies["initiative_declared"]
