# src/mindhive/pgstore.py
"""Connection state and the read primitives for the Postgres read mirror.

**The file ledger stays canonical and stays the sole writer.** This module is a
one-way mirror beside it, and every design choice here follows from that: it
holds only the READER credential, it never lets a database failure reach a
caller that was doing something else, and the one function without a fallback
exists solely so a gate can prove the mirror is faithful.

Four properties, each of which has a test and a canary:

**`psycopg` is optional.** `import psycopg` happens inside `_connect_raw`, not
at module scope, following `vectors._require_deps` exactly. So `import
mindhive.pgstore` costs nothing on a machine with no driver, which is every
machine running the default test tier.

**The reader DSN is the only credential reachable from here.** The owner DSN's
variable name does not appear in this file at all — `pgmirror` holds it. A
session that never learns the name cannot use the credential however badly the
rest of this module fails.

**The breaker is not an optimisation.** `connect_timeout=2` alone means 50
appends against a dead host cost 100 seconds on a path every session hook sits
on. Two policies, deliberately different: ``"once"`` for short-lived hook and
CLI processes, where one strike for the life of the process is right; ``"timed"``
for the long-lived MCP server, where a permanent trip after one blip is a server
that never recovers.

**`read_all_strict` has no file fallback, and that absence is the point.** A
strict reader that degraded to files would make the parity gate compare the file
ledger with itself and pass — green, and proving nothing. `read_all_or_none` is
the total twin the ordinary read path uses.

## What is unverified

**No PostgreSQL server exists on this machine and psycopg is not installed.**
Everything below that would touch a socket is therefore written but unproven:
`_connect_raw`, `connect`, the bodies of `read_all_strict` / `read_all_or_none`
/ `mirror_health` / `mirror_one` past their guards, and `READ_ALL_SQL` itself.
They are written to FAIL CLOSED — raise for the strict reader, `None` for the
total one, `False` for the mirror write — so the unproven half cannot silently
answer with something wrong. What IS proven today is every path that runs with
the backend absent, which is the path this repo actually takes.
"""
import os
import threading
import time

from . import paths

# The reader DSN, and nothing else. `service=mindhive_ro` resolved through
# libpq's .pg_service.conf (password in PGPASSFILE) is the recommended value, so
# neither the repo nor the environment holds a secret; a full connection URI
# with an inline credential also works and is the documented fallback, but no
# example of one is written down here — a repo gate greps src/ and tests/ for
# exactly that shape, and a "just an example" match is how the gate learns to
# be ignored.
_DSN_ENV = "MINDHIVE_PG_DSN_RO"

# How long to wait for a socket before giving up. Two seconds is not chosen for
# comfort: it is the number the breaker multiplies by when it fails to open.
CONNECT_TIMEOUT_S = 2

# The closed vocabulary `available()` answers in. Prose here would be prose a
# model matches on, and a model matching on prose is a model that loops.
#
# NOT the same list as the tools' wire vocabulary, and the difference is
# deliberate: `breaker_open` is an INTERNAL state — from the model's side an
# open breaker is indistinguishable from an unreachable backend, and giving it a
# separate code the tool descriptions never mention would be a code the model
# has no rule for. Whatever surfaces these to an AI must map `breaker_open` onto
# `backend_unavailable` and report the breaker's state in its own field.
UNAVAILABLE_REASONS = ("deps_missing", "backend_unavailable", "breaker_open")


# --- the optional dependency -------------------------------------------------

def _require_pg():
    """Raise with the install command if the Postgres extra is missing.

    Same shape as `vectors._require_deps`, for the same reason: the error a
    developer sees has to contain the fix."""
    try:
        import psycopg  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "the Postgres mirror needs extras: pip install -e .[pg]") from e


_DEPS: bool | None = None


def deps_available() -> bool:
    """True when the driver can be imported. Never raises — this is called from
    `available()`, which is called from the session-hook path.

    MEMOISED, and not as a micro-optimisation. Python caches SUCCESSFUL imports
    in `sys.modules` and never failed ones, so a bare `import psycopg` re-walks
    the whole of `sys.path` on every call: measured at 393 us here against a
    347 us `append_event`, i.e. the probe cost more than the append it was
    guarding. `unavailable_reason` checks deps BEFORE the breaker, so the
    breaker — the thing that exists to stop repeated cost — could never absorb
    it, and `_note_failure` is not called for a missing driver either, so it was
    invisible to `hive pg status` as well. Missing-driver is the current state of
    this machine and of CI, so this was the live path, not an edge case.

    One probe is the whole answer: a driver cannot appear or vanish inside a
    running process. `reset_state` clears it so a test may still decide."""
    global _DEPS
    if _DEPS is None:
        try:
            import psycopg  # noqa: F401
            _DEPS = True
        except Exception:
            _DEPS = False
    return _DEPS


