# tests/test_pgstore.py
"""The half of the Postgres read path that needs no Postgres.

There is no server on this machine and `psycopg` is not installed, which sounds
like it leaves nothing to test. It does not. The properties below are exactly
the ones that a server would NOT prove — most of them are about what happens
when the database is absent, which is the state this suite runs in permanently:

* **`psycopg` stays optional.** `hive`, the dashboard, the hooks and the whole
  default test tier must keep working with neither extra installed. The import
  happens inside `_connect_raw`, so `import mindhive.pgstore` costs nothing.
* **The reader DSN is the only credential this module can reach.** A session
  physically cannot write if the process holding the AI path never learns the
  admin DSN's name.
* **The circuit breaker actually breaks the circuit.** `connect_timeout=2` is
  not enough on a path a session hook sits on: 50 appends against a dead host is
  100 seconds. The breaker is the thing that makes it 2. Untested, it is code
  that looks right — so the timing test here has a canary that removes the
  breaker and requires the budget to blow.
* **`read_all_strict` has no file fallback, and cannot grow one.** This is R2:
  a strict reader that quietly degrades to files makes the parity gate compare
  the file ledger with itself and pass, proving nothing while looking green.

What is NOT proven here, and cannot be until PostgreSQL 17 exists: that any of
the SQL is valid, that the ordering it asks for is the ordering it gets, or that
a connection ever succeeds. Those are the `pg`-marked tier, which does not exist
yet because it must land with its own CI job.
"""
import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mindhive import pgstore

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mindhive"
RO_ENV = "MINDHIVE_PG_DSN_RO"
ADMIN_ENV = "MINDHIVE_PG_DSN_ADMIN"


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Connection state is module-level (one pool, one breaker, one counter per
    process), so a test that trips the breaker would otherwise decide the result
    of every test after it."""
    monkeypatch.delenv(RO_ENV, raising=False)
    monkeypatch.delenv(ADMIN_ENV, raising=False)
    pgstore.reset_state()
    yield
    pgstore.reset_state()


# --------------------------------------------------------------------------
# DSN resolution — from the environment, never a literal
# --------------------------------------------------------------------------

def test_no_dsn_configured_is_none_not_a_default():
    """A default DSN would mean an unconfigured machine quietly tries to reach
    somebody's localhost:5432."""
    assert pgstore.dsn() is None


def test_the_reader_dsn_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    assert pgstore.dsn() == "service=mindhive_ro"


def test_a_blank_dsn_is_treated_as_unconfigured(monkeypatch):
    """An empty or whitespace variable is what an unset variable looks like
    after a shell script interpolates a missing value. Treating it as a real
    DSN turns a configuration mistake into a connect attempt with a useless
    error message."""
    for blank in ("", "   ", "\t\n"):
        monkeypatch.setenv(RO_ENV, blank)
        assert pgstore.dsn() is None, repr(blank)


def test_pgstore_refuses_to_resolve_the_admin_role():
    """The privilege split is the design: `pgmirror` holds the owner DSN and
    nothing on the AI path imports it. A `role="admin"` that quietly worked here
    would make that split a convention instead of a boundary."""
    with pytest.raises(ValueError) as e:
        pgstore.dsn("admin")
    assert "pgmirror" in str(e.value)


def test_the_admin_dsn_being_set_changes_nothing_here(monkeypatch):
    """The realistic leak: an operator exports both variables in one shell, and
    every process in that shell inherits the owner credential. This module must
    still see only the reader's."""
    monkeypatch.setenv(ADMIN_ENV, "postgresql://owner@localhost/mindhive")
    assert pgstore.dsn() is None
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    assert pgstore.dsn() == "service=mindhive_ro"


def test_the_admin_variable_is_not_even_named_in_this_module():
    """Asserted over the source, because the guard above only covers the paths
    somebody remembered to write. If the string is not in the file, no future
    code path can read it by accident."""
    src = (SRC / "pgstore.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines()
                     if not line.strip().startswith("#"))
    assert ADMIN_ENV not in code, \
        "pgstore names the admin DSN variable; only pgmirror may"


