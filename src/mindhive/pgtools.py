# src/mindhive/pgtools.py
"""The four AI tools over the Postgres read mirror — and, the part that actually
decides whether they work, **result shaping**.

A tool that returns rows is easy. A tool an agent can reason correctly from is
not, and the difference is entirely in this module: the caps, the payload
summariser, the envelope, and a truncation vocabulary that announces itself so a
capped result can never be mistaken for a complete one.

Five properties, each closing a specific way a model gets confidently wrong:

1. **`truncated` is an object, never a boolean, never absent.** Three
   independent caps can bind and they demand opposite follow-ups: more rows
   means re-query narrower, a shortened string means fetch that one value.
2. **Truncation is DETECTED, not applied.** The cursor is asked for
   ``max_rows + 1`` and hands back ``max_rows``. A tool that returns exactly the
   cap and says nothing is how an agent reports "there were 200 events" about
   40,000.
3. **`notice` is prose, present whenever any leg is true.** Models act on
   sentences more reliably than on flags.
4. **Rows are never partial**, and a single row larger than the whole cap is
   still returned — a legitimate one-row answer must not be lost to a cap.
5. **No `total_estimate`.** A wrong total is strictly worse than no total: it is
   the number the agent will quote. ``row_count`` is always and only
   ``len(rows)``.

**Nothing here raises.** An exception escaping into an MCP handler is an outage
inside the agent's turn, so every entry point ends in a blanket catch that
converts to the envelope — the same deliberate construction CLAUDE.md defends
for ``hook_decision``, for the same reason.

## What is verified today, and what is not

**No PostgreSQL server exists on this machine and `psycopg` is not installed.**
Everything above the connection is pure Python and is fully tested: the
streamer, the summariser, the envelope, the error mapping, the wall-clock
behaviour with the backend absent. What is NOT verified is the SQL — whether it
parses, whether the ordering it asks for is the ordering it gets, whether a
connection ever succeeds. Those wait for the `pg`-marked tier. The functions are
written to FAIL CLOSED, so the unproven half cannot answer with something wrong:
with no backend every tool returns a structured `backend_unavailable`.
"""
import json
import numbers
import os
import re
import time

from . import pgschema, pgstore

# --- caps, chosen against a budget rather than picked round -------------------
# This repo already fights a ~51,000-token auto-loaded context budget, and JSON
# is punctuation-heavy — ~3 characters per token, not the ~4 of prose. One tool
# result must be a FRACTION of a turn, not a turn.

# The default page for every helper tool. 20, not 200: an agent asking "what
# happened lately" wants a page it can reason over, and a tool that hands back
# hundreds of rows spends the context window it was built to protect. The
# ceiling stays high for the rare deliberate `limit=200`, and `query_database`
# keeps MAX_ROWS_DEFAULT because a model writing its own aggregate has already
# said what shape it wants.
DEFAULT_LIMIT = 20
# Paging past this is a scan wearing a page's clothes -- OFFSET makes Postgres
# walk and discard every skipped row, so a large offset costs the whole prefix.
OFFSET_CEILING = 10_000
MAX_ROWS_DEFAULT = 200          # ~200 memo-shaped rows ~= 25 KB summarised
MAX_ROWS_CEILING = 500          # the most a model can usefully reason over
TIMEOUT_MS_DEFAULT = 5_000      # matches the role-level statement_timeout
TIMEOUT_MS_CEILING = 15_000     # above this an agent turn stalls visibly

# Two caps at two boundaries, because they protect different things and neither
# can do the other's job: the WIRE cap protects this process's memory (200 rows
# x a multi-MB `assistant_response` is a gigabyte-scale allocation, and that
# payload type is real, not hypothetical); the RESPONSE cap protects the agent's
# context and is strictly downstream of it.
WIRE_MAX_BYTES = 262_144
RESPONSE_MAX_BYTES_DEFAULT = 32_768      # ~11,000 tokens, ~21% of the budget
RESPONSE_MAX_BYTES_CEILING = 131_072
RESPONSE_BYTES_ENV = "MINDHIVE_PG_MAX_BYTES"

# Per-row payload budget. 200 x 1,024 = 200 KB, which exceeds the response cap
# ON PURPOSE: for wide rows the byte cap binds first, for narrow rows the row cap
# does, and both must exist because neither dominates.
PAYLOAD_ROW_BUDGET = 1_024
STRING_HEAD = 200               # enough to identify a value, too short to be it
TEXT_HEAD = 400                 # memos are the point; matches left(…,400) in SQL
OBJECT_KEY_LIST = 30            # the key names are the map