# --- the circuit breaker -----------------------------------------------------

class Breaker:
    """A per-process circuit breaker with two policies.

    ``"once"`` — the first failure disables the mirror for the life of the
    process. Correct for hook and CLI processes: they are short-lived and a
    reset would let the next of fifty appends pay the connect timeout again.

    ``"timed"`` — open for `window` seconds, then one half-open probe. Correct
    for the MCP server, which is long-lived: a one-strike breaker there means a
    single transient blip disables all four tools until an operator notices.

    `clock` is injectable so the window can be tested without sleeping through
    it — a 30-second test is a test somebody eventually marks skip."""

    POLICIES = ("once", "timed")

    def __init__(self, policy: str = "once", window: float = 30.0, clock=None):
        if policy not in self.POLICIES:
            # A typo'd policy that fell back to "never open" would be the one
            # failure this design is least able to notice.
            raise ValueError(f"unknown breaker policy {policy!r}; "
                             f"expected one of {self.POLICIES}")
        self.policy = policy
        self.window = window
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._opened_at = None
        self._probing = False

    def is_open(self) -> bool:
        """True when a call must return WITHOUT touching a socket."""
        with self._lock:
            if self._opened_at is None:
                return False
            if self.policy == "once":
                return True
            if self._clock() - self._opened_at < self.window:
                return True
            self._probing = True      # let exactly one call through
            return False

    def record_failure(self, exc=None) -> None:
        with self._lock:
            self._opened_at = self._clock()
            self._probing = False

    def record_success(self) -> None:
        with self._lock:
            if self.policy == "once" and self._opened_at is not None:
                return                # cannot be talked out of it
            self._opened_at = None
            self._probing = False

    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self.policy == "timed" and self._probing:
                return "half_open"
            if self.policy == "timed" and \
                    self._clock() - self._opened_at >= self.window:
                return "half_open"
            return "open"


class NullBreaker:
    """A breaker that never opens.

    Exists for ONE purpose: the canary in `tests/test_pgstore.py` that removes
    the breaker and requires the 50-appends timing budget to blow. Without a way
    to remove it, the breaker is untested code that looks right. Nothing in
    `src/` may use this."""

    policy = "none"

    def is_open(self) -> bool:
        return False

    def record_failure(self, exc=None) -> None:
        pass

    def record_success(self) -> None:
        pass

    def state(self) -> str:
        return "closed"


# --- module-level state ------------------------------------------------------
# One breaker and one counter per process. The default policy is the hook-path
# one, because that is the path a mistake costs the most on; the MCP server sets
# "timed" explicitly at startup.

_STATE_LOCK = threading.Lock()
_BREAKER = Breaker(policy="once")
_FAILURES = 0
_LAST_ERROR = None
_LAST_ERROR_AT = None


def set_breaker_policy(policy: str, window: float = 30.0) -> None:
    """Choose the breaker policy for this process. Called by the long-lived MCP
    server with ``"timed"``; everything else keeps the ``"once"`` default."""
    global _BREAKER
    _BREAKER = Breaker(policy=policy, window=window)


def reset_state() -> None:
    """Clear the breaker and the counters. For tests: module-level connection
    state means a test that trips the breaker would otherwise decide the result
    of every test after it."""
    global _BREAKER, _FAILURES, _LAST_ERROR, _LAST_ERROR_AT, _DEPS
    _DEPS = None                      # `deps_available` memoises; let a test decide
    # The policy survives a reset, EXCEPT when a test has swapped in a
    # NullBreaker — whose policy is not a policy `Breaker` accepts, and
    # rebuilding from it would turn the fixture's cleanup into an error.
    policy = getattr(_BREAKER, "policy", "once")
    if policy not in Breaker.POLICIES:
        policy = "once"
    with _STATE_LOCK:
        _BREAKER = Breaker(policy=policy)
        _FAILURES = 0
        _LAST_ERROR = None
        _LAST_ERROR_AT = None


