# src/mindhive/pgmirror.py
"""The admin half of the Postgres read mirror: migrations, the loader, parity.

**This module is the only place the owner DSN is read, and nothing on the AI
read path may import it.** That is the whole privilege split, and it is
structural rather than advisory: `MINDHIVE_PG_DSN_ADMIN` is absent from a normal
session's environment, so a session physically cannot write even if every other
layer failed. `pgcli` — a human running `hive pg migrate` — is the only importer
this module will ever have, and `tests/test_pgmirror.py` asserts that as an
allowlist.

## Status: built and executed against a real server

`apply_migrations`, `sync_type_registry` and `rebuild` were skeletons that
raised, because their behaviour IS their interaction with a server and there was
none on this machine. There is one now (a user-space PostgreSQL 17 cluster, no
installer and no Windows service), so they have bodies, and every claim below is
one a test run against that cluster can falsify.

What each is, in one line:

* `apply_migrations` — transaction per file, skip on a matching digest, REFUSE
  when a recorded file's bytes changed.
* `sync_type_registry` — DELETE + re-insert of `pgschema.EVENT_TYPE_CLASSES`, so
  the table cannot drift from the frozen registry.
* `refresh` — `DELETE` then `COPY ... FROM STDIN`, in one transaction, so MVCC
  gives every concurrent reader either the whole old snapshot or the whole new
  one and never a half-loaded namespace. **Never `TRUNCATE`**: it cannot be
  scoped to one namespace, so rebuilding one project would take every other
  project's rows with it. `rebuild` is `refresh` with no segment filter — ONE
  loader, because two would number `seg_line` differently the first time a
  segment gained a blank line.
* `refresh_changed` — the incremental caller: `changed_segments` names what
  moved from a per-segment byte fingerprint, `refresh` reloads only those, and
  `parity` re-verifies. The write is incremental; the verifying fold is not, and
  that is deliberate (see the docstring).
* `parity` — answers `ok=False` rather than raising when it cannot look, because
  "I could not check" is a verdict a gate must be able to give.

`scan_segments` is the fold the loader and the gate SHARE, and that sharing is
deliberate: see its docstring for why it re-implements `events.read_all` instead
of calling it, and for what the arrangement structurally cannot catch.
"""
import hashlib
import os
from importlib import resources

# `pgstore` is NOT imported here. The dependency the architecture allows is
# pgmirror -> pgstore (DSN helpers), never the reverse; keeping even that edge
# unwritten until something needs it means the owner DSN and the reader DSN
# share no code path at all today.

_ADMIN_ENV = "MINDHIVE_PG_DSN_ADMIN"
_MIGRATION_PACKAGE = "mindhive.pgmigrations"

# Re-exported from `pgschema`, which is where it is defined and why: the reader
# needs the same integer to decide whether a recorded mirror is one it
# understands, and it may not import this module to get it (see the note above).
from . import pgschema as _pgschema           # noqa: E402  — stdlib-only module

SCHEMA_VERSION = _pgschema.SCHEMA_VERSION


def admin_dsn() -> str | None:
    """The owner DSN from the environment, or None when unconfigured.

    NO fallback to the reader DSN. The tempting shortcut — "use the DSN we do
    have" — would hand every migration to a role with no privilege to run them,
    and would put the two credentials on one code path, which is exactly what
    the two-variable design exists to prevent. A blank value counts as unset."""
    return (os.environ.get(_ADMIN_ENV) or "").strip() or None


def _require_admin(dsn: str | None = None) -> str:
    resolved = dsn or admin_dsn()
    if not resolved:
        raise RuntimeError(
            f"{_ADMIN_ENV} is not set. Migrations, rebuilds and the type "
            f"registry run as the OWNER role; the reader DSN cannot perform "
            f"them and is deliberately not accepted here.")
    return resolved


# --- the migration inventory (no server needed) ------------------------------

