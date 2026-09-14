-- 007_append_seq.sql -- the append stamp, so same-second rows keep append order.
--
-- APPLIED FILES ARE IMMUTABLE (see 001_schema.sql's header). Every statement is
-- re-runnable.
--
-- THE DEFECT. `ts` is second-resolution and caller-supplied, so rows from two
-- sessions routinely share one. The ordering contract broke that tie on the
-- segment FILENAME, which is not append order: a `sop` update written by session
-- `aaa-...` sorted before the add that session `zzz-...` wrote a moment earlier,
-- and every fold that replays history applied the edit to a record that did not
-- exist yet. The file readers and this mirror shared the defect exactly, which
-- is why the parity gate never saw it.
--
-- THE STAMP. `events.append_event` now writes `seq` into each row: a microsecond
-- wall-clock reading, forced above the high-water mark `append.lock` keeps when
-- the append holds the namespace-wide lock. Where the mark did not rule a stamp
-- -- an append on the lock's fail-open paths, or one that found the mark lost
-- or stale while the clock read no later than a stamp already issued (a step
-- back, or appends inside one tick of a coarse clock) -- it can repeat or fall
-- below one issued earlier (`events._next_seq`), so a stamp is a tie-break, not a unique
-- key. The file readers order each row by its ORDERING
-- stamp -- its own `seq` raised to the highest stamp above it in its segment
-- (`events._segment_events`), because line order inside a segment is append
-- order -- on `(ts, seq, segment, line)`, a row with none sorting as -1
-- (`events._order_key`). This column holds that ordering stamp, and
-- `pgstore.READ_ALL_SQL` orders on `COALESCE(append_seq, -1)` in the same
-- position.
--
-- NULL, NOT -1, for a row with no stamp to order by: none of its own and none
-- above it in its segment. Every row written before the stamp existed is one,
-- and no truthful value can be invented for it: nothing durable records which of
-- two same-second rows in different segments was written first. The sentinel
-- belongs to the ORDER BY, where it reproduces Python exactly; stored, it would
-- make "never stamped" look like a stamp.
--
-- BIGINT because the loader accepts only 0 <= seq < 2^63, which is BIGINT's
-- range. No index: 005 records why no index can serve an ordered read through the
-- security-barrier view, and the filtered tool queries sort a few hundred rows.
--
-- CREATE OR REPLACE, NOT DROP VIEW. 001 and 003 warn that CREATE OR REPLACE VIEW
-- cannot change a view's column list. That holds for renaming, removing,
-- retyping or reordering a column; Postgres does accept new columns APPENDED to
-- the end of the list, which is exactly this change, so `append_seq` goes last.
-- Replacing in place keeps the view's owner (004), its GRANT (003) and anything
-- that depends on it, where a DROP would have to re-establish all three and a
-- missed GRANT fails every AI read closed. `WITH (security_barrier = true)` is
-- restated because a replace sets the view's options to exactly those given --
-- omitting it would silently remove the barrier. The GRANT is restated too: it
-- is a no-op on a database that already holds it, and it keeps this file correct
-- on its own.
--
-- Existing rows get NULL here and keep it until the loader runs again.
-- `pgschema.SCHEMA_VERSION` moved to 2 with this file, so `mirror_health`
-- reports a mirror built before it as stale, and `pgmirror.recorded_fingerprint`
-- answers None for it, which makes `refresh_changed` reload every segment rather
-- than only the ones whose bytes moved.
--
-- Runs as the ADMIN/owner DSN only.

ALTER TABLE mindhive.events ADD COLUMN IF NOT EXISTS append_seq BIGINT;

CREATE OR REPLACE VIEW mindhive_ro.events WITH (security_barrier = true) AS
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags,
       segment, seg_line, append_seq
FROM   mindhive.events
WHERE  project = current_setting('mindhive.project', true);

GRANT SELECT ON mindhive_ro.events TO mindhive_reader;