def _note_failure(exc) -> None:
    """Count a failure and open the breaker.

    NEVER records a ledger event: `append_event` is the caller on the mirror
    path, so an event about a failed mirror would recurse. The counter is the
    surface instead — quiet on the hot path, visible on demand."""
    global _FAILURES, _LAST_ERROR, _LAST_ERROR_AT
    with _STATE_LOCK:
        _FAILURES += 1
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        _LAST_ERROR_AT = time.time()
    _BREAKER.record_failure(exc)


def degraded() -> dict:
    """In-process failure counters, for `hive pg status` and the dashboard.

    The defect the requirements name is a dead import degrading QUIETLY. Quiet
    on the hot path is correct; invisible is not, and this is the difference."""
    with _STATE_LOCK:
        return {"failures": _FAILURES, "breaker": _BREAKER.state(),
                "policy": _BREAKER.policy, "last_error": _LAST_ERROR,
                "last_error_at": _LAST_ERROR_AT}


# --- configuration -----------------------------------------------------------

def dsn(role: str = "reader") -> str | None:
    """The reader DSN from the environment, or None when unconfigured.

    Two DSNs rather than one plus a role argument, so "which credential" is a
    DEPLOYMENT fact rather than a runtime argument somebody can get wrong — and
    this module can only ever resolve the reader's. A blank value counts as
    unconfigured: an empty variable is what an unset one looks like after a
    shell script interpolates a missing value, and treating it as a real DSN
    turns a configuration mistake into an unhelpful connect error."""
    if role != "reader":
        raise ValueError(
            f"pgstore resolves the reader DSN only (got role={role!r}); "
            f"the admin credential belongs to pgmirror and nothing on the AI "
            f"read path may reach it")
    value = (os.environ.get(_DSN_ENV) or "").strip()
    return value or None


def unavailable_reason() -> str | None:
    """Why the mirror cannot answer right now, or None when it can.

    Order matters: a missing driver is reported ahead of a missing DSN because
    it is the one with an install command attached."""
    if not deps_available():
        return "deps_missing"
    if dsn() is None:
        return "backend_unavailable"
    if _BREAKER.is_open():
        return "breaker_open"
    return None


def available() -> bool:
    """True when a query could be attempted. NEVER raises.

    This is a configuration-and-breaker check, not a handshake: it opens no
    socket, which is what makes it callable from the hook path. A connection can
    still fail after this returns True."""
    try:
        return unavailable_reason() is None
    except Exception:
        return False


# --- connections -------------------------------------------------------------
# VERIFIED against PostgreSQL 17.11 (`pytest -m pg`, 24 tests). One connection
# per call, opened and closed inside `_session` — no pool. That is a deliberate
# non-decision rather than an oversight: the transport is one stdio pipe serving
# one agent, so there is never a second concurrent request for a pool to serve,
# and a pool would add idle sessions holding a `security_barrier` view open.

def _connect_raw(dsn_str: str):
    """Open one reader connection. The injectable seam every test substitutes.

    The driver import lives HERE rather than at module scope, which is what
    keeps `psycopg` optional: importing this module must not pay for it.

    `connect_timeout` is what bounds a dead host: without it a blocked TCP
    connect would stall the caller for the OS default, which on the hook path is
    an outage rather than a slow feature."""
    _require_pg()
    import psycopg
    return psycopg.connect(dsn_str, connect_timeout=CONNECT_TIMEOUT_S)


def connect(role: str = "reader"):
    """A reader connection, or an exception. Trips the breaker on failure.

    Raises rather than returning None on purpose: the caller that must not fail
    (`read_all_or_none`) catches it itself, and the one that must not degrade
    (`read_all_strict`) needs it to propagate."""
    target = dsn(role)
    if target is None:
        raise RuntimeError(f"{_DSN_ENV} is not set")
    if _BREAKER.is_open():
        raise RuntimeError("postgres mirror circuit breaker is open")
    try:
        conn = _connect_raw(target)
    except Exception as e:
        _note_failure(e)
        raise
    # L4 of the read-only enforcement: a read-only TRANSACTION, set on every
    # connection this module hands out rather than per query. The role already
    # carries `default_transaction_read_only = on`, so this is a second brace
    # and not the defence — but it is the one that still holds if the role's
    # settings are ever reset, and it costs one attribute.
    conn.read_only = True
    _BREAKER.record_success()
    return conn