# `get_session_summary`'s lists are bounded in SQL before they ever reach the
# shaper. Repeated here so the shaper can tell a list that hit its cap from one
# that merely ended.
SUMMARY_CAPS = {"memos": 25, "code_changes": 25, "verifications": 10,
                "counts_by_type": 40}

# The closed error vocabulary. A model matching on prose is a model that loops,
# so this is a fixed set and every one of them appears in the tool descriptions
# `pgschema` publishes.
# EVERY member is reachable, and two that were not have been removed:
#
# * `mirror_stale` — staleness ANSWERS, with a stamp and a notice; it never
#   refuses. A live append-only ledger is stale within seconds of any rebuild,
#   so a tool that errored on it would error always.
# * `result_too_large` — the byte cap drops trailing rows and reports
#   `truncated.bytes`; the first row always survives (`_fit_response`), so no
#   input produces an error rather than a shorter list.
#
# A published code that cannot arrive is worse than a missing one: the whole
# point of a closed vocabulary is that the model may key behaviour on it, and
# behaviour keyed on an unreachable code is dead code inside the model's plan.
# `test_every_published_error_code_is_reachable` holds this.
ERROR_CODES = ("backend_unavailable", "deps_missing", "parity_failed",
               "query_rejected", "query_timeout", "db_error", "bad_argument")

# `pgstore.unavailable_reason()` answers in an INTERNAL vocabulary that includes
# `breaker_open`. From the model's side an open breaker is indistinguishable
# from an unreachable backend, and a code the descriptions never mention is a
# code the model has no rule for — so it is folded here and the real state is
# reported in the envelope's own `breaker` field instead. `deps_missing`
# survives the fold because it is the one unavailability with a fix attached.
_REASON_TO_WIRE = {"deps_missing": "deps_missing",
                   "backend_unavailable": "backend_unavailable",
                   "breaker_open": "backend_unavailable"}

_REMEDIES = {
    "deps_missing": "install the Postgres extra: pip install -e .[pg]",
    "backend_unavailable": "start PostgreSQL 17 and set MINDHIVE_PG_DSN_RO, "
                           "then run `hive pg status`",
    "parity_failed": "the mirror does not match the file ledger; "
                     "run `hive pg rebuild --project <name>` and re-check "
                     "`hive pg parity`",
}


