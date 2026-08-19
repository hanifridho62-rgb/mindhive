-- 003_roles_and_views.sql -- the reader role and the only window it has.
--
-- APPLIED FILES ARE IMMUTABLE (see 001_schema.sql's header). Note the specific
-- trap this file carries: CREATE OR REPLACE VIEW cannot change a view's COLUMN
-- LIST. Adding, removing or reordering a column of mindhive_ro.events needs an
-- explicit DROP VIEW in a NEW numbered file -- never an edit here, which would
-- fail against an already-applied database with "cannot change name of view
-- column".
--
-- This is L5 of the read-only enforcement, and L5 is the layer that actually
-- holds. The Python-side SQL shape check (L1) is a belt for message quality and
-- is never the defence: the design property under test is that deleting L1
-- entirely changes no outcome for any hostile input.
--
-- Runs as the ADMIN/owner DSN only. The reader role holds no privilege that
-- could reach this file's statements.

-- CREATE ROLE has no IF NOT EXISTS in any Postgres version, so idempotency
-- needs the DO block.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindhive_owner') THEN
        CREATE ROLE mindhive_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mindhive_reader') THEN
        -- LOGIN, no password here. The operator sets it out of band
        -- (ALTER ROLE ... PASSWORD, or a .pgpass entry) so no secret is ever
        -- committed, and re-running this file cannot reset it.
        CREATE ROLE mindhive_reader LOGIN NOINHERIT;
    END IF;
END $$;

ALTER SCHEMA mindhive    OWNER TO mindhive_owner;
ALTER SCHEMA mindhive_ro OWNER TO mindhive_owner;
ALTER TABLE  mindhive.events OWNER TO mindhive_owner;

-- The reader's ONLY window. security_barrier so a (hypothetical) leaky function
-- cannot be evaluated against other namespaces' rows before the project filter.
-- NOT security_invoker: the view runs with the OWNER's rights, which is what
-- lets the reader hold no privilege on the base table at all.
--
-- Named `events` so the AI's SQL reads naturally -- and so `SELECT * FROM
-- events` can only ever mean "this namespace". `project` is deliberately NOT in
-- the select list: the namespace predicate is the view's, not the model's, and
-- a description that told the model to write `WHERE project = ...` would break
-- every query it wrote. An unset setting yields NULL and therefore zero rows,
-- which is the fail-closed direction.
CREATE OR REPLACE VIEW mindhive_ro.events WITH (security_barrier = true) AS
SELECT global_seq, ts, ts_utc, session_id, event_type, payload, tags,
       segment, seg_line
FROM   mindhive.events
WHERE  project = current_setting('mindhive.project', true);

CREATE OR REPLACE VIEW mindhive_ro.event_types AS
SELECT event_type, class FROM mindhive.event_type_registry;

-- Least privilege, stated positively and then fenced negatively.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA mindhive FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE mindhive FROM PUBLIC;   -- no temp tables, ever
REVOKE ALL ON SCHEMA mindhive FROM mindhive_reader;  -- base table not even visible

GRANT CONNECT ON DATABASE mindhive TO mindhive_reader;
GRANT USAGE   ON SCHEMA mindhive_ro TO mindhive_reader;
GRANT SELECT  ON mindhive_ro.events, mindhive_ro.event_types TO mindhive_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE mindhive_owner IN SCHEMA mindhive_ro
    GRANT SELECT ON TABLES TO mindhive_reader;

-- Role-level defaults: braces that survive a bug in the Python data-access
-- layer, because they are set on the ROLE rather than per connection.
ALTER ROLE mindhive_reader SET default_transaction_read_only = on;
ALTER ROLE mindhive_reader SET statement_timeout = '5s';
ALTER ROLE mindhive_reader SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE mindhive_reader SET search_path = mindhive_ro, pg_catalog;
ALTER ROLE mindhive_reader SET lock_timeout = '2s';
-- Every AI-issued statement, with its duration, on disk. Turns "what did the
-- model actually ask" from a reconstruction into a file, and costs nothing.
ALTER ROLE mindhive_reader SET log_min_duration_statement = 0;
