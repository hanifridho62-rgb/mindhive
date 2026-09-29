# SPDX-License-Identifier: Apache-2.0
# src/mindhive/pgsearch.py
"""The eighth AI tool: search the ledger by MEANING rather than by string.

The other seven answer questions you can already phrase — this session, this
event type, this payload key. `search_events_by_meaning` answers the one you
cannot: *"where did we decide something about mirror staleness"*, when the memo
never used the word "staleness". `ILIKE` finds the word; this finds the memo.

**Why this is its own module and not an eighth function in `pgtools`.** Every
other tool is pure SQL and imports nothing heavier than `psycopg`. This one
needs an encoder (`sentence-transformers`) and NumPy, and `pgtools` is imported
by `mcp_server` at startup to LIST the tools with the database down. A heavy
import at that module's top would make listing the tools depend on an ML stack;
a heavy import buried in a function there would hide a whole dependency class
inside a file whose other 1,200 lines have none. The split is along the
dependency boundary, which is where a split is worth making.

`pgtools` owns the shared machinery — the envelope, the caps, the availability
and parity guards, the connection seam — and this module calls it. The direction
is one-way (`pgsearch -> pgtools`) so there is no import cycle: `pgtools`
reaches this module only through a one-line wrapper it defines itself.

## The ranking is not in SQL, and that is measured rather than assumed

Fetch the vectors, rank in NumPy. Against the same 1,000 x 384 vectors on this
cluster, a pure-SQL dot product over `real[]` took 101.7 ms where fetch-plus-
NumPy took 10.4 ms — and the SQL side only gets worse, because `unnest ... WITH
ORDINALITY` joins per row. There is no pgvector on this server (the extension is
not in `pg_available_extensions`), so an index-assisted ORDER BY is not on the
table; the honest choice is the fast one that works everywhere.

The cost of that choice is stated rather than hidden: this reads every vector in
scope over the wire, ~1.5 KB each. `VECTOR_SCAN_CEILING` bounds it, the bound is
BIASED (newest first), and a response that hit the bound says so.
"""
from . import pgtools, pgstore  # noqa: F401  — pgstore for the module allowlist

# 20,000 vectors is ~30 MB on the wire and ~0.2 s to rank. Past that the
# statement timeout is the real limit anyway, so a higher ceiling would trade a
# clear notice for an opaque `query_timeout`.
VECTOR_SCAN_CEILING = 20_000

# Smaller than the other tools' 20. A semantic hit list is read in full by the
# model, and ten memos is already more context than most questions need.
DEFAULT_LIMIT = 10
LIMIT_CEILING = 50

# The scan is ordered NEWEST FIRST, so when the ceiling binds it drops the
# OLDEST vectors rather than an arbitrary set. That is a bias, not a neutral
# sample, and `notice` says so in the response.
VECTORS_SQL = """
SELECT segment, seg_line, dim, embedding
FROM   mindhive_ro.event_vectors
WHERE  model = %s
ORDER  BY ts DESC NULLS LAST, segment DESC, seg_line DESC
LIMIT  %s
"""

VECTORS_TYPED_SQL = """
SELECT segment, seg_line, dim, embedding
FROM   mindhive_ro.event_vectors
WHERE  model = %s AND event_type = %s
ORDER  BY ts DESC NULLS LAST, segment DESC, seg_line DESC
LIMIT  %s
"""

# Hydration by the ledger's own key. `unnest` of two parallel arrays rather than
# an IN-list of literals: one statement, one plan, however many hits.
HYDRATE_SQL = """
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags,
       segment, seg_line
FROM   mindhive_ro.events
WHERE  (segment, seg_line) IN
       (SELECT s, l FROM unnest(%s::text[], %s::int[]) AS t(s, l))
"""

COLUMNS = ["score", "global_seq", "ts", "ts_utc", "session_id", "event_type",
           "payload", "tags"]


def _missing_deps() -> list:
    """Which of the two extras are absent, by name, so the message is actionable."""
    missing = []
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        missing.append("sentence-transformers")
    return missing


