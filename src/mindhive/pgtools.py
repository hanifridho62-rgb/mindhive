# src/mindhive/pgtools.py
"""The AI tools over the Postgres read mirror: argument validation, one SQL
statement each, and the dispatch table `pgschema` publishes.

Seven of the eight live here; the eighth, `search_events_by_meaning`, lives in
`pgsearch` and is reached through a lazy import so that answering
`get_recent_events` never pays for NumPy. What decides whether any of them can
be reasoned from — the caps, the payload summariser, the envelope — moved to
`pgshape` (TODOS item 18) and is re-exported below.

The five properties that make the envelope readable — `truncated` as an object,
truncation detected rather than applied, the notice as prose, rows never
partial, no invented total — are documented where they are now implemented, in
`pgshape`. They are not restated here: two copies of one rule diverge the first
time either is edited.

**Nothing here raises.** An exception escaping into an MCP handler is an outage
inside the agent's turn, so every entry point ends in a blanket catch that
converts to the envelope — the same deliberate construction CLAUDE.md defends
for ``hook_decision``, for the same reason.

## What is verified today, and what is not

The SQL is verified: the `pg`-marked tier runs against a real PostgreSQL 17, on
this machine and on a `postgres:17` service container in CI, so "does it parse",
"is the ordering it asks for the ordering it gets" and "does a connection ever
succeed" are answered by a run rather than by inspection. The default tier
covers everything above the connection with no server and no driver at all.

What is NOT verified is the deployment around it. The cluster used here is
loopback with trust auth; nothing about TLS, real authentication, or backups has
been exercised. That is a gap in the environment, not in the fail-closed
behaviour: with no backend every tool still returns a structured
`backend_unavailable` rather than an exception or a guess.
"""
import json
import time

from . import pgschema, pgstore

# The shaper lives in `pgshape` now (TODOS item 18) and is re-exported here
# rather than referenced through it. That is deliberate: `pgtools.envelope` and
# `pgtools.shape_rows` are addresses other modules and tests already hold, and
# this repo has been bitten once by a mechanical split that moved monkeypatch
# targets out from under its tests. The names that ARE patched — `_session`,
# `_run`, `_freshness`, `_parity_refusal`, `_availability`, `_project` — all
# stay in this module, so no test address moved at all.
from .pgshape import (                                          # noqa: F401
    DEFAULT_LIMIT, OFFSET_CEILING, MAX_ROWS_DEFAULT, MAX_ROWS_CEILING,
    TIMEOUT_MS_DEFAULT, TIMEOUT_MS_CEILING, WIRE_MAX_BYTES,
    RESPONSE_MAX_BYTES_DEFAULT, RESPONSE_MAX_BYTES_CEILING, RESPONSE_BYTES_ENV,
    PAYLOAD_ROW_BUDGET, STRING_HEAD, TEXT_HEAD, OBJECT_KEY_LIST, SUMMARY_CAPS,
    ERROR_CODES, _REASON_TO_WIRE, _REMEDIES, _CREDENTIALS,
    response_byte_cap, _jsonable, _size, _descriptor, summarise_payload,
    stream_rows, shape_rows, new_truncated, notice_for, envelope, scrub,
    error_envelope, _fit_response,
)



# --- the guards every tool runs before it touches anything --------------------

def _clock():
    return time.monotonic()


def _ms(started) -> int:
    return int((_clock() - started) * 1000)


def _bad(tool, detail, project, started):
    return error_envelope(tool, "bad_argument", detail, project=project,
                          retryable=False, elapsed_ms=_ms(started),
                          remedy="fix the argument and call again; the bound is "
                                 "in the tool's input schema")