def test_no_credential_literal_in_the_postgres_modules():
    """§9.10's repo gate, scoped to the modules that exist so far."""
    import re
    for name in ("pgstore.py", "pgschema.py", "pgmirror.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        assert not re.search(r"postgres(?:ql)?://[^\s\"']*:[^\s\"']*@", text), name
        assert not re.search(r"password\s*=\s*[\"'][^\"']+[\"']", text), name


# --------------------------------------------------------------------------
# psycopg stays optional
# --------------------------------------------------------------------------

def test_importing_the_postgres_modules_does_not_import_a_driver():
    """The base install has no psycopg, and importing `mindhive.events` (which
    reaches `pgstore` through the mirror seam) must not pay for a driver import
    even when one IS installed. Run in a subprocess so the answer is about a
    fresh interpreter rather than about whatever this suite imported earlier."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    out = subprocess.run(
        [sys.executable, "-c",
         "import mindhive.pgschema, mindhive.pgstore, mindhive.pgmirror, sys; "
         "print('psycopg' in sys.modules or 'psycopg2' in sys.modules)"],
        capture_output=True, text=True, cwd=str(REPO), env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout


def test_the_dependency_check_answers_rather_than_raising():
    """`deps_available()` is called from `available()`, which is called from the
    hook path. It must be a question, not an exception."""
    assert pgstore.deps_available() in (True, False)


def test_the_missing_extra_names_the_install_command():
    """The `vectors._require_deps` precedent: the error a developer sees has to
    contain the fix. Skipped rather than inverted if psycopg is ever installed
    on this machine — the assertion is about the message, not the absence."""
    if pgstore.deps_available():
        pytest.skip("psycopg is installed here; nothing to assert")
    with pytest.raises(RuntimeError) as e:
        pgstore._require_pg()
    assert ".[pg]" in str(e.value)


def test_availability_is_false_and_silent_without_a_dsn(monkeypatch):
    """Unconfigured is the normal state of this machine and of CI, and it must
    be an ANSWER rather than an exception on a path a hook sits on."""
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    assert pgstore.available() is False
    assert pgstore.unavailable_reason() == "backend_unavailable"


def test_a_missing_driver_is_reported_ahead_of_a_missing_dsn(monkeypatch):
    """Both are true on a bare machine, and the order is a choice: the missing
    extra is the one with an install command attached, so it is the answer a
    developer can act on."""
    monkeypatch.setattr(pgstore, "deps_available", lambda: False)
    monkeypatch.delenv(RO_ENV, raising=False)
    assert pgstore.unavailable_reason() == "deps_missing"


def test_availability_is_false_when_the_driver_is_missing(monkeypatch):
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: False)
    assert pgstore.available() is False
    assert pgstore.unavailable_reason() == "deps_missing"


def test_the_reason_vocabulary_is_closed():
    """A model matching on prose is a model that loops, so the reason strings
    are a fixed set rather than free text."""
    assert pgstore.unavailable_reason() in pgstore.UNAVAILABLE_REASONS


# --------------------------------------------------------------------------
# the circuit breaker
# --------------------------------------------------------------------------

def test_one_strike_opens_the_breaker_for_the_life_of_the_process():
    """The hook-path policy. A session hook process is short-lived and fires
    once; a breaker that reset would let the next of 50 appends pay the connect
    timeout all over again."""
    b = pgstore.Breaker(policy="once")
    assert b.is_open() is False
    b.record_failure(RuntimeError("down"))
    assert b.is_open() is True
    assert b.state() == "open"
    b.record_success()                       # cannot be talked out of it
    assert b.is_open() is True


def test_the_timed_policy_half_opens_after_its_window():
    """The MCP-server policy, and the reason there are two: the server is
    long-lived, and a one-strike breaker there means a single transient blip
    disables all four tools until somebody notices and restarts it."""
    now = [1000.0]
    b = pgstore.Breaker(policy="timed", window=30.0, clock=lambda: now[0])
    b.record_failure(RuntimeError("blip"))
    assert b.is_open() is True
    now[0] += 29.0
    assert b.is_open() is True, "the window must not expire early"
    now[0] += 2.0
    assert b.is_open() is False
    assert b.state() == "half_open"
    b.record_success()
    assert b.state() == "closed"


def test_a_failed_probe_re_opens_the_timed_breaker():
    now = [0.0]
    b = pgstore.Breaker(policy="timed", window=30.0, clock=lambda: now[0])
    b.record_failure(RuntimeError("down"))
    now[0] += 31.0
    assert b.state() == "half_open"
    b.record_failure(RuntimeError("still down"))
    now[0] += 1.0
    assert b.is_open() is True, "a failed probe must not leave the breaker shut"


def test_an_unknown_breaker_policy_is_refused():
    """A typo'd policy that silently fell back to 'never open' is the failure
    this design is least able to notice."""
    with pytest.raises(ValueError):
        pgstore.Breaker(policy="sometimes")


def test_an_open_breaker_short_circuits_before_the_socket(monkeypatch):
    """The whole point: an open breaker must return without touching the
    network, not merely fail faster."""
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    touched = []
    monkeypatch.setattr(pgstore, "_connect_raw",
                        lambda d: touched.append(d) or (_ for _ in ()).throw(
                            RuntimeError("connect refused")))
    assert pgstore.read_all_or_none("proj") is None
    assert len(touched) == 1
    assert pgstore.available() is False, "the first failure must open the breaker"
    assert pgstore.read_all_or_none("proj") is None
    assert len(touched) == 1, "the breaker let a second call reach the socket"


# --------------------------------------------------------------------------
# the hot path: never raises, never blocks, never silent
# --------------------------------------------------------------------------
#
# These used to drive `mirror_one`, the single-row WRITE seam. That seam is gone
# — this module holds a role-level read-only credential, so it could never have
# written, and the mirror now follows the ledger through
# `pgmirror.refresh_changed`.
#
# The guarantees it was proving did NOT go with it. `read_all_or_none` is the
# surviving total seam; it sits under `events.read_all` and therefore under every
# briefing, recall, projection and dashboard fold, and it reaches the socket
# through the same `available()` -> `connect()` -> `_note_failure` path. Pointing
# these at it keeps the coverage instead of deleting it along with the seam,
# which is the failure mode a stale-test cleanup usually has.


def test_the_read_seam_never_raises_however_the_driver_fails(monkeypatch):
    """A fold must not crash because the mirror is sick. `None` means "read the
    files", which is how reads stay total — and never an empty list, which would
    be indistinguishable from a namespace with no history."""
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)

    for boom in (RuntimeError("dead"), OSError("refused"), ValueError("garbage")):
        pgstore.reset_state()
        monkeypatch.setattr(pgstore, "_connect_raw",
                            lambda d, e=boom: (_ for _ in ()).throw(e))
        assert pgstore.read_all_or_none("proj") is None


def test_the_read_seam_is_a_no_op_when_nothing_is_configured():
    """No DSN is the normal state of this machine and of CI. It must cost one
    environment lookup, not a connect attempt."""
    assert pgstore.read_all_or_none("proj") is None
    assert pgstore.degraded()["failures"] == 0, \
        "an unconfigured mirror is not a degraded one"


def test_failures_are_counted_so_the_degradation_is_never_silent(monkeypatch):
    """The defect the requirements name is a dead import degrading quietly.
    Quiet on the hot path is correct; invisible is not — `hive pg status` reads
    this counter."""
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    monkeypatch.setattr(pgstore, "_connect_raw",
                        lambda d: (_ for _ in ()).throw(RuntimeError("dead host")))
    pgstore.read_all_or_none("proj")
    d = pgstore.degraded()
    assert d["failures"] == 1
    assert d["breaker"] == "open"
    assert "dead host" in d["last_error"]


def test_a_dead_host_costs_one_timeout_not_fifty(monkeypatch):
    """THE TIMING GATE. 50 folds against a dead host is 50 x connect_timeout
    without a breaker — 100 seconds on the path `events.read_all` sits on under
    backend `pg`. The connector here sleeps 20 ms standing in for that
    timeout."""
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    calls = []

    def slow_dead(dsn_str):
        calls.append(dsn_str)
        time.sleep(0.02)
        raise OSError("connection refused")

    monkeypatch.setattr(pgstore, "_connect_raw", slow_dead)
    start = time.monotonic()
    for _ in range(50):
        pgstore.read_all_or_none("proj")
    elapsed = time.monotonic() - start
    assert len(calls) == 1, f"the breaker let {len(calls)} calls reach the socket"
    assert elapsed < 0.30, f"50 folds against a dead host took {elapsed:.2f}s"


def test_the_timing_gate_fails_without_the_breaker(monkeypatch):
    """CANARY for the test above. Remove the breaker and the same 50 folds must
    blow the budget — without this, that test passes against a seam that never
    connects at all, and the breaker is untested code that looks right."""
    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    monkeypatch.setattr(pgstore, "_BREAKER", pgstore.NullBreaker())
    calls = []

    def slow_dead(dsn_str):
        calls.append(dsn_str)
        time.sleep(0.02)
        raise OSError("connection refused")

    monkeypatch.setattr(pgstore, "_connect_raw", slow_dead)
    start = time.monotonic()
    for _ in range(50):
        pgstore.read_all_or_none("proj")
    elapsed = time.monotonic() - start
    assert len(calls) == 50, "the canary did not actually remove the breaker"
    assert elapsed > 0.30, (
        f"50 unbreakered folds took only {elapsed:.2f}s — the budget in the "
        f"test above is too loose to detect a missing breaker")


# --------------------------------------------------------------------------
# R2 — the strict reader has no fallback and must not grow one
# --------------------------------------------------------------------------

def test_every_connection_this_module_hands_out_is_read_only(monkeypatch):
    """L4, and one of the few enforcement layers testable with no server: the
    connection object is put in a read-only transaction before any caller sees
    it. The ROLE already carries `default_transaction_read_only = on`, so this
    is a second brace — but it is the one that survives somebody resetting the
    role's settings."""
    class FakeConn:
        read_only = None

    monkeypatch.setenv(RO_ENV, "service=mindhive_ro")
    monkeypatch.setattr(pgstore, "deps_available", lambda: True)
    monkeypatch.setattr(pgstore, "_connect_raw", lambda d: FakeConn())
    assert pgstore.connect().read_only is True


def test_the_strict_reader_raises_rather_than_degrading():
    with pytest.raises(RuntimeError):
        pgstore.read_all_strict("proj")


def test_the_total_reader_answers_none_so_the_caller_reads_files():
    """`events.read_all` treats None as 'read the files', which is how reads
    stay total. Anything else here — an empty list especially — would look like
    a namespace with no history."""
    assert pgstore.read_all_or_none("proj") is None


def _calls_in(func_name: str, src: str) -> set[str]:
    """Every function name called inside `func_name`'s body."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            out = set()
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    f = call.func
                    out.add(f.attr if isinstance(f, ast.Attribute)
                            else getattr(f, "id", ""))
            return out
    raise AssertionError(f"{func_name} is not defined in this module")


def test_the_strict_reader_never_calls_the_file_reader():
    """R2, asserted structurally. A file fallback here makes the parity gate
    compare the file ledger against itself and pass — a green gate that proves
    nothing, which is worse than a red one."""
    src = (SRC / "pgstore.py").read_text(encoding="utf-8")
    assert "read_all" not in _calls_in("read_all_strict", src)


def test_the_fallback_scan_would_notice_one():
    """CANARY for the scan: it is an absence claim over an AST walk, and a walk
    that found no calls at all would pass it forever."""
    planted = ("def read_all_strict(project=None):\n"
               "    try:\n"
               "        return _query(project)\n"
               "    except Exception:\n"
               "        return events.read_all(project)\n")
    assert "read_all" in _calls_in("read_all_strict", planted)


def test_health_is_answerable_with_the_backend_down():
    """Every tool response carries this stamp unconditionally — a field that
    appears only on trouble is a field whose absence is ambiguous, and ambiguity
    resolves toward the optimistic reading every time."""
    health = pgstore.mirror_health("proj")
    assert health["available"] is False
    assert health["reason"] in pgstore.UNAVAILABLE_REASONS
    assert health["parity_ok"] is False, "an unreachable mirror is not a proven one"


# --------------------------------------------------------------------------
# the read SQL, checked as text (there is no server to run it against)
# --------------------------------------------------------------------------

def test_the_read_query_orders_on_the_segment_not_the_session_id():
    """The divergence the design corrects: `'-'` (0x2D) sorts below `'.'`
    (0x2E), so ordering by the bare id puts `manual` before `manual-2` while
    ordering by the FILENAME puts `manual-2.jsonl` first. `manual` is the real
    default id, so this is reachable rather than theoretical."""
    sql = pgstore.READ_ALL_SQL
    assert "session_id," in sql, "session_id is still SELECTed — just not ordered on"
    order = sql.split("ORDER BY", 1)[1]
    assert "segment" in order and "seg_line" in order
    assert "session_id" not in order


def test_the_read_query_sorts_a_null_timestamp_where_python_does():
    """`events.read_all` sorts on `ev.ts or ""`, so a NULL `ts` sorts FIRST;
    Postgres sorts NULLs LAST in an ascending order by default. A NULL `ts` is
    explicitly legal — the ledger checks key PRESENCE only — so a bare
    `ORDER BY ts` would disagree with the file reader on a row shape the schema
    deliberately admits, and disagreeing about ORDER means disagreeing about the
    chain head."""
    order = pgstore.READ_ALL_SQL.split("ORDER BY", 1)[1]
    assert "COALESCE(ts, '')" in order, \
        "a bare ORDER BY ts puts NULL rows at the opposite end from Python"


def test_the_read_query_uses_the_window_the_reader_actually_has():
    """The reader role holds no privilege on `mindhive.events` and no USAGE on
    schema `mindhive` — its whole universe is the view. A parity read naming the
    base table (or ordering by `ts_key`, which the view does not expose) would
    fail at run time as a permission error, which is a long way from its cause.

    The namespace rides a transaction-scoped GUC, not a predicate: the view
    carries the project filter itself, so scoping is a database property rather
    than something a caller has to remember."""
    sql = pgstore.READ_ALL_SQL
    assert "mindhive_ro.events" in sql
    assert "mindhive.events" not in sql.replace("mindhive_ro.events", "")
    assert "ts_key" not in sql, "ts_key is not in the reader's view"
    assert "project" not in sql, "the view carries the namespace predicate"


def test_the_driver_probe_is_memoised_because_failed_imports_are_never_cached(
        monkeypatch):
    """Python caches SUCCESSFUL imports in `sys.modules` and never failed ones,
    so a bare `import psycopg` re-walked the whole of `sys.path` on EVERY call —
    measured at 393 us on this machine against a 347 us `append_event`, i.e. the
    guard cost more than the thing it guarded, on the path every hook fires.

    Two things made it invisible. `unavailable_reason` checks deps BEFORE the
    breaker, so the breaker — the mechanism whose whole job is to stop repeated
    cost — could never absorb it; and `_note_failure` is not called for a missing
    driver, so `hive pg status` reported zero failures while paying it.

    Counted as real import attempts, not as elapsed time: a timing assertion on
    a shared machine is a flake, and this is a claim about how many times the
    work happens."""
    import builtins
    pgstore.reset_state()
    real_import = builtins.__import__
    attempts = []

    def counting(name, *a, **kw):
        if name == "psycopg":
            attempts.append(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", counting)
    results = [pgstore.deps_available() for _ in range(5)]

    assert len(set(results)) == 1, "the probe disagreed with itself across calls"
    assert len(attempts) <= 1, (
        f"the driver was probed {len(attempts)} times for 5 calls — a failed "
        f"import is not cached by Python, so this is a full sys.path walk each "
        f"time")


def test_resetting_state_lets_the_driver_probe_run_again():
    """CANARY for the memo above. A `deps_available` hard-wired to a constant
    would satisfy it forever while answering for a machine other than this one.
    `reset_state` must clear the cache, or a test that arranges for the driver
    to be present can never observe it."""
    pgstore.reset_state()
    assert pgstore._DEPS is None, "reset_state left a stale driver verdict behind"
    pgstore.deps_available()
    assert pgstore._DEPS is not None, "the probe did not record its verdict"


def test_the_namespace_scope_is_a_statement_postgres_can_actually_parameterise():
    """This assertion previously read `"SET LOCAL mindhive.project" in
    PROJECT_SCOPE_SQL`, which PINNED a defect instead of catching one.

    `SET` is a utility statement: its grammar accepts no parameter, so
    `SET LOCAL mindhive.project = %s` is a syntax error (42601) the moment a real
    server sees it. The entire namespace scope would have failed on its first
    call — fail-closed, so nothing leaks, but nothing reads either — and no test
    could notice, because every test asserting on it asserted on the string.

    `set_config(name, value, is_local)` is a function call, so the value binds.
    The third argument `true` is is_local, which keeps it transaction-scoped
    exactly as `SET LOCAL` was.

    The last assertion is the one that matters most: the project name is
    model-supplied on all four tools, so interpolating it — the obvious way to
    make `SET LOCAL` work — would convert a fail-closed bug into real SQL
    injection."""
    sql = pgstore.PROJECT_SCOPE_SQL
    assert "set_config" in sql, "SET LOCAL cannot take a parameter; use set_config"
    assert "mindhive.project" in sql
    assert ", true)" in sql, "is_local dropped — the scope would outlive the txn"
    assert "%s" in sql, "the project name must stay a bound parameter"
    assert sql.count("%s") == 1


def test_the_read_query_never_casts_the_hashed_timestamp():
    """A straight column projection. Any cast, `to_char`, or `::text` on `ts`
    hands `compute_chain` a different string than the file reader would, and the
    parity gate is the only thing that would notice."""
    sql = pgstore.READ_ALL_SQL
    assert "ts::" not in sql and "to_char" not in sql


def test_rows_rebuild_events_the_file_reader_would_recognise():
    """The reconstruction is pure Python and therefore testable today: the DAL
    must hand `compute_chain` the same five values `events.read_all` would."""
    rows = [("2026-08-17T09:00:00Z", "s1", "decision", {"text": "a"}, ["t"]),
            (None, None, None, {"text": "b"}, [])]
    evs = pgstore._rows_to_events(rows)
    assert [e.ts for e in evs] == ["2026-08-17T09:00:00Z", None]
    assert evs[0].type == "decision" and evs[0].tags == ["t"]
    assert evs[1].payload == {"text": "b"} and evs[1].tags == []


def test_the_rebuilt_event_hashes_like_the_file_readers():
    """The property that actually matters, asserted end to end rather than
    field by field: identical canonical JSON means an identical chain head."""
    from mindhive import events
    row = ("2026-08-17T09:00:00Z", "s1", "decision", {"text": "a"}, ["t"])
    rebuilt = pgstore._rows_to_events([row])[0]
    original = events.Event(ts=row[0], session_id=row[1], type=row[2],
                            payload=row[3], tags=row[4])
    assert events.canonical_json(events.to_dict(rebuilt)) == \
        events.canonical_json(events.to_dict(original))


# --------------------------------------------------------------------------
# the forbidden import edges
# --------------------------------------------------------------------------

HOT_PATH = ("enforce.py", "gateguard.py", "sopgate.py",
            "rotation/hook.py", "mindnet/enforcer.py")


def _imports_pgstore(src: str) -> bool:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("pgstore", "mindhive.pgstore"):
                return True
            if any(a.name == "pgstore" for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.endswith("pgstore") for a in node.names):
                return True
    return False


@pytest.mark.parametrize("name", HOT_PATH)
def test_no_hook_path_module_acquires_a_socket_dependency(name):
    """`PreToolUse` fires on every tool call, `UserPromptSubmit` on every
    prompt, and the Stop hook fires in every Claude Code session on this
    machine. A socket on those paths is not a slow feature, it is an outage —
    the same rule `mindnet/enforcer.py` already lives under for `events`."""
    path = SRC / name
    assert path.is_file(), f"{name} moved; this guard now covers nothing"
    assert not _imports_pgstore(path.read_text(encoding="utf-8"))


def test_the_import_scan_detects_all_three_spellings():
    """CANARY. Three ways to write the import that matters, and a scanner that
    knew only one would pass every assertion above while covering a third of
    them."""
    assert _imports_pgstore("from . import pgstore")
    assert _imports_pgstore("from .pgstore import available")
    assert _imports_pgstore("import mindhive.pgstore")
    assert not _imports_pgstore("from . import paths, events")
