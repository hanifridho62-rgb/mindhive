-- 004_mirror_state_view_and_ownership.sql -- two defects a real server exposed.
--
-- APPLIED FILES ARE IMMUTABLE (see 001_schema.sql's header), which is why these
-- are corrections in a NEW file rather than edits to 003. `apply_migrations`
-- enforces that by refusing when a recorded file's sha256 has changed, so
-- editing 003 would have failed loudly against this very database.
--
-- Both defects were invisible without a server, and neither is theoretical:
-- each was observed by connecting AS `mindhive_reader` and reading the error.
--
-- DEFECT 1 -- the reader cannot see the parity verdict it is told to refuse on.
--   `mirror_state` lives in schema `mindhive`, and 003 says
--   `REVOKE ALL ON SCHEMA mindhive FROM mindhive_reader`. So
--   `pgstore.mirror_health`, which every tool response depends on for its
--   freshness stamp, gets `InsufficientPrivilege: permission denied for schema
--   mindhive` and can only ever fail closed. The mirror could be perfectly
--   fresh and every tool call would still answer `parity_failed` -- correct
--   direction, useless outcome. The reader needs a window on the verdict, and
--   it needs the SAME namespace scoping as the events view so one project
--   cannot read another's row.
--
-- DEFECT 2 -- ownership stopped halfway.
--   003 sets the owner of the two schemas and of `mindhive.events`, and nothing
--   else. Everything created by the migration runner therefore belongs to the
--   role that ran it -- in practice a superuser. That matters here because
--   these views are deliberately NOT `security_invoker`: they execute with
--   their OWNER's rights, so a view owned by a superuser is a superuser-rights
--   execution context reachable from the reader's session. It also silently
--   voids 003's `ALTER DEFAULT PRIVILEGES FOR ROLE mindhive_owner IN SCHEMA
--   mindhive_ro`, which only ever applies to objects mindhive_owner creates.
--
-- Runs as the ADMIN/owner DSN only.

-- --- Defect 2 first: the views below must be created as the right owner ------
ALTER TABLE mindhive.event_type_registry OWNER TO mindhive_owner;
ALTER TABLE mindhive.mirror_state        OWNER TO mindhive_owner;
ALTER TABLE mindhive.schema_migrations   OWNER TO mindhive_owner;
ALTER VIEW  mindhive_ro.events           OWNER TO mindhive_owner;
ALTER VIEW  mindhive_ro.event_types      OWNER TO mindhive_owner;

-- --- Defect 1: the reader's window on its own freshness ----------------------
-- security_barrier and the same `current_setting` predicate as mindhive_ro.events,
-- for the same reason: an unset setting yields NULL, NULL matches no row, and
-- zero rows is the fail-closed direction. `project` is omitted from the select
-- list because the row IS this namespace's by construction.
CREATE OR REPLACE VIEW mindhive_ro.mirror_state WITH (security_barrier = true) AS
SELECT schema_version, row_count, file_head, pg_head, parity_ok,
       ledger_fingerprint, rebuilt_at
FROM   mindhive.mirror_state
WHERE  project = current_setting('mindhive.project', true);

ALTER VIEW mindhive_ro.mirror_state OWNER TO mindhive_owner;
GRANT SELECT ON mindhive_ro.mirror_state TO mindhive_reader;