def _empty_index_notice(model, event_type):
    scope = f" of type {event_type!r}" if event_type else ""
    return (f"no vectors{scope} are indexed for this namespace under model "
            f"{model!r}, so there is nothing to compare against. This is not a "
            f"failure and not an empty ledger — the index is built by an "
            f"operator running `hive pg embed`, which needs "
            f"MINDHIVE_PG_DSN_ADMIN and the encoder. Until then, search the "
            f"same events by type with `search_events_by_type` or by text with "
            f"`query_database`.")


def search_events_by_meaning(query, *, limit=None, event_type=None,
                             min_score=None, model=None, project=None) -> dict:
    """Nearest semantic events to a free-text query, best first. Never raises.

    Four validated arguments and one envelope, same shape as every other tool,
    with `score` as the first column of each row: a cosine similarity in
    [-1, 1] where 1.0 is identical. Both sides are unit vectors — the stored
    ones were normalised on the way in — so the dot product IS the cosine and
    the number means what a reader assumes it means.

    Two rounds trips inside ONE connection: the vectors, then the events those
    vectors point at. Hydrating separately is what keeps the payloads out of the
    ranking fetch; ranking 664 rows would otherwise drag 664 full payloads
    across the wire to discard all but ten of them."""
    started = pgtools._clock()
    try:
        from . import vectors
        name = project or pgtools._project()
        model_name = model or vectors.EMBEDDING_MODEL

        if not isinstance(query, str) or not query.strip():
            return pgtools._bad("search_events_by_meaning",
                                "query must be a non-empty string", name,
                                started)
        limit, bad = pgtools._check_int("limit", limit, 1, LIMIT_CEILING)
        if bad:
            return pgtools._bad("search_events_by_meaning", bad, name, started)
        limit = limit if limit is not None else DEFAULT_LIMIT
        if event_type is not None and (not isinstance(event_type, str)
                                       or not event_type.strip()):
            return pgtools._bad("search_events_by_meaning",
                                "event_type must be a non-empty string when "
                                "given", name, started)
        if min_score is not None and not isinstance(min_score, (int, float)):
            return pgtools._bad("search_events_by_meaning",
                                "min_score must be a number in [-1, 1]", name,
                                started)

        missing = _missing_deps()
        if missing:
            return pgtools.error_envelope(
                "search_events_by_meaning", "deps_missing",
                f"semantic search needs {' and '.join(missing)}; "
                f"pip install -e .[vectors]", project=name,
                elapsed_ms=pgtools._ms(started))

        down = pgtools._availability("search_events_by_meaning", name, started)
        if down:
            return down
        fresh = pgtools._freshness(name)
        refusal = pgtools._parity_refusal("search_events_by_meaning", name,
                                          fresh, started)
        if refusal:
            return refusal

        # Encode BEFORE opening the connection: the first call loads a model,
        # and a multi-second load must not sit inside a session holding a
        # statement timeout.
        try:
            query_vec = vectors.encode([query])[0]
        except Exception as e:                     # noqa: BLE001
            return pgtools.error_envelope(
                "search_events_by_meaning", "deps_missing",
                f"the encoder could not run ({type(e).__name__}: {e})",
                project=name, elapsed_ms=pgtools._ms(started))

        return _search(name, query_vec, model_name, event_type, limit,
                       min_score, started, fresh)
    except Exception as e:                         # pragma: no cover
        return pgtools._blanket("search_events_by_meaning", e, project, started)


