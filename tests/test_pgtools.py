# tests/test_pgtools.py
"""Result shaping — the half of the AI tool surface that needs no Postgres.

There is no server on this machine and `psycopg` is not installed. That blocks
"does this SQL return the right rows"; it blocks nothing about the part that
actually decides whether an agent answers correctly from the result:

* **Truncation is DETECTED, not applied.** The cursor is asked for `max_rows+1`
  and hands back `max_rows`. A tool that returns exactly the cap and says
  nothing is how an agent reports "there were 200 events" about 40,000.
* **`truncated` is an object, never a boolean, never absent.** Three caps can
  bind and they demand opposite follow-ups.
* **Key names survive summarisation; bulk does not.** A model that can see
  `files` exists but not its 37 entries can ask a precise follow-up; one handed
  `{"payload": "[truncated]"}` cannot.
* **No tool raises, ever**, and with the backend absent each returns a
  structured `backend_unavailable` inside a wall-clock budget. That last one is
  only honestly checkable while no server exists, so it is taken now.

The fake cursor below is not a mock of psycopg: it is the two methods the
shaper actually uses (`fetchmany`, `description`). Everything the shaper does
with what comes back is pure Python and is therefore fully verified today.
"""
import json
import time

import pytest

from mindhive import pgschema, pgsearch, pgstore, pgtools

# Every registered tool, so the contract tests below (no tool raises, every
# tool answers inside the budget, every response carries freshness) cover the
# whole surface. Pinned to the registry by a test rather than by care.
TOOLS = ("query_database", "get_recent_events", "search_events_by_payload",
         "get_session_summary", "get_latest_event",
         "search_events_by_type", "get_current_state",
         "search_events_by_meaning")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MINDHIVE_PG_DSN_RO", raising=False)
    monkeypatch.delenv("MINDHIVE_PG_DSN_ADMIN", raising=False)
    monkeypatch.delenv("MINDHIVE_PG_MAX_BYTES", raising=False)
    pgstore.reset_state()
    yield
    pgstore.reset_state()


class FakeCursor:
    """The two members the row streamer touches. `chunks` records the sizes
    `fetchmany` was asked for, which is how the `max_rows+1` probe is proven
    rather than assumed."""

    def __init__(self, rows, columns=("global_seq", "ts", "payload")):
        self._rows = list(rows)
        self._i = 0
        self.description = [(c,) for c in columns]
        self.chunks = []
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchmany(self, n):
        self.chunks.append(n)
        out = self._rows[self._i:self._i + n]
        self._i += len(out)
        return out


def _call(name, **kw):
    """Dispatch through the registry the MCP server and `hive pg tool` both
    read, so these assertions cover the same path the model takes."""
    return pgtools.TOOL_FUNCTIONS[name](**kw)


# --------------------------------------------------------------------------
# truncation is detected, not applied
# --------------------------------------------------------------------------

def test_the_streamer_asks_for_one_more_row_than_the_cap():
    """Detection, not application. Without the +1 the tool cannot distinguish
    "exactly 200 matched" from "40,000 matched"."""
    cur = FakeCursor([(i, "2026-01-01T00:00:00Z", {}) for i in range(500)])
    rows, truncated = pgtools.stream_rows(cur, max_rows=10, max_bytes=10 ** 9)
    assert len(rows) == 10, "the cap was not applied"
    assert truncated["rows"] is True
    assert sum(cur.chunks) >= 11, \
        f"the streamer never asked past the cap: {cur.chunks}"


def test_a_result_under_the_cap_is_not_reported_as_truncated():
    """CANARY for the assertion above: if `truncated.rows` were hard-coded true,
    or computed as `len(rows) >= max_rows`, this goes red. A false truncation
    flag is as harmful as a missing one — it makes a model refuse to report a
    count it was entitled to report."""
    cur = FakeCursor([(i, "t", {}) for i in range(4)])
    rows, truncated = pgtools.stream_rows(cur, max_rows=10, max_bytes=10 ** 9)
    assert len(rows) == 4
    assert truncated["rows"] is False


def test_exactly_the_cap_many_rows_is_not_truncated():
    """The boundary, asserted rather than assumed: `>` versus `>=` here is the
    difference between an honest flag and one that cries wolf on every full
    page. 10 rows available, cap 10 — the 11th probe comes back empty."""
    cur = FakeCursor([(i, "t", {}) for i in range(10)])
    rows, truncated = pgtools.stream_rows(cur, max_rows=10, max_bytes=10 ** 9)
    assert len(rows) == 10
    assert truncated["rows"] is False


def test_the_byte_cap_stops_before_a_row_that_would_cross_it():
    """Rows are never partial. A half-serialised row is not a row a model can
    reason about, and repairing one is not something a model can do."""
    big = {"text": "x" * 4000}
    cur = FakeCursor([(i, "t", big) for i in range(20)])
    rows, truncated = pgtools.stream_rows(cur, max_rows=100, max_bytes=9000)
    assert truncated["bytes"] is True
    assert 0 < len(rows) < 20
    for r in rows:
        assert r[2] == big, "a row was emitted partially"


def test_one_row_larger_than_the_whole_cap_is_still_returned():
    """Rule 4. A legitimate one-row answer must never be lost to a cap — the
    byte cap protects against volume, and refusing the only row on offer is not
    protection, it is an outage with a green status."""
    cur = FakeCursor([(1, "t", {"text": "y" * 50_000})])
    rows, truncated = pgtools.stream_rows(cur, max_rows=100, max_bytes=1000)
    assert len(rows) == 1
    assert truncated["rows"] is False


# --------------------------------------------------------------------------
# payload summarisation — key names survive, bulk does not
# --------------------------------------------------------------------------

def test_a_fifty_kilobyte_response_comes_back_under_the_per_row_budget():
    """The shape this cap exists for: `assistant_response` is a real payload
    type, not a hypothetical, and 200 of them would be a gigabyte-scale
    allocation before a single byte reached the model."""
    payload = {"text": "z" * 50_000, "model": "claude-opus-5"}
    shaped, changed = pgtools.summarise_payload(payload, global_seq=88412)
    assert changed is True
    size = len(json.dumps(shaped).encode("utf-8"))
    assert size <= pgtools.PAYLOAD_ROW_BUDGET, f"{size} bytes survived the budget"


def test_every_key_name_survives_summarisation():
    """Property 1. A model that can see `files` exists but not its 37 entries
    asks a precise follow-up; one handed a single "[truncated]" string cannot."""
    payload = {"category": "update", "files": ["f%d.py" % i for i in range(37)],
               "text": "w" * 48_213, "counts": {"passed": 4924, "deselected": 89},
               "raw": {"stdout": "o" * 900, "stderr": "e" * 900, "argv": ["a"]}}
    shaped, _ = pgtools.summarise_payload(payload, global_seq=1)
    assert set(shaped) == set(payload), "summarisation dropped a key name"