def migration_files() -> list[str]:
    """The .sql migrations, sorted into application order.

    Lexicographic filename order IS the application order — schema, then
    indexes, then roles and views — so the sequence is a property of the names
    rather than of a list somebody has to keep in step with the directory.

    Read through `importlib.resources` rather than a path derived from
    `__file__`: the frozen build has no source directory to walk, and a
    migration runner that works only from a checkout fails exactly where an
    operator is least able to debug it."""
    return sorted(p.name for p in resources.files(_MIGRATION_PACKAGE).iterdir()
                  if p.name.endswith(".sql"))


def migration_text(name: str) -> str:
    """One migration's SQL. Raises for a name that is not a migration."""
    if name not in migration_files():
        raise FileNotFoundError(f"no such migration: {name}")
    return resources.files(_MIGRATION_PACKAGE).joinpath(name).read_text(
        encoding="utf-8")


def migration_sha256(name: str) -> str:
    """The digest `schema_migrations.sha256` stores.

    This is what makes an applied migration immutable: on a later run, a
    filename that is recorded but whose digest differs is an operator error, and
    `apply_migrations` stops. Silently re-running the file and silently skipping
    it are both worse — one may fail against a schema that has moved on, the
    other leaves the database in a state nobody described."""
    return hashlib.sha256(migration_text(name).encode("utf-8")).hexdigest()


# --- the server-touching half ------------------------------------------------
#
# Written and executed against a real PostgreSQL 17 cluster (build steps S4-S6).
# The three functions below were skeletons that raised, on the principle that a
# body no test can reach is worse than an honest refusal. They now have a server
# to be checked against, so they have bodies.

_CONNECT_TIMEOUT_S = 10


def _connect_admin(dsn: str | None = None):
    """Open an OWNER connection, with autocommit off.

    `psycopg` is imported inside the body, not at module scope: the inventory
    half above (discovery, digests) and `parity`'s refusal must keep working on
    a machine with no driver, which is the state of CI and was the state of this
    machine until S3."""
    resolved = _require_admin(dsn)
    try:
        import psycopg
    except ModuleNotFoundError as e:                       # pragma: no cover
        raise RuntimeError(
            "psycopg is not installed, so migrations cannot run. "
            "`pip install -e .[pg]` first.") from e
    return psycopg.connect(resolved, autocommit=False,
                           connect_timeout=_CONNECT_TIMEOUT_S)


_RECORD_SQL = ("INSERT INTO mindhive.schema_migrations (filename, sha256) "
               "VALUES (%s, %s)")


def _applied(cur) -> dict:
    """`{filename: sha256}` already recorded, or `{}` before 001 has ever run.

    Probed with `to_regclass` rather than by SELECTing and catching: a failed
    statement poisons the whole transaction in Postgres, so the tempting
    try/except would leave the connection unusable for the very migration it
    was checking for."""
    cur.execute("SELECT to_regclass('mindhive.schema_migrations')")
    if cur.fetchone()[0] is None:
        return {}
    cur.execute("SELECT filename, sha256 FROM mindhive.schema_migrations")
    return {r[0]: r[1] for r in cur.fetchall()}


