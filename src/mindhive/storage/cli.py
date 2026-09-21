# SPDX-License-Identifier: Apache-2.0
"""The `hive db` group: replay the DATA, where `hive pg migrate` moves SCHEMA.

**Two commands called migrate, and the difference is the whole reason this
group is separate.** `hive pg migrate` applies the numbered `.sql` files --
tables, indexes, views. `hive db migrate` replays the file ledger's EVENTS into
those tables, verifying the hash chain as it goes. One creates the shape, the
other fills it, and a single verb doing both would make "migrate failed"
ambiguous in exactly the situation where an operator needs it not to be.

**Every import is inside a handler.** `hive --help` builds this parser, and a
module-scope `from .. import pgmirror` would put the owner-DSN module -- and,
behind it, psycopg -- on the import graph of every `hive` invocation.

**Exit codes are for scripts, and they mean one thing each.** 0 is a verified
run. 1 is a run that happened and whose chain did not verify -- the DATA is
wrong. 2 is a run the ENVIRONMENT stopped: no owner DSN, no psycopg, a
destination that will not answer, a schema that would not apply, or a project
argument argparse refused. The split is not cosmetic: only 1 is worth waking
someone for, and 2 always names the thing to fix.
"""
import json

from .. import cliparse

__all__ = ["cmd_db_migrate", "cmd_db_status", "register", "ADMIN_ENV"]

#: Named here as a literal so the refusal message can carry it without
#: importing `pgmirror` just to read a constant.
ADMIN_ENV = "MINDHIVE_PG_DSN_ADMIN"

_NO_DSN = (f"{ADMIN_ENV} is not set, so there is nowhere to replay INTO. "
           f"Set it to the owner DSN, or pass --dry-run to verify the chain "
           f"and forecast the work without writing.")


def _destination(dsn, project):
    """A `PostgresEventStore` on the owner DSN, or `RuntimeError`."""
    from .postgres_driver import PostgresEventStore
    return PostgresEventStore.from_dsn(dsn, project=project)