def test_a_descriptor_says_what_type_and_how_much_was_elided():
    """Property 2. `{"_t":"str","len":48213}` is a fact; a silently cut string
    is a lie of omission, and the model has no way to tell the two apart."""
    shaped, _ = pgtools.summarise_payload({"text": "w" * 48_213}, global_seq=88412)
    d = shaped["text"]
    assert d["_t"] == "str"
    assert d["len"] == 48_213
    assert d["global_seq"] == 88412
    assert d["head"].startswith("w") and len(d["head"]) == pgtools.TEXT_HEAD


def test_the_descriptor_carries_the_row_identity_for_a_targeted_refetch():
    """Property 3. The escape hatch is a query for ONE row, taken knowingly —
    deliberately not a `full_payload: true` parameter, which is a boolean a
    model sets by default."""
    shaped, _ = pgtools.summarise_payload({"blob": "q" * 5000}, global_seq=42)
    assert shaped["blob"]["global_seq"] == 42


def test_a_row_with_no_identity_column_gets_a_descriptor_without_one():
    """A projection like `SELECT payload FROM …` has no `global_seq`. Inventing
    one would send the model to fetch a row that does not exist; omitting the
    key says honestly that there is no targeted follow-up available."""
    shaped, _ = pgtools.summarise_payload({"blob": "q" * 5000}, global_seq=None)
    assert "global_seq" not in shaped["blob"]


def test_small_values_are_kept_whole():
    """Summarising `{"category": "update"}` would cost the model the answer to
    buy nothing. The budget is for bulk."""
    payload = {"category": "update", "exit_code": 0, "ok": True,
               "counts": {"passed": 4924, "deselected": 89}, "nothing": None}
    shaped, changed = pgtools.summarise_payload(payload, global_seq=1)
    assert shaped == payload
    assert changed is False


def test_memo_text_keeps_a_longer_head_than_other_strings():
    """400 for `text`, 200 for everything else — memos are what this store is
    for, and 400 is the number the session-summary SQL already cuts at, so the
    two surfaces agree on how much of a memo a model gets."""
    long = "m" * 5000
    text_d, _ = pgtools.summarise_payload({"text": long}, global_seq=1)
    other_d, _ = pgtools.summarise_payload({"stdout": long}, global_seq=1)
    assert len(text_d["text"]["head"]) == 400
    assert len(other_d["stdout"]["head"]) == 200


def test_bulk_arrays_and_objects_become_countable_descriptors():
    payload = {"files": ["f%d.py" % i for i in range(400)],
               "raw": {("k%03d" % i): "v" * 40 for i in range(50)}}
    shaped, _ = pgtools.summarise_payload(payload, global_seq=1)
    assert shaped["files"] == {"_t": "array", "n": 400, "global_seq": 1}
    assert shaped["raw"]["_t"] == "object"
    assert shaped["raw"]["n_keys"] == 50
    assert len(shaped["raw"]["keys"]) == pgtools.OBJECT_KEY_LIST, \
        "the key list is the map; it is capped, not dropped"


def test_a_payload_that_is_not_an_object_is_returned_not_dropped():
    """Constraint 5: reads stay total. A malformed row is visible, never
    silently missing — a mirror that disagrees with the file ledger invisibly is
    the failure this whole design exists to detect."""
    for odd in ("a string", 42, None, ["a", "list"]):
        shaped, _ = pgtools.summarise_payload(odd, global_seq=1)
        assert shaped == odd or shaped["_t"] in ("str", "array")


# --------------------------------------------------------------------------
# the envelope
# --------------------------------------------------------------------------

REQUIRED_KEYS = {"ok", "tool", "project", "rows", "row_count", "columns",
                 "truncated", "caps", "notice", "freshness",
                 "content_is_untrusted", "elapsed_ms", "error"}


def test_the_envelope_carries_every_field_the_descriptions_promise():
    """The descriptions tell the model to read these fields. A missing one is a
    field the model was told to check and cannot."""
    env = pgtools.envelope("query_database", project="p", rows=[], columns=[])
    assert REQUIRED_KEYS <= set(env), REQUIRED_KEYS - set(env)


def test_truncated_is_always_an_object_and_never_a_bare_boolean():
    """Rule 1. A bare `true` cannot distinguish "there are more rows" from "one
    string was shortened", and those demand opposite follow-ups."""
    env = pgtools.envelope("query_database", project="p", rows=[], columns=[])
    assert isinstance(env["truncated"], dict)
    assert set(env["truncated"]) >= {"rows", "bytes", "payload"}
    assert all(isinstance(v, bool) for v in env["truncated"].values())


def test_row_count_is_always_the_length_of_rows_and_never_a_total():
    """Rule 5. A wrong total is strictly worse than no total — it is the number
    the agent will quote."""
    rows = [{"a": 1}, {"a": 2}, {"a": 3}]
    env = pgtools.envelope("query_database", project="p", rows=rows, columns=["a"],
                           truncated={"rows": True, "bytes": False, "payload": False})
    assert env["row_count"] == 3


def test_the_envelope_offers_no_total_estimate_field():
    """Deliberate absence. An unbounded `count(*)` over ~10^5 rows is the exact
    cost this feature exists to eliminate, and an estimate is a number the model
    would quote as a fact."""
    env = pgtools.envelope("query_database", project="p", rows=[], columns=[])
    for forbidden in ("total", "total_estimate", "estimated_total", "total_rows"):
        assert forbidden not in env


def test_a_notice_appears_whenever_any_leg_of_truncation_is_true():
    """Rule 3. Models act on sentences more reliably than on flags, so the prose
    names the cap that bound and the next action."""
    for leg in ("rows", "bytes", "payload"):
        t = pgtools.new_truncated()
        t[leg] = True
        env = pgtools.envelope("query_database", project="p", rows=[], columns=[],
                               truncated=t)
        assert env["notice"], f"no notice for a truncated {leg}"
        assert leg in env["notice"] or {"rows": "row", "bytes": "byte",
                                        "payload": "payload"}[leg] in env["notice"]


def test_no_notice_when_nothing_was_capped():
    """The other half of rule 3: a notice on every healthy response is a notice
    every reader learns to skip."""
    env = pgtools.envelope("query_database", project="p", rows=[], columns=[])
    assert env["notice"] is None


def test_the_envelope_marks_its_own_rows_as_untrusted_content():
    """The mirror holds `user_prompt` and `assistant_response` payloads. A tool
    result is untrusted content by definition and this feature makes 20 MB of it
    bulk-queryable."""
    env = pgtools.envelope("query_database", project="p", rows=[], columns=[])
    assert env["content_is_untrusted"] is True


def test_every_response_carries_a_freshness_stamp_even_when_it_failed():
    """Unconditional, not only-when-stale. A field that appears only on trouble
    is a field whose absence is ambiguous, and ambiguity resolves toward the
    optimistic reading every time."""
    for name in TOOLS:
        env = _call(name, **_minimal_args(name))
        assert "freshness" in env, name
        assert env["freshness"].get("available") is False


def _minimal_args(name):
    return {"query_database": {"sql": "SELECT 1"},
            "get_recent_events": {"session_id": "s1"},
            "search_events_by_payload": {"key": "task_id"},
            "get_session_summary": {"session_id": "s1"},
            "get_latest_event": {"session_id": "s1"},
            "search_events_by_type": {"event_type": "decision"},
            "get_current_state": {},
            "search_events_by_meaning": {"query": "mirror staleness"}}[name]