def apply_migrations(dsn: str | None = None) -> dict:
    """Apply every pending migration, each in its own transaction.

    Three outcomes per file, and the middle one is why this is not a loop over
    `cur.execute`:

    * filename recorded AND digest matches -> skip.
    * filename recorded AND digest differs -> REFUSE, loudly, before touching
      anything. An applied migration whose bytes changed means the database and
      the checkout disagree about what was run, and both silent options are
      worse: re-running executes a file against a schema that has moved on,
      skipping leaves a database in a state nobody described.
    * not recorded -> execute the whole file AND insert its `schema_migrations`
      row in the SAME transaction, so a crash between the two is impossible.

    Each file gets its own transaction rather than one wrapping the run: 001 is
    what creates the table 002 and 003 depend on, and a single transaction that
    failed at 003 would roll back a schema that was fine.

    The file text is executed with NO parameters, which is what makes psycopg
    choose the simple protocol and accept the multi-statement bodies (and the
    dollar-quoted DO blocks) the migrations are written as. That is the one
    place in this codebase where the simple protocol is wanted rather than
    guarded against -- the text is a packaged resource, never model input."""
    conn = _connect_admin(dsn)
    applied, skipped = [], []
    try:
        with conn.cursor() as cur:
            recorded = _applied(cur)
        conn.commit()

        for name in migration_files():
            digest = migration_sha256(name)
            if name in recorded:
                if recorded[name] != digest:
                    raise RuntimeError(
                        f"migration {name} is recorded as applied with sha256 "
                        f"{recorded[name][:12]} but the file on disk hashes to "
                        f"{digest[:12]}. An applied migration is immutable; "
                        f"write a NEW numbered file instead of editing this "
                        f"one. Nothing was applied.")
                skipped.append(name)
                continue
            with conn.cursor() as cur:
                cur.execute(migration_text(name))
                cur.execute(_RECORD_SQL, (name, digest))
            conn.commit()
            applied.append(name)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"applied": applied, "skipped": skipped,
            "schema_version": SCHEMA_VERSION}