def _check_int(name, value, low, high):
    """``(value, None)`` or ``(None, complaint)``. The complaint NAMES the bound,
    because a rejection a model cannot act on is a rejection it repeats."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{name} must be an integer between {low} and {high}"
    if value < low or value > high:
        return None, (f"{name}={value} is outside its bounds "
                      f"({low}..{high}); the schema publishes them")
    return value, None


def _availability(tool, project, started):
    """The unavailability envelope, or None when a query may be attempted.

    Runs AFTER argument validation on purpose: with the backend down every call
    could answer `backend_unavailable`, and reporting that for a call that was
    also malformed sends the model to fix the server when the fix is in its own
    arguments."""
    reason = pgstore.unavailable_reason()
    if reason is None:
        return None
    code = _REASON_TO_WIRE.get(reason, "backend_unavailable")
    state = pgstore.degraded().get("breaker")
    return error_envelope(
        tool, code,
        f"the Postgres read mirror is not available ({reason})",
        project=project, retryable=True, elapsed_ms=_ms(started),
        freshness=_freshness(project),
        breaker="open" if reason == "breaker_open" else state)


def _freshness(project):
    """The stamp every response carries, unconditionally. Never raises: a
    freshness check that could take a tool down would be a check that costs more
    than the answer it guards."""
    try:
        return pgstore.mirror_health(project)
    except Exception as e:
        return {"available": False, "reason": "backend_unavailable",
                "parity_ok": False, "stale": True,
                "stale_reason": f"the freshness check itself failed: {e}"}


def _parity_refusal(tool, project, fresh, started):
    """The ONE place this design refuses to answer.

    Staleness is bounded and describable — you know exactly what you are missing
    (everything after `rebuilt_at`), so it answers with a stamp. Parity failure
    is unbounded: rows may be missing, duplicated or misordered and you cannot
    say which, so an agent answering from it is confidently wrong with no way to
    know.

    A mirror loaded under another schema version refuses as well
    (`pgstore.mirror_health` reports `parity_ok` False for it), but no chain
    comparison failed there, and the chain-head sentence would send an operator
    hunting for a divergence that does not exist. So when the freshness stamp
    carries `refusal_reason`, that is the detail, and its `repair` commands, in
    order, are the remedy."""
    if fresh.get("parity_ok") is not False:
        return None
    steps = fresh.get("repair")
    return error_envelope(
        tool, "parity_failed",
        fresh.get("refusal_reason") or (
            "the mirror's chain head does not match the file ledger's, so rows "
            "may be missing, duplicated or misordered and this tool cannot say "
            "which"),
        project=project, retryable=False, elapsed_ms=_ms(started),
        remedy=(f"an operator runs {_commands(steps)} (they need "
                f"MINDHIVE_PG_DSN_ADMIN), then re-checks `hive pg status`"
                if steps else None),
        freshness=fresh)


def _commands(steps) -> str:
    """`["hive pg migrate", "hive pg refresh"]` -> the two, backticked, joined
    by ", then " -- the order is the instruction."""
    return ", then ".join(f"`{s}`" for s in steps)


def _stale_notice(fresh):
    """Problem, cause, FIX, and who can perform the fix.

    The first three lines of this used to stop after the cause: "check
    `freshness` before answering a question about now". A model that reads that,
    and is holding a question about now, has been told what it cannot do and
    nothing about what to do instead — so it goes looking, finds
    `hive pg rebuild` in the help, runs it, and gets
    `MINDHIVE_PG_DSN_ADMIN is not set`. Two tool calls and a failed command to
    learn one fact this sentence can carry.

    Naming the ACTOR matters as much as naming the command. The reader role this
    tool connects with carries `default_transaction_read_only` set on the ROLE,
    so the repair is not merely unavailable to the model, it is structurally
    impossible for it. Without that clause the honest response to the notice is
    to retry; with it, the honest response is to hand the command to the
    operator and answer with what the mirror does hold.

    The COMMAND is `hive pg refresh` unless the freshness stamp says otherwise.
    A mirror loaded under another schema version carries `repair` (see
    `pgstore._version_mismatch`), because a refresh alone cannot load a column a
    pending migration has not added yet: then the notice names those commands,
    in order, instead."""
    if not fresh.get("stale"):
        return None
    reason = fresh.get("stale_reason") or "the mirror has not been rebuilt lately"
    steps = fresh.get("repair")
    if not steps:
        how = ("runs `hive pg refresh`, which reloads only the segments that "
               "changed. You cannot run it yourself — it needs")
    elif len(steps) == 1:
        how = f"runs {_commands(steps)}. You cannot run it yourself — it needs"
    else:
        how = (f"runs {_commands(steps)}, in that order. You cannot run them "
               f"yourself — they need")
    return [f"the mirror is stale ({reason}); rows are still real but anything "
            f"newer than the last rebuild is absent. To repair: the OPERATOR "
            f"{how} MINDHIVE_PG_DSN_ADMIN, and the reader role this tool "
            f"connects with is read-only at the role level."]


# --- the connection seam ------------------------------------------------------

def _session(project, timeout_ms):
    """A cursor on the reader connection, scoped to one namespace.

    The seam every test substitutes, and the ONLY place in this module that
    would touch a socket. The namespace rides `SET LOCAL`, never a predicate:
    the reader's view carries the project filter itself, so scoping is a
    database property rather than something a caller has to remember.

    Verified against PostgreSQL 17.11. `set_config(..., true)` rather than
    `SET LOCAL … = %s`, which is a syntax error — `SET` is a utility statement
    and takes no parameter, so the first real call would have answered 42601."""
    import contextlib

    @contextlib.contextmanager
    def _open():
        conn = pgstore.connect()
        try:
            with conn.cursor() as cur:
                # `set_config`, not `SET LOCAL … = %s`, for the reason spelled
                # out at `pgstore.PROJECT_SCOPE_SQL`: `SET` takes no parameter
                # and would have answered 42601 on the first real call. The
                # value must carry its unit, because `set_config` takes text
                # where `SET` took a bare integer meaning milliseconds.
                cur.execute("SELECT set_config('statement_timeout', %s, true)",
                            (f"{int(timeout_ms)}ms",))
                cur.execute(pgstore.PROJECT_SCOPE_SQL, (project,))
                yield cur
        finally:
            conn.close()

    return _open()


def _classify(exc):
    """Map a driver exception onto the closed vocabulary.

    By SQLSTATE where one is available, because a message match is a match that
    breaks on the next Postgres release or the next locale."""
    state = getattr(exc, "sqlstate", None) or getattr(
        getattr(exc, "diag", None), "sqlstate", None)
    if state == "57014":
        return "query_timeout", False
    if state in ("42601", "42501", "0A000", "25006"):
        return "query_rejected", False
    if state and state.startswith("08"):
        return "backend_unavailable", True
    return "db_error", False


def _run(tool, project, sql, params, *, columns=None, max_rows, max_bytes,
         timeout_ms, started, fresh):
    """Execute one statement and shape what comes back. Never raises.

    Verified against PostgreSQL 17.11 for all seven tools."""
    try:
        with _session(project, timeout_ms) as cur:
            cur.execute(sql, params)
            rows, truncated = stream_rows(cur, max_rows=max_rows,
                                          max_bytes=WIRE_MAX_BYTES)
            names = columns or [d[0] for d in (cur.description or [])]
            shaped = shape_rows(rows, names, truncated)
    except Exception as e:
        code, retryable = _classify(e)
        return error_envelope(
            tool, code, f"{type(e).__name__}: {e}", project=project,
            retryable=retryable, elapsed_ms=_ms(started), freshness=fresh,
            hint="filter on session_id, event_type or ts_utc — those are the "
                 "indexed predicates" if code == "query_timeout" else None)
    # The response cap is applied AFTER summarisation, whole rows only, so the
    # model never receives a half row and never receives a cap it was not told
    # about.
    shaped, truncated = _fit_response(shaped, truncated, max_bytes)
    caps = {"max_rows": max_rows, "max_bytes": max_bytes,
            "timeout_ms": timeout_ms}
    return envelope(tool, project=project, rows=shaped, columns=names,
                    truncated=truncated, caps=caps, freshness=fresh,
                    elapsed_ms=_ms(started), extra_notice=_stale_notice(fresh))


# --- the tools ----------------------------------------------------------------

def query_database(sql, *, max_rows=None, max_bytes=None, timeout_ms=None,
                   project=None) -> dict:
    """One read-only SQL statement, capped and shaped. Never raises.

    The wrap — `SELECT * FROM ( <sql> ) AS mh_q LIMIT max_rows+1` — is L2 of the
    read-only layering and is structural: a legitimate SELECT/WITH/VALUES
    survives it unchanged including its own ORDER BY and LIMIT, and everything
    that is not a query becomes a syntax error, which is a safe failure. It is
    NOT the defence — L5, the least-privilege role that cannot even name the
    base table, is; this is the brace that also gives a legible error."""
    started = _clock()
    try:
        name = project or _project()
        if not isinstance(sql, str) or not sql.strip():
            return _bad("query_database", "sql must be a non-empty string",
                        name, started)
        if len(sql) > 8000:
            return _bad("query_database",
                        f"sql is {len(sql)} characters; the schema bounds it at "
                        f"8000", name, started)
        rows_cap, complaint = _check_int("max_rows", max_rows, 1, MAX_ROWS_CEILING)
        if complaint:
            return _bad("query_database", complaint, name, started)
        ms, complaint = _check_int("timeout_ms", timeout_ms, 100,
                                   TIMEOUT_MS_CEILING)
        if complaint:
            return _bad("query_database", complaint, name, started)
        bytes_cap, complaint = _check_int("max_bytes", max_bytes, 1024,
                                          RESPONSE_MAX_BYTES_CEILING)
        if complaint:
            return _bad("query_database", complaint, name, started)

        rows_cap = rows_cap or MAX_ROWS_DEFAULT
        ms = ms or TIMEOUT_MS_DEFAULT
        bytes_cap = bytes_cap or response_byte_cap()

        down = _availability("query_database", name, started)
        if down:
            return down
        fresh = _freshness(name)
        refusal = _parity_refusal("query_database", name, fresh, started)
        if refusal:
            return refusal
        # The cap is a BOUND PARAMETER, and that single fact is doing two jobs.
        #
        # 1. It cannot be commented out. `rstrip(';')` removes only TRAILING
        #    semicolons, so `SELECT 1) AS a; <payload>; --` closed the wrap's
        #    paren early and ran the tail past a `--` that swallowed the rest of
        #    the line, `LIMIT` included. A literal cap is forgeable text; a bound
        #    one is not.
        # 2. It forces the EXTENDED protocol, which is what actually stops the
        #    payload from running. psycopg3 picks the transport on the
        #    TRUTHINESS of the converted params, not on `is not None` — so the
        #    empty tuple that used to sit here selected `send_query`, the simple
        #    protocol, which happily executes several statements in one string.
        #    A non-empty sequence selects `send_query_params`, and the server
        #    refuses more than one command in a prepared statement.
        #
        # The design document asked for exactly this ("always pass a non-`None`
        # params sequence to force the extended protocol"); `()` satisfied the
        # letter of that and none of it.
        wrapped = (f"SELECT * FROM ( {sql.strip().rstrip(';')} ) AS mh_q "
                   f"LIMIT %s")
        return _run("query_database", name, wrapped, (rows_cap + 1,),
                    max_rows=rows_cap,
                    max_bytes=bytes_cap, timeout_ms=ms, started=started,
                    fresh=fresh)
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket("query_database", e, project, started)


RECENT_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  session_id = %s
ORDER  BY ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""


def get_recent_events(session_id, *, limit=None, offset=0, project=None) -> dict:
    """The newest events of one session, in TRUE ledger order.

    "Recent" is `(ts, COALESCE(append_seq, -1), segment, seg_line)` reversed,
    not `ts` alone: timestamps are second-resolution, so several events
    routinely share one `ts` and sorting by time alone hands back an arbitrary
    permutation of them."""
    started = _clock()
    try:
        name = project or _project()
        if not isinstance(session_id, str) or not session_id.strip():
            return _bad("get_recent_events",
                        "session_id must be a non-empty string", name, started)
        cap, complaint = _check_int("limit", limit, 1, 200)
        if complaint:
            return _bad("get_recent_events", complaint, name, started)
        cap = cap or DEFAULT_LIMIT
        skip, complaint = _check_int("offset", offset, 0, OFFSET_CEILING)
        if complaint:
            return _bad("get_recent_events", complaint, name, started)
        skip = skip or 0
        down = _availability("get_recent_events", name, started)
        if down:
            return down
        fresh = _freshness(name)
        refusal = _parity_refusal("get_recent_events", name, fresh, started)
        if refusal:
            return refusal
        out = _run("get_recent_events", name, RECENT_SQL,
                   (session_id, cap + 1, skip), max_rows=cap,
                   max_bytes=response_byte_cap(),
                   timeout_ms=TIMEOUT_MS_DEFAULT, started=started, fresh=fresh)
        out["offset"] = skip
        return out
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket("get_recent_events", e, project, started)


SEARCH_VALUE_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  payload @> jsonb_build_object(%s::text, %s::jsonb)
ORDER  BY ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""

SEARCH_KEY_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  payload ? %s
ORDER  BY ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""


def search_events_by_payload(key, value=None, *, limit=None, offset=0,
                             project=None) -> dict:
    """JSONB containment over top-level payload keys, newest first.

    With a value this is an indexed containment lookup. WITHOUT one it is
    key-existence, which the `jsonb_path_ops` GIN index does not serve — it
    sequential-scans, except for `task_id` and `plan_id`, which carry their own
    partial btrees. The tool description says so, so the model can predict the
    cost rather than discover it as a timeout."""
    started = _clock()
    try:
        name = project or _project()
        if not isinstance(key, str) or not key.strip():
            return _bad("search_events_by_payload",
                        "key must be a non-empty string", name, started)
        if len(key) > 100:
            return _bad("search_events_by_payload",
                        f"key is {len(key)} characters; the schema bounds it at "
                        f"100", name, started)
        cap, complaint = _check_int("limit", limit, 1, 200)
        if complaint:
            return _bad("search_events_by_payload", complaint, name, started)
        cap = cap or DEFAULT_LIMIT
        skip, complaint = _check_int("offset", offset, 0, OFFSET_CEILING)
        if complaint:
            return _bad("search_events_by_payload", complaint, name, started)
        skip = skip or 0
        down = _availability("search_events_by_payload", name, started)
        if down:
            return down
        fresh = _freshness(name)
        refusal = _parity_refusal("search_events_by_payload", name, fresh, started)
        if refusal:
            return refusal
        if value is None:
            sql, params = SEARCH_KEY_SQL, (key, cap + 1, skip)
        else:
            sql, params = SEARCH_VALUE_SQL, (
                key, json.dumps(_jsonable(value), ensure_ascii=False),
                cap + 1, skip)
        out = _run("search_events_by_payload", name, sql, params, max_rows=cap,
                   max_bytes=response_byte_cap(),
                   timeout_ms=TIMEOUT_MS_DEFAULT, started=started, fresh=fresh)
        out["offset"] = skip
        return out
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket("search_events_by_payload", e, project, started)


def get_session_summary(session_id, *, project=None) -> dict:
    """One session, summarised in a single round trip.

    Every list is bounded in SQL before the shaper sees it (25 memos, 25 code
    changes, 10 verifications, 40 type counts), and a list that came back AT its
    cap is reported: a session with 60 decisions gives 25, and "the session made
    25 decisions" is the wrong sentence to let a model write.

    A capped list rides `truncated.rows` rather than a fourth leg, because the
    truncation vocabulary is closed and published in the tool descriptions — a
    key the descriptions never mention is a key the model has no rule for. The
    `notice` names which list, which is the part a model acts on."""
    started = _clock()
    try:
        name = project or _project()
        if not isinstance(session_id, str) or not session_id.strip():
            return _bad("get_session_summary",
                        "session_id must be a non-empty string", name, started)
        down = _availability("get_session_summary", name, started)
        if down:
            return down
        fresh = _freshness(name)
        refusal = _parity_refusal("get_session_summary", name, fresh, started)
        if refusal:
            return refusal
        result = _run("get_session_summary", name, SUMMARY_SQL,
                      (session_id, session_id), max_rows=1,
                      max_bytes=response_byte_cap(),
                      timeout_ms=TIMEOUT_MS_DEFAULT, started=started,
                      fresh=fresh)
        if not result["ok"]:
            return result
        return _as_summary(result, session_id, fresh)
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket("get_session_summary", e, project, started)


def _as_summary(result, session_id, fresh):
    """Fold the one-row result into `summary`, and report every capped list."""
    rows = result["rows"]
    body = {}
    if rows:
        only = rows[0]
        body = next((v for v in only.values() if isinstance(v, dict)), only)
    if not body:
        body = {"rows": 0, "session_id": session_id}
    capped = [name for name, cap in SUMMARY_CAPS.items()
              if isinstance(body.get(name), list) and len(body[name]) >= cap]
    truncated = new_truncated()
    extra = None
    if capped:
        truncated["rows"] = True
        extra = [f"these lists came back AT their cap and are therefore "
                 f"incomplete: {', '.join(sorted(capped))}. Do not report their "
                 f"lengths as totals; page the rest with query_database."]
    stale = _stale_notice(fresh)
    return envelope("get_session_summary", project=result["project"], rows=[],
                    columns=[], truncated=truncated, caps=result["caps"],
                    freshness=fresh, elapsed_ms=result["elapsed_ms"],
                    summary=body,
                    extra_notice=(extra or []) + (stale or []) or None)


def _blanket(tool, exc, project, started):
    """The last resort. Nothing may escape into an MCP handler — an exception
    there is an outage inside the agent's turn, and the host renders it as the
    tool being broken rather than the backend being down."""
    return error_envelope(tool, "db_error", f"{type(exc).__name__}: {exc}",
                          project=project, retryable=False,
                          elapsed_ms=_ms(started),
                          remedy="report this; the tool caught an unexpected "
                                 "failure rather than letting it escape")


def _project():
    """The namespace this call answers for. Deferred import: `paths` is cheap,
    but the rule in this cluster is that nothing on the AI path imports more
    than it uses."""
    from . import paths
    return paths.detect_project()


# The session-summary statement. Every CTE is bounded and the `s` CTE is read
# once; the `unfinished` anti-join is over one session, not the ledger.
#
# Verified against PostgreSQL 17.11, including that the `s` CTE is materialised
# ONCE and read by the three branches that follow (EXPLAIN: Index Scan 1.161 ms,
# then CTE Scans at 0.078 / 0.043 ms) — a review had claimed it is read eight
# times, and it is not.
SUMMARY_SQL = """
WITH s AS (
    SELECT * FROM mindhive_ro.events WHERE session_id = %s
), bounds AS (
    SELECT count(*) AS rows,
           min(ts) AS first_ts, max(ts) AS last_ts,
           min(ts_utc) AS first_utc, max(ts_utc) AS last_utc,
           bool_or(event_type = 'session_start') AS opened,
           bool_or(event_type = 'session_end') AS closed,
           (%s LIKE 'research-%%') AS self_reliant
      FROM s
), by_type AS (
    SELECT jsonb_object_agg(event_type, n) AS counts
      FROM (SELECT event_type, count(*) n FROM s GROUP BY 1
             ORDER BY 2 DESC LIMIT 40) t
), by_class AS (
    -- The ledger's core distinction: prose a human meant (semantic) against
    -- telemetry a machine stamped (deterministic). The tool description has
    -- always promised this field; until now nothing computed it and every
    -- caller got null. `unregistered` is kept rather than dropped -- a type in
    -- the mirror with no registry row is drift worth seeing, not a row to hide.
    SELECT jsonb_object_agg(class, n) AS counts
      FROM (SELECT coalesce(t.class, 'unregistered') AS class, count(*) AS n
              FROM s LEFT JOIN mindhive_ro.event_types t USING (event_type)
             GROUP BY 1) x
), memos AS (
    SELECT jsonb_agg(jsonb_build_object('ts', ts, 'type', event_type,
                                        'text', left(payload->>'text', 400))) AS items
      FROM (SELECT * FROM s
             WHERE event_type IN ('decision','caveat','experiment_result',
                                  'next_step','open_question')
             ORDER BY ts, COALESCE(append_seq, -1), segment, seg_line
             LIMIT 25) m
), changes AS (
    SELECT jsonb_agg(jsonb_build_object('ts', ts, 'category', payload->>'category',
                                        'text', left(payload->>'text', 200),
                                        'files', payload->'files')) AS items
      FROM (SELECT * FROM s WHERE event_type = 'code_change'
             ORDER BY ts LIMIT 25) c
), verifications AS (
    SELECT jsonb_agg(jsonb_build_object('ts', ts, 'exit_code', payload->'exit_code',
                                        'counts', payload->'counts')) AS items
      FROM (SELECT * FROM s WHERE event_type = 'verification'
             ORDER BY ts DESC LIMIT 10) v
), trouble AS (
    SELECT count(*) FILTER (WHERE event_type = 'tool_failed') AS tool_failed,
           count(*) FILTER (WHERE event_type = 'policy_denied') AS policy_denied,
           count(*) FILTER (WHERE event_type = 'skill_used'
                              AND payload->>'ok' = 'false') AS skills_failed
      FROM s
), unfinished AS (
    SELECT jsonb_agg(jsonb_build_object('job_id', payload->>'job_id',
                                        'kind', payload->>'kind')) AS items
      FROM s a
     WHERE a.event_type = 'job_started'
       AND NOT EXISTS (SELECT 1 FROM s b
                        WHERE b.event_type IN ('job_finished','job_abandoned')
                          AND b.payload->>'job_id' = a.payload->>'job_id')
)
SELECT to_jsonb(bounds) || jsonb_build_object(
         'counts_by_type',  (SELECT counts FROM by_type),
         'counts_by_class', (SELECT counts FROM by_class),
         'memos',           (SELECT items FROM memos),
         'code_changes',    (SELECT items FROM changes),
         'verifications',   (SELECT items FROM verifications),
         'trouble',         (SELECT to_jsonb(t) FROM trouble t),
         'unfinished_jobs', (SELECT items FROM unfinished)) AS summary