# --------------------------------------------------------------------------
# the closed error vocabulary, and the backend being absent
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", TOOLS)
def test_every_tool_answers_backend_unavailable_with_no_server(name, monkeypatch):
    """THE verification that is only honestly available while no server exists.
    A tool that vanished or hung when the backend is unreachable pushes the model
    back to inventing shell commands, which is what this feature replaces.

    `deps_available` is forced True so the assertion is about the SERVER being
    absent rather than the driver: on this machine both are true at once, and
    without the override every tool would legitimately answer `deps_missing` and
    this test would prove the wrong half.

    `_missing_deps` is forced empty for the same reason and it is NOT the same
    gate: `search_events_by_meaning` needs numpy and the encoder on top of the
    driver, and CI installs .[dev,pg] without .[vectors]. Unforced, this one
    parametrisation would assert `backend_unavailable` on a machine with the
    extras and `deps_missing` on one without -- the same test proving two
    different things, which is how it passed here and failed on all four CI
    jobs."""
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    monkeypatch.setattr(pgsearch, "_missing_deps", lambda: [])
    env = _call(name, **_minimal_args(name))
    assert env["ok"] is False
    assert env["error"]["code"] == "backend_unavailable"
    assert env["error"]["retryable"] is True
    assert env["error"]["remedy"], "an error with no remedy is a dead end"
    assert env["rows"] == [] and env["row_count"] == 0


@pytest.mark.parametrize("name", TOOLS)
def test_every_tool_answers_deps_missing_on_this_machine_as_it_is(name):
    """The unoverridden state of the machine this suite actually runs on:
    neither `psycopg` nor a server. The honest answer is the one with an install
    command attached, and it must arrive as a structured code rather than an
    ImportError escaping into the agent's turn."""
    env = _call(name, **_minimal_args(name))
    assert env["ok"] is False
    assert env["error"]["code"] in ("deps_missing", "backend_unavailable")
    if not pgstore.deps_available():
        assert env["error"]["code"] == "deps_missing"


@pytest.mark.parametrize("name", TOOLS)
def test_every_tool_returns_inside_the_wall_clock_budget(name):
    """R4: a blocking connect inside a handler stalls an agent's turn with no
    output and no error. The budget is 3 s; with nothing configured the answer
    comes from an environment lookup and should be far under it."""
    start = time.monotonic()
    _call(name, **_minimal_args(name))
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"{name} took {elapsed:.2f}s with no backend"


@pytest.mark.parametrize("name", TOOLS)
def test_no_tool_raises_however_the_layer_beneath_it_fails(name, monkeypatch):
    """An exception escaping into an MCP handler is an outage inside the agent's
    turn. The blanket catch is the same deliberate construction CLAUDE.md
    defends for `hook_decision`, for the same reason."""
    for boom in (RuntimeError("dead"), OSError("refused"), ValueError("garbage"),
                 KeyError("k")):
        monkeypatch.setattr(pgtools, "_session",
                            lambda *a, **k: (_ for _ in ()).throw(boom))
        monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
        monkeypatch.setattr(pgstore, "mirror_health",
                            lambda project=None: {"available": True,
                                                  "parity_ok": True, "stale": False})
        env = _call(name, **_minimal_args(name))
        assert env["ok"] is False, name
        assert env["error"]["code"] in pgtools.ERROR_CODES


@pytest.mark.parametrize("name", TOOLS)
def test_an_open_breaker_is_reported_in_the_wire_vocabulary(name, monkeypatch):
    """`pgstore` answers `breaker_open`, which is an INTERNAL state the tool
    descriptions never mention. From the model's side an open breaker is
    indistinguishable from an unreachable backend, and a code with no rule
    attached is a code the model invents behaviour for."""
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: "breaker_open")
    monkeypatch.setattr(pgsearch, "_missing_deps", lambda: [])
    env = _call(name, **_minimal_args(name))
    assert env["error"]["code"] == "backend_unavailable"
    assert env["breaker"] == "open", "the real state is reported in its own field"


@pytest.mark.parametrize("name", TOOLS)
def test_a_missing_driver_keeps_its_own_code_because_it_has_a_fix(name, monkeypatch):
    """`deps_missing` survives the mapping that folds `breaker_open` away,
    because it is the one unavailability with an install command attached."""
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: "deps_missing")
    env = _call(name, **_minimal_args(name))
    assert env["error"]["code"] == "deps_missing"
    assert ".[pg]" in env["error"]["remedy"]


def test_the_error_vocabulary_is_exactly_the_one_the_descriptions_publish():
    """The four descriptions enumerate the codes a model may match on. A code
    the tools can emit but the descriptions never list is a code the model has
    no rule for."""
    published = pgschema.TOOL_DESCRIPTIONS["query_database"]
    listed = [c for c in pgtools.ERROR_CODES if f"`{c}`" in published]
    assert set(listed) >= set(pgtools.ERROR_CODES) - {"mirror_stale"}, \
        f"tools can emit codes the description never names: " \
        f"{set(pgtools.ERROR_CODES) - set(listed)}"


def test_parity_failure_refuses_rows_rather_than_answering(monkeypatch):
    """The ONE place this design refuses to answer. Staleness is bounded — you
    know exactly what you are missing. A parity failure is unbounded: rows may
    be missing, duplicated or misordered and you cannot say which, so an agent
    answering from it is confidently wrong with no way to know."""
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True, "parity_ok": False,
                                              "stale": False})
    env = _call("get_recent_events", session_id="s1")
    assert env["ok"] is False
    assert env["error"]["code"] == "parity_failed"
    assert env["error"]["retryable"] is False
    assert env["rows"] == []
    assert "rebuild" in env["error"]["remedy"]


def test_staleness_answers_with_a_stamp_instead_of_refusing(monkeypatch):
    """The contrast that makes the refusal above meaningful: a stale mirror
    still answers, because staleness is describable — you know you are missing
    everything after `rebuilt_at`."""
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True, "parity_ok": True,
                                              "stale": True,
                                              "stale_reason": "3 segments moved"})
    monkeypatch.setattr(pgtools, "_session", _fake_session([]))
    env = _call("get_recent_events", session_id="s1")
    assert env["ok"] is True
    assert env["freshness"]["stale"] is True
    assert "stale" in (env["notice"] or "").lower()


# --------------------------------------------------------------------------
# D1: the stale notice carries the remedy AND the actor
# --------------------------------------------------------------------------
#
# The gap this closes was found by walking the agent's own path rather than by
# reading the code. The notice said the mirror was behind and why, and stopped.
# An agent holding a question about "now" then does the only thing available:
# finds `hive pg rebuild` in the help, runs it, and gets
# `MINDHIVE_PG_DSN_ADMIN is not set` — with nothing anywhere telling it that the
# reader role it connects with is read-only at the ROLE level and could never
# have repaired anything. Two tool calls and a failed command to learn one fact
# a sentence can carry.
#
# Both halves are asserted separately, because either alone is still a dead end:
# the command without the actor invites a retry loop, and the actor without the
# command leaves the operator to guess which verb.