# --- the read path -----------------------------------------------------------

# The namespace is set on the TRANSACTION, not written into the query: the
# reader's view carries `WHERE project = current_setting('mindhive.project')`
# itself, which is what makes namespace scoping a database property rather than
# a predicate somebody has to remember. Unset -> NULL -> zero rows, fail-closed.
#
# `set_config(..., true)` and NOT `SET LOCAL mindhive.project = %s`, which is
# what this line said first and which cannot work: `SET` is a utility statement,
# the grammar takes no parameter there, and the extended protocol answers 42601
# for it. The whole namespace scope would have failed on its first real call —
# fail-closed, so nothing would have been disclosed, but nothing would have been
# readable either, and the only test on it asserted the STRING contained
# "SET LOCAL mindhive.project", which pinned the defect rather than catching it.
# The `true` third argument is is_local, so this is still transaction-scoped and
# still ends at COMMIT/ROLLBACK exactly as `SET LOCAL` would.
#
# The value stays a BOUND PARAMETER. Interpolating it would be the one change
# that turns this from a fail-closed bug into a real injection: `project` is a
# model-supplied string on every one of the four tools.
PROJECT_SCOPE_SQL = "SELECT set_config('mindhive.project', %s, true)"

# A straight column projection with NO cast on `ts`: chain.compute_chain hashes
# the canonical JSON of the timestamp STRING, so `ts::text`, `to_char(...)` or a
# TIMESTAMPTZ round trip would hand it a different value than the file reader
# would and silently end parity.
#
# FOUR things about the ORDER BY, and the design document gets one of them
# wrong:
#
# 1. `segment`, never `session_id`. '-' (0x2D) sorts below '.' (0x2E), so
#    ordering by the bare id puts `manual` before `manual-2` while ordering by
#    the filename puts `manual-2.jsonl` first. `manual` is the real default id,
#    so the divergence is reachable rather than theoretical.
# 2. `COALESCE(ts, '')`, not `ts_key` — even though `ts_key` is the column that
#    exists precisely to hold that value. The design's parity read orders by
#    `ts_key`, but `ts_key` is NOT in `mindhive_ro.events`' select list, and the
#    reader role has no privilege on the base table where it lives. The
#    expression reproduces the same ordering through the window the reader
#    actually has.
# 3. `COALESCE`, not bare `ts` — and this is the part that would have been a
#    silent parity failure. `events.read_all` sorts on `ev.ts or ""`, so a row
#    with a NULL `ts` sorts FIRST in Python; Postgres sorts NULLs LAST in an
#    ascending order by default. A NULL `ts` is explicitly legal (the ledger
#    checks key presence only), so the two would disagree on a row shape the
#    schema deliberately admits.
# 4. `COALESCE(append_seq, -1)` BEFORE `segment`. Within one second the filename
#    is not append order: a `sop` update from session `aaa-…` sorted before the
#    add session `zzz-…` had written first, so every fold replaying history lost
#    the edit — on BOTH sides of the gate, which is why parity never noticed.
#    `append_seq` is the ORDERING stamp `pgmirror.scan_segments` loads -- the
#    row's own `seq` raised to the highest one above it in its segment
#    (`events._segment_events`), NULL when there is none -- and -1 is exactly
#    what `events._order_key` gives a None one. Bare `append_seq` would sort
#    NULLs LAST and put every row loaded as NULL after the stamped rows of its
#    second: point 3's trap, one column over.
#
# VERIFIED against PostgreSQL 17.11, BEFORE point 4 existed: this statement is
# what `pgmirror.parity` reads, and it folded 37,435 real events to the same
# chain head as the file ledger. Point 4 landed with no server reachable, so it
# is proven only against text and fakes until `tests/test_pg_live.py`'s
# same-second fixture runs. Point 1 is covered by a constructed fixture rather than by that run —
# the real ledger has 868 timestamps shared across segments and NOT ONE of them
# inverts under session_id ordering, so parity on real data passes with the sort
# key mutated. `tests/test_pg_live.py` builds the `verify.jsonl` /
# `verify-e2e-001.jsonl` pair where the two orders really do disagree, and
# mutates THIS statement to prove the gate bites.
#
# The mutation has to be here, not in the loader: row order is not a stored
# property of the mirror. COPY may insert in any order at all — this ORDER BY is
# what defines the sequence the chain is folded over, so it is the only place an
# ordering bug can live.
READ_ALL_SQL = """
SELECT ts, session_id, event_type, payload, tags
FROM   mindhive_ro.events
ORDER BY COALESCE(ts, ''), COALESCE(append_seq, -1), segment, seg_line
"""