FROM bounds
"""


# --- the state-shaped tools --------------------------------------------------
#
# The four tools above answer "show me events". These three answer "what IS the
# state", which is the question an agent actually opens a session with, and the
# one that used to cost a 19 MB read of 930 JSONL segments to answer.
#
# All three are bounded by DEFAULT_LIMIT and none of them has an unbounded mode.
# That is the point: a tool that CAN return ten thousand rows will, and the
# context window is the resource being protected.

LATEST_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  session_id = %s
ORDER  BY ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  1
"""


def get_latest_event(session_id, *, project=None) -> dict:
    """The single newest event of one session.

    Deliberately not `get_recent_events(limit=1)`, even though it would give the
    same row: this is the "where did that session get to" question, and a tool
    whose name says so is a tool a model reaches for correctly. It also cannot
    be talked into returning more, which `limit` can.

    Newest is `(ts, COALESCE(append_seq, -1), segment, seg_line)` reversed, not
    `ts` alone -- timestamps are second-resolution and several events routinely
    share one."""
    started = _clock()
    try:
        name = project or _project()
        if not isinstance(session_id, str) or not session_id.strip():
            return _bad("get_latest_event",
                        "session_id must be a non-empty string", name, started)
        down = _availability("get_latest_event", name, started)
        if down:
            return down
        fresh = _freshness(name)
        refusal = _parity_refusal("get_latest_event", name, fresh, started)
        if refusal:
            return refusal
        return _run("get_latest_event", name, LATEST_SQL, (session_id,),
                    max_rows=1, max_bytes=response_byte_cap(),
                    timeout_ms=TIMEOUT_MS_DEFAULT, started=started, fresh=fresh)
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket("get_latest_event", e, project, started)