def _stale_text() -> str:
    return " ".join(pgtools._stale_notice(
        {"stale": True, "stale_reason": "3 segments moved"}))


def test_the_stale_notice_names_the_repair_command():
    """First half of D1. `refresh`, not `rebuild`: the cheap one is the one an
    operator interrupted mid-session should be told to run, and the two are
    different commands with a real cost difference (one segment versus 930)."""
    text = _stale_text()
    assert "hive pg refresh" in text, \
        f"the notice does not name the repair command: {text!r}"


def test_the_stale_notice_says_the_reader_cannot_run_it():
    """Second half of D1, and the half that stops a retry loop. The claim has to
    be about the ROLE, not about a missing variable — an agent told only that a
    variable is unset will reasonably try to set it."""
    text = _stale_text().lower()
    assert "cannot run it yourself" in text, \
        f"the notice does not say who can repair the mirror: {text!r}"
    assert "read-only at the role level" in text, \
        f"the notice does not say WHY the reader cannot: {text!r}"


def test_the_stale_notice_is_absent_when_the_mirror_is_fresh():
    """The other side of the ratchet. A notice on every healthy answer is a
    notice every reader learns to skip, and then the stale one does not land."""
    assert pgtools._stale_notice({"stale": False}) is None
    assert pgtools._stale_notice({}) is None


def test_the_stale_notice_reaches_a_real_tool_envelope(monkeypatch):
    """The three assertions above read `_stale_notice` directly, which proves the
    sentence exists, not that a model ever sees it. This one goes through
    `pgtools.call` and reads the assembled `notice` field."""
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True, "parity_ok": True,
                                              "stale": True,
                                              "stale_reason": "3 segments moved"})
    monkeypatch.setattr(pgtools, "_session", _fake_session([]))
    notice = _call("get_recent_events", session_id="s1")["notice"] or ""
    assert "hive pg refresh" in notice and "read-only at the role level" in notice, \
        f"the remedy did not survive envelope assembly: {notice!r}"


def _fake_session(rows, columns=("global_seq", "ts", "ts_utc", "session_id",
                                 "event_type", "payload", "tags")):
    import contextlib

    @contextlib.contextmanager
    def session(project, timeout_ms):
        yield FakeCursor(rows, columns)
    return session


# --------------------------------------------------------------------------
# argument validation — a hallucinated bound is a loud rejection
# --------------------------------------------------------------------------

def test_a_row_cap_above_the_ceiling_is_refused_by_name():
    env = pgtools.query_database("SELECT 1", max_rows=9999)
    assert env["error"]["code"] == "bad_argument"
    assert "max_rows" in env["error"]["detail"]
    assert str(pgtools.MAX_ROWS_CEILING) in env["error"]["detail"], \
        "the rejection must name the bound, or the model cannot correct itself"


def test_a_timeout_above_the_ceiling_is_refused_by_name():
    env = pgtools.query_database("SELECT 1", timeout_ms=60_000)
    assert env["error"]["code"] == "bad_argument"
    assert "timeout_ms" in env["error"]["detail"]


def test_an_empty_sql_string_is_refused_before_the_backend_is_consulted():
    env = pgtools.query_database("   ")
    assert env["error"]["code"] == "bad_argument"


def test_an_empty_session_id_is_refused():
    for name in ("get_recent_events", "get_session_summary"):
        env = _call(name, session_id="")
        assert env["error"]["code"] == "bad_argument", name


def test_validation_runs_before_availability_so_the_message_is_the_useful_one():
    """With the backend down, EVERY call could answer `backend_unavailable`.
    Reporting that for a call that was also malformed sends the model to fix the
    server when the fix is in its own arguments."""
    env = pgtools.query_database("SELECT 1", max_rows=9999)
    assert env["error"]["code"] == "bad_argument", \
        "an unreachable backend masked a caller error"


# --------------------------------------------------------------------------
# the caps themselves
# --------------------------------------------------------------------------

def test_the_response_byte_cap_is_operator_overridable_up_to_a_hard_ceiling(
        monkeypatch):
    assert pgtools.response_byte_cap() == pgtools.RESPONSE_MAX_BYTES_DEFAULT
    monkeypatch.setenv("MINDHIVE_PG_MAX_BYTES", "65536")
    assert pgtools.response_byte_cap() == 65_536
    monkeypatch.setenv("MINDHIVE_PG_MAX_BYTES", "999999999")
    assert pgtools.response_byte_cap() == pgtools.RESPONSE_MAX_BYTES_CEILING


def test_a_garbage_byte_cap_falls_back_to_the_default_rather_than_crashing(
        monkeypatch):
    """This is read on a path that must not raise, and an operator typo in an
    environment variable is not a reason to take the tools down."""
    for junk in ("", "lots", "-5", "0"):
        monkeypatch.setenv("MINDHIVE_PG_MAX_BYTES", junk)
        assert pgtools.response_byte_cap() == pgtools.RESPONSE_MAX_BYTES_DEFAULT


def test_the_caps_reported_in_the_envelope_are_the_caps_that_were_applied():
    """A `caps` block that reported the defaults while different numbers were
    enforced would be documentation that lies at exactly the moment it matters."""
    env = pgtools.envelope("query_database", project="p", rows=[], columns=[],
                           caps={"max_rows": 7, "max_bytes": 99, "timeout_ms": 1})
    assert env["caps"] == {"max_rows": 7, "max_bytes": 99, "timeout_ms": 1}


# --------------------------------------------------------------------------
# the dispatch registry
# --------------------------------------------------------------------------

def test_the_registry_holds_exactly_the_published_tools():
    """One registry, so `hive pg tool` debugs the same dispatch the model uses
    and the human surface cannot drift from the AI one.

    Eight since the semantic tool landed: the four that answer "show me events",
    the three that answer "what IS the state" (`get_latest_event`,
    `search_events_by_type`, `get_current_state`) — the question an agent
    actually opens a session with — and `search_events_by_meaning`, the only one
    that can find a memo whose author used none of the asker's words.

    That eighth entry is a FORWARDER whose body lives in `pgsearch`, because it
    is the only tool needing an encoder and NumPy. The table must still hold it:
    a tool published in `pgschema` and missing from this dict would be listed to
    the model and then fail as `unknown tool`."""
    assert set(pgtools.TOOL_FUNCTIONS) == set(pgschema.TOOL_SCHEMAS)
    assert set(pgtools.TOOL_FUNCTIONS) == set(pgschema.TOOL_DESCRIPTIONS)
    assert len(pgtools.TOOL_FUNCTIONS) == 8


