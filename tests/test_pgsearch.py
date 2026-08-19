# tests/test_pgsearch.py
"""The eighth tool, and the byte format it shares with the loader.

Two halves, and the split is what makes the file runnable with no server, no
encoder and no NumPy in the default tier:

* **the format** — `vectors.pack_embedding` / `unpack_matrix` / `rank`. Pure
  functions over bytes and floats. The loader WRITES with the first and the
  search READS with the second, in different processes and different
  credentials, so the day they disagree the symptom is not an exception; it is
  plausible nonsense in the ranking. These tests are the only place the two are
  compared directly.
* **the tool** — `pgsearch.search_events_by_meaning`, driven against a fake
  cursor. It cannot answer whether the SQL compiles (that is the `pg` tier's
  job) but it is where the argument guards, the empty-index answer, the orphan
  drop and the never-raises contract are asserted.

NumPy rides in with `sentence-transformers`, which is an optional extra, so
every test that needs it is skipped rather than failed on a base install --
the same rule the rest of the semantic layer follows.
"""
import struct

import pytest

from mindhive import pgschema, pgsearch, pgtools, vectors

np = pytest.importorskip("numpy", reason="the vector format needs NumPy")


# --------------------------------------------------------------------------
# the byte format
# --------------------------------------------------------------------------

def test_a_packed_vector_is_little_endian_float32_of_the_declared_width():
    blob, dim, norm = vectors.pack_embedding([1.0, 2.0, 2.0])
    assert dim == 3
    assert len(blob) == dim * 4, "the DDL's octet_length check assumes 4 bytes"
    assert norm == pytest.approx(3.0)
    # Byte order asserted against `struct`, not against `array.tobytes()`:
    # `array` uses whatever the CPU is, and the mirror is a file another machine
    # may read.
    assert blob == struct.pack("<3f", 1 / 3, 2 / 3, 2 / 3)


def test_packing_normalises_so_cosine_is_a_plain_dot_product():
    blob, dim, _ = vectors.pack_embedding([3.0, 4.0])
    m = vectors.unpack_matrix([blob], dim)
    assert float(np.linalg.norm(m[0])) == pytest.approx(1.0, abs=1e-6)


def test_the_recorded_norm_is_the_magnitude_before_normalising():
    """The one piece of evidence normalisation destroys. Without it a zero
    vector -- which scores 0.0 against everything and reads exactly like a
    weak-but-real match -- cannot be told apart after the fact."""
    _, _, norm = vectors.pack_embedding([0.0, 0.0, 0.0])
    assert norm == 0.0
    _, _, norm = vectors.pack_embedding([0.6, 0.8])
    assert norm == pytest.approx(1.0)


def test_a_zero_vector_is_stored_rather_than_dividing_by_zero():
    blob, dim, norm = vectors.pack_embedding([0.0, 0.0])
    m = vectors.unpack_matrix([blob], dim)
    assert norm == 0.0
    assert not np.isnan(m).any(), "normalising a zero vector produced NaN"
    assert float((m @ np.array([1.0, 0.0], dtype="<f4"))[0]) == 0.0


def test_packing_refuses_a_zero_width_vector():
    with pytest.raises(ValueError):
        vectors.pack_embedding([])


