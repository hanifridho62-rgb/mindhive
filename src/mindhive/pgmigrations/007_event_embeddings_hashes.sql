-- 007_event_embeddings_hashes.sql -- chain coordinates beside the vectors, and
-- the optional pgvector column.
--
-- APPLIED FILES ARE IMMUTABLE (see 001_schema.sql's header). A change here is a
-- NEW numbered file; `apply_migrations` records this file's sha256 and refuses
-- to continue if these bytes ever change. Every statement is re-runnable.
--
-- WHY THE HASHES. 006 stores a vector keyed by (project, segment, seg_line,
-- model) and the event's CONTENT hash. That is enough to tell whether a vector
-- is stale, and not enough to answer the question the storage SPI asks: give me
-- back an `EventRecord`. A record carries `prev_hash` and `chain_hash`, and
-- without them a similarity hit can only be turned into a record by re-reading
-- and re-folding the segment it came from -- one file read per hit, on the
-- query path, to recover two strings the writer already had in its hand.
--
-- They are NULLABLE and they are not part of any key. A row written by
-- `pgmirror.embed_events` (which mirrors the ledger and has no fold) leaves
-- them NULL; a row written through `storage.pgvector_driver` fills them in.
-- `PgVectorIndex.query` reads that difference directly: hashes present means
-- hydrate from the columns, NULL means fall back to the fold. A NOT NULL here
-- would have made the existing loader unable to write at all.
--
-- WHY embedding_vec IS CONDITIONAL. 006 explains at length why the vectors are
-- BYTEA on this cluster: `pg_available_extensions` has no `vector` row, and
-- installing one would mean fetching an unvetted Windows binary. That has not
-- changed, so this file must apply cleanly on a server WITHOUT the extension --
-- which means the `vector(384)` column cannot appear in static DDL, because the
-- type would not resolve at parse time. It is added inside a DO block through
-- EXECUTE, where the string is only parsed if the extension is there. On this
-- machine that branch is unreachable and therefore untested against a server;
-- that is stated in IMPLEMENTATION_PLAN rather than hidden behind a test that
-- would have to lie to be green.

ALTER TABLE mindhive.event_embeddings
    ADD COLUMN IF NOT EXISTS prev_hash  TEXT COLLATE "C",
    ADD COLUMN IF NOT EXISTS chain_hash TEXT COLLATE "C";

-- The pgvector shadow column. Same numbers as `embedding`, the other of two
-- storage shapes -- see `pgmirror._upsert_embedding_sql`, which writes both in
-- one statement so they cannot drift apart.
--
-- 384 is `vectors.EMBEDDING_DIM` for all-MiniLM-L6-v2, spelled as a literal
-- because DDL cannot read Python. A model of another width keeps using the
-- BYTEA column, whose `dim` is per-row; a second width in pgvector would be a
-- second column and a later migration.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        EXECUTE 'ALTER TABLE mindhive.event_embeddings '
                'ADD COLUMN IF NOT EXISTS embedding_vec vector(384)';
        EXECUTE 'CREATE INDEX IF NOT EXISTS event_embeddings_vec_idx '
                'ON mindhive.event_embeddings '
                'USING hnsw (embedding_vec vector_cosine_ops) '
                'WITH (m = 16, ef_construction = 100)';
    END IF;
END
$$;

-- --- the reader's two windows, re-stated -------------------------------------
--
-- CREATE OR REPLACE rather than DROP + CREATE: replacing a view keeps the
-- GRANTs and the ownership, and there is never a moment when the reader's view
-- does not exist. It is legal here for the one reason that makes it legal
-- anywhere -- the new columns are APPENDED; no existing column changes name,
-- type or position, which is the only thing CREATE OR REPLACE VIEW refuses.
--
-- `embedding_vec` is deliberately absent from both. `event_vectors` already
-- exposes the same numbers as BYTEA, and a second copy would double what a
-- `SELECT *` costs a language model's context for no new information -- the
-- exact reason 006 split these two views in the first place.

CREATE OR REPLACE VIEW mindhive_ro.event_embeddings
    WITH (security_barrier = true) AS
SELECT segment, seg_line, model, ts, ts_utc, session_id, event_type,
       event_id, dim, norm, indexed_at,
       octet_length(embedding) AS embedding_bytes,
       prev_hash, chain_hash
FROM   mindhive.event_embeddings
WHERE  project = current_setting('mindhive.project', true);

CREATE OR REPLACE VIEW mindhive_ro.event_vectors
    WITH (security_barrier = true) AS
SELECT segment, seg_line, model, ts, ts_utc, session_id, event_type,
       event_id, dim, norm, embedding,
       prev_hash, chain_hash
FROM   mindhive.event_embeddings
WHERE  project = current_setting('mindhive.project', true);

ALTER VIEW mindhive_ro.event_embeddings OWNER TO mindhive_owner;
ALTER VIEW mindhive_ro.event_vectors    OWNER TO mindhive_owner;

GRANT SELECT ON mindhive_ro.event_embeddings TO mindhive_reader;
GRANT SELECT ON mindhive_ro.event_vectors    TO mindhive_reader;