def _rows_to_events(rows) -> list:
    """Rebuild `events.Event` objects from projected rows.

    The whole hash-parity requirement reduces to this function: `compute_chain`
    must receive the same five Python values the file reader would have built.
    `events` is imported inside the body to keep the module-level import graph
    acyclic — `events` reaches this module through the mirror seam."""
    from . import events as _events
    return [_events.Event(ts=r[0], session_id=r[1], type=r[2],
                          payload=r[3], tags=list(r[4] or []))
            for r in rows]


def read_all_strict(project: str | None = None) -> list:
    """Every mirrored event for one namespace, in ledger order — or an exception.

    THE ONLY FUNCTION HERE WITHOUT A FALLBACK, and that is deliberate. It exists
    for the parity gate, and a gate whose reader can fall back to files compares
    the file ledger with itself and passes while proving nothing (R2). If this
    function ever grows a `try: ... except: return events.read_all(...)`, the
    gate becomes decorative — there is a test asserting it calls no such thing.

    Verified on the real ledger: 37,618 events read back through this function
    fold to the same chain head as the files."""
    name = project or paths.detect_project()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(PROJECT_SCOPE_SQL, (name,))
            cur.execute(READ_ALL_SQL, ())
            return _rows_to_events(cur.fetchall())
    finally:
        conn.close()


def read_all_or_none(project: str | None = None) -> list | None:
    """The total twin: every mirrored event, or None on ANY failure.

    None means "read the files" to `events.read_all`, which is how reads stay
    total. Never an empty list on failure — that would look like a namespace
    with no history."""
    if not available():
        return None
    try:
        return read_all_strict(project)
    except Exception:
        return None


HEALTH_SQL = """
SELECT schema_version, row_count, file_head, pg_head, parity_ok,
       ledger_fingerprint, rebuilt_at
FROM   mindhive_ro.mirror_state
"""
# No WHERE and no parameter: the view carries the namespace predicate, so a row
# comes back only for the project `PROJECT_SCOPE_SQL` just set. Reaching it needs
# migration 004 -- 003 revoked schema `mindhive` from the reader, which made the
# verdict table it is told to refuse on unreadable by the role that refuses.