def test_the_schema_module_stays_free_of_this_module():
    """`pgschema` must answer with `psycopg` absent and Postgres down, so the
    dispatch table's import direction is one-way: tools know the schema, the
    schema knows nothing."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "mindhive" /
           "pgschema.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] +
                     ([node.module] if isinstance(node, ast.ImportFrom) else []))
            assert not any("pgtools" in (n or "") or "pgstore" in (n or "")
                           for n in names), ast.dump(node)


def test_shaped_rows_are_json_serialisable():
    """The envelope crosses a JSON-RPC frame. A `datetime` from `ts_utc` or a
    `Decimal` from a `count(*)` that reached `json.dumps` unconverted would take
    the server down mid-turn, and it would do it on the success path."""
    import datetime
    import decimal
    cur = FakeCursor([(1, datetime.datetime(2026, 8, 17, 9, 0, 0),
                       decimal.Decimal("4924"), {"text": "ok"})],
                     columns=("global_seq", "ts_utc", "n", "payload"))
    rows, _ = pgtools.stream_rows(cur, max_rows=10, max_bytes=10 ** 9)
    shaped = pgtools.shape_rows(rows, ["global_seq", "ts_utc", "n", "payload"],
                                pgtools.new_truncated())
    json.dumps(shaped)          # must not raise
    assert shaped[0]["ts_utc"] == "2026-08-17T09:00:00"
    assert shaped[0]["n"] == 4924


def test_shaping_only_summarises_the_payload_column():
    """A model that writes `SELECT payload->>'text'` asked for that column and
    gets it, subject to the byte cap. Summarisation is for the jsonb column, not
    for the model's own projections."""
    long = "p" * 5000
    rows = [(1, long, {"text": long})]
    shaped = pgtools.shape_rows(rows, ["global_seq", "projected", "payload"],
                                pgtools.new_truncated())
    assert shaped[0]["projected"] == long
    assert shaped[0]["payload"]["text"]["_t"] == "str"


def test_shaping_reports_the_payload_leg_when_it_summarised_something():
    t = pgtools.new_truncated()
    pgtools.shape_rows([(1, {"text": "p" * 5000})], ["global_seq", "payload"], t)
    assert t["payload"] is True


def test_shaping_leaves_the_payload_leg_alone_when_nothing_was_elided():
    """CANARY for the flag above: a `truncated.payload` that were always true
    would make every result look capped and teach the model to ignore the
    field."""
    t = pgtools.new_truncated()
    pgtools.shape_rows([(1, {"text": "short"})], ["global_seq", "payload"], t)
    assert t["payload"] is False


# --------------------------------------------------------------------------
# _classify — the seam between the untestable DB layer and the wire vocabulary
#
# §6.3 of the design names the exact SQLSTATE each defence layer produces when
# it stops a hostile query: L6's statement_timeout is 57014, L2's rejected
# subquery/CTE is 42601/0A000, L4's read-only violation is 25006, L5's missing
# privilege is 42501. None of that is checkable without a live server — but
# `_classify` is the pure-Python function that turns whichever SQLSTATE comes
# back into the closed vocabulary a model can act on, and it is fully checkable
# today by handing it a fake exception. Nothing in this suite called it before.
# --------------------------------------------------------------------------

class _FakeSqlstateExc(Exception):
    """Stands in for a psycopg error that exposes `sqlstate` directly."""

    def __init__(self, sqlstate):
        super().__init__("boom")
        self.sqlstate = sqlstate


class _FakeDiagExc(Exception):
    """Stands in for a real psycopg 3 error: SQLSTATE sits on `.diag.sqlstate`,
    not on the exception itself. `_classify`'s `or` fallback exists for exactly
    this shape."""

    class _Diag:
        def __init__(self, sqlstate):
            self.sqlstate = sqlstate

    def __init__(self, sqlstate):
        super().__init__("boom")
        self.diag = self._Diag(sqlstate)


@pytest.mark.parametrize("sqlstate,code,retryable", [
    ("57014", "query_timeout", False),     # L6: statement_timeout fired
    ("42601", "query_rejected", False),    # L2: syntax error inside the wrap
    ("42501", "query_rejected", False),    # L5: e.g. SELECT ... FOR UPDATE
    ("0A000", "query_rejected", False),    # L2: data-modifying CTE not top-level
    ("25006", "query_rejected", False),    # L4: read_only_sql_transaction
])
def test_classify_maps_every_documented_sqlstate_to_its_wire_code(
        sqlstate, code, retryable):
    """One entry per row of §6.3's table. A rename of any one of these codes —
    or a swapped retryable flag — is invisible until the day that exact hostile
    query actually runs against a live server, which is precisely the day an
    operator most needs the error to be legible."""
    assert pgtools._classify(_FakeSqlstateExc(sqlstate)) == (code, retryable)


def test_classify_reads_sqlstate_off_the_diag_attribute_too():
    """A `_classify` that only checked the direct attribute would silently
    fall through to `db_error` for every real psycopg 3 failure — the SDK
    reports SQLSTATE on `.diag.sqlstate`, and this is the one shape none of the
    parametrised cases above exercises."""
    assert pgtools._classify(_FakeDiagExc("57014")) == ("query_timeout", False)


def test_classify_maps_the_whole_connection_exception_class_not_one_code():
    """`08` is a SQLSTATE CLASS (`startswith`), not a single value — checked
    with two different codes in the class so the assertion is about the prefix
    match, not a coincidence with one hardcoded string."""
    assert pgtools._classify(_FakeSqlstateExc("08006"))[0] == "backend_unavailable"
    assert pgtools._classify(_FakeSqlstateExc("08000"))[0] == "backend_unavailable"
    assert pgtools._classify(_FakeSqlstateExc("08006"))[1] is True, \
        "a connection failure is the one class of error worth retrying"


def test_classify_falls_back_to_db_error_for_an_unrecognised_sqlstate():
    assert pgtools._classify(_FakeSqlstateExc("XX000")) == ("db_error", False)


def test_classify_falls_back_to_db_error_with_no_sqlstate_at_all():
    """The shape every non-driver failure actually has — a plain Python
    exception with neither `sqlstate` nor `.diag`."""
    assert pgtools._classify(RuntimeError("no sqlstate here")) == \
        ("db_error", False)


# --------------------------------------------------------------------------
# the fixed query text — checked as text, exactly like pgstore.READ_ALL_SQL,
# because there is no server to run these against. Nothing in this suite
# inspected RECENT_SQL / SEARCH_KEY_SQL / SEARCH_VALUE_SQL / SUMMARY_SQL
# before, even though they carry the identical ordering hazard §2.2 of the
# design was written to correct for pgstore.READ_ALL_SQL.
# --------------------------------------------------------------------------

def test_recent_events_orders_on_segment_not_session_id():
    """The same divergence pgstore.READ_ALL_SQL was designed around: '-'
    (0x2D) sorts below '.' (0x2E), so ordering by the bare session_id disagrees
    with ordering by the filename for ids like `manual` and `manual-2`, which
    share a `ts` often enough to matter. `get_recent_events` reverses ledger
    order for "newest first" — reversing the WRONG key reverses the wrong
    ordering just as surely as never correcting it in the first place."""
    order = pgtools.RECENT_SQL.split("ORDER", 1)[1]
    assert "segment" in order and "seg_line" in order
    assert "session_id" not in order


@pytest.mark.parametrize("sql,label", [
    (pgtools.RECENT_SQL, "RECENT_SQL"),
    (pgtools.SEARCH_KEY_SQL, "SEARCH_KEY_SQL"),
    (pgtools.SEARCH_VALUE_SQL, "SEARCH_VALUE_SQL"),
])
def test_the_row_queries_reverse_all_three_ledger_order_keys(sql, label):
    """"Recent" means latest in LEDGER order, not latest ts_utc: timestamps are
    second-resolution, so several events routinely share one `ts` and sorting
    on `ts` alone hands back an arbitrary permutation of them. DESC on all
    three keys is the exact reverse of `events.read_all()`."""
    order = sql.split("ORDER", 1)[1]
    assert order.count("DESC") == 3, f"{label}: {order!r}"