BY_TYPE_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  event_type = %s
ORDER  BY ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""

BY_TYPE_SESSION_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  event_type = %s AND session_id = %s
ORDER  BY ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""


def search_events_by_type(event_type, *, limit=None, offset=0, session_id=None,
                          project=None) -> dict:
    """Every event of one type, newest first, one page at a time.

    Served by `events_type_idx (project, event_type, ts_key)`, so the filter is
    an index condition and only the matching rows are sorted -- measured at
    0.3 ms for `code_change` over 37k events, against the 0.46 s a full
    `events.read_all` costs to answer the same question.

    An unknown type is not an error: it is an empty page, with the registry's
    known types named in the notice. A model that mistyped `code_changes` learns
    the vocabulary from the answer instead of from a stack trace."""
    started = _clock()
    try:
        name = project or _project()
        if not isinstance(event_type, str) or not event_type.strip():
            return _bad("search_events_by_type",
                        "event_type must be a non-empty string", name, started)
        if len(event_type) > 100:
            return _bad("search_events_by_type",
                        f"event_type is {len(event_type)} characters; the "
                        f"schema bounds it at 100", name, started)
        cap, complaint = _check_int("limit", limit, 1, MAX_ROWS_CEILING)
        if complaint:
            return _bad("search_events_by_type", complaint, name, started)
        cap = cap or DEFAULT_LIMIT
        skip, complaint = _check_int("offset", offset, 0, OFFSET_CEILING)
        if complaint:
            return _bad("search_events_by_type", complaint, name, started)
        skip = skip or 0
        if session_id is not None and (not isinstance(session_id, str)
                                       or not session_id.strip()):
            return _bad("search_events_by_type",
                        "session_id, when given, must be a non-empty string",
                        name, started)
        down = _availability("search_events_by_type", name, started)
        if down:
            return down
        fresh = _freshness(name)
        refusal = _parity_refusal("search_events_by_type", name, fresh, started)
        if refusal:
            return refusal
        if session_id is None:
            sql, params = BY_TYPE_SQL, (event_type, cap + 1, skip)
        else:
            sql, params = BY_TYPE_SESSION_SQL, (event_type, session_id,
                                                cap + 1, skip)
        out = _run("search_events_by_type", name, sql, params, max_rows=cap,
                   max_bytes=response_byte_cap(),
                   timeout_ms=TIMEOUT_MS_DEFAULT, started=started, fresh=fresh)
        out["offset"] = skip
        if out.get("ok") and out.get("row_count") == 0 and skip == 0:
            out["notice"] = _unknown_type_notice(event_type, out.get("notice"))
        return out
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket("search_events_by_type", e, project, started)


