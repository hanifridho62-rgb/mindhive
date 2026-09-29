# tests/test_storage_cli.py
"""`hive db` — replaying the ledger's DATA, where `hive pg migrate` moves SCHEMA.

Two commands are called `migrate` in this CLI and they do different jobs. That
is a deliberate split, not an accident of naming: `hive pg migrate` applies the
numbered `.sql` files, `hive db migrate` replays the events into the tables
they made. A single verb doing both would make "migrate failed" ambiguous in
exactly the situation where an operator needs it not to be, so the first tests
below pin that both verbs exist and that the group registers itself.

Everything here runs with **no PostgreSQL and no `psycopg`**, which is the
state that matters most for this command: it is run once, by a human, on a
machine that is not set up yet. Every way that machine can fail -- no owner
DSN, no driver, an unreachable server, a schema that will not apply -- has to
arrive as a sentence naming the fix, and the exit code has to say whether the
DATA is wrong (1) or the ENVIRONMENT is (2). Those two are what a script pages
on, and they are pinned one by one.
"""
import ast
import json
from pathlib import Path

import pytest

from mindhive import __main__ as cli
from mindhive import chain, events, paths, pgmirror
from mindhive.storage import cli as db_cli
from mindhive.storage.local_driver import LocalEventStore
from tests.test_pg_repo_gates import _sql_writes

SRC = Path(__file__).resolve().parents[1] / "src" / "mindhive"
STORAGE = SRC / "storage"


@pytest.fixture(autouse=True)
def _no_ambient_dsn(monkeypatch):
    """The developer's own owner DSN must not decide what these tests do.

    `cmd_db_status` reports `dsn_configured` straight off the environment and
    `cmd_db_migrate` branches on it, so a machine with `MINDHIVE_PG_DSN_ADMIN`
    exported -- this one -- would run a different set of assertions than CI."""
    monkeypatch.delenv("MINDHIVE_PG_DSN_ADMIN", raising=False)
    monkeypatch.delenv("MINDHIVE_PG_DSN_RO", raising=False)


def _run(*argv):
    return cli.main(list(argv))


def _seed(*sessions):
    """Append one `decision` per (session, n) pair through the SPI itself."""
    store = LocalEventStore("test")
    for session_id, count in sessions:
        for i in range(count):
            store.append(session_id, "decision", {"text": f"{session_id}-{i}"})
    return store