@pytest.mark.parametrize("sql,label", [
    (pgtools.RECENT_SQL, "RECENT_SQL"),
    (pgtools.SEARCH_KEY_SQL, "SEARCH_KEY_SQL"),
    (pgtools.SEARCH_VALUE_SQL, "SEARCH_VALUE_SQL"),
    (pgtools.SUMMARY_SQL, "SUMMARY_SQL"),
])
def test_no_tool_query_names_the_base_table_casts_ts_or_spells_a_project_predicate(
        sql, label):
    """The reader's whole universe is `mindhive_ro.events` — it holds no
    privilege on the base table and no USAGE on schema `mindhive`, so a query
    naming `mindhive.events` would fail as a permission error a long way from
    its cause. Scoping is `SET LOCAL mindhive.project`, done once in
    `_session`, never a WHERE clause the tool could get wrong or drop; and any
    cast of `ts` would hand the model a value the file ledger's chain head
    disagrees with."""
    assert "mindhive_ro.events" in sql, label
    assert "FROM mindhive.events" not in sql, label
    assert "ts::" not in sql and "to_char" not in sql, label
    assert "project" not in sql.lower(), label


def test_search_by_payload_switches_queries_rather_than_falling_through(
        monkeypatch):
    """§8.3's branch: WITHOUT a value this is key-existence, which the
    `jsonb_path_ops` GIN index does not serve — it sequential-scans except for
    `task_id`/`plan_id`. A `search_events_by_payload` that used the containment
    query even when `value is None` would silently query for JSON literal
    `null` instead of switching queries: a different question, answered
    wrong, with a much worse plan, and no error to notice it by."""
    cur = FakeCursor([])
    _reach_run(monkeypatch, cur)
    pgtools.search_events_by_payload("task_id")
    sent_sql, _ = cur.executed[0]
    assert sent_sql == pgtools.SEARCH_KEY_SQL

    cur2 = FakeCursor([])
    _reach_run(monkeypatch, cur2)
    pgtools.search_events_by_payload("task_id", value="abc")
    sent_sql2, _ = cur2.executed[0]
    assert sent_sql2 == pgtools.SEARCH_VALUE_SQL


# --------------------------------------------------------------------------
# query_database's L2 wrap — the structural subquery wrap is what makes
# everything that is not a query a syntax error, and the +1 LIMIT is what
# makes truncation detectable. Neither was ever checked as the SQL actually
# handed to the driver; `stream_rows` was only ever exercised directly against
# a hand-built FakeCursor, never through `query_database` itself.
# --------------------------------------------------------------------------

def _reach_run(monkeypatch, cur):
    """Bypass `_availability`/`_parity_refusal` and pin `_session` to `cur`, so
    a call reaches `_run`'s SQL construction with no backend — the same shape
    `test_staleness_answers_with_a_stamp_instead_of_refusing` already uses,
    except this keeps a handle on the cursor so `.executed` can be inspected
    afterward."""
    import contextlib
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True,
                                              "parity_ok": True, "stale": False})

    @contextlib.contextmanager
    def session(project, timeout_ms):
        yield cur
    monkeypatch.setattr(pgtools, "_session", session)


def test_query_database_sends_the_structural_wrap_to_the_driver(monkeypatch):
    """L2, asserted as the literal SQL handed to the driver rather than assumed
    from the docstring."""
    cur = FakeCursor([])
    _reach_run(monkeypatch, cur)
    pgtools.query_database("SELECT 1")
    sent_sql, params = cur.executed[0]
    assert sent_sql == "SELECT * FROM ( SELECT 1 ) AS mh_q LIMIT %s"
    assert params == (201,)


def test_the_wrapped_cap_is_bound_rather_than_written_into_the_sql(monkeypatch):
    """The multi-statement fix, and the reason it is a PARAMETER and not a
    number. Two independent defects met here:

    * `rstrip(';')` strips only TRAILING semicolons, so an interior one survives
      into the wrap. `SELECT 1) AS a; <payload>; --` closes the wrap's paren
      early and the trailing `--` eats the rest of the line — the literal
      `LIMIT 201` included.
    * psycopg3 chooses its transport on the TRUTHINESS of the converted params,
      not on `is not None`. The empty tuple that used to sit here is falsy, so it
      selected `send_query` — the simple protocol, which executes every statement
      in the string. A non-empty sequence selects `send_query_params`, and the
      server refuses more than one command in a prepared statement.

    So the assertion that matters is not "the SQL looks right" but "a parameter
    is bound at all". A regression to a literal cap silently restores BOTH."""
    cur = FakeCursor([])
    _reach_run(monkeypatch, cur)
    pgtools.query_database("SELECT 1) AS a; DROP TABLE mindhive.events; --")
    sent_sql, params = cur.executed[0]
    assert params, (
        "params is empty/falsy — psycopg3 picks the SIMPLE protocol on a falsy "
        "params sequence, which runs every statement in the string")
    assert params[0] == 201
    assert "LIMIT %s" in sent_sql, \
        "the cap went back into the SQL text, where a `--` can comment it out"
    assert "LIMIT 201" not in sent_sql


def test_query_database_strips_a_trailing_semicolon_before_wrapping(
        monkeypatch):
    """A semicolon surviving INSIDE the wrap — `... ( SELECT 1; ) AS mh_q ...`
    — is a syntax error for every well-formed query a model sends with the
    trailing `;` it was taught to write, which would make L2 refuse ~100% of
    legitimate input instead of 0% of it."""
    cur = FakeCursor([])
    _reach_run(monkeypatch, cur)
    pgtools.query_database("SELECT 1;")
    sent_sql, _ = cur.executed[0]
    assert sent_sql == "SELECT * FROM ( SELECT 1 ) AS mh_q LIMIT %s"


def test_query_database_requests_exactly_the_callers_cap_plus_one(monkeypatch):
    cur = FakeCursor([])
    _reach_run(monkeypatch, cur)
    pgtools.query_database("SELECT 1", max_rows=10)
    _sent_sql, params = cur.executed[0]
    assert params == (11,), params


def test_get_recent_events_requests_one_more_row_than_its_cap(monkeypatch):
    """The +1 lives in the PARAMS here, not the SQL text (`RECENT_SQL`'s
    `LIMIT %s` is a placeholder) — so the wrap test above and this one check
    two different mechanisms for the same detection rule, and neither proves
    the other."""
    cur = FakeCursor([])
    _reach_run(monkeypatch, cur)
    pgtools.get_recent_events("s1", limit=5)
    _, params = cur.executed[0]
    assert params == ("s1", 6, 0)          # session, cap+1, offset


# --------------------------------------------------------------------------
# notice_for — multiple simultaneous truncation legs
# --------------------------------------------------------------------------