def _unknown_type_notice(event_type, existing):
    """Name the registered types when a type match came back empty.

    Read from `pgschema.EVENT_TYPE_CLASSES` rather than queried: the registry
    table holds the same values (`sync_type_registry` writes it from here), and
    spending a round trip to re-learn a constant this process already holds is
    the kind of cost that shows up as latency on the AI's slowest path."""
    known = sorted(pgschema.EVENT_TYPE_CLASSES)
    if event_type in known:
        return existing
    line = (f"no event type {event_type!r} is registered, which is why this "
            f"page is empty rather than merely exhausted. Registered types: "
            f"{', '.join(known)}")
    return f"{existing} {line}" if existing else line


CURRENT_STATE_SQL = """
SELECT DISTINCT ON (event_type)
       event_type, global_seq, ts, ts_utc, session_id, payload, tags
FROM   mindhive_ro.events
ORDER  BY event_type, ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  %s
"""

CURRENT_STATE_ONE_SQL = """
SELECT DISTINCT ON (event_type)
       event_type, global_seq, ts, ts_utc, session_id, payload, tags
FROM   mindhive_ro.events
WHERE  event_type = %s
ORDER  BY event_type, ts DESC, COALESCE(append_seq, -1) DESC, segment DESC, seg_line DESC
LIMIT  %s
"""