def _json_of(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------
# registration — the group wires itself on, like `pg` and `bench`
# --------------------------------------------------------------------------

def test_the_group_is_registered_on_the_real_parser():
    """Two lines in `commands/parsers/__init__.py`, not an inline argparse
    block: the verbs live beside the handlers that read their flags."""
    import argparse
    parser = cli._build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    assert "db" in sub.choices
    inner = next(a for a in sub.choices["db"]._actions
                 if isinstance(a, argparse._SubParsersAction))
    assert sorted(inner.choices) == ["migrate", "status"]


def test_the_help_for_the_group_renders_and_exits_zero():
    with pytest.raises(SystemExit) as e:
        _run("db", "--help")
    assert e.value.code == 0


@pytest.mark.parametrize("verb", ["migrate", "status"])
def test_each_verb_has_its_own_help(verb):
    with pytest.raises(SystemExit) as e:
        _run("db", verb, "--help")
    assert e.value.code == 0


def test_the_group_alone_is_a_usage_error():
    """`required=True` on the inner subparsers. `hive db` with no verb is a
    typo, and answering it with a report of SOME namespace would be a guess."""
    with pytest.raises(SystemExit) as e:
        _run("db")
    assert e.value.code == 2


def test_the_help_text_says_which_half_of_migrate_this_is(capsys):
    with pytest.raises(SystemExit):
        _run("db", "--help")
    text = capsys.readouterr().out
    assert "hive pg migrate" in text and "SCHEMA" in text


# --------------------------------------------------------------------------
# `--project` is a sink, not a label
# --------------------------------------------------------------------------

@pytest.mark.parametrize("verb", ["migrate", "status"])
@pytest.mark.parametrize("bad", ["../x", "a/b", "unsafe-0000000000000000"])
def test_a_project_that_names_a_path_is_refused_by_argparse(verb, bad, capsys):
    """`type=cliparse.project_arg`, so the refusal happens before a handler
    runs and before anything touches the store. A whitelist check in the
    handler would have let `unsafe-...` through to write under another hash."""
    with pytest.raises(SystemExit) as e:
        _run("db", verb, "--project", bad)
    assert e.value.code == 2
    assert "invalid project" in capsys.readouterr().err


# --------------------------------------------------------------------------
# `db migrate --dry-run` — the whole command with nothing configured
# --------------------------------------------------------------------------

def test_a_dry_run_on_an_empty_store_reports_nothing_and_exits_zero(capsys):
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    got = _json_of(capsys)
    assert got["sessions"] == 0
    assert got["events_read"] == 0
    assert got["events_inserted"] == 0
    assert got["chain_ok"] is True
    assert got["error"] is None


def test_the_json_report_is_exactly_the_documented_shape(capsys):
    """The report dict plus the three facts the CLI knows and the engine does
    not. Pinned as a SET: a key that appears by accident is a key a script
    starts depending on."""
    _seed(("s1", 2))
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    assert set(_json_of(capsys)) == {
        "sessions", "sessions_quarantined", "events_read", "events_inserted",
        "events_skipped", "chain_ok", "duration_s", "dry_run", "error",
        "project", "destination", "checkpoint"}


def test_a_dry_run_counts_every_event_and_inserts_none(capsys):
    _seed(("s1", 3), ("s2", 2))
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    got = _json_of(capsys)
    assert got["sessions"] == 2
    assert got["events_read"] == 5
    assert got["events_inserted"] == 5          # *would* insert
    assert got["events_skipped"] == 0
    assert got["dry_run"] is True
    assert got["destination"] is False


def test_a_dry_run_names_the_namespace_it_read(capsys):
    _seed(("s1", 1))
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    assert _json_of(capsys)["project"] == "test"


def test_a_quarantined_session_is_replayed_and_counted(capsys):
    """A session whose id was refused lives in an `unsafe-<hash>` segment,
    and a dry run used to walk it reading nothing -- the session counted, its
    rows not, silently. Its rows are read now, and the report says how many
    sessions came in that way, so an operator can tell a quarantined id
    from a lost one."""
    _seed(("s1", 2))
    assert _run("db", "migrate", "--dry-run") == 0     # no refused id: no line
    assert "quarantined" not in capsys.readouterr().out
    events.append_event("decision", {"text": "q"}, session_id="../escape")
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    payload = _json_of(capsys)
    assert (payload["sessions"], payload["sessions_quarantined"],
            payload["events_read"]) == (2, 1, 3)
    assert _run("db", "migrate", "--dry-run") == 0
    assert "quarantined: 1 session(s)" in capsys.readouterr().out


def test_the_human_lines_say_the_mode_the_counts_and_the_verdict(capsys):
    _seed(("s1", 3))
    assert _run("db", "migrate", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run: 3 event(s) read across 1 session(s)" in out
    assert "would insert: 3" in out
    assert "chain: ok" in out
    assert "took" in out


def test_the_human_lines_say_that_nothing_was_written_and_why(capsys):
    """A command that wrote nothing and said nothing about it reads exactly
    like a command that wrote everything."""
    _seed(("s1", 1))
    assert _run("db", "migrate", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "no destination" in out
    assert db_cli.ADMIN_ENV in out


# --------------------------------------------------------------------------
# the checkpoint gate — absent is not broken
# --------------------------------------------------------------------------

def test_a_namespace_that_was_never_checkpointed_is_replayed_and_says_so(capsys):
    """`chain.verify` answers `ok: False, reason: "no chain.json"` for a store
    `hive project` has simply never run against. Refusing THAT would make this
    command unusable on the fresh store it is most wanted on, so absence is
    read off the file and reported as its own fact."""
    _seed(("s1", 2))
    assert not (paths.project_dir("test") / "chain.json").exists()
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    assert _json_of(capsys)["checkpoint"] == "absent"


def test_the_missing_checkpoint_note_names_the_command_that_writes_one(capsys):
    _seed(("s1", 1))
    assert _run("db", "migrate", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "no chain.json" in out and "hive project" in out


def test_a_checkpointed_namespace_reports_ok_and_drops_the_note(capsys):
    _seed(("s1", 2))
    chain.write_chain("test")
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    assert _json_of(capsys)["checkpoint"] == "ok"


def test_a_checkpoint_that_disagrees_with_the_disk_stops_the_run(capsys):
    """Exit 1 and not 2: the checkpoint EXISTS and contradicts the ledger,
    which is a claim about the data, not about the environment."""
    _seed(("s1", 2))
    (paths.project_dir("test") / "chain.json").write_text(
        json.dumps({"count": 2, "head": "0" * 64}), encoding="utf-8")
    assert _run("db", "migrate", "--dry-run", "--json") == 1
    err = capsys.readouterr().err
    assert "does not verify" in err and "hive verify" in err


def test_the_refusal_happens_before_a_single_record_is_read(monkeypatch,
                                                            capsys):
    """The gate is worth nothing if it fires after the replay. Pinned by
    making the replay itself explode: it must never be reached."""
    _seed(("s1", 2))
    (paths.project_dir("test") / "chain.json").write_text(
        json.dumps({"count": 2, "head": "0" * 64}), encoding="utf-8")
    import mindhive.storage.migration as mig

    def _boom(*a, **k):                               # pragma: no cover
        raise AssertionError("the replay ran despite a broken checkpoint")

    monkeypatch.setattr(mig.EventReplayMigrationEngine,
                        "migrate_to_postgres", _boom)
    assert _run("db", "migrate", "--dry-run") == 1
    capsys.readouterr()


# --------------------------------------------------------------------------
# a real run with nothing configured — exit 2, and it names the variable
# --------------------------------------------------------------------------

def test_a_real_run_without_an_owner_dsn_exits_two(capsys):
    assert _run("db", "migrate") == 2
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 2
    assert db_cli.ADMIN_ENV in err[1]
    assert "--dry-run" in err[1]


def test_the_first_line_is_the_underlying_refusal(capsys):
    """Two lines, not one: the first is what `_require_admin` said, the second
    is what to do about it. Collapsing them would throw away the detail that
    tells a reader WHICH variable was consulted."""
    assert _run("db", "migrate") == 2
    assert db_cli.ADMIN_ENV in capsys.readouterr().err.splitlines()[0]


def test_a_dry_run_needs_no_dsn_at_all(capsys):
    """The same missing configuration that stops a real run must not stop the
    forecast -- that is the one thing an unconfigured machine CAN do."""
    _seed(("s1", 1))
    assert _run("db", "migrate", "--dry-run", "--json") == 0
    assert _json_of(capsys)["destination"] is False


# --------------------------------------------------------------------------
# a real run against a destination (faked at the CLI's own seam)
# --------------------------------------------------------------------------

class _Dest:
    """Enough of `PostgresEventStore` for the engine, and nothing more."""

    def __init__(self, have=None, fail=None):
        self.have = dict(have or {})
        self.fail = fail
        self.rows: list = []
        self.commits = 0

    def segment_of(self, session_id):
        return events.segment_name(session_id)

    def max_seg_line(self, segment):
        if self.fail is not None:
            raise self.fail
        return self.have.get(segment, -1)

    def transaction(self):
        from contextlib import contextmanager

        @contextmanager
        def _tx():
            staged: list = []
            yield staged
            self.commits += 1
            self.rows.extend(staged)

        return _tx()

    def insert_record(self, cur, rec):
        cur.append((self.segment_of(rec.session_id), rec.seg_line - 1))
        return True


@pytest.fixture
def dest(monkeypatch):
    """Swap the CLI's destination factory and neutralise the schema step."""
    made = {}

    def _factory(*_a, **_k):
        return made["store"]

    monkeypatch.setattr(db_cli, "_destination", _factory)
    monkeypatch.setattr(pgmirror, "apply_migrations",
                        lambda dsn=None: {"applied": []})
    made["store"] = _Dest()
    return made


def test_a_real_run_inserts_every_record_and_exits_zero(dest, capsys):
    _seed(("s1", 3))
    assert _run("db", "migrate", "--json") == 0
    got = _json_of(capsys)
    assert got["events_inserted"] == 3
    assert got["dry_run"] is False
    assert got["destination"] is True
    assert [r[1] for r in dest["store"].rows] == [0, 1, 2]


def test_a_second_run_skips_by_arithmetic_and_writes_nothing(dest, capsys):
    """One `max(seg_line)` per session, then subtraction. A per-row probe
    would be a round trip per event to learn the same thing."""
    _seed(("s1", 3))
    dest["store"] = _Dest(have={"s1.jsonl": 2})
    assert _run("db", "migrate", "--json") == 0
    got = _json_of(capsys)
    assert got["events_read"] == 3
    assert got["events_skipped"] == 3
    assert got["events_inserted"] == 0
    assert dest["store"].rows == []


def test_the_schema_is_brought_up_to_date_before_any_row_is_offered(dest,
                                                                    monkeypatch):
    """`apply_migrations` is idempotent, so calling it every time costs one
    SELECT and removes the failure mode where a replay meets a table that 007
    had not yet added."""
    seen = []
    monkeypatch.setattr(pgmirror, "apply_migrations",
                        lambda dsn=None: seen.append(dsn) or {"applied": []})
    _seed(("s1", 1))
    assert _run("db", "migrate") == 0
    assert seen == [None]


def test_a_dry_run_never_touches_the_schema(dest, monkeypatch):
    called = []
    monkeypatch.setattr(pgmirror, "apply_migrations",
                        lambda dsn=None: called.append(dsn))
    _seed(("s1", 1))
    assert _run("db", "migrate", "--dry-run") == 0
    assert called == []


def test_a_schema_that_will_not_apply_stops_the_run_with_exit_two(dest,
                                                                  monkeypatch,
                                                                  capsys):
    def _refuse(dsn=None):
        raise RuntimeError("psycopg is not installed, so migrations cannot run.")

    monkeypatch.setattr(pgmirror, "apply_migrations", _refuse)
    _seed(("s1", 1))
    assert _run("db", "migrate") == 2
    assert "schema could not be brought up to date" in capsys.readouterr().err


def test_a_destination_that_dies_mid_replay_is_a_sentence_not_a_traceback(
        dest, capsys):
    """The first connection is opened INSIDE the replay (`max_seg_line`), so
    an unreachable server arrives here and not at construction. A command run
    once, by a human, on a machine that is not set up yet must not answer with
    a stack trace."""
    _seed(("s1", 2))
    dest["store"] = _Dest(fail=RuntimeError("connection refused"))
    assert _run("db", "migrate") == 2
    err = capsys.readouterr().err
    assert "the replay stopped" in err and "connection refused" in err


def test_the_mid_replay_message_says_a_re_run_is_safe(dest, capsys):
    """And says it ACCURATELY. The engine commits per chunk, so a replay that
    dies at session 40 has written the first 39; a message promising that
    nothing was written would be a comfortable lie. What is true is that the
    committed part is a prefix an idempotent re-run skips."""
    _seed(("s1", 2))
    dest["store"] = _Dest(fail=RuntimeError("connection refused"))
    assert _run("db", "migrate") == 2
    err = capsys.readouterr().err
    assert "re-running resumes" in err
    assert "PREFIX" in err
    assert "nothing is half-migrated" not in err


# --------------------------------------------------------------------------
# `db status`
# --------------------------------------------------------------------------

def test_status_reports_the_namespace_and_its_sessions(capsys):
    _seed(("s1", 1), ("s2", 1))
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["project"] == "test"
    assert got["sessions"] == 2


def test_the_status_json_is_exactly_the_documented_shape(capsys):
    assert _run("db", "status", "--json") == 0
    assert set(_json_of(capsys)) == {
        "project", "sessions", "checkpoint", "chain_ok", "dsn_env",
        "dsn_configured", "schema_version", "migrations",
        "mirrored_sessions", "error"}


def test_status_reports_the_schema_inventory_without_a_server(capsys):
    """`migration_files` reads the package directory, so the one question an
    unconfigured machine CAN answer is answered."""
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["migrations"] == pgmirror.migration_files()
    assert got["schema_version"] == pgmirror.SCHEMA_VERSION


def test_an_unconfigured_destination_is_reported_not_probed(capsys):
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["dsn_configured"] is False
    assert got["dsn_env"] == db_cli.ADMIN_ENV
    assert got["mirrored_sessions"] is None
    assert got["error"] is None


def test_chain_ok_is_none_when_the_namespace_was_never_checkpointed(capsys):
    """Tri-state on purpose. `true` here would tell a script the check had
    passed when it had never run."""
    _seed(("s1", 1))
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["checkpoint"] == "absent"
    assert got["chain_ok"] is None


def test_chain_ok_is_true_only_after_a_checkpoint_verifies(capsys):
    _seed(("s1", 1))
    chain.write_chain("test")
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["checkpoint"] == "ok"
    assert got["chain_ok"] is True


def test_a_broken_checkpoint_is_reported_without_failing_the_command(capsys):
    """Status REPORTS; it does not gate. Exit 0 with `chain_ok: false` is the
    answer to "what is the state", and `db migrate` is what refuses to run."""
    _seed(("s1", 2))
    (paths.project_dir("test") / "chain.json").write_text(
        json.dumps({"count": 2, "head": "0" * 64}), encoding="utf-8")
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["checkpoint"] == "broken"
    assert got["chain_ok"] is False


class _Listing:
    """A destination that can be listed and nothing else -- which is all
    `db status` asks of one."""

    def __init__(self, sessions):
        self._sessions = list(sessions)

    def list_sessions(self):
        return list(self._sessions)


def test_status_counts_the_destinations_sessions_when_one_is_configured(
        monkeypatch, capsys):
    monkeypatch.setenv(db_cli.ADMIN_ENV, "postgresql:///x")
    monkeypatch.setattr(db_cli, "_destination",
                        lambda *a, **k: _Listing(["s1.jsonl", "s2.jsonl"]))
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["dsn_configured"] is True
    assert got["mirrored_sessions"] == 2


def test_a_destination_that_will_not_answer_is_a_field_not_a_crash(monkeypatch,
                                                                   capsys):
    """`hive db status` is the command an operator runs BECAUSE something is
    wrong. Raising on the unreachable server would take away the rest of the
    report at the moment it is most wanted."""
    monkeypatch.setenv(db_cli.ADMIN_ENV, "postgresql:///x")

    def _refuse(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db_cli, "_destination", _refuse)
    assert _run("db", "status", "--json") == 0
    got = _json_of(capsys)
    assert got["error"] == "connection refused"
    assert got["mirrored_sessions"] is None


def test_the_status_human_lines_name_each_fact(capsys):
    _seed(("s1", 1))
    assert _run("db", "status") == 0
    out = capsys.readouterr().out
    assert "namespace: test" in out
    assert "sessions on disk: 1" in out
    assert "no checkpoint yet" in out and "hive project" in out
    assert "destination: not configured" in out


def test_the_broken_chain_line_names_the_command_that_investigates(capsys):
    _seed(("s1", 1))
    (paths.project_dir("test") / "chain.json").write_text(
        json.dumps({"count": 1, "head": "0" * 64}), encoding="utf-8")
    assert _run("db", "status") == 0
    assert "hash chain: BROKEN" in capsys.readouterr().out


# --------------------------------------------------------------------------
# repo gates — the storage package holds no write SQL and no eager pg import
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name", sorted(p.name for p in STORAGE.glob("*.py")))
def test_no_storage_module_carries_a_write_statement(name):
    """`tests/test_pg_repo_gates.py` D4 says every statement that can mutate
    `mindhive.*` lives in `pgmirror.py`. This asserts the same thing from the
    other side, file by file, so a new storage module that grows an INSERT is
    named HERE rather than in a gate that reports a set difference."""
    assert _sql_writes(STORAGE / name) == []


def test_the_cli_module_imports_nothing_heavy_at_module_scope():
    """`hive --help` builds this parser. A module-scope `from .. import
    pgmirror` would put the owner-DSN module -- and psycopg behind it -- on
    the import graph of every single `hive` invocation."""
    tree = ast.parse((STORAGE / "cli.py").read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = set()
    for node in top:
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        else:
            names.update(a.name for a in node.names)
    assert names == {"json", "cliparse"}


def test_building_the_real_parser_imports_neither_psycopg_nor_pgmirror():
    """The end-to-end version of the check above, in a child process so this
    suite's own imports cannot make it pass."""
    import subprocess
    import sys
    code = ("import sys;"
            "from mindhive import __main__ as m;"
            "m._build_parser();"
            "print('psycopg' in sys.modules, 'mindhive.pgmirror' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=str(SRC.parents[1]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False"