def sync_type_registry(dsn: str | None = None) -> dict:
    """Rewrite `mindhive.event_type_registry` from `pgschema.EVENT_TYPE_CLASSES`.

    Wholesale -- DELETE then re-insert in one transaction -- so the table cannot
    drift from the frozen registry. An upsert would leave a type that had been
    REMOVED from the registry sitting in the database forever, which is exactly
    the drift this exists to prevent."""
    from . import pgschema
    rows = pgschema.registry_rows()
    conn = _connect_admin(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mindhive.event_type_registry")
            cur.executemany(
                "INSERT INTO mindhive.event_type_registry (event_type, class) "
                "VALUES (%s, %s)", rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"types": len(rows)}


# --- the file fold the loader and the gate SHARE -----------------------------

def scan_segments(project: str | None = None) -> list:
    """The file ledger as `(segment, seg_line, Event)` triples, in ledger order.

    A deliberate re-implementation of `events.read_all`'s fold rather than a
    call to it, for two reasons that both bite:

    * `read_all` consults the Postgres seam. Under `MINDHIVE_EVENT_BACKEND=pg`
      the parity gate would fetch its "file" side from Postgres and compare the
      mirror with itself -- the same vacuity `read_all_strict`'s missing
      fallback exists to prevent, arriving from the other direction.
    * `read_all` discards the segment name and the line index, and those two ARE
      the ordering key the mirror stores.

    The line index is `enumerate`'s, counting blank and malformed lines exactly
    as `read_all` does. A loader that numbered only surviving rows would order
    identically today and diverge the first time a segment gained a blank line
    -- a divergence that would surface as a parity failure with no obvious
    cause.

    The loader and the gate calling the SAME function is the point: if this fold
    is wrong, parity compares two things that are wrong together and passes.
    Wrong-but-consistent is what the canary tests have to catch, because the
    gate structurally cannot."""
    from . import events as _events, paths
    d = paths.events_dir(project)
    rows: list = []
    if not d.exists():
        return rows
    for seg in sorted(d.glob("*.jsonl")):
        for i, line in enumerate(seg.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            ev = _events._row_to_event(line)
            if ev is None:
                continue           # skip a torn row, exactly as read_all does
            rows.append((seg.name, i, ev))
    rows.sort(key=lambda t: ((t[2].ts or ""), t[0], t[1]))
    return rows


def _ts_utc(ts):
    """The queryable UTC instant for `ts`, or None.

    None is a reportable anomaly, never a load failure -- 001_schema.sql says so
    and reads stay total. `events.now_iso` produces `%Y-%m-%dT%H:%M:%SZ`; the
    ISO fallback catches offset-carrying rows older tooling wrote."""
    if not ts:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        v = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


_COPY_SQL = """
COPY mindhive.events (project, segment, seg_line, ts, ts_utc,
                      session_id, event_type, payload, tags)
FROM STDIN
"""
# `global_seq` is omitted deliberately -- identity, and row identity only, never
# ordering authority -- and so is `ts_key`, which is GENERATED ALWAYS and so
# would be an error to name here rather than an optimisation to omit.


_DELETE_ALL_SQL = "DELETE FROM mindhive.events WHERE project = %s"
_DELETE_SEGS_SQL = ("DELETE FROM mindhive.events "
                    "WHERE project = %s AND segment = ANY(%s)")
# The `project` predicate is on BOTH, and on the segment form it is not
# redundant: segment names are session ids, and two namespaces can hold a
# `manual.jsonl` each. Dropping it would let a refresh of one project delete
# another project's rows and then not COPY them back -- the same class of damage
# `TRUNCATE` would do, arriving through a narrower door.


def refresh(project: str | None = None, segments=None) -> dict:
    """Reload the named segments of one namespace. `None` means every segment.

    `DELETE` then `COPY ... FROM STDIN`, in ONE transaction, so MVCC hands every
    concurrent reader either the whole old snapshot or the whole new one and
    never a half-loaded namespace.

    Never `TRUNCATE`: it cannot be scoped to a namespace, so rebuilding one
    project would silently take every other project's rows with it.

    **This is the only loader, and `rebuild` is a call to it rather than a
    sibling of it.** Two loaders would agree today and diverge the first time a
    segment gained a blank line, because `seg_line` is `scan_segments`'
    `enumerate` index over every PHYSICAL line -- see that docstring. A second
    write path computing its own line number is that divergence waiting for an
    empty line, and it is why the single-row mirror seam was deleted rather than
    implemented.

    **Per-segment reload cannot reorder anything.** Global order is not a stored
    property of the mirror: `pgstore.READ_ALL_SQL` re-establishes it at read time
    with `ORDER BY COALESCE(ts,''), segment, seg_line`, and `global_seq` is row
    identity only (see `_COPY_SQL`). Rows re-inserted for one segment therefore
    get new `global_seq` values and the fold sees the same sequence as before.

    An empty `segments` list is a real answer -- nothing changed -- and returns
    without opening a connection at all."""
    from . import paths
    name = project or paths.detect_project()

    if segments is not None:
        wanted = sorted(set(segments))
        if not wanted:
            return {"project": name, "rows": 0, "deleted": 0, "segments": [],
                    "schema_version": SCHEMA_VERSION}
    else:
        wanted = None

    triples = scan_segments(name)
    if wanted is not None:
        keep = set(wanted)
        triples = [t for t in triples if t[0] in keep]

    conn = _connect_admin()
    from psycopg.types.json import Jsonb
    try:
        with conn.cursor() as cur:
            if wanted is None:
                cur.execute(_DELETE_ALL_SQL, (name,))
            else:
                cur.execute(_DELETE_SEGS_SQL, (name, wanted))
            deleted = cur.rowcount
            with cur.copy(_COPY_SQL) as cp:
                cp.set_types(["text", "text", "integer", "text", "timestamptz",
                              "text", "text", "jsonb", "jsonb"])
                for seg, line_no, ev in triples:
                    cp.write_row((name, seg, line_no, ev.ts, _ts_utc(ev.ts),
                                  ev.session_id, ev.type,
                                  Jsonb(ev.payload),
                                  Jsonb(list(ev.tags or []))))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"project": name, "rows": len(triples), "deleted": deleted,
            "segments": wanted, "schema_version": SCHEMA_VERSION}


def rebuild(project: str | None = None) -> dict:
    """Reload EVERY segment of one namespace. `refresh` with no segment filter.

    Kept as its own name because it is the one an operator reaches for when the
    mirror is wrong rather than merely behind, and because "reload everything"
    should not require knowing that `None` means everything."""
    return refresh(project, segments=None)


def _fingerprint(project: str) -> dict:
    """What the mirror was built FROM, cheap enough to take on every rebuild.

    A per-segment map, `{filename: bytes}`, plus the total. The map is what makes
    an INCREMENTAL refresh possible: comparing it against a fresh `stat()` names
    the segments that moved without opening one of them. The total is kept
    because `mirror_health`'s staleness message reports it and because a total is
    the cheaper thing to eyeball.

    It used to be `{"segments": <count>, "bytes": <total>}`. A recorded
    fingerprint still carrying that shape is handled as UNKNOWN by
    `changed_segments`, never as unchanged -- see there for why that direction is
    the only safe one."""
    from . import paths
    d = paths.events_dir(project)
    if not d.exists():
        return {"segments": {}, "bytes": 0}
    sizes = {p.name: p.stat().st_size for p in sorted(d.glob("*.jsonl"))}
    return {"segments": sizes, "bytes": sum(sizes.values())}


def changed_segments(project: str, recorded) -> list | None:
    """Segments whose bytes differ from the fingerprint the mirror was built
    from, or `None` meaning "cannot tell -- reload everything".

    FAILS OPEN, and the direction is the whole safety property. An unreadable,
    missing, or legacy-shaped fingerprint returns `None`, which costs a full
    reload; returning `[]` for the same input would cost CORRECTNESS, because a
    mirror that is never refreshed reports itself fresh forever. Cheap-and-wrong
    is not available here.

    A segment present in the recorded map and absent from disk is reported as
    changed: its rows are then deleted and nothing is copied back, which is the
    right answer for a segment that went away."""
    live = _fingerprint(project)["segments"]
    if not isinstance(recorded, dict):
        return None
    was = recorded.get("segments")
    if not isinstance(was, dict):
        return None          # the legacy integer shape: unknown, NOT unchanged
    return sorted(n for n in set(live) | set(was) if live.get(n) != was.get(n))


_RECORDED_FP_SQL = ("SELECT ledger_fingerprint FROM mindhive.mirror_state "
                    "WHERE project = %s")


def recorded_fingerprint(project: str):
    """The fingerprint `mirror_state` holds for one namespace, or None.

    None on every failure path -- no admin DSN, no driver, no row, no table yet
    -- because every one of them means the same thing to the only caller:
    `changed_segments` cannot tell what moved, so reload everything."""
    if not admin_dsn():
        return None
    try:
        conn = _connect_admin()
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(_RECORDED_FP_SQL, (project,))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return dict(row[0]) if row and row[0] else None


def refresh_changed(project: str | None = None) -> dict:
    """Bring the mirror level with the ledger, reloading only what moved.

    This is what `hive pg refresh` and the scheduled `autosync` run call, and the
    cheap half is the WRITE: a normal session appends to one segment, so one
    segment is deleted and re-copied instead of 37k rows.

    THE VERIFICATION FOLD IS NOT INCREMENTAL, deliberately. `parity` runs at the
    end and reads both sides in full. It has to: after new rows land, the
    `parity_ok` recorded in `mirror_state` describes rows that no longer exist,
    and a stamp that says "proven" about a superseded snapshot is worse than the
    seconds the fold costs. Running it is also what re-stamps
    `ledger_fingerprint` -- without that the same segments would be recomputed as
    changed on every run and the mirror would report itself stale forever."""
    from . import paths
    name = project or paths.detect_project()
    segs = changed_segments(name, recorded_fingerprint(name))
    out = refresh(name, segments=segs)
    out["verified"] = parity(name)
    return out


_STATE_SQL = """
INSERT INTO mindhive.mirror_state
    (project, schema_version, row_count, file_head, pg_head, parity_ok,
     ledger_fingerprint, rebuilt_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (project) DO UPDATE SET
    schema_version     = EXCLUDED.schema_version,
    row_count          = EXCLUDED.row_count,
    file_head          = EXCLUDED.file_head,
    pg_head            = EXCLUDED.pg_head,
    parity_ok          = EXCLUDED.parity_ok,
    ledger_fingerprint = EXCLUDED.ledger_fingerprint,
    rebuilt_at         = now()
"""


def _record_state(name, rows, file_head, pg_head, ok) -> bool:
    """Stamp the verdict where a READER can see it. Best-effort by design.

    Writing needs the owner DSN, and `parity` must still answer for a session
    holding only the reader credential -- so a failure here degrades the
    verdict's visibility, never the verdict itself."""
    if not admin_dsn():
        return False
    try:
        from psycopg.types.json import Jsonb
        conn = _connect_admin()
    except Exception:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_STATE_SQL, (name, SCHEMA_VERSION, rows,
                                     file_head or "", pg_head or "", ok,
                                     Jsonb(_fingerprint(name))))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def parity(project: str | None = None) -> dict:
    """Compare the file ledger's chain head against the mirror's.

    ANSWERS rather than raises, including when it could not look: "I could not
    check" is a verdict a gate must be able to give, and an exception here would
    be indistinguishable to a caller from a crash somewhere else.

    Two non-vacuity properties, because the gate is worthless without them:

    * a row count of zero FAILS. Two empty folds hash to the same head, so an
      empty-versus-empty comparison would otherwise pass while proving nothing.
    * the mirror side is read through `pgstore.read_all_strict`, which has no
      file fallback -- a falling-back reader compares the file ledger with
      itself and passes.

    The COUNT is compared as well as the head. They cannot disagree today, the
    head being a fold over the same list, but the check costs nothing and turns
    a future short read into a named failure rather than a bare hash
    mismatch."""
    from . import chain, paths
    name = project or paths.detect_project()

    file_evs = [t[2] for t in scan_segments(name)]
    file_head = chain.compute_chain(file_evs)["head"] if file_evs else None

    try:
        from . import pgstore
        pg_evs = pgstore.read_all_strict(name)
    except Exception as e:
        return {"ok": False, "project": name, "rows": 0,
                "file_rows": len(file_evs),
                "file_head": file_head, "pg_head": None,
                "reason": f"the mirror could not be read "
                          f"({type(e).__name__}: {e})"}

    rows = len(pg_evs)
    if rows == 0:
        return {"ok": False, "project": name, "rows": 0,
                "file_rows": len(file_evs),
                "file_head": file_head, "pg_head": None,
                "reason": "the mirror holds no rows for this namespace, so "
                          "there is nothing to compare; two empty folds hash "
                          "alike, which is why this is a failure not a pass"}

    # ONE fold over the file ledger, with a checkpoint at exactly the number of
    # rows the mirror holds. `compute_chain` already returns the head at that
    # prefix, so the "is the mirror a correct PREFIX of the ledger" question
    # costs nothing beyond the comparison.
    fold = chain.compute_chain(file_evs, checkpoint=rows)
    pg_head = chain.compute_chain(pg_evs)["head"]
    lag = len(file_evs) - rows

    exact = bool(pg_head == fold["head"] and lag == 0)
    prefix = bool(lag > 0 and fold["checkpoint_head"] is not None
                  and pg_head == fold["checkpoint_head"])

    # LAG IS NOT DIVERGENCE, and conflating them makes the whole feature
    # unusable rather than safe. This ledger is append-only and live: a session
    # writes events while it works, so by the time any tool asks, the mirror is
    # already behind by a handful of rows. A gate that refused on that would
    # refuse always — measured, not theorised: every one of the four AI tools
    # returned `parity_failed` on a mirror whose chain head was correct, because
    # the ledger had grown 3,782 bytes since the rebuild.
    #
    # A mirror that is BEHIND is still exactly right about everything it holds,
    # and the prefix check PROVES that rather than assuming it from
    # append-only-ness: every one of the first `rows` events hashes to the same
    # chain as the file ledger's first `rows` events. What must still fail is
    # real divergence — a row missing from the middle, duplicated, reordered, or
    # a mirror somehow longer than its source.
    ok = exact or prefix
    if exact:
        reason = None
    elif prefix:
        reason = (f"the mirror is a correct prefix of the file ledger, {lag} "
                  f"event(s) behind; everything it holds is exact")
    elif lag < 0:
        reason = (f"the mirror holds MORE rows than the file ledger "
                  f"({rows} against {len(file_evs)}), which append-only cannot "
                  f"produce — it was built from a different or truncated store")
    else:
        reason = (f"the mirror diverges from the file ledger: its {rows} rows "
                  f"hash to {pg_head[:12]}, but the ledger's first {rows} "
                  f"events hash to "
                  f"{(fold['checkpoint_head'] or 'nothing')[:12]}")

    verdict = {"ok": ok, "exact": exact, "lag": lag,
               "project": name, "rows": rows, "file_rows": len(file_evs),
               "file_head": fold["head"], "pg_head": pg_head,
               "reason": reason}
    verdict["recorded"] = _record_state(name, rows, fold["head"], pg_head, ok)
    return verdict


