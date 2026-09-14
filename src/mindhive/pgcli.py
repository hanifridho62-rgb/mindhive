# src/mindhive/pgcli.py
"""The `hive pg` group: status, migrate, rebuild, refresh, embed, parity,
tool, mcp.

**A printer, never a second implementation.** `hive pg tool <name>` dispatches
through `pgtools.TOOL_FUNCTIONS` — the exact table `mcp_server.handle_call`
reads — so the human debug surface cannot drift from the AI one, and testing
either tests the dispatch of the other. If this module ever grows its own SQL,
its own caps, or its own shaping, that is the drift to refuse in review;
`tests/test_pgcli.py` makes the refusal mechanical rather than a matter of
someone noticing.

**Why the group exists at all**, since MCP already covers the seven tools: the
admin half has to be a CLI regardless. `pgmirror` holds the owner DSN and must
never be reachable from the AI path, so `migrate`, `rebuild` and `parity` are
human commands by necessity. Given the group is being created anyway, `status`
and `tool` cost nothing extra and turn "the tool returned nothing" from a
question you can only ask a model into one command with one stack trace.

**This module registers its own parser** onto `__main__`'s subparsers, which
`bench.register` introduced as a deliberate deviation. Its docstring said that
if a second module ever registered this way the deviation stops being one and
becomes the convention. This is that second module: `__main__.py` gets two lines
instead of the ~180 an inline argparse block for six subcommands would cost, and
the flags stay beside the code that reads them, so a new flag cannot be added to
one and forgotten in the other.
"""
import json


def _parse_args(pairs) -> dict:
    """`["limit=50", "key=task_id"]` -> `{"limit": 50, "key": "task_id"}`.

    Values are parsed as JSON, falling back to the raw string. That fallback is
    the ergonomics: `session_id=manual` is not valid JSON, and requiring
    `session_id='"manual"'` on a Windows shell is how a debug command stops
    being used. The JSON attempt is what makes `limit=50` an integer and
    `value=true` a boolean — the tools' bound checks compare against types, so a
    string "50" would be refused for a reason that has nothing to do with what
    the operator typed."""
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--arg expects k=v, got {pair!r}")
        key, _, raw = pair.partition("=")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


# The single pointer from the operator surface to the mirror's column
# contract. Named here rather than inlined into the print, because a string
# that appears in exactly one place can be gate-checked:
# `tests/test_docs_parity.py` asserts this section still exists under this
# heading. The four columns most often proposed for "simplification" — one
# JSONB payload rather than two, `ts` as TEXT rather than TIMESTAMPTZ,
# `global_seq` as identity rather than sort key, and what `numeric` does to a
# number's spelling — are all explained there, and each has been proposed by
# a reader who never found it.
CONTRACT_DOC = "docs/DATA_MODEL.md"
CONTRACT_SECTION = "The table, and the columns that are not obvious"


def cmd_status(args) -> int:
    """What the mirror can do right now, and why it cannot do the rest.

    Non-zero when the mirror is unavailable: a status command that always exits
    0 is a status command no script can gate on."""
    from . import pgmirror, pgstore
    reason = pgstore.unavailable_reason()
    health = pgstore.degraded()
    report = {
        "available": reason is None,
        "reason": reason,
        "dsn_configured": pgstore.dsn() is not None,
        "driver_installed": pgstore.deps_available(),
        "failures": health["failures"],
        "breaker": health["breaker"],
        "breaker_policy": health["policy"],
        "last_error": health["last_error"],
        "admin_dsn_configured": pgmirror.admin_dsn() is not None,
        "schema_version": pgmirror.SCHEMA_VERSION,
        "migrations": pgmirror.migration_files(),
        "freshness": pgstore.mirror_health(getattr(args, "project", None)),
        "contract": {"doc": CONTRACT_DOC,
                     "section": CONTRACT_SECTION},
    }
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        state = "available" if report["available"] else \
            f"UNAVAILABLE ({report['reason']})"
        print(f"postgres read mirror: {state}")
        print(f"  reader DSN (MINDHIVE_PG_DSN_RO): "
              f"{'set' if report['dsn_configured'] else 'not set'}")
        print(f"  psycopg installed:               "
              f"{'yes' if report['driver_installed'] else 'no  (pip install -e .[pg])'}")
        print(f"  breaker:                         {report['breaker']} "
              f"(policy {report['breaker_policy']})")
        print(f"  mirror failures this process:    {report['failures']}")
        if report["last_error"]:
            print(f"  last error:                      {report['last_error']}")
        print(f"  migrations on disk:              "
              f"{len(report['migrations'])} ({', '.join(report['migrations'])})")
        fresh = report["freshness"]
        print(f"  freshness:                       "
              f"{'stale' if fresh.get('stale') else 'fresh'} — "
              f"{fresh.get('stale_reason') or 'up to date'}")
        print(f"  column contract:                 {CONTRACT_DOC} — "
              f"{CONTRACT_SECTION}")
    return 0 if report["available"] else 1


