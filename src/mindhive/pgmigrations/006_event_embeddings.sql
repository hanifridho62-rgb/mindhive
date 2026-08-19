-- 006_event_embeddings.sql -- the vectors, stored in Postgres beside the events.
--
-- APPLIED FILES ARE IMMUTABLE (see 001_schema.sql's header). A change here is a
-- NEW numbered file; `apply_migrations` records this file's sha256 and refuses
-- to continue if these bytes ever change. Every statement is re-runnable.
--
-- WHY THIS TABLE EXISTS. Semantic recall already worked, but only through
-- ChromaDB — a store on the local filesystem that the Postgres tools cannot see.
-- An AI holding the seven SQL tools could ask "which events mention parity" only
-- by ILIKE, which finds the word and not the meaning. Putting the vectors in the
-- mirror puts them inside the one surface the model already has.
--
-- WHY BYTEA AND NOT pgvector. `pg_available_extensions` on the target cluster
-- has no `vector` row, and installing one would mean fetching an unvetted
-- Windows binary. So the design must work on a stock server, and it does:
--
--   * BYTEA holding float32 little-endian is the SAME on-disk size as real[]
--     (measured: 2120 kB either way for 1000 x 384) and loads 5.5x faster
--     (19.6 ms vs 107.4 ms for the same COPY).
--   * Ranking happens in NumPy, not in SQL, because a pure-SQL dot product over
--     real[] measured 101.7 ms per 1000 rows against 10.4 ms for fetch-plus-
--     NumPy — a 10x gap that only widens. `np.frombuffer` reads this byte layout
--     with zero copies, which is the whole reason for the format.
--   * Vectors are stored L2-NORMALISED, so cosine similarity IS the dot product
--     and no per-query division is needed. `norm` records the PRE-normalisation
--     magnitude, because a zero vector must be detectable after the fact.
--
-- The pgvector upgrade path stays open and cheap: `embedding` becomes a
-- `vector(dim)` column populated from these same bytes, and the ranking moves
-- from NumPy into an ORDER BY. Nothing about the loader or the tool contract
-- changes. That is why the dimension and the model are columns rather than
-- assumptions.
--
-- Runs as the ADMIN/owner DSN only.

CREATE TABLE IF NOT EXISTS mindhive.event_embeddings (
    -- The same namespace scope as mindhive.events, for the same reason: one
    -- database holds every project and nothing may read across the boundary.
    project     TEXT COLLATE "C" NOT NULL,

    -- The NATURAL KEY of a mirrored event: (segment, seg_line) is what the
    -- ledger's own ordering contract is built on, and it is stable across a
    -- reload. `global_seq` is deliberately NOT used -- it is row identity only
    -- and a per-segment refresh re-issues it, so a foreign key on it would break
    -- every time a segment was reloaded.
    segment     TEXT COLLATE "C" NOT NULL,
    seg_line    INTEGER NOT NULL CHECK (seg_line >= 0),

    -- Which encoder produced these bytes. Part of the key, so two models can
    -- coexist during a migration between them instead of one silently
    -- overwriting the other with vectors of a different geometry.
    model       TEXT COLLATE "C" NOT NULL,

    -- Denormalised from mindhive.events on purpose. The similarity search
    -- pre-filters on these BEFORE any vector leaves the server, and a join to
    -- the base table would put the reader back inside schema `mindhive`, which
    -- 003 revokes from it. Kept correct by the loader, which writes both tables
    -- from the same scan.
    ts          TEXT COLLATE "C",
    ts_utc      TIMESTAMPTZ,
    session_id  TEXT COLLATE "C",
    event_type  TEXT COLLATE "C",

    -- ChromaDB's id for the same event. Not a key here; it exists so the two
    -- stores can be compared row for row when they disagree.
    event_id    TEXT COLLATE "C" NOT NULL,

    dim         INTEGER NOT NULL CHECK (dim > 0 AND dim <= 4096),

    -- float32, little-endian, L2-normalised, exactly `dim` values. The CHECK is
    -- the guard that a truncated or double-written blob cannot sit here looking
    -- like a vector -- the failure it prevents is a silent one, because a short
    -- buffer reshapes without error and yields plausible garbage scores.
    embedding   BYTEA NOT NULL,
    CONSTRAINT event_embeddings_width CHECK (octet_length(embedding) = dim * 4),

    -- The magnitude BEFORE normalisation. A zero vector normalises to NaN and
    -- would score against everything; recording the norm makes that detectable
    -- without re-reading the bytes.
    norm        REAL NOT NULL,

    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (project, segment, seg_line, model)
);

ALTER TABLE mindhive.event_embeddings OWNER TO mindhive_owner;

-- The pre-filter path: every similarity search narrows by project and model
-- first, and usually by event_type as well.
CREATE INDEX IF NOT EXISTS event_embeddings_scope_idx
    ON mindhive.event_embeddings (project, model, event_type);

-- Coverage questions ("what is indexed, and how far behind is it") are asked by
-- time, and they are asked by an operator watching a long re-index.
CREATE INDEX IF NOT EXISTS event_embeddings_indexed_at_idx
    ON mindhive.event_embeddings (project, indexed_at DESC);

-- --- the reader's two windows ------------------------------------------------
--
-- TWO views, not one, and the split is about the context window rather than
-- about privilege. A vector is 1536 bytes of noise to a language model; a
-- `SELECT *` over one view carrying them would spend the entire response budget
-- on bytes no model can read. So:
--
--   mindhive_ro.event_embeddings -- metadata only. What `query_database` and a
--       human both actually want: what is indexed, by which model, how wide, how
--       recently. Safe to `SELECT *`.
--   mindhive_ro.event_vectors    -- the same rows WITH the bytes. Read by the
--       similarity tool, which asks for specific columns and never echoes them
--       back into the envelope.
--
-- Both are security_barrier and both carry the same `current_setting` predicate
-- as mindhive_ro.events: an unset setting yields NULL, NULL matches no row, and
-- zero rows is the fail-closed direction. Neither is security_invoker, so they
-- execute with the OWNER's rights and the reader keeps no privilege at all on
-- schema `mindhive`.

CREATE OR REPLACE VIEW mindhive_ro.event_embeddings
    WITH (security_barrier = true) AS
SELECT segment, seg_line, model, ts, ts_utc, session_id, event_type,
       event_id, dim, norm, indexed_at,
       octet_length(embedding) AS embedding_bytes
FROM   mindhive.event_embeddings
WHERE  project = current_setting('mindhive.project', true);

CREATE OR REPLACE VIEW mindhive_ro.event_vectors
    WITH (security_barrier = true) AS
SELECT segment, seg_line, model, ts, ts_utc, session_id, event_type,
       event_id, dim, norm, embedding
FROM   mindhive.event_embeddings
WHERE  project = current_setting('mindhive.project', true);

ALTER VIEW mindhive_ro.event_embeddings OWNER TO mindhive_owner;
ALTER VIEW mindhive_ro.event_vectors    OWNER TO mindhive_owner;

GRANT SELECT ON mindhive_ro.event_embeddings TO mindhive_reader;
GRANT SELECT ON mindhive_ro.event_vectors    TO mindhive_reader;