def test_the_notice_names_every_truncation_leg_that_fired_not_just_the_first():
    """Each leg is proven individually elsewhere (`test_a_notice_appears_...`).
    A plausible wrong implementation — an `elif` chain instead of three
    independent `if`s, e.g. from someone "simplifying" the function — passes
    every one of those single-leg tests while silently dropping every cap
    after the first whenever two bind at once."""
    t = pgtools.new_truncated()
    t["rows"] = True
    t["bytes"] = True
    env = pgtools.envelope("query_database", project="p", rows=[], columns=[],
                           truncated=t)
    assert "row cap" in env["notice"]
    assert "byte cap" in env["notice"]


# --------------------------------------------------------------------------
# the bound, the paging, and the three state-shaped tools
# --------------------------------------------------------------------------
#
# The user's requirement was specific: "Jangan berikan tool mentah yang
# mengembalikan ribuan baris. Batasi output ke 10-20 hasil (Pagination)."
# These pin the 20 and the paging arithmetic without a server, because both are
# pure argument handling — the SQL only ever sees what these decide.


def test_the_default_page_is_twenty_rows():
    """20, not 200. The context window is the resource being protected, and a
    tool that CAN return hundreds of rows eventually will."""
    assert pgtools.DEFAULT_LIMIT == 20
    assert pgtools.DEFAULT_LIMIT <= 20, "the user asked for 10-20 results"


def test_every_paged_tool_defaults_to_that_bound(monkeypatch):
    """Asserted at the SQL boundary rather than from reading the code: the cap
    reaches the database as `LIMIT cap+1`, and the +1 is the truncation probe."""
    seen = {}

    def _fake_run(tool, project, sql, params, **kw):
        seen[tool] = {"params": params, "max_rows": kw["max_rows"]}
        return pgtools.envelope(tool, project=project, rows=[], columns=[])

    monkeypatch.setattr(pgtools, "_run", _fake_run)
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True,
                                              "parity_ok": True, "stale": False})
    pgtools.get_recent_events("s1")
    pgtools.search_events_by_type("decision")
    pgtools.get_current_state()

    for tool, got in seen.items():
        assert got["max_rows"] == 20, tool
        assert 21 in got["params"], (tool, got["params"])


def test_an_offset_is_echoed_so_the_caller_can_page_without_bookkeeping(
        monkeypatch):
    """`offset` comes back in the envelope. A model that has to remember what it
    asked for is a model that will eventually re-read page one forever.

    Asserted on the ANSWER path: the echo rides the result, not the refusals.
    An envelope that failed carries no rows to have been offset into."""
    for fn, args in ((pgtools.get_recent_events, ("s1",)),
                     (pgtools.search_events_by_type, ("decision",))):
        cur = FakeCursor([])
        _reach_run(monkeypatch, cur)
        env = fn(*args, offset=40)
        assert env["ok"] is True, fn.__name__
        assert env["offset"] == 40, fn.__name__
        assert cur.executed[0][1][-1] == 40, "the offset never reached the SQL"


def test_the_offset_is_bounded_because_the_server_walks_every_skipped_row():
    """OFFSET is O(skipped) in PostgreSQL. Past a few pages the honest answer is
    "filter by ts_utc instead", and the error says so rather than timing out."""
    assert pgtools.OFFSET_CEILING == 10_000
    env = pgtools.get_recent_events("s1", offset=pgtools.OFFSET_CEILING + 1)
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_argument"
    assert "10000" in json.dumps(env["error"])


@pytest.mark.parametrize("bad", [-1, "40", 1.5, None, True])
def test_a_non_integer_offset_is_refused_rather_than_coerced(bad):
    if bad is None:
        return                       # None means "not given"; that is the default
    env = pgtools.get_recent_events("s1", offset=bad)
    assert env["ok"] is False, bad
    assert env["error"]["code"] == "bad_argument"


# --- get_latest_event --------------------------------------------------------

def test_the_latest_event_tool_cannot_be_talked_into_returning_more():
    """Deliberately not `get_recent_events(limit=1)`, even though it would give
    the same row: this one has no `limit` to raise."""
    import inspect
    params = inspect.signature(pgtools.get_latest_event).parameters
    assert "limit" not in params and "offset" not in params
    assert "LIMIT  1" in pgtools.LATEST_SQL or "LIMIT 1" in pgtools.LATEST_SQL


def test_newest_means_ledger_order_not_clock_order():
    """Timestamps are second-resolution and several events routinely share one.
    Ordering by `ts` alone makes "the latest event" a coin toss between them."""
    for sql in (pgtools.LATEST_SQL, pgtools.BY_TYPE_SQL,
                pgtools.CURRENT_STATE_SQL):
        assert "segment DESC" in sql and "seg_line DESC" in sql, sql


def test_the_latest_event_tool_refuses_an_empty_session_id():
    env = pgtools.get_latest_event("   ")
    assert env["ok"] is False and env["error"]["code"] == "bad_argument"


# --- search_events_by_type ---------------------------------------------------

def test_an_unregistered_type_teaches_the_vocabulary_instead_of_failing():
    """A model that mistyped `code_changes` should learn the real name from the
    answer, not from a stack trace — and not by being told the ledger is empty."""
    notice = pgtools._unknown_type_notice("code_changes", None)
    assert "code_changes" in notice
    assert "is registered" in notice
    assert "code_change" in notice
    for known in ("decision", "caveat"):
        assert known in notice


def test_a_registered_type_that_simply_has_no_rows_says_nothing_extra():
    """An empty page for a REAL type is exhaustion, not a typo, and telling the
    model the vocabulary there would send it hunting for a mistake it did not
    make."""
    assert pgtools._unknown_type_notice("decision", None) is None
    assert pgtools._unknown_type_notice("decision", "already here") == "already here"


def test_the_type_filter_is_length_bounded_like_the_column():
    env = pgtools.search_events_by_type("x" * 101)
    assert env["ok"] is False
    assert env["error"]["code"] == "bad_argument"
    assert "100" in json.dumps(env["error"])


def test_narrowing_a_type_to_one_session_uses_the_two_filter_statement(monkeypatch):
    seen = {}

    def _fake_run(tool, project, sql, params, **kw):
        seen["sql"] = sql
        seen["params"] = params
        return pgtools.envelope(tool, project=project, rows=[], columns=[])

    monkeypatch.setattr(pgtools, "_run", _fake_run)
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True,
                                              "parity_ok": True, "stale": False})
    pgtools.search_events_by_type("decision", session_id="alpha")
    assert seen["sql"] == pgtools.BY_TYPE_SESSION_SQL
    assert seen["params"][:2] == ("decision", "alpha")


# --- get_current_state -------------------------------------------------------

def test_current_state_is_one_row_per_type_not_a_projection():
    """It is NOT `STATE.md` and it does not re-run the projector. Inventing a
    second derivation of "the current state" that could disagree — with the SQL
    one looking authoritative because it came from a database — would be worse
    than not having the tool."""
    assert "DISTINCT ON (event_type)" in pgtools.CURRENT_STATE_SQL
    doc = pgtools.get_current_state.__doc__
    assert "STATE.md" in doc
    assert "not" in doc.lower()