def _search(name, query_vec, model_name, event_type, limit, min_score,
            started, fresh):
    """The two statements and the ranking between them. Never raises."""
    from . import vectors
    truncated = pgtools.new_truncated()
    try:
        with pgtools._session(name, pgtools.TIMEOUT_MS_DEFAULT) as cur:
            if event_type:
                cur.execute(VECTORS_TYPED_SQL,
                            (model_name, event_type, VECTOR_SCAN_CEILING))
            else:
                cur.execute(VECTORS_SQL, (model_name, VECTOR_SCAN_CEILING))
            scanned = cur.fetchall()
            if not scanned:
                return pgtools.envelope(
                    "search_events_by_meaning", project=name, rows=[],
                    columns=COLUMNS, truncated=truncated,
                    caps={"limit": limit, "scan_ceiling": VECTOR_SCAN_CEILING},
                    freshness=fresh, elapsed_ms=pgtools._ms(started),
                    extra_notice=[_empty_index_notice(model_name,
                                                      event_type)],
                    summary={"model": model_name, "dim": None,
                             "vectors_compared": 0, "orphaned": 0})

            dims = {int(r[2]) for r in scanned}
            if len(dims) != 1:
                return pgtools.error_envelope(
                    "search_events_by_meaning", "db_error",
                    f"the index holds vectors of mixed widths {sorted(dims)} "
                    f"under one model name; re-run `hive pg embed --rebuild`",
                    project=name, elapsed_ms=pgtools._ms(started),
                    freshness=fresh)
            dim = dims.pop()
            matrix = vectors.unpack_matrix([r[3] for r in scanned], dim)
            ranked = vectors.rank(query_vec, matrix, limit)
            if min_score is not None:
                ranked = [(i, s) for i, s in ranked if s >= float(min_score)]
            if len(scanned) >= VECTOR_SCAN_CEILING:
                truncated["rows"] = True
            if not ranked:
                return pgtools.envelope(
                    "search_events_by_meaning", project=name, rows=[],
                    columns=COLUMNS, truncated=truncated,
                    caps={"limit": limit, "scan_ceiling": VECTOR_SCAN_CEILING},
                    freshness=fresh, elapsed_ms=pgtools._ms(started),
                    extra_notice=[f"{len(scanned)} vectors were compared "
                                  f"and none scored at or above min_score="
                                  f"{min_score}; lower it or drop it "
                                  f"entirely."],
                    summary={"model": model_name, "dim": dim,
                             "vectors_compared": len(scanned),
                             "orphaned": 0})

            keys = [(scanned[i][0], int(scanned[i][1])) for i, _ in ranked]
            cur.execute(HYDRATE_SQL,
                        ([k[0] for k in keys], [k[1] for k in keys]))
            hydrated = {(r[7], int(r[8])): r for r in cur.fetchall()}
    except Exception as e:
        code, retryable = pgtools._classify(e)
        return pgtools.error_envelope(
            "search_events_by_meaning", code, f"{type(e).__name__}: {e}",
            project=name, retryable=retryable, elapsed_ms=pgtools._ms(started),
            freshness=fresh)

    return _shape(name, scanned, ranked, hydrated, truncated, limit,
                  started, fresh, model_name, dim)


def _shape(name, scanned, ranked, hydrated, truncated, limit, started,
           fresh, model_name, dim):
    """Attach the scores to the hydrated rows, in rank order.

    A vector whose event is NOT in `mindhive_ro.events` is dropped and counted.
    That is not paranoia: the two tables are refreshed by separate commands, so
    a segment reloaded after the index was built can legitimately leave a vector
    pointing at a row that has moved. Silently returning it as an empty row
    would look like a memo with no text."""
    rows, orphaned = [], 0
    for idx, score in ranked:
        key = (scanned[idx][0], int(scanned[idx][1]))
        row = hydrated.get(key)
        if row is None:
            orphaned += 1
            continue
        rows.append((round(float(score), 6),) + tuple(row[:7]))

    shaped = pgtools.shape_rows(rows, COLUMNS, truncated)
    shaped, truncated = pgtools._fit_response(shaped, truncated,
                                              pgtools.response_byte_cap())

    extra = []
    if truncated.get("rows"):
        extra.append(f"only the {VECTOR_SCAN_CEILING} most recent vectors were "
                     f"compared, so older events could not match at all — "
                     f"narrow with event_type, or ask by type and time instead")
    if orphaned:
        extra.append(f"{orphaned} hit(s) pointed at events the mirror no longer "
                     f"holds at that position and were dropped; the vector "
                     f"index is behind the event mirror — an operator can "
                     f"re-run `hive pg embed`")
    # `_stale_notice` answers with a LIST or None, like every other producer
    # of these paragraphs -- extend, never append.
    extra.extend(pgtools._stale_notice(fresh) or [])

    return pgtools.envelope(
        "search_events_by_meaning", project=name, rows=shaped, columns=COLUMNS,
        truncated=truncated,
        caps={"limit": limit, "scan_ceiling": VECTOR_SCAN_CEILING},
        freshness=fresh, elapsed_ms=pgtools._ms(started),
        extra_notice=extra or None,
        summary={"model": model_name, "dim": dim,
                 "vectors_compared": len(scanned),
                 "orphaned": orphaned})