def get_current_state(key=None, *, limit=None, project=None) -> dict:
    """The newest event of each kind -- the ledger's current state, one row per
    type.

    WHAT THIS IS NOT, stated first because the name invites the wrong reading:
    it is **not** `STATE.md`, and it does not re-run the projector's fold. There
    is no `current_state` table in this schema and inventing one that pretended
    to be the projection would be worse than not having it -- two derivations of
    "the current state" that could disagree, with the SQL one looking
    authoritative because it came from a database.

    What it IS: `DISTINCT ON (event_type)` over the namespace, newest first. For
    every type the ledger records, the most recent instance. That answers "what
    is the latest decision / code change / verification / session boundary"
    directly, in one indexed round trip, which is the question that otherwise
    costs a full ledger read.

    Pass `key` to get one type's newest row (the `WHERE id = X` shape). Omit it
    for the whole map. For the narrative state -- active task, next step, open
    questions -- read `STATE.md`; it is a projection with structure this cannot
    reproduce, and `CLAUDE.md` routes there deliberately."""
    started = _clock()
    try:
        name = project or _project()
        if key is not None and (not isinstance(key, str) or not key.strip()):
            return _bad("get_current_state",
                        "key, when given, must be a non-empty event type",
                        name, started)
        cap, complaint = _check_int("limit", limit, 1, MAX_ROWS_CEILING)
        if complaint:
            return _bad("get_current_state", complaint, name, started)
        cap = cap or DEFAULT_LIMIT
        down = _availability("get_current_state", name, started)
        if down:
            return down
        fresh = _freshness(name)
        refusal = _parity_refusal("get_current_state", name, fresh, started)
        if refusal:
            return refusal
        if key is None:
            sql, params = CURRENT_STATE_SQL, (cap + 1,)
        else:
            sql, params = CURRENT_STATE_ONE_SQL, (key, cap + 1)
        out = _run("get_current_state", name, sql, params, max_rows=cap,
                   max_bytes=response_byte_cap(),
                   timeout_ms=TIMEOUT_MS_DEFAULT, started=started, fresh=fresh)
        if out.get("ok") and out.get("row_count") == 0 and key is not None:
            out["notice"] = _unknown_type_notice(key, out.get("notice"))
        return out
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket("get_current_state", e, project, started)