# --- the vector loader -------------------------------------------------------
#
# One row of `mindhive.event_embeddings` per SEMANTIC event, written by the same
# owner credential and on the same admin path as everything else in this module.
# The read side never imports any of it; `vectors.pack_embedding` /
# `vectors.unpack_matrix` are the shared description of the byte format, and
# they live in a module with no database in it precisely so both halves can hold
# the same one.

_EMB_COPY_SQL = """
COPY mindhive.event_embeddings (project, segment, seg_line, model, ts, ts_utc,
                                session_id, event_type, event_id, dim,
                                embedding, norm)
FROM STDIN
"""
# `indexed_at` is omitted so its DEFAULT now() stamps each load.

_EMB_KEYS_SQL = ("SELECT segment, seg_line, event_id "
                 "FROM mindhive.event_embeddings "
                 "WHERE project = %s AND model = %s")

_EMB_DELETE_ALL_SQL = ("DELETE FROM mindhive.event_embeddings "
                       "WHERE project = %s AND model = %s")

# Keyed by the POSITION, not by the content hash: the row being removed is the
# one whose event changed or went away, and its hash is by definition the old
# one. `unnest` of two parallel arrays rather than a VALUES list, so one
# statement covers 30k stale keys without a query of 30k literals.
_EMB_DELETE_KEYS_SQL = (
    "DELETE FROM mindhive.event_embeddings "
    "WHERE project = %s AND model = %s "
    "AND (segment, seg_line) IN "
    "(SELECT s, l FROM unnest(%s::text[], %s::int[]) AS t(s, l))")