def response_byte_cap() -> int:
    """The envelope byte cap, operator-overridable up to a hard ceiling.

    Read on a path that must not raise, so an unparseable or nonsensical value
    falls back to the default rather than taking the tools down: an operator
    typo in an environment variable is not a reason for an outage."""
    raw = (os.environ.get(RESPONSE_BYTES_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return RESPONSE_MAX_BYTES_DEFAULT
    if value <= 0:
        return RESPONSE_MAX_BYTES_DEFAULT
    return min(value, RESPONSE_MAX_BYTES_CEILING)


# --- JSON coercion ------------------------------------------------------------

def _jsonable(value):
    """Whatever the driver handed back, in something `json.dumps` accepts.

    This is not tidiness. The envelope crosses a JSON-RPC frame, and a
    `datetime` from `ts_utc` or a `Decimal` from a `count(*)` reaching
    `json.dumps` unconverted takes the server down mid-turn — on the SUCCESS
    path, which is the worst place for it."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, numbers.Number):
        # `Decimal` is what `count(*)` and `sum()` come back as, and it is not
        # JSON-serialisable. Stringifying it would be safe and wrong: the model
        # would receive `"4924"` for a count and has no way to know whether the
        # quotes are the data or the transport.
        try:
            as_float = float(value)
        except Exception:
            return str(value)
        return int(as_float) if as_float.is_integer() else as_float
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    try:
        return float(value) if isinstance(value, (int, float)) else str(value)
    except Exception:
        return repr(value)


def _size(value) -> int:
    """The JSON byte size of a value, for the caps. Never raises: a value that
    cannot be measured is treated as large, which fails toward summarising."""
    try:
        return len(json.dumps(_jsonable(value), ensure_ascii=False)
                   .encode("utf-8"))
    except Exception:
        return PAYLOAD_ROW_BUDGET + 1


# --- payload summarisation ----------------------------------------------------

def _descriptor(key, value, global_seq):
    """The replacement for one bulk value: what it was, how much of it there
    was, a head, and where to fetch the whole thing.

    Key names are NEVER the thing elided. A model that can see `files` exists
    but not its 37 entries asks a precise follow-up; one handed
    `{"payload": "[truncated]"}` cannot."""
    if isinstance(value, str):
        head = TEXT_HEAD if key == "text" else STRING_HEAD
        d = {"_t": "str", "len": len(value), "head": value[:head]}
    elif isinstance(value, (list, tuple)):
        d = {"_t": "array", "n": len(value)}
    elif isinstance(value, dict):
        keys = [str(k) for k in list(value)[:OBJECT_KEY_LIST]]
        d = {"_t": "object", "keys": keys, "n_keys": len(value)}
    else:
        return _jsonable(value)
    if global_seq is not None:
        # The escape hatch is a TARGETED query — `SELECT payload FROM events
        # WHERE global_seq = N` — so the model takes the byte cost knowingly, on
        # one row. Deliberately not a `full_payload: true` parameter: a boolean
        # that unlocks bulk is a boolean a model sets by default.
        d["global_seq"] = global_seq
    return d


def summarise_payload(payload, global_seq=None, budget=PAYLOAD_ROW_BUDGET):
    """``(shaped, was_changed)`` for one `payload` value.

    Two passes, in this order, because the priorities are not equal:

    1. Every top-level value that is bulk on its own becomes a descriptor.
    2. If the payload is STILL over budget, the largest remaining values become
       descriptors too, biggest first, until it fits.

    A payload of a hundred small scalar keys can exceed the budget with nothing
    left to summarise. It is emitted anyway, over budget, because rule 1 —
    key names survive — outranks the byte budget; the caller flags
    ``truncated.payload`` either way so the model is never told a summarised
    result is a whole one. A non-object payload (the ledger admits them) is
    returned as it is rather than dropped: reads stay total."""
    if not isinstance(payload, dict):
        if isinstance(payload, str) and len(payload) > STRING_HEAD:
            return _descriptor("", payload, global_seq), True
        if isinstance(payload, (list, tuple)) and _size(payload) > budget:
            return _descriptor("", payload, global_seq), True
        return _jsonable(payload), False

    shaped, changed = {}, False
    for key, value in payload.items():
        if _size(value) > STRING_HEAD:
            shaped[str(key)] = _descriptor(str(key), value, global_seq)
            changed = True
        else:
            shaped[str(key)] = _jsonable(value)

    if _size(shaped) > budget:
        for key in sorted(shaped, key=lambda k: _size(shaped[k]), reverse=True):
            if _size(shaped) <= budget:
                break
            original = payload[key] if key in payload else shaped[key]
            replacement = _descriptor(key, original, global_seq)
            if replacement != shaped[key] and isinstance(replacement, dict):
                shaped[key] = replacement
                changed = True
            else:
                break                 # nothing left that summarising can shrink
    return shaped, changed


# --- the row streamer ---------------------------------------------------------

def stream_rows(cur, *, max_rows, max_bytes, chunk=50):
    """Pull rows out of a cursor under two caps, and report WHICH one bound.

    NOT a server-side (named) cursor, and it does not need to be. psycopg's
    default client cursor buffers the whole result set — which would matter if
    the result set could be large, and it cannot: every statement this reaches
    carries `LIMIT max_rows + 1` (bounded at 501 by `MAX_ROWS_CEILING`), applied
    by the server before a byte crosses the socket. A named cursor would add a
    transaction that must be held open and closed on every error path, to bound
    something the LIMIT already bounds.

    ``max_rows + 1`` is requested and at most ``max_rows`` returned: that one
    extra row is the entire truncation-detection mechanism. Chunked rather than
    `fetchall`, because a plain fetch materialises every row first — at 200 rows
    x a multi-MB payload that is a gigabyte-scale client-side allocation.

    Rows are never partial, and the FIRST row is always emitted even if it alone
    exceeds the byte cap: refusing the only row on offer is not protection, it
    is an outage with a green status."""
    truncated = new_truncated()
    rows, total = [], 0
    probe = max_rows + 1
    while len(rows) < probe:
        batch = cur.fetchmany(min(chunk, probe - len(rows)))
        if not batch:
            break
        for row in batch:
            size = _size(list(row))
            if rows and total + size > max_bytes:
                truncated["bytes"] = True
                return rows, truncated
            rows.append(row)
            total += size
            if len(rows) >= probe:
                break
    if len(rows) > max_rows:
        truncated["rows"] = True
        rows = rows[:max_rows]
    return rows, truncated


def shape_rows(rows, columns, truncated):
    """Turn driver tuples into JSON-safe dicts, summarising `payload` only.

    Summarisation applies to the jsonb `payload` column, NOT to the model's own
    projections: a model that writes `SELECT payload->>'text'` asked for that
    column and gets it, subject to the byte cap. Mutates ``truncated`` in place,
    which is how the payload leg reaches the envelope."""
    names = list(columns)
    seq_at = names.index("global_seq") if "global_seq" in names else None
    out = []
    for row in rows:
        values = list(row)
        global_seq = values[seq_at] if seq_at is not None else None
        shaped = {}
        for i, name in enumerate(names):
            value = values[i] if i < len(values) else None
            if name == "payload":
                value, changed = summarise_payload(value, global_seq)
                if changed:
                    truncated["payload"] = True
                shaped[name] = value
            else:
                shaped[name] = _jsonable(value)
        out.append(shaped)
    return out


# --- the envelope -------------------------------------------------------------

def new_truncated() -> dict:
    """The three legs, always present, always booleans. Never a bare `true`: it
    cannot distinguish "there are more rows" from "one string was shortened"."""
    return {"rows": False, "bytes": False, "payload": False}


def notice_for(truncated, caps, extra=None) -> str | None:
    """Prose naming the cap that bound and the exact next action.

    Present whenever any leg is true and absent otherwise — a notice in every
    healthy response is a notice every reader learns to skip."""
    parts = []
    if truncated.get("rows"):
        parts.append(
            f"row cap {caps.get('max_rows', MAX_ROWS_DEFAULT)} reached: more "
            f"rows matched than you were given, so `row_count` is NOT a total. "
            f"Narrow the WHERE, or ask for an explicit aggregate such as "
            f"SELECT count(*).")
    if truncated.get("bytes"):
        parts.append(
            f"byte cap {caps.get('max_bytes', RESPONSE_MAX_BYTES_DEFAULT)} "
            f"reached: rows were stopped BEFORE the cap was crossed, so later "
            f"rows are missing entirely. Select fewer columns or fewer rows.")
    if truncated.get("payload"):
        parts.append(
            "payload values were summarised: their `_t`/`len`/`global_seq` "
            "descriptors say what was elided and how to fetch one whole — "
            "SELECT payload FROM events WHERE global_seq = <n>.")
    if extra:
        parts.extend(extra)
    return " ".join(parts) if parts else None


def envelope(tool, *, project=None, rows=None, columns=None, truncated=None,
             caps=None, freshness=None, elapsed_ms=0, error=None, summary=None,
             breaker=None, extra_notice=None) -> dict:
    """The one response shape all four tools return, success or failure.

    Identical across the four so a model learns it once. `row_count` is computed
    here and only here, from `len(rows)` — never passed in, so it cannot be a
    total by accident."""
    rows = list(rows or [])
    truncated = dict(truncated or new_truncated())
    caps = dict(caps or {"max_rows": MAX_ROWS_DEFAULT,
                         "max_bytes": response_byte_cap(),
                         "timeout_ms": TIMEOUT_MS_DEFAULT})
    env = {
        "ok": error is None,
        "tool": tool,
        "project": project,
        "rows": rows,
        "row_count": len(rows),
        "columns": list(columns or []),
        "truncated": truncated,
        "caps": caps,
        "notice": notice_for(truncated, caps, extra_notice),
        "freshness": freshness if freshness is not None else {"available": False},
        # Not decoration. The mirror holds `user_prompt` and
        # `assistant_response` payloads: a tool result is untrusted content by
        # definition, and this feature makes 20 MB of it bulk-queryable.
        "content_is_untrusted": True,
        "elapsed_ms": elapsed_ms,
        "error": error,
    }
    if summary is not None:
        env["summary"] = summary
    if breaker is not None:
        env["breaker"] = breaker
    return env


# A connection string can carry a password, and driver messages quote the
# conninfo they were handed. This is the ONE place every failure detail passes
# through, so it is where the scrub belongs: a detail assembled correctly in
# nine call sites and carelessly in the tenth still leaves through here.
#
# Deliberately shaped to survive being wrong about the message format. It does
# not try to parse a DSN — it replaces the credentials segment of anything that
# looks like a Postgres URI, so a message that embeds one in prose, in quotes,
# or twice is covered the same way.
_CREDENTIALS = re.compile(r"(postgres(?:ql)?://)[^\s\"']*@")


def scrub(text) -> str:
    """Replace credentials in any Postgres URI with `***`.

    Applied to error details before they reach a model, an MCP host, or a log.
    The host and database survive because they are diagnostic and not secret;
    the user and password do not."""
    if not isinstance(text, str):
        return text
    return _CREDENTIALS.sub(r"\1***@", text)


def error_envelope(tool, code, detail, *, project=None, retryable=False,
                   remedy=None, elapsed_ms=0, freshness=None, breaker=None,
                   hint=None) -> dict:
    """A failure in the closed vocabulary, with a remedy attached.

    An error with no remedy is a dead end: the model retries, gets the same
    thing, and burns the turn.

    `detail` is scrubbed here rather than at each call site: it is built from
    driver exception text in several of them, and a connection string quoted
    back by libpq would otherwise ride into the agent's context."""
    err = {"code": code, "retryable": retryable, "detail": scrub(detail),
           "remedy": remedy or _REMEDIES.get(code, "see `hive pg status`")}
    if hint:
        err["hint"] = hint
    return envelope(tool, project=project, rows=[], columns=[],
                    freshness=freshness, elapsed_ms=elapsed_ms, error=err,
                    breaker=breaker)


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
    know."""
    if fresh.get("parity_ok") is not False:
        return None
    return error_envelope(
        tool, "parity_failed",
        "the mirror's chain head does not match the file ledger's, so rows may "
        "be missing, duplicated or misordered and this tool cannot say which",
        project=project, retryable=False, elapsed_ms=_ms(started),
        freshness=fresh)


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
    operator and answer with what the mirror does hold."""
    reason = fresh.get("stale_reason") or "the mirror has not been rebuilt lately"
    return [f"the mirror is stale ({reason}); rows are still real but anything "
            f"newer than the last rebuild is absent. To repair: the OPERATOR "
            f"runs `hive pg refresh`, which reloads only the segments that "
            f"changed. You cannot run it yourself — it needs "
            f"MINDHIVE_PG_DSN_ADMIN, and the reader role this tool connects "
            f"with is read-only at the role level."] \
        if fresh.get("stale") else None


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


def _fit_response(shaped, truncated, max_bytes):
    """Drop trailing whole rows until the envelope fits, and say so.

    The first row always survives: a one-row answer that is genuinely large is
    still the answer, and rule 4 says a cap must not eat it."""
    total, kept = 0, []
    for row in shaped:
        size = _size(row)
        if kept and total + size > max_bytes:
            truncated["bytes"] = True
            break
        kept.append(row)
        total += size
    return kept, truncated


# --- the four tools -----------------------------------------------------------

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
ORDER  BY ts DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""


def get_recent_events(session_id, *, limit=None, offset=0, project=None) -> dict:
    """The newest events of one session, in TRUE ledger order.

    "Recent" is `(ts, segment, seg_line)` reversed, not `ts` alone: timestamps
    are second-resolution, so several events routinely share one `ts` and
    sorting by time alone hands back an arbitrary permutation of them."""
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
ORDER  BY ts DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""

SEARCH_KEY_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  payload ? %s
ORDER  BY ts DESC, segment DESC, seg_line DESC
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
             ORDER BY ts, segment, seg_line LIMIT 25) m
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
ORDER  BY ts DESC, segment DESC, seg_line DESC
LIMIT  1
"""


def get_latest_event(session_id, *, project=None) -> dict:
    """The single newest event of one session.

    Deliberately not `get_recent_events(limit=1)`, even though it would give the
    same row: this is the "where did that session get to" question, and a tool
    whose name says so is a tool a model reaches for correctly. It also cannot
    be talked into returning more, which `limit` can.

    Newest is `(ts, segment, seg_line)` reversed, not `ts` alone -- timestamps
    are second-resolution and several events routinely share one."""
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
ORDER  BY ts DESC, segment DESC, seg_line DESC
LIMIT  %s OFFSET %s
"""

BY_TYPE_SESSION_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags
FROM   mindhive_ro.events
WHERE  event_type = %s AND session_id = %s
ORDER  BY ts DESC, segment DESC, seg_line DESC
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
ORDER  BY event_type, ts DESC, segment DESC, seg_line DESC
LIMIT  %s
"""

CURRENT_STATE_ONE_SQL = """
SELECT DISTINCT ON (event_type)
       event_type, global_seq, ts, ts_utc, session_id, payload, tags
FROM   mindhive_ro.events
WHERE  event_type = %s
ORDER  BY event_type, ts DESC, segment DESC, seg_line DESC
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