def _driver_error(exc) -> bool:
    """True when `exc` is a psycopg error, decided WITHOUT importing psycopg.

    Read off `sys.modules`: an exception can only be a psycopg error if psycopg
    was already imported to raise it, and importing it here would make every
    `hive` command pay for the driver, which
    `tests/test_pgcli.py::test_importing_the_cli_module_costs_no_driver`
    forbids."""
    import sys
    driver = sys.modules.get("psycopg")
    return driver is not None and isinstance(exc, driver.Error)


def _admin(fn, *a, **kw) -> int:
    """Run an admin operation, printing the refusal rather than a traceback.

    Both refusals an operator meets today — no owner DSN, and the server-touching
    half being unbuilt — are configuration facts, not crashes, and a stack trace
    for a configuration fact buries the one sentence that would fix it.

    A DRIVER error prints the same way, as `error: <Type>: <message>`. Between
    pulling a change that adds a column and running `hive pg migrate`, a
    loader's SQL can name a column the table lacks, and psycopg raises
    `UndefinedColumn` — a `ProgrammingError`, not a `RuntimeError` — so the
    operator got a traceback whose last line was the only useful one. The catch
    widens to `psycopg.Error` and no further: any other exception is a bug in
    this code, and still raises.

    When the result carries a `verified` verdict, the EXIT CODE follows it. Same
    rule `cmd_parity` states: a loader that reports success while its own
    verification failed is worse than one that reports failure, because a script
    gates on the code and a human reads the JSON."""
    import sys
    try:
        result = fn(*a, **kw)
    except (RuntimeError, NotImplementedError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        if not _driver_error(e):
            raise
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    verified = result.get("verified") if isinstance(result, dict) else None
    if isinstance(verified, dict) and not verified.get("ok"):
        return 1
    return 0


def cmd_migrate(args) -> int:
    from . import pgmirror
    return _admin(pgmirror.apply_migrations, getattr(args, "dsn", None))


def _loaded_and_verified(load, project):
    """Load, then prove it, then stamp it — the three things an operator means.

    **`parity` is not an optional follow-up; it is what records the state.** The
    row it writes to `mirror_state` carries the `ledger_fingerprint`, and that
    fingerprint is the entire basis of the freshness check every tool response
    reports. A rebuild that skipped it left the mirror answering "has never been
    built for this namespace; run `hive pg rebuild`" *immediately after a
    successful rebuild* — a message that is both false and a loop, and one an AI
    reading the tool envelope has no way to see past.

    The library functions stay pure loaders on purpose: `pgmirror.rebuild` is
    composed by `refresh` and called by tests that want the load alone. This
    coupling belongs at the operator surface, where "reload it" always means
    "and leave it in a state something can report on".

    **The load's fingerprint is handed to `parity`, and only that hand-off lets
    the row claim anything about the load.** `parity` records a schema version
    and a `ledger_fingerprint` only when it is told which load it follows;
    without one it updates the verdict columns alone (see
    `pgmirror._record_state`). A bare `hive pg parity` therefore proves the rows
    and cannot clear the version gate that forces a full reload."""
    from . import pgmirror
    out = load(project)
    out["verified"] = pgmirror.parity(project or out.get("project"),
                                      loaded_fingerprint=out.get("fingerprint"))
    return out


def cmd_rebuild(args) -> int:
    """The expensive one: every segment, then a full parity fold."""
    from . import pgmirror
    return _admin(_loaded_and_verified, pgmirror.rebuild,
                  getattr(args, "project", None))


def cmd_refresh(args) -> int:
    """The cheap one, and the one the stale notice names.

    `rebuild` reloads every segment; this reloads only the segments whose bytes
    moved since the recorded fingerprint, then re-runs parity. It is what the
    scheduled `autosync` run calls, so an operator typing it by hand is doing
    exactly what the machine does, not a special manual variant."""
    from . import pgmirror
    return _admin(pgmirror.refresh_changed, getattr(args, "project", None))


def cmd_embed(args) -> int:
    """Index this namespace's semantic events as vectors inside the mirror.

    Incremental by default and re-runnable: an event already indexed under the
    same model, with the same content hash, is skipped rather than re-encoded.
    `--rebuild` drops the model's rows first, which is what to reach for after
    changing what gets embedded rather than which events exist.

    No `verified` key, so `_admin` exits 0 on success: there is nothing to prove
    against the chain here. The vectors are DERIVED from mirrored rows, not a
    second copy of them, and `hive pg parity` remains the gate that says whether
    the rows themselves are right."""
    from . import pgmirror
    return _admin(pgmirror.embed_events,
                  getattr(args, "project", None),
                  model=getattr(args, "model", None),
                  batch=getattr(args, "batch", 256),
                  rebuild=getattr(args, "rebuild", False),
                  force=getattr(args, "force", False))


def cmd_parity(args) -> int:
    """The gate. `parity()` ANSWERS rather than raising, including when it could
    not look — but "I could not check" exits non-zero, because a green pipeline
    that includes an unchecked gate is worse than a red one."""
    from . import pgmirror
    verdict = pgmirror.parity(getattr(args, "project", None))
    if getattr(args, "json", False):
        print(json.dumps(verdict, indent=2))
    else:
        print(f"parity: {'OK' if verdict['ok'] else 'FAILED'}")
        for key in ("project", "rows", "file_head", "pg_head", "reason"):
            if verdict.get(key) is not None:
                print(f"  {key}: {verdict[key]}")
    return 0 if verdict.get("ok") else 1


def cmd_tool(args) -> int:
    """Run one AI tool by name and print the envelope the model would receive.

    The identical code path, deliberately: no extra formatting of the rows, no
    friendlier error, no second set of caps. What an operator reads here is what
    the model read."""
    import sys
    from . import pgtools
    if getattr(args, "list", False):
        for name in sorted(pgtools.TOOL_FUNCTIONS):
            print(name)
        return 0
    if not getattr(args, "name", None):
        print("error: a tool name is required (or --list)", file=sys.stderr)
        return 2
    try:
        arguments = _parse_args(getattr(args, "arg", None))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    envelope = pgtools.call(args.name, arguments)
    print(json.dumps(envelope, indent=None if getattr(args, "json", False) else 2,
                     ensure_ascii=False, default=str))
    return 0 if envelope.get("ok") else 1


def cmd_mcp(args) -> int:
    """Serve the MCP tools over stdio. One body, two spellings — this and
    `python -m mindhive.mcp_server` call the same `main()`.

    Nothing may print to stdout here but MCP frames, which is why this handler
    returns the server's own exit code and prints nothing itself."""
    import sys
    from . import mcp_server
    try:
        return mcp_server.main()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def register(subparsers) -> None:
    """Wire the `pg` group onto ``__main__``'s subparser set.

    The SECOND module to register itself this way (after `bench`), which is what
    turns that deviation into the documented convention for a new
    multi-subcommand group — see `docs/session-legend.md`. Two lines in
    `__main__.py`, and every flag beside the handler that reads it."""
    pg = subparsers.add_parser(
        "pg", help="the Postgres read mirror: status, migrations, rebuild, "
                   "refresh, embed, parity, and the AI tools")
    sub = pg.add_subparsers(dest="pg_cmd", required=True)

    st = sub.add_parser("status", help="is the mirror reachable, fresh, and "
                                       "how many failures has this process seen")
    st.add_argument("--project", default=None, help="namespace (default: this repo)")
    st.add_argument("--json", action="store_true", help="machine-readable report")
    st.set_defaults(func=cmd_status)

    mg = sub.add_parser("migrate", help="apply pending SQL migrations "
                                        "(needs MINDHIVE_PG_DSN_ADMIN)")
    mg.add_argument("--dsn", default=None,
                    help="owner DSN; defaults to MINDHIVE_PG_DSN_ADMIN")
    mg.set_defaults(func=cmd_migrate)

    rb = sub.add_parser("rebuild", help="reload EVERY segment of one namespace "
                                        "from the file ledger (needs "
                                        "MINDHIVE_PG_DSN_ADMIN)")
    rb.add_argument("--project", default=None, help="namespace (default: this repo)")
    rb.set_defaults(func=cmd_rebuild)

    rf = sub.add_parser("refresh", help="reload only the segments that changed, "
                                        "then re-verify parity — the cheap way "
                                        "to make a stale mirror current (needs "
                                        "MINDHIVE_PG_DSN_ADMIN)")
    rf.add_argument("--project", default=None, help="namespace (default: this repo)")
    rf.set_defaults(func=cmd_refresh)

    em = sub.add_parser("embed", help="encode this namespace's semantic events "
                                      "as vectors INSIDE the mirror, so the AI "
                                      "tools can search by meaning (needs "
                                      "MINDHIVE_PG_DSN_ADMIN and "
                                      "pip install -e .[vectors])")
    em.add_argument("--project", default=None, help="namespace (default: this repo)")
    em.add_argument("--model", default=None,
                    help="encoder name; also the row key, so two models can "
                         "coexist instead of overwriting each other "
                         "(default: all-MiniLM-L6-v2)")
    em.add_argument("--batch", type=int, default=256,
                    help="documents per encoder call (default: 256)")
    em.add_argument("--rebuild", action="store_true",
                    help="drop this model's rows first and re-encode "
                         "everything, instead of only what changed")
    em.add_argument("--force", action="store_true",
                    help="run even while a LIVE_LOCK is held; this is a "
                         "burst-CPU operation and the lock exists to hold it off")
    em.set_defaults(func=cmd_embed)

    pa = sub.add_parser("parity", help="compare the file ledger's chain head "
                                       "against the mirror's; non-zero if it "
                                       "cannot be proven")
    pa.add_argument("--project", default=None, help="namespace (default: this repo)")
    pa.add_argument("--json", action="store_true", help="machine-readable verdict")
    pa.set_defaults(func=cmd_parity)

    tl = sub.add_parser("tool", help="run one AI tool and print the envelope a "
                                     "model would receive")
    # `choices` rather than a runtime lookup: a typo is then caught by argparse,
    # which also prints what does exist. Read off the schema registry so a new
    # tool cannot ship listable-but-not-runnable.
    from . import pgschema
    tl.add_argument("name", nargs="?", choices=sorted(pgschema.TOOL_SCHEMAS),
                    help="tool to run")
    tl.add_argument("--arg", action="append", metavar="k=v", default=[],
                    help="tool argument; the value is parsed as JSON when it "
                         "parses, else kept as a string (repeatable)")
    tl.add_argument("--list", action="store_true", help="list the tool names")
    tl.add_argument("--json", action="store_true", help="compact one-line JSON")
    tl.set_defaults(func=cmd_tool)

    mc = sub.add_parser("mcp", help="serve the seven tools over stdio MCP "
                                    "(needs pip install -e .[mcp])")
    mc.set_defaults(func=cmd_mcp)