def embed_events(project: str | None = None, *, model: str | None = None,
                 batch: int = 256, rebuild: bool = False, types=None,
                 force: bool = False) -> dict:
    """Encode every semantic event that is not already indexed, and store it.

    Returns `{"project", "model", "dim", "eligible", "embedded", "skipped",
    "removed", "indexed"}`. `skipped` is the count that was already correct,
    which on a second run is all of them -- the number worth printing, because a
    run that silently re-encoded everything would look identical without it.

    ## What "already indexed" means, and why it is not just a key check

    The row key is `(project, segment, seg_line, model)` -- the ledger's own
    ordering key, the same one `mindhive.events` is loaded under, so a per-
    segment reload lines up row for row. But presence alone would be the wrong
    test: an event REWRITTEN at the same position would keep its old vector
    forever, and the failure would be invisible because a stale vector still
    returns confident-looking hits. So the stored `event_id` -- the content hash
    `vectors.event_id` computes over the canonical JSON, the same material the
    chain covers -- is compared too. Different hash, or gone from the ledger
    entirely, and the row is deleted before the new one is written.

    ## Three deliberate orderings

    * **Probe, then encode, then write.** The recorded keys are read on a short
      connection that is closed before the model is loaded. A re-run with nothing
      to do therefore costs one SELECT: no encoder, no second connection, no
      transaction held open across a multi-second model load.
    * **DELETE and COPY in ONE transaction.** Concurrent readers see either the
      whole old index or the whole new one, never a half-written one -- the same
      property `refresh` gives the events table, for the same reason.
    * **Encode in batches, hold the packed rows.** Memory is `O(fresh x dim x 4)`
      -- 1.5 KB a row, so the 656 embeddable events in this ledger cost about a
      megabyte. `batch` bounds what the ENCODER sees at once, not what the loader
      holds; the two are separate knobs on purpose.

    `guard.ensure_safe` gates it like every other encode path: this is the
    burst-CPU operation the LIVE_LOCK exists to hold off."""
    from . import guard, paths, vectors
    guard.ensure_safe(force=force)
    name = project or paths.detect_project()
    model_name = model or vectors.EMBEDDING_MODEL

    wanted = set(types) if types else set(vectors._semantic_types())
    eligible = [(seg, ln, ev) for seg, ln, ev in scan_segments(name)
                if ev.type in wanted]
    live = {(seg, ln): vectors.event_id(ev) for seg, ln, ev in eligible}

    # 1. probe -- a short connection, closed before anything expensive.
    if rebuild:
        recorded = {}
    else:
        conn = _connect_admin()
        try:
            with conn.cursor() as cur:
                cur.execute(_EMB_KEYS_SQL, (name, model_name))
                recorded = {(r[0], r[1]): r[2] for r in cur.fetchall()}
            conn.commit()
        finally:
            conn.close()

    stale = sorted(k for k, digest in recorded.items() if live.get(k) != digest)
    current = {k for k, digest in recorded.items() if live.get(k) == digest}
    fresh = [(seg, ln, ev) for seg, ln, ev in eligible
             if (seg, ln) not in current]

    if not fresh and not stale and not rebuild:
        return {"project": name, "model": model_name, "dim": None,
                "eligible": len(eligible), "embedded": 0,
                "skipped": len(current), "removed": 0,
                "indexed": len(current)}

    # 2. encode -- outside every transaction, so a slow model load holds no lock.
    packed = []
    if fresh:
        for start in range(0, len(fresh), batch):
            chunk = fresh[start:start + batch]
            docs = [vectors.event_document(ev) for _, _, ev in chunk]
            for (seg, ln, ev), vec in zip(chunk, vectors.encode(docs)):
                blob, dim, norm = vectors.pack_embedding(vec)
                packed.append((name, seg, ln, model_name, ev.ts, _ts_utc(ev.ts),
                               ev.session_id, ev.type, live[(seg, ln)], dim,
                               blob, norm))

    # 3. write -- delete the superseded rows and copy the new ones, atomically.
    conn = _connect_admin()
    try:
        with conn.cursor() as cur:
            if rebuild:
                cur.execute(_EMB_DELETE_ALL_SQL, (name, model_name))
                removed = cur.rowcount
            elif stale:
                cur.execute(_EMB_DELETE_KEYS_SQL,
                            (name, model_name, [k[0] for k in stale],
                             [k[1] for k in stale]))
                removed = cur.rowcount
            else:
                removed = 0
            if packed:
                with cur.copy(_EMB_COPY_SQL) as cp:
                    cp.set_types(["text", "text", "integer", "text", "text",
                                  "timestamptz", "text", "text", "text",
                                  "integer", "bytea", "real"])
                    for row in packed:
                        cp.write_row(row)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"project": name, "model": model_name,
            "dim": packed[0][9] if packed else None,
            "eligible": len(eligible), "embedded": len(packed),
            "skipped": len(current), "removed": removed,
            "indexed": len(current) + len(packed)}