def mirror_health(project: str | None = None) -> dict:
    """The freshness stamp every tool response carries, UNCONDITIONALLY.

    A field that appears only on trouble is a field whose absence is ambiguous —
    the model cannot tell "fresh" from "not implemented" from "the check failed"
    — and ambiguity resolves toward the optimistic reading every time.

    `parity_ok` is the verdict the last parity run recorded, forced False
    whenever the mirror cannot be reached, has never been built, or was stamped
    by a different schema version: an unproven mirror is not a proven one, and
    this is the one field a caller is told to refuse on. Ledger growth since the
    build is not one of those cases: it sets `stale` and leaves `parity_ok` as
    recorded (the two are never ANDed; see the comment above the return).

    STALENESS IS CHECKED WITHOUT RE-RUNNING PARITY. The recorded row carries the
    `ledger_fingerprint` (segment count and total bytes) the rebuild saw; this
    compares it against the ledger's fingerprint now. That is a `stat` per
    segment rather than a 37k-event double fold — the difference between a
    freshness stamp a tool can afford on every call and one it cannot. It
    detects growth, which is how this ledger actually goes stale (it is
    append-only); it would not detect an in-place edit that preserved the byte
    count, and nothing short of a real parity run would."""
    reason = unavailable_reason()
    if reason is not None:
        return {"available": False, "reason": reason, "parity_ok": False,
                "stale": True, "stale_reason": "mirror unreachable",
                "breaker": _BREAKER.state()}

    name = project or paths.detect_project()
    try:
        conn = connect()
    except Exception:
        return {"available": False, "reason": "backend_unavailable",
                "parity_ok": False, "stale": True,
                "stale_reason": "could not open a reader connection",
                "breaker": _BREAKER.state()}
    try:
        with conn.cursor() as cur:
            cur.execute(PROJECT_SCOPE_SQL, (name,))
            cur.execute(HEALTH_SQL)
            row = cur.fetchone()
    except Exception as e:
        _note_failure(e)
        return {"available": False, "reason": "backend_unavailable",
                "parity_ok": False, "stale": True,
                "stale_reason": f"mirror_state unreadable ({type(e).__name__})",
                "breaker": _BREAKER.state()}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if row is None:
        return {"available": True, "reason": None, "parity_ok": False,
                "stale": True, "rows": 0,
                "stale_reason": "the mirror has never been built for this "
                                "namespace; run `hive pg rebuild`",
                "breaker": _BREAKER.state()}

    version, rows, file_head, pg_head, parity_ok, fingerprint, rebuilt_at = row

    stale_reason = refusal_reason = repair = None
    if version != _schema_version():
        # Not served, however the stored verdict reads: see `_version_mismatch`.
        stale_reason, refusal_reason, repair = _version_mismatch(version)
        parity_ok = False
    else:
        now = _ledger_fingerprint(name)
        if fingerprint and now != dict(fingerprint):
            was = dict(fingerprint)
            stale_reason = (
                f"the ledger has changed since the rebuild "
                f"({_seg_count(was)} segments / {was.get('bytes')} bytes then, "
                f"{_seg_count(now)} / {now.get('bytes')} now)")

    # `parity_ok` and `stale` are DIFFERENT questions and are never ANDed.
    # parity_ok answers "does the mirror agree with the ledger about the rows it
    # holds" — the verdict of the last real chain comparison, which since S6
    # passes for a correct PREFIX as well as an exact match. `stale` answers
    # "has the ledger grown since". A live append-only ledger is stale within
    # seconds of any rebuild, so folding staleness into parity_ok would make
    # `pgtools._parity_refusal` refuse every call forever — which is exactly
    # what it did before this was separated.
    stale = stale_reason is not None
    return {"available": True, "reason": None,
            "parity_ok": bool(parity_ok),
            "stale": stale, "stale_reason": stale_reason,
            "refusal_reason": refusal_reason, "repair": repair,
            "rows": rows, "schema_version": version,
            "file_head": file_head, "pg_head": pg_head,
            "rebuilt_at": rebuilt_at.isoformat() if rebuilt_at else None,
            "breaker": _BREAKER.state()}


# The owner credential is described, never named: this module may not contain
# that variable's name at all (tests/test_pgstore.py asserts it over the
# source), and `pgtools`' remedy line names it for the operator.
_NOT_YOURS = ("You cannot run {it} yourself — {need} the owner credential, "
              "and the reader role this tool connects with is read-only at the "
              "role level.")


def _version_mismatch(version) -> tuple:
    """`(stale_reason, refusal_reason, repair)` for a mirror whose recorded
    `schema_version` is not the one this reader expects.

    The recorded `parity_ok` is not served in this case, however it reads. It
    was proven by whatever wrote the row, under that writer's ordering contract:
    version 1 broke same-second ties on the segment filename, so its True says
    nothing about the order this reader returns rows in. No chain comparison
    failed, though, so `refusal_reason` is what `pgtools._parity_refusal` shows
    instead of its chain-head sentence, and `repair` lists the operator's
    commands in the order they must run.

    Two shapes, with different repairs:

    * `pgschema.UNLOADED_SCHEMA_VERSION` -- `pgmirror.parity` wrote the row with
      no load behind it, so nothing records which loader filled the rows. No
      pending migration explains that (parity cannot read a mirror 007 has not
      reached), and a refresh, which reloads every segment and records itself,
      is the whole repair.
    * any other version -- a loader that is not this reader's. After a pull the
      likeliest cause is that `hive pg migrate` has not run, and a refresh alone
      then refuses (`pgmirror.refresh` checks for pending migrations). So
      migrate, THEN refresh."""
    from . import pgschema
    expected = pgschema.SCHEMA_VERSION
    if version == pgschema.UNLOADED_SCHEMA_VERSION:
        return (
            "no load has been recorded for this mirror: its state row was "
            "written by a parity check alone, which proves the rows agree with "
            "the ledger but not which loader wrote them or from which bytes; "
            "the OPERATOR runs `hive pg refresh`, which reloads every segment "
            "and records the load",
            "the mirror's recorded state has no load behind it (a parity check "
            "wrote it alone), so this reader cannot tell which loader filled "
            "the rows it would answer from. To repair: the OPERATOR runs "
            "`hive pg refresh`, which reloads every segment and records the "
            "load. " + _NOT_YOURS.format(it="it", need="it needs"),
            ["hive pg refresh"])
    return (
        f"the mirror was loaded under schema version {version}, and this "
        f"reader expects {expected}: migrations may be pending, so the OPERATOR "
        f"runs `hive pg migrate`, then `hive pg refresh`, which reloads every "
        f"segment",
        f"the mirror was loaded under schema version {version} and this reader "
        f"expects {expected}, so the parity verdict it recorded was proven "
        f"under a different ordering contract and cannot vouch for the order "
        f"this tool would return rows in. To repair: the OPERATOR runs "
        f"`hive pg migrate` (migrations may be pending), then "
        f"`hive pg refresh`, which reloads every segment. "
        + _NOT_YOURS.format(it="either", need="both need"),
        ["hive pg migrate", "hive pg refresh"])


