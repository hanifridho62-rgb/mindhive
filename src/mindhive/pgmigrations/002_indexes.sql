-- 002_indexes.sql -- every index the four tools and the parity fold rely on.
--
-- APPLIED FILES ARE IMMUTABLE (see 001_schema.sql's header). Every statement is
-- IF NOT EXISTS so applying twice is a no-op rather than an error -- that is
-- asserted by a migration-idempotency test whose canary drops one IF NOT EXISTS
-- and requires the second run to fail.
--
-- No CONCURRENTLY here: these run inside apply_migrations' single transaction,
-- and CREATE INDEX CONCURRENTLY cannot run in a transaction block. The mirror
-- is rebuilt by an operator command, never live-served during a migration, so
-- the table lock is free.

-- The parity read and every full-namespace fold:
--   WHERE project = $1 ORDER BY ts_key, segment, seg_line
-- An index-order scan, no sort node, which is the whole point: the file
-- substrate re-sorts the entire ledger on EVERY read_all().
-- Ordered on ts_key rather than ts so a NULL ts and an empty-string ts cannot
-- disagree with Python about which sorts first.
CREATE INDEX IF NOT EXISTS events_order_idx
    ON mindhive.events (project, ts_key, segment, seg_line);

-- get_recent_events: equality on (project, session_id), then a BACKWARD index
-- scan for ORDER BY ... DESC LIMIT n -- again no sort node. Also serves
-- get_session_summary's bounds CTE.
CREATE INDEX IF NOT EXISTS events_session_idx
    ON mindhive.events (project, session_id, ts_key, segment, seg_line);

-- "every code_change", "every verification", and the per-type counts in the
-- session summary.
CREATE INDEX IF NOT EXISTS events_type_idx
    ON mindhive.events (project, event_type, ts_key);

-- The only reason ts_utc exists: WHERE ts_utc > now() - interval '7 days'.
-- Range predicates on the TEXT ts would work too (ISO-8601 is lexicographically
-- ordered) but cannot express interval arithmetic without a cast per row.
-- DESC because every caller wants the recent end.
CREATE INDEX IF NOT EXISTS events_ts_utc_idx
    ON mindhive.events (project, ts_utc DESC);

-- search_events_by_payload via payload @> ...
--
-- jsonb_path_ops, NOT the default jsonb_ops, for three reasons and one of them
-- is decisive:
--   1. @> is the tool's actual operator; containment is what it does.
--   2. jsonb_path_ops is roughly a third the size and more selective on
--      containment (a whole path+value collapses to one hash).
--   3. VALUE LENGTH. GIN entries are subject to the index-tuple size limit
--      (~2712 bytes on 8 kB pages). This ledger stores assistant_response and
--      user_prompt payloads and bounded-but-large verification excerpts. A
--      jsonb_ops GIN indexes every key and every VALUE verbatim and can fail
--      the insert with "index row size ... exceeds maximum". jsonb_path_ops
--      hashes to a fixed 32 bits and has no such limit.
-- [UNVERIFIED] Point 3 is the load-bearing reason and needs a server to prove.
-- After the first full load: CREATE INDEX CONCURRENTLY tmp_probe ON
-- mindhive.events USING GIN (payload jsonb_ops); -- an error proves the choice,
-- success means keeping jsonb_path_ops on size and selectivity alone.
--
-- THE COST, stated rather than discovered: under jsonb_path_ops a
-- key-existence query (payload ? 'files') has NO index and sequential-scans.
-- That is why the two known join keys get their own partial btrees below, and
-- why a future third key gets one too rather than a switch back to jsonb_ops.
CREATE INDEX IF NOT EXISTS events_payload_gin
    ON mindhive.events USING GIN (payload jsonb_path_ops);

-- tags @> '["memo"]' -- the graph/recall filter axis. Same operator class, same
-- reasoning.
CREATE INDEX IF NOT EXISTS events_tags_gin
    ON mindhive.events USING GIN (tags jsonb_path_ops);

-- The two documented join keys, as PARTIAL expression indexes so each covers
-- only the small fraction of rows that actually carry the key. `?` on jsonb is
-- immutable, so the predicate is legal in an index.
--   task_id threads three streams and joins outward to code_change;
--   plan_id threads roadmap <-> autoresume <-> work_injected.
CREATE INDEX IF NOT EXISTS events_task_id_idx
    ON mindhive.events (project, (payload->>'task_id')) WHERE payload ? 'task_id';
CREATE INDEX IF NOT EXISTS events_plan_id_idx
    ON mindhive.events (project, (payload->>'plan_id')) WHERE payload ? 'plan_id';

-- DELIBERATELY NOT INDEXED, so nobody adds these by reflex:
--   * `project` alone -- every btree above leads with it; a standalone index
--     could only ever be redundant.
--   * substring search inside payload text (payload->>'text' ILIKE '%foo%').
--     It sequential-scans and no index here can help; making it fast needs
--     CREATE EXTENSION pg_trgm plus a GIN trigram index. Out of scope, and the
--     tool descriptions tell the model the cost so it is not a surprise.
--   * a multicolumn GIN (project, payload), which would need CREATE EXTENSION
--     btree_gin. The planner can bitmap-AND events_payload_gin with
--     events_order_idx, and the project filter is applied by the view anyway.
--     Revisit only with a measurement.