def _emit(payload, as_json: bool, lines) -> None:
    """JSON for a script, named lines for a human. Both from ONE dict, so the
    two surfaces cannot come to describe different runs."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    for line in lines(payload):
        print(line)


def cmd_db_migrate(args) -> int:
    """Replay this namespace's ledger into Postgres.

    The chain is verified against its CHECKPOINT first, by `chain.verify`, and
    a namespace that fails is refused before a single row is offered. That is
    the check the replay itself cannot make: `storage.migration` walks records
    a fold produced, so it catches a broken STREAM and not an edited ledger --
    its own docstring says so, and this is the other half of that sentence.

    `pgmirror.apply_migrations` runs first on a real run. It is idempotent by
    construction (it records each file's sha256 and skips what is applied), so
    calling it every time costs one SELECT and removes the failure mode where
    a replay hits a table 007 had not yet added."""
    import sys

    from .local_driver import LocalEventStore
    from .migration import EventReplayMigrationEngine

    project = getattr(args, "project", None)
    dry = bool(getattr(args, "dry_run", False))
    as_json = bool(getattr(args, "json", False))

    checkpoint, verdict = _checkpoint_state(project)
    if checkpoint == "broken":
        print(f"error: the hash chain does not verify for this namespace, so "
              f"there is nothing safe to replay. Run `hive verify` and settle "
              f"that first. ({verdict.get('reason') or verdict})",
              file=sys.stderr)
        return 1

    destination = None
    try:
        destination = _destination(getattr(args, "dsn", None), project)
    except RuntimeError as e:
        if not dry:
            print(f"error: {e}", file=sys.stderr)
            print(f"error: {_NO_DSN}", file=sys.stderr)
            return 2
        # A dry run with no DSN still answers the question it was asked: is
        # this ledger replayable, and how much of it is there.

    if destination is not None and not dry:
        from .. import pgmirror
        try:
            pgmirror.apply_migrations(getattr(args, "dsn", None))
        except (RuntimeError, NotImplementedError) as e:
            print(f"error: the schema could not be brought up to date: {e}",
                  file=sys.stderr)
            return 2

    store = LocalEventStore(project)
    engine = EventReplayMigrationEngine(dry_run=dry)
    try:
        report = engine.migrate_to_postgres(store, destination)
    except Exception as e:                                     # noqa: BLE001
        # Broad on purpose. The first connection is opened INSIDE the replay
        # (`max_seg_line`), so a missing driver, an unreachable server and a
        # revoked grant all arrive here, and every one of them is a setup
        # problem a human is about to fix. A traceback names the frame; this
        # names the fix. A chain break does not reach here -- the engine
        # catches it and reports it as `chain_ok: False`, which is exit 1.
        print(f"error: the replay stopped: {type(e).__name__}: {e}",
              file=sys.stderr)
        print("error: whatever committed before this is a PREFIX, not a "
              "half-written session, and every insert is idempotent -- so "
              "re-running resumes from it rather than starting over.",
              file=sys.stderr)
        return 2
    payload = dict(report.as_dict(), project=_namespace(project),
                   destination=destination is not None,
                   checkpoint=checkpoint)
    _emit(payload, as_json, _migrate_lines)
    return 0 if report.chain_ok else 1


def _namespace(project) -> str:
    from .. import paths
    return project or paths.detect_project()


def _checkpoint_state(project):
    """`("ok"|"absent"|"broken", verdict)` for this namespace's checkpoint.

    Absence is read off the FILE, not off `chain.verify`'s reason string. A
    ledger that has never been checkpointed is not a ledger under suspicion --
    `hive project` simply has not run -- and refusing to replay one would make
    this command unusable on exactly the fresh store it is most wanted on. A
    checkpoint that EXISTS and disagrees with the disk is a different fact and
    stops the run."""
    from .. import chain, paths
    if not (paths.project_dir(project) / "chain.json").exists():
        return "absent", {"ok": None, "reason": "no chain.json"}
    verdict = chain.verify(project)
    return ("ok" if verdict.get("ok") else "broken"), verdict


def _migrate_lines(p):
    mode = "dry run" if p["dry_run"] else "replay"
    yield (f"{mode}: {p['events_read']} event(s) read across "
           f"{p['sessions']} session(s)")
    verb = "would insert" if p["dry_run"] else "inserted"
    yield f"  {verb}: {p['events_inserted']}   skipped: {p['events_skipped']}"
    yield f"  chain: {'ok' if p['chain_ok'] else 'BROKEN'}"
    if p.get("error"):
        yield f"  error: {p['error']}"
    if p.get("checkpoint") == "absent":
        yield ("  note: this namespace has no chain.json, so the replay was "
               "not checked against a checkpoint (run: hive project)")
    if not p["destination"]:
        yield f"  note: no destination ({ADMIN_ENV} unset); nothing was written"
    yield f"  took {p['duration_s']:.2f}s"


def cmd_db_status(args) -> int:
    """What is here, what is there, and whether a replay could run.

    It reports the SAME checkpoint verdict `hive db migrate` gates on, from the
    same `chain.verify`, so "status says ok" and "migrate will start" cannot
    come apart. That costs one scan of the namespace -- the scan `hive verify`
    pays -- and an earlier draft of this docstring claimed the opposite. A
    status that cannot answer its own headline question would be cheaper and
    useless. Everything else here IS cheap: segment names off the directory,
    the migration inventory (no server needed), and -- only when a DSN is
    configured -- how many segments the destination already holds.

    `chain_ok` is TRI-STATE. `None` means this namespace has never been
    checkpointed, which is not the same fact as a chain that verified;
    reporting that as `true` would tell a script the check had passed when it
    had never run."""
    from .. import pgmirror
    from .local_driver import LocalEventStore

    project = getattr(args, "project", None)
    store = LocalEventStore(project)
    sessions = store.list_sessions()
    checkpoint, _verdict = _checkpoint_state(project)
    payload = {"project": _namespace(project),
               "sessions": len(sessions),
               "checkpoint": checkpoint,
               "chain_ok": None if checkpoint == "absent" else checkpoint == "ok",
               "dsn_env": ADMIN_ENV,
               "dsn_configured": pgmirror.admin_dsn() is not None,
               "schema_version": pgmirror.SCHEMA_VERSION,
               "migrations": pgmirror.migration_files(),
               "mirrored_sessions": None,
               "error": None}
    if payload["dsn_configured"]:
        try:
            payload["mirrored_sessions"] = len(
                _destination(None, project).list_sessions())
        except Exception as e:                                 # noqa: BLE001
            payload["error"] = str(e)
    _emit(payload, bool(getattr(args, "json", False)), _status_lines)
    return 0


def _status_lines(p):
    yield f"namespace: {p['project'] or '(none)'}"
    yield f"  sessions on disk: {p['sessions']}"
    if p["checkpoint"] == "absent":
        yield "  hash chain: no checkpoint yet (run: hive project)"
    else:
        yield ("  hash chain: ok" if p["chain_ok"]
               else "  hash chain: BROKEN — run hive verify")
    yield f"  schema: v{p['schema_version']}, {len(p['migrations'])} migration(s)"
    if not p["dsn_configured"]:
        yield f"  destination: not configured ({p['dsn_env']} unset)"
    elif p["error"]:
        yield f"  destination: unreachable — {p['error']}"
    else:
        yield f"  destination: holds {p['mirrored_sessions']} session(s)"


def register(subparsers) -> None:
    """Wire the `db` group onto the root subparser set.

    Self-registering, the convention `bench` introduced and `pg` made the rule
    (`docs/session-legend.md`): the verbs live beside the handlers that read
    their flags, and `commands/parsers/__init__.py` gains two lines instead of
    an inline argparse block."""
    db = subparsers.add_parser(
        "db", help="replay the file ledger's EVENTS into Postgres, hash chain "
                   "verified — the DATA half of `hive pg migrate`'s SCHEMA",
        description="Replay the file ledger's EVENTS into the Postgres mirror, "
                    "with the hash chain verified as it goes. This is the DATA "
                    "half of `hive pg migrate`, which applies the SCHEMA -- the "
                    "numbered .sql files. Two verbs and not one, so that "
                    "\"migrate failed\" is never ambiguous: `hive pg migrate` "
                    "builds the tables, `hive db migrate` fills them.")
    sub = db.add_subparsers(dest="db_cmd", required=True)

    mg = sub.add_parser("migrate", help="replay every session into the mirror; "
                                        "idempotent, so re-running is cheap "
                                        f"(needs {ADMIN_ENV})")
    mg.add_argument("--dsn", default=None,
                    help=f"owner DSN; defaults to {ADMIN_ENV}")
    mg.add_argument("--project", type=cliparse.project_arg, default=None,
                    help="namespace (default: this repo)")
    mg.add_argument("--dry-run", action="store_true",
                    help="verify the chain and forecast the work; write nothing")
    mg.add_argument("--json", action="store_true", help="machine-readable report")
    mg.set_defaults(func=cmd_db_migrate)

    st = sub.add_parser("status", help="what is on disk, what the destination "
                                       "already holds, and whether a replay "
                                       "could run at all")
    st.add_argument("--project", type=cliparse.project_arg, default=None,
                    help="namespace (default: this repo)")
    st.add_argument("--json", action="store_true", help="machine-readable report")
    st.set_defaults(func=cmd_db_status)