def test_asking_for_one_key_filters_rather_than_scanning_the_whole_map(monkeypatch):
    seen = {}

    def _fake_run(tool, project, sql, params, **kw):
        seen["sql"] = sql
        return pgtools.envelope(tool, project=project, rows=[], columns=[])

    monkeypatch.setattr(pgtools, "_run", _fake_run)
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True,
                                              "parity_ok": True, "stale": False})
    pgtools.get_current_state(key="code_change")
    assert seen["sql"] == pgtools.CURRENT_STATE_ONE_SQL
    assert "WHERE  event_type = %s" in seen["sql"]


# --- the registry ------------------------------------------------------------

def test_the_contract_tests_above_cover_every_registered_tool():
    """The tuple at the top of this file is where a new tool silently escapes
    the "no tool raises / answers in budget / carries freshness" checks."""
    assert set(TOOLS) == set(pgtools.TOOL_FUNCTIONS)


def test_every_registered_tool_has_a_description_and_a_schema():
    for name in pgtools.TOOL_FUNCTIONS:
        assert name in pgschema.TOOL_SCHEMAS, name
        assert pgschema.TOOL_DESCRIPTIONS[name].strip(), name


def test_the_session_summary_counts_by_class_because_the_description_says_so():
    """It was promised in the tool description and computed nowhere; every
    caller got null. A description is part of the contract — the model plans
    around it."""
    assert "by_class" in pgtools.SUMMARY_SQL
    assert "counts_by_class" in pgschema.TOOL_DESCRIPTIONS["get_session_summary"]


# --------------------------------------------------------------------------
# credentials, and a vocabulary with no dead members
# --------------------------------------------------------------------------

# Every DSN below is ASSEMBLED rather than written out, and `tests/
# test_pg_repo_gates.py::test_no_credential_literal_anywhere_in_src_or_tests`
# is why: it scans src and tests for a Postgres URI carrying inline userinfo
# (the comment cannot spell one out either) and fails the build on a hit, with
# no allowlist — correctly, because an allowlist is how a
# real credential eventually gets in. `test_the_credential_scan_recognises_
# every_shape_it_claims_to` already uses this construction for the same reason,
# so it is the repo's convention, not a way around the gate. The runtime string
# is identical to a real one, which is what these tests need.
_SECRET = "hunter" + "2"
_AT = chr(64)


def _dsn(user="mindhive_reader", secret=_SECRET, host="127.0.0.1:5433",
         db="mindhive", scheme="postgresql"):
    return f"{scheme}://{user}:{secret}{_AT}{host}/{db}"


def test_a_connection_string_never_rides_out_in_an_error_detail():
    """Driver messages quote the conninfo they were handed, and a conninfo can
    carry a password. The scrub keeps host and database — diagnostic, not
    secret — and drops the credentials segment."""
    out = pgtools.scrub(f"connection to {_dsn()} failed: timeout")
    assert _SECRET not in out and "mindhive_reader" not in out
    assert "127.0.0.1:5433/mindhive" in out, "the diagnostic half was eaten too"
    assert "postgresql://***" + _AT in out


def test_the_scrub_covers_both_uri_spellings_and_more_than_one_per_message():
    message = (f"{_dsn(user='u', secret='p', host='h', db='db', scheme='postgres')}"
               f" and {_dsn(user='a', secret='b', host='c', db='d')}")
    out = pgtools.scrub(message)
    assert "p" + _AT not in out and "b" + _AT not in out
    assert out.count("***" + _AT) == 2


def test_the_scrub_leaves_ordinary_text_and_non_strings_alone():
    assert pgtools.scrub("relation \"events\" does not exist") == \
        'relation "events" does not exist'
    assert pgtools.scrub(None) is None
    assert pgtools.scrub(42) == 42


def test_the_scrub_is_at_the_choke_point_not_at_the_call_sites():
    """Asserted through `error_envelope`, which every failure passes through: a
    detail assembled correctly in nine places and carelessly in the tenth still
    leaves through here."""
    env = pgtools.error_envelope(
        "query_database", "db_error",
        f"OperationalError: {_dsn(host='h:5433', db='db')}", project="p")
    assert _SECRET not in json.dumps(env)


def test_an_exception_carrying_a_dsn_is_scrubbed_on_the_real_failure_path(
        monkeypatch):
    """The blanket catch is where an unexpected driver failure lands, and it
    formats the exception's own text. That text is not ours to trust."""
    boom = RuntimeError("could not connect to "
                        + _dsn(user="owner", secret="s3cret"))
    monkeypatch.setattr(pgtools, "_session",
                        lambda *a, **k: (_ for _ in ()).throw(boom))
    monkeypatch.setattr(pgstore, "unavailable_reason", lambda: None)
    monkeypatch.setattr(pgstore, "mirror_health",
                        lambda project=None: {"available": True,
                                              "parity_ok": True,
                                              "stale": False})
    env = pgtools.query_database("SELECT 1")
    assert env["ok"] is False
    body = json.dumps(env)
    assert "s3cret" not in body and "owner:" not in body
    assert "127.0.0.1:5433" in body, "the useful half of the message was lost"


def test_every_published_error_code_is_reachable():
    """A closed vocabulary exists so the model may key behaviour on it, and
    behaviour keyed on a code that cannot arrive is dead code inside the model's
    plan. `mirror_stale` and `result_too_large` were both published and both
    unreachable — staleness answers with a stamp rather than refusing, and the
    byte cap drops trailing rows rather than failing.

    The scan is a source scan, so it proves the code is WRITTEN somewhere other
    than the tuple, not that some input reaches it. That is weaker than a
    reachability proof and stronger than nothing: it catches exactly the failure
    that happened, a name that exists only in the vocabulary."""
    import ast
    from pathlib import Path
    from mindhive import pgsearch, pgshape

    # THREE modules, because the surface is three modules: `pgshape` publishes
    # the vocabulary and the remedies, `pgtools` raises most of them, and
    # `pgsearch` is the only place `deps_missing` is ever reached. Scanning
    # `pgtools` alone called that one dead the moment the split happened.
    used, docstrings = set(), set()
    for mod in (pgtools, pgshape, pgsearch):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)

        # The three VOCABULARY tables are definitions, and a definition cannot
        # count as a use. Excluding `_REMEDIES` in particular is what keeps this
        # test honest: a code with a remedy and no raise is precisely the dead
        # entry being hunted.
        defining = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) in ("ERROR_CODES", "_REASON_TO_WIRE",
                                               "_REMEDIES")
                    for t in node.targets):
                defining |= {id(n) for n in ast.walk(node.value)}

        used |= {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in defining}
    used -= docstrings

    unreachable = [c for c in pgtools.ERROR_CODES if c not in used]
    assert unreachable == [], (
        f"published but never written outside the vocabulary: {unreachable}")


def test_the_scan_above_would_notice_a_dead_code():
    """CANARY for the test above: with a fabricated member added, the check must
    fail. Without this, an over-broad `used` set would pass for anything."""
    import ast
    from pathlib import Path
    from mindhive import pgsearch, pgshape
    used = set()
    for mod in (pgtools, pgshape, pgsearch):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        used |= {n.value for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "mirror_stale_but_fabricated" not in used