def test_unpacking_refuses_a_truncated_blob_instead_of_reshaping_it():
    """THE failure this guard exists for. A short buffer reshapes without error
    into a matrix of the wrong length, and every score after it is garbage that
    looks like data."""
    blob, dim, _ = vectors.pack_embedding([1.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError) as e:
        vectors.unpack_matrix([blob[:-4]], dim)
    assert "truncated" in str(e.value) or "expected" in str(e.value)


def test_unpacking_refuses_a_blob_that_is_too_long():
    blob, dim, _ = vectors.pack_embedding([1.0, 0.0])
    with pytest.raises(ValueError):
        vectors.unpack_matrix([blob + blob], dim)


def test_the_round_trip_preserves_row_order():
    """`unpack_matrix` concatenates and reshapes, so a row-order mistake would
    silently attach every score to the wrong event."""
    rows = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
    blobs = [vectors.pack_embedding(r)[0] for r in rows]
    m = vectors.unpack_matrix(blobs, 2)
    assert m.shape == (3, 2)
    assert [round(float(x), 6) for x in m @ np.array([1.0, 0.0], dtype="<f4")] \
        == [1.0, 0.0, -1.0]


def test_rank_returns_best_first_with_cosine_scores():
    m = vectors.unpack_matrix(
        [vectors.pack_embedding(v)[0]
         for v in ([1.0, 0.0], [0.7, 0.7], [0.0, 1.0], [-1.0, 0.0])], 2)
    out = vectors.rank([1.0, 0.0], m, 3)
    assert [i for i, _ in out] == [0, 1, 2]
    assert out[0][1] == pytest.approx(1.0, abs=1e-6)
    assert out[-1][1] == pytest.approx(0.0, abs=1e-6)


def test_rank_normalises_the_query_too():
    """Both sides must be unit length or the score is not a cosine -- and an
    un-normalised query would make the number scale with how long the sentence
    happened to be."""
    m = vectors.unpack_matrix([vectors.pack_embedding([1.0, 0.0])[0]], 1 * 2)
    assert vectors.rank([50.0, 0.0], m, 1)[0][1] == pytest.approx(1.0, abs=1e-6)


def test_rank_asks_for_more_than_there_is_without_falling_over():
    m = vectors.unpack_matrix([vectors.pack_embedding([1.0, 0.0])[0]], 2)
    assert len(vectors.rank([1.0, 0.0], m, 50)) == 1
    assert vectors.rank([1.0, 0.0], m, 0) == []


def test_rank_over_an_empty_matrix_is_empty_not_an_exception():
    assert vectors.rank([1.0, 0.0], np.zeros((0, 2), dtype="<f4"), 5) == []


# --------------------------------------------------------------------------
# the tool: argument guards, answerable with nothing installed
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_backend(monkeypatch):
    """No DSN, so the availability guard answers before anything is attempted.
    Each test that wants to get past it installs its own fake session."""
    monkeypatch.delenv("MINDHIVE_PG_DSN_RO", raising=False)
    from mindhive import pgstore
    pgstore.reset_state()
    yield
    pgstore.reset_state()


@pytest.mark.parametrize("bad", [None, "", "   ", 7, [], {}])
def test_an_unusable_query_is_bad_argument_not_a_crash(bad):
    env = pgsearch.search_events_by_meaning(bad)
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_argument"


@pytest.mark.parametrize("bad", [0, -1, 51, 1000, "10", True])
def test_a_limit_outside_its_published_bound_is_refused(bad):
    env = pgsearch.search_events_by_meaning("x", limit=bad)
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_argument"
    published = pgschema.TOOL_SCHEMAS["search_events_by_meaning"]["properties"]
    assert str(published["limit"]["maximum"]) in env["error"]["detail"] or \
        "integer" in env["error"]["detail"]


def test_an_unusable_event_type_is_refused_before_any_work():
    env = pgsearch.search_events_by_meaning("x", event_type="")
    assert env["ok"] is False and env["error"]["code"] == "bad_argument"


def test_a_non_numeric_min_score_is_refused():
    env = pgsearch.search_events_by_meaning("x", min_score="high")
    assert env["ok"] is False and env["error"]["code"] == "bad_argument"


def test_arguments_are_validated_before_availability():
    """Same rule the other seven follow: with the backend down every call could
    answer `backend_unavailable`, and reporting that for a call that was ALSO
    malformed sends the model to fix the server when the fix is its own."""
    env = pgsearch.search_events_by_meaning("")
    assert env["error"]["code"] == "bad_argument"


def test_with_no_backend_it_answers_in_the_wire_vocabulary():
    env = pgsearch.search_events_by_meaning("a real question")
    assert env["ok"] is False
    assert env["error"]["code"] in pgtools.ERROR_CODES
    assert env["freshness"] is not None


def test_a_missing_encoder_is_deps_missing_and_names_the_extra(monkeypatch):
    """A model that gets `db_error` retries. One that gets `deps_missing`
    naming `.[vectors]` stops asking and uses another tool."""
    monkeypatch.setattr(pgsearch, "_missing_deps",
                        lambda: ["sentence-transformers"])
    env = pgsearch.search_events_by_meaning("x")
    assert env["ok"] is False
    assert env["error"]["code"] == "deps_missing"
    assert "sentence-transformers" in env["error"]["detail"]
    assert "[vectors]" in env["error"]["detail"]


def test_the_dependency_probe_really_looks(monkeypatch):
    """CANARY for the test above: `_missing_deps` returning `[]` unconditionally
    would make every deps test pass while checking nothing."""
    import builtins
    real = builtins.__import__

    def refuse(name, *a, **k):
        if name == "sentence_transformers":
            raise ImportError("planted")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert "sentence-transformers" in pgsearch._missing_deps()


def test_an_encoder_that_raises_is_reported_not_propagated(monkeypatch):
    monkeypatch.setattr(pgsearch, "_missing_deps", lambda: [])
    monkeypatch.setattr(pgsearch, "_search",
                        lambda *a, **k: pytest.fail("reached the query"))
    monkeypatch.setattr(pgtools, "_availability", lambda *a: None)
    monkeypatch.setattr(pgtools, "_freshness", lambda p: {"available": True})
    monkeypatch.setattr(pgtools, "_parity_refusal", lambda *a: None)
    monkeypatch.setattr(vectors, "encode",
                        lambda t: (_ for _ in ()).throw(OSError("no model")))
    env = pgsearch.search_events_by_meaning("x")
    assert env["ok"] is False
    assert env["error"]["code"] == "deps_missing"
    assert "no model" in env["error"]["detail"]


# --------------------------------------------------------------------------
# the tool: driven against a fake cursor
# --------------------------------------------------------------------------

class FakeCursor:
    """Answers the two statements `_search` runs, in order."""

    def __init__(self, vectors_rows, event_rows):
        self._answers = [vectors_rows, event_rows]
        self._current = []
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._current = self._answers.pop(0) if self._answers else []

    def fetchall(self):
        return self._current


def _install(monkeypatch, cursor):
    import contextlib

    @contextlib.contextmanager
    def session(project, timeout_ms):
        yield cursor

    monkeypatch.setattr(pgtools, "_session", session)
    monkeypatch.setattr(pgtools, "_availability", lambda *a: None)
    monkeypatch.setattr(pgtools, "_freshness",
                        lambda p: {"available": True, "stale": False,
                                   "parity_ok": True})
    monkeypatch.setattr(pgtools, "_parity_refusal", lambda *a: None)
    monkeypatch.setattr(pgtools, "_project", lambda: "proj")
    monkeypatch.setattr(pgsearch, "_missing_deps", lambda: [])
    monkeypatch.setattr(vectors, "encode", lambda texts: [[1.0, 0.0]])


def _vector_row(segment, line, vec, event_type="decision"):
    blob, dim, _ = vectors.pack_embedding(vec)
    return (segment, line, dim, blob)


def _event_row(seq, segment, line, text, event_type="decision"):
    return (seq, "2026-01-01T00:00:00Z", None, "s1", event_type,
            {"text": text}, [], segment, line)


def test_hits_come_back_scored_and_in_rank_order(monkeypatch):
    cur = FakeCursor(
        [_vector_row("a.jsonl", 0, [0.0, 1.0]),
         _vector_row("a.jsonl", 1, [1.0, 0.0]),
         _vector_row("a.jsonl", 2, [0.7, 0.7])],
        [_event_row(1, "a.jsonl", 0, "orthogonal"),
         _event_row(2, "a.jsonl", 1, "exact"),
         _event_row(3, "a.jsonl", 2, "close")])
    _install(monkeypatch, cur)
    env = pgsearch.search_events_by_meaning("anything", limit=3)
    assert env["ok"] is True
    assert env["columns"][0] == "score"
    texts = [r["payload"]["text"] for r in env["rows"]]
    assert texts == ["exact", "close", "orthogonal"]
    assert env["rows"][0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert env["summary"]["vectors_compared"] == 3
    assert env["summary"]["dim"] == 2


def test_min_score_turns_no_good_match_into_an_explicit_empty_answer(monkeypatch):
    """There is ALWAYS a nearest neighbour. Without a threshold a question the
    ledger never recorded still returns rows, and a model reads them as the
    answer."""
    cur = FakeCursor([_vector_row("a.jsonl", 0, [0.0, 1.0])],
                     [_event_row(1, "a.jsonl", 0, "unrelated")])
    _install(monkeypatch, cur)
    env = pgsearch.search_events_by_meaning("anything", min_score=0.5)
    assert env["ok"] is True
    assert env["row_count"] == 0
    assert "min_score" in env["notice"]
    assert len(cur.statements) == 1, "it hydrated rows it had already rejected"


def test_an_index_that_was_never_built_says_so_and_names_the_command(monkeypatch):
    """Not an error and not an empty ledger. The distinction is the whole
    message: the same events are still reachable by type and by payload."""
    cur = FakeCursor([], [])
    _install(monkeypatch, cur)
    env = pgsearch.search_events_by_meaning("anything")
    assert env["ok"] is True
    assert env["row_count"] == 0
    assert "hive pg embed" in env["notice"]
    assert "search_events_by_type" in env["notice"]


def test_a_vector_whose_event_moved_is_dropped_and_counted(monkeypatch):
    """The two tables are refreshed by separate commands, so a segment reloaded
    after the index was built can leave a vector pointing at a row that has
    moved. Returning it as an empty row would look like a memo with no text."""
    cur = FakeCursor(
        [_vector_row("a.jsonl", 0, [1.0, 0.0]),
         _vector_row("a.jsonl", 9, [0.9, 0.1])],
        [_event_row(1, "a.jsonl", 0, "still here")])
    _install(monkeypatch, cur)
    env = pgsearch.search_events_by_meaning("anything", limit=5)
    assert env["ok"] is True
    assert [r["payload"]["text"] for r in env["rows"]] == ["still here"]
    assert env["summary"]["orphaned"] == 1
    assert "re-run `hive pg embed`" in env["notice"]


def test_mixed_vector_widths_are_refused_rather_than_reshaped(monkeypatch):
    """Two models under one name, or a half-finished migration between them.
    Reshaping the concatenation would produce scores from interleaved halves of
    unrelated vectors."""
    wide, _, _ = vectors.pack_embedding([1.0, 0.0, 0.0])
    cur = FakeCursor([_vector_row("a.jsonl", 0, [1.0, 0.0]),
                      ("a.jsonl", 1, 3, wide)], [])
    _install(monkeypatch, cur)
    env = pgsearch.search_events_by_meaning("anything")
    assert env["ok"] is False
    assert env["error"]["code"] == "db_error"
    assert "rebuild" in env["error"]["detail"]


def test_the_scan_ceiling_is_reported_rather_than_hidden(monkeypatch):
    """A silently truncated search space reads as a complete one. The bias is
    NEWEST-first, which is a bias and is named as such."""
    monkeypatch.setattr(pgsearch, "VECTOR_SCAN_CEILING", 2)
    cur = FakeCursor(
        [_vector_row("a.jsonl", 0, [1.0, 0.0]),
         _vector_row("a.jsonl", 1, [0.9, 0.1])],
        [_event_row(1, "a.jsonl", 0, "one"), _event_row(2, "a.jsonl", 1, "two")])
    _install(monkeypatch, cur)
    env = pgsearch.search_events_by_meaning("anything", limit=2)
    assert env["ok"] is True
    assert env["truncated"]["rows"] is True
    assert "most recent vectors" in env["notice"]


def test_a_type_filter_reaches_the_statement(monkeypatch):
    cur = FakeCursor([_vector_row("a.jsonl", 0, [1.0, 0.0])],
                     [_event_row(1, "a.jsonl", 0, "hit")])
    _install(monkeypatch, cur)
    pgsearch.search_events_by_meaning("anything", event_type="caveat")
    sql, params = cur.statements[0]
    assert "event_type" in sql
    assert "caveat" in params


def test_a_driver_failure_becomes_an_envelope_not_an_exception(monkeypatch):
    class Exploding(FakeCursor):
        def execute(self, sql, params=None):
            raise RuntimeError("the socket went away")

    _install(monkeypatch, Exploding([], []))
    env = pgsearch.search_events_by_meaning("anything")
    assert env["ok"] is False
    assert env["error"]["code"] in pgtools.ERROR_CODES


def test_the_stale_notice_still_rides_a_successful_search(monkeypatch):
    """Every other tool carries it; a semantic answer off a stale index is the
    one most likely to be read as current."""
    cur = FakeCursor([_vector_row("a.jsonl", 0, [1.0, 0.0])],
                     [_event_row(1, "a.jsonl", 0, "hit")])
    _install(monkeypatch, cur)
    monkeypatch.setattr(pgtools, "_freshness",
                        lambda p: {"available": True, "stale": True,
                                   "stale_reason": "the ledger grew",
                                   "parity_ok": True})
    env = pgsearch.search_events_by_meaning("anything")
    assert env["ok"] is True
    assert "hive pg refresh" in env["notice"]


def test_the_notice_is_prose_and_not_one_character_per_word(monkeypatch):
    """CANARY for a specific bug this file was written after: `notice_for`
    EXTENDS its extra argument, so passing a bare string spreads it one
    character per part and joins it back with spaces."""
    cur = FakeCursor([], [])
    _install(monkeypatch, cur)
    env = pgsearch.search_events_by_meaning("anything")
    assert " ".join("hive") not in env["notice"], \
        "the notice was spread one character at a time"
    assert "hive pg embed" in env["notice"]


# --------------------------------------------------------------------------
# the module boundary
# --------------------------------------------------------------------------

def test_the_search_module_never_imports_the_admin_one():
    """Same rule as every other reader: the owner DSN lives in `pgmirror` and
    nothing on the AI path may name it. Asserted over the AST so a future
    convenience import fails here rather than in a security review."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "mindhive"
           / "pgsearch.py").read_text(encoding="utf-8")
    named = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.level:
            named.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            named.update(a.name.split(".")[-1] for a in node.names)
    assert "pgmirror" not in named, \
        "the search path imported the module holding the owner credential"


def test_pgtools_reaches_the_search_body_only_through_a_lazy_import():
    """`mcp_server` imports `pgtools` at startup to LIST the tools with the
    database down and neither extra installed. A module-level import of
    `pgsearch` there would put NumPy on that path."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "mindhive"
           / "pgtools.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top = {n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    for node in top:
        names = [a.name for a in node.names]
        assert "pgsearch" not in names, \
            "pgtools imports pgsearch at module level; that is the cycle"