def _schema_version() -> int:
    """The row-shape version this reader understands.

    Read from `pgschema`, NOT from `pgmirror`. The allowed edge is
    pgmirror -> pgstore and never the reverse: a lazy `from . import pgmirror`
    here still puts the owner DSN one attribute lookup from the AI read path,
    and `tests/test_pgmirror.py` fails on it — which is how this was caught,
    after the first version of this function did exactly that."""
    from . import pgschema
    return pgschema.SCHEMA_VERSION


def _seg_count(fp) -> int:
    """How many segments a fingerprint describes, in either shape.

    The stored value is a per-segment map now and was a bare count before. This
    reads both because the staleness MESSAGE has to stay legible while a mirror
    built by the old shape is still on disk — the comparison itself already
    (correctly) calls those two unequal, so such a mirror reports stale until the
    next refresh re-stamps it, which is the direction to fail in."""
    segs = (fp or {}).get("segments")
    if isinstance(segs, dict):
        return len(segs)
    return segs if isinstance(segs, int) else 0


def _ledger_fingerprint(project: str) -> dict:
    """The same value `pgmirror._fingerprint` records, computed here.

    Deliberately duplicated rather than imported: `pgmirror` is the ADMIN half
    and reads the owner DSN, and nothing on the AI read path may import it —
    that structural split is what makes the privilege separation real rather
    than advisory. `tests/test_pgmirror.py` asserts the importer allowlist, and
    the two definitions are pinned to each other by a test rather than by an
    import.

    A per-segment map, `{filename: bytes}`, plus the total. This side only ever
    COMPARES it (that is the staleness check); the admin side additionally uses
    the map to decide which segments an incremental refresh must reload. Both
    definitions changed shape together, and the pinning test is what keeps the
    comparison from silently becoming "every mirror is stale"."""
    d = paths.events_dir(project)
    if not d.exists():
        return {"segments": {}, "bytes": 0}
    sizes = {p.name: p.stat().st_size for p in sorted(d.glob("*.jsonl"))}
    return {"segments": sizes, "bytes": sum(sizes.values())}


# --- there is no write seam here, and that is the design ----------------------
#
# `mirror_one` and `_insert_one` used to live at the bottom of this file. They
# were deleted rather than implemented, and the reasons are worth keeping where
# the next person looks for them:
#
# 1. **This module cannot write, by construction.** It holds the READER
#    credential, and `mindhive_reader` carries `default_transaction_read_only`
#    set on the ROLE (003_roles_and_views.sql). An INSERT from here is refused by
#    the server, not merely discouraged by the architecture. Handing this module
#    a writer DSN would put a write credential in the one module built
#    specifically not to have one, and `tests/test_pg_repo_gates.py` enforces
#    that split structurally.
# 2. **A second write path would drift from the loader.** `seg_line` is
#    `pgmirror.scan_segments`' `enumerate` index over every PHYSICAL line, blank
#    and torn rows included. A single-row writer computing its own line number
#    would agree today and diverge the first time a segment gained a blank line.
# 3. **Per-segment reload is cheap enough that row-level never pays.** A normal
#    session appends to ONE segment, so `pgmirror.refresh_changed` rewrites one
#    segment's rows, not 37k.
#
# The mirror follows the ledger through `pgmirror.refresh_changed`, called by the
# scheduled `autosync` run and by `hive pg refresh`. Freshness is therefore
# bounded by that cadence rather than instant, which `mirror_health` reports
# honestly in `stale` / `stale_reason` — and, since this change, with the repair
# command and the fact that the reader role cannot run it.