# The dispatch table `mcp_server.call_tool` and `hive pg tool` both read. ONE
# registry, so the human debug surface cannot drift from the AI one: testing
# either tests the dispatch of the other.
def search_events_by_meaning(query, *, limit=None, event_type=None,
                             min_score=None, model=None, project=None) -> dict:
    """Nearest semantic events to a free-text query. Lives in `pgsearch`.

    A ONE-LINE FORWARDER, deliberately. The body needs an encoder and NumPy, and
    this module is imported by `mcp_server` at startup to list every tool with
    the database down and neither extra installed. Keeping the import inside the
    call is what lets the eighth tool be LISTED on a machine that cannot RUN it
    — which is the state a model should meet as a `deps_missing` envelope, not
    as a tool that silently does not exist.

    The dispatch table below must still hold the real callable, because
    `tests/test_pgschema.py` checks the published schemas and this table name
    for name; a tool present in one and absent from the other is exactly the
    drift that check exists to catch."""
    from . import pgsearch
    return pgsearch.search_events_by_meaning(
        query, limit=limit, event_type=event_type, min_score=min_score,
        model=model, project=project)


TOOL_FUNCTIONS = {
    "query_database": query_database,
    "get_recent_events": get_recent_events,
    "search_events_by_payload": search_events_by_payload,
    "get_session_summary": get_session_summary,
    "get_latest_event": get_latest_event,
    "search_events_by_type": search_events_by_type,
    "get_current_state": get_current_state,
    "search_events_by_meaning": search_events_by_meaning,
}


def call(name, arguments) -> dict:
    """Dispatch one tool call by name. Never raises.

    An unknown name is `bad_argument` rather than a `KeyError`, and an argument
    the function does not accept is the same — `additionalProperties: false` in
    the JSON Schema makes a hallucinated parameter a loud rejection at the MCP
    boundary, and this is the second brace behind it for callers that never went
    through a schema validator (`hive pg tool`, tests)."""
    started = _clock()
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return _bad(str(name),
                    f"unknown tool {name!r}; this server offers "
                    f"{', '.join(sorted(TOOL_FUNCTIONS))}", None, started)
    args = dict(arguments or {})
    try:
        return fn(**args)
    except TypeError as e:
        return _bad(name, f"bad arguments for {name}: {e}", args.get("project"),
                    started)
    except Exception as e:                       # pragma: no cover - last resort
        return _blanket(name, e, args.get("project"), started)


def schema_of(name) -> dict:
    """The published input schema for one tool, from `pgschema`."""
    return pgschema.TOOL_SCHEMAS[name]
