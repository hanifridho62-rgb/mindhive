# src/mindhive/pgshape.py
"""Result shaping for the Postgres tool surface: the caps, the payload
summariser, and the envelope every tool answers in.

Lifted out of `pgtools` because the two are different jobs wearing one name. A
tool is argument validation plus one SQL statement; THIS is what decides whether
an agent can reason correctly from the answer, and it has no opinion about which
tool called it. `pgsearch` and `pgtools` both go through here, which is the
point: one envelope shape, learned once.

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

**Stdlib only, and no import of `pgstore`, `pgmirror` or `psycopg`.** Shaping a
row must not depend on a driver being installed or a server being up: the error
envelope is what a caller reaches for precisely when the backend is gone, and a
shaper that could not be imported then would turn every outage into a crash.
`tests/test_pgshape.py` holds that as an AST gate rather than as a convention.

**Nothing here raises.** An exception escaping into an MCP handler is an outage
inside the agent's turn, so `_size` treats an unmeasurable value as large
(failing toward summarising) and `response_byte_cap` falls back to the default
rather than taking the tools down over an operator typo.
"""
import json
import numbers
import os
import re

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
