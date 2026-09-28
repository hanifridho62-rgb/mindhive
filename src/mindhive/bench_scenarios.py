# SPDX-License-Identifier: Apache-2.0
# src/mindhive/bench_scenarios.py
"""Two benchmarks for two published numbers: `hive bench cli-startup` and
`hive bench recall-semantic`.

Both numbers were written down before anything measured them. The MASTER
ENGINEERING BLUEPRINT says the CLI starts in "42 ms" (section 1.5; decision K15
sets the bar at 45 ms), and spec section 4.7 says the ChromaDB projection
answers a query in 74 ms where a full ledger scan takes 1,090 ms -- "14.7x".
Neither had a harness behind it. K15's answer is *measure, don't claim*: this
module is the harness, and the figures ride along in every report as what they
are -- claims, printed beside the measurement for comparison -- and never as a
gate. An antivirus scanner, a cold disk and a ledger of twelve files are
conditions the reader compares against, not failures the command exits on.

The rules are `bench.py`'s, inherited whole:

**It measures and never fixes.** `cli-startup` spawns children of THIS
interpreter and reads their exit codes; `recall-semantic` calls the functions
behind `hive recall` and `hive recall --semantic` in-process. Nothing here
embeds, rebuilds, writes an event or creates a collection. `hive recall
--semantic` creates the chroma directory and its collections on demand, so on
a namespace that was never embedded this command says "unavailable" and leaves
no `chroma/` behind, rather than measuring an index it just made. And because
chromadb re-saves its own files whenever a client opens them, the semantic
queries run against a temporary copy of the index, removed afterwards.

**A failure is never timed as a success.** A child that crashed on import
exits in a millisecond, and a search that raised returns in microseconds; both
would be the fastest sample of the run. Every sample carries a state, and the
statistics are over the ones that answered. A failed child or a raised search
is counted, named with its error, and exits 1.

**Cold and warm are separate figures on the recall paths.** The first sample of
each path is its cold one; the semantic path also pays the model load and the
index open, timed apart as `open_ms` because it is what the first `hive recall
--semantic` of a process waits for and it is not a query. The median, min and
max are over the warm samples. `cli-startup` keeps every sample in one set on
purpose: each child is a fresh process, and the only warmth is the OS file
cache -- a machine condition that belongs in `max_ms`, not a sample to discard.

The seams -- `spawn` and `clock` for `cli-startup`, `lexical` and
`semantic_open` for `recall-semantic` -- are how the arithmetic and the
classification are tested without a child process or the `vectors` extra. Their
limit is the one `bench.py` names: this module's whole output *is* the time
dimension, so the seams prove classification and arithmetic, never latency.
`tests/test_cli_startup.py` runs the real children once.

Stdlib only, and cheap to import: `commands.parsers._build_parser` registers
`bench` on every `hive` invocation, so anything imported at the top of this
module is startup cost the module itself would then measure.
"""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager

from . import cliparse, paths

__all__ = [
    "CLI_STARTUP_CLAIM_MS", "CLI_STARTUP_CLAIM_SOURCE", "RECALL_CLAIM",
    "DEFAULT_STARTUP_REPS", "DEFAULT_CHILD_TIMEOUT_S", "STARTUP_SCENARIOS",
    "DEFAULT_QUERIES", "DEFAULT_RECALL_REPS", "DEFAULT_RECALL_LIMIT",
    "OK", "NONZERO", "TIMEOUT", "SPAWN_ERROR", "FAILED", "UNAVAILABLE",
    "child_env", "startup_argv", "time_child", "time_scenario",
    "run_cli_startup", "render_cli_startup", "cmd_bench_cli_startup",
    "default_lexical", "default_semantic_open", "time_searches",
    "run_recall_semantic", "render_recall_semantic",
    "cmd_bench_recall_semantic", "register",
]

# --- what the documents claim -------------------------------------------------

#: MEB section 1.5 says "42 ms"; decision K15 measures against < 45 ms. Printed
#: beside every measured median; the exit code never compares against it.
CLI_STARTUP_CLAIM_MS = 45.0
CLI_STARTUP_CLAIM_SOURCE = ('MEB section 1.5 "42 ms" (K15 measures against < 45 ms) '
                            "-- printed for comparison, not a gate")

#: Spec section 4.7, "Pencarian Vektor vs Pemindaian Penuh": the two latencies
#: and their ratio, with the ledger they were taken on. The same table's token
#: and RAM rows are not measured by this command and are not repeated here.
RECALL_CLAIM = {
    "source": 'spec section 4.7 "Pencarian Vektor vs Pemindaian Penuh"',
    "ledger_events": 22835,
    "ledger_mb": 12.54,
    "full_scan_ms": 1090.0,
    "vector_ms": 74.0,
    "ratio": 14.7,
}

# --- cli-startup --------------------------------------------------------------

DEFAULT_STARTUP_REPS = 10

#: Per child. Generous because a cold antivirus scan of a 121-module import is a
#: real condition on this machine; a child past it is reported as a timeout,
#: which is a failure, never as a slow success.
DEFAULT_CHILD_TIMEOUT_S = 60.0

#: The two children K15 names, as `(name, argv after the interpreter)`.
STARTUP_SCENARIOS = (
    ("import", ("-c", "import mindhive.__main__")),
    ("help", ("-m", "mindhive", "--help")),
)

STARTUP_METHOD = ("wall-clock of one child process from spawn to exit, interpreter "
                  "startup included; every sample kept, statistics over the "
                  "children that exited 0")

# --- recall-semantic ----------------------------------------------------------

#: One query per memo kind every ledger carries. Single tokens on purpose: the
#: lexical scorer matches whole words against an entry's `type:` frontmatter,
#: so each hits on any store holding one entry of that kind, and `--help`
#: cannot wrap them across lines.
DEFAULT_QUERIES = ("decision", "caveat", "next_step")
DEFAULT_RECALL_REPS = 5
DEFAULT_RECALL_LIMIT = 10

#: The file-level collection `vectors._collection` opens. Spelled here because
#: that module keeps it as a literal inside the function; `EVENT_COLLECTION`
#: is imported from it.
FILE_COLLECTION = "ledger"

RECALL_METHOD = ("in-process calls of the functions behind `hive recall` (the "
                 "lexical scan of LEDGER/*.md) and `hive recall --semantic` (the "
                 "chroma projection) on the same queries; query time only -- the "
                 "semantic open (deps import, index copy, model load, index open) "
                 "is timed apart")

# --- sample and row states ----------------------------------------------------

OK = "ok"
NONZERO = "nonzero_exit"       # the child exited, and not with 0
TIMEOUT = "timeout"            # the child was killed at the deadline
SPAWN_ERROR = "spawn_error"    # the child never started
FAILED = "failed"              # a search row with at least one raised sample
UNAVAILABLE = "unavailable"    # the semantic path could not be opened

# Worst-wins when a row's samples disagree, as in `bench._STATE_RANK`.
_CHILD_RANK = {OK: 0, NONZERO: 1, TIMEOUT: 2, SPAWN_ERROR: 3}

MAX_ERROR_CHARS = 300

#: Every millisecond figure keeps microseconds. A lexical scan of a dozen
#: ledger files takes tens of them; at 0.1 ms it read "0.0ms", and so did its
#: ratio -- a measured time printed as nothing.
MS_DIGITS = 3


# --- shared arithmetic --------------------------------------------------------

def _round_ms(value) -> float:
    return round(value, MS_DIGITS)


def _median(values):
    """The median of *values*, or None for none. The mean of the two middle
    values on an even count: an upper-median shortcut agrees on every odd
    count and is wrong on every even one."""
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    mid = n // 2
    if n % 2:
        return _round_ms(ordered[mid])
    return _round_ms((ordered[mid - 1] + ordered[mid]) / 2.0)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ms(value) -> str:
    """One decimal from a millisecond up; below it, the microseconds."""
    if value is None:
        return "—"
    return f"{value:.1f}ms" if value >= 1 else f"{value:.3f}ms"


def _ratio_text(value) -> str:
    """One decimal from 1.0x up; below it, three significant digits, so a
    semantic path twenty times SLOWER reads 0.05x rather than 0.1x or 0.0x."""
    if value is None:
        return "—"
    return f"{value:.1f}x" if value >= 1 else f"{value:.3g}x"


def _env_value(env, name) -> str:
    """*name*'s value in *env*, matched the way the platform matches it."""
    for key, value in env.items():
        if key == name or (os.name == "nt" and str(key).upper() == name):
            return str(value)
    return ""


# --- cli-startup: children of this interpreter --------------------------------

def child_env(env=None) -> dict:
    """The environment a benchmark child runs under: a copy of *env* (`None`:
    this process's, PYTHONPATH included, so a checkout measures itself) with
    `PYTHONDONTWRITEBYTECODE` defaulted to 1 through `paths.no_bytecode_env`
    (MEB K14). A default, never a switch: the operator's own value wins."""
    return paths.no_bytecode_env(env)


def _default_spawn(argv, timeout_s, env):
    """``(returncode, timed_out, error)`` for one child, run to completion.
    Never raises: a child killed at the deadline and an interpreter that could
    not be started both arrive as a return value, so the caller has one shape
    to classify. Output is captured and dropped -- `--help` prints a page --
    except the tail of stderr, the only evidence a crash leaves."""
    try:
        done = subprocess.run(list(argv), env=env, capture_output=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, True, f"timed out after {timeout_s:g}s"
    except Exception as e:                     # OSError: no such interpreter, ...
        return None, False, str(e) or e.__class__.__name__
    tail = (done.stderr or b"").decode("utf-8", errors="replace").strip()
    error = tail[-MAX_ERROR_CHARS:] if done.returncode else ""
    return int(done.returncode), False, error


def _classify_child(returncode, timed_out) -> str:
    if timed_out:
        return TIMEOUT
    if returncode is None:
        return SPAWN_ERROR
    return OK if int(returncode) == 0 else NONZERO


def startup_argv(interpreter=None) -> list:
    """`(name, argv)` for the two K15 children under *interpreter* (default:
    this process's `sys.executable`, so the venv that runs `hive` is the one
    measured)."""
    exe = str(interpreter or sys.executable)
    return [(name, [exe, *tail]) for name, tail in STARTUP_SCENARIOS]


def time_child(argv, *, timeout_s=DEFAULT_CHILD_TIMEOUT_S, spawn=None, clock=None,
               env=None) -> dict:
    """One timed child. ``ms`` is always the measured wall-clock -- for a
    failure too, where it is emphatically NOT a startup time, which is why
    every statistic in this module filters on ``state == OK``."""
    spawn = spawn or _default_spawn
    clock = clock or time.perf_counter
    env = child_env() if env is None else env
    start = clock()
    returncode, timed_out, error = spawn(list(argv), timeout_s, env)
    elapsed_ms = (clock() - start) * 1000.0
    return {"ms": _round_ms(elapsed_ms), "returncode": returncode,
            "state": _classify_child(returncode, timed_out),
            "error": str(error or "")[:MAX_ERROR_CHARS]}


def time_scenario(name, argv, *, reps=DEFAULT_STARTUP_REPS,
                  timeout_s=DEFAULT_CHILD_TIMEOUT_S, spawn=None, clock=None,
                  env=None) -> dict:
    """One scenario's row: *reps* children, every sample kept, the statistics
    over the ones that exited 0."""
    reps = int(reps)
    if reps < 1:
        raise ValueError("reps must be >= 1")
    samples = [time_child(argv, timeout_s=timeout_s, spawn=spawn, clock=clock,
                          env=env) for _ in range(reps)]
    good = [s["ms"] for s in samples if s["state"] == OK]
    return {
        "name": name,
        "argv": list(argv),
        "samples": samples,
        "ok": len(good),
        "failed": len(samples) - len(good),
        "total": len(samples),
        "median_ms": _median(good),
        "min_ms": min(good) if good else None,
        "max_ms": max(good) if good else None,
        "state": max((s["state"] for s in samples),
                     key=lambda st: _CHILD_RANK.get(st, 0)),
        "error": next((s["error"] for s in samples if s["error"]), ""),
    }


def run_cli_startup(*, reps=DEFAULT_STARTUP_REPS, timeout_s=DEFAULT_CHILD_TIMEOUT_S,
                    spawn=None, clock=None, interpreter=None, env=None,
                    timestamp="") -> dict:
    """The whole report: conditions, one row per scenario, the failure count.
    Raises ValueError for reps < 1 (the command refuses that with exit 2
    before getting here); never raises for anything a child does."""
    import platform
    reps = int(reps)
    if reps < 1:
        raise ValueError("reps must be >= 1")
    env = child_env(env)
    scenarios = startup_argv(interpreter)
    rows = [time_scenario(name, argv, reps=reps, timeout_s=timeout_s, spawn=spawn,
                          clock=clock, env=env) for name, argv in scenarios]
    pythonpath = _env_value(env, "PYTHONPATH")
    return {
        "conditions": {
            "interpreter": scenarios[0][1][0],
            "python_version": platform.python_version(),
            "os": sys.platform,
            "reps": reps,
            "timeout_s": timeout_s,
            # Which code the children imported. Unset means the editable
            # install, i.e. the MAIN checkout, whatever directory this ran in.
            "pythonpath": pythonpath or ("(unset -- the children resolve mindhive "
                                         "from site-packages: the editable install)"),
            "dont_write_bytecode": _env_value(env, paths.NO_BYTECODE_ENV),
            "method": STARTUP_METHOD,
            "timestamp": timestamp or _now(),
            "claim_ms": CLI_STARTUP_CLAIM_MS,
            "claim_source": CLI_STARTUP_CLAIM_SOURCE,
        },
        "rows": rows,
        "failed": sum(r["failed"] for r in rows),
        "measured": sum(1 for r in rows if r["median_ms"] is not None),
    }


def render_cli_startup(report) -> str:
    c = report["conditions"]
    out = ["cli startup bench — %s" % c["interpreter"],
           f"  python      {c['python_version']} on {c['os']}",
           f"  pythonpath  {c['pythonpath']}",
           f"  bytecode    {paths.NO_BYTECODE_ENV}="
           f"{c['dont_write_bytecode'] or '(unset)'}",
           f"  method      {c['method']}",
           f"  samples     {c['reps']} per scenario, timeout {c['timeout_s']:g}s "
           "per child",
           f"  run at      {c['timestamp']}",
           f"  claim       {c['claim_source']}",
           "",
           "  scenario                          median     claim      min      max"
           "      ok  state",
           "  " + "-" * 82]
    for row in report["rows"]:
        label = " ".join(row["argv"][1:])
        out.append("  {:<30} {:>9} {:>9} {:>8} {:>8} {:>7}  {}".format(
            label[:30], _ms(row["median_ms"]), _ms(c["claim_ms"]),
            _ms(row["min_ms"]), _ms(row["max_ms"]),
            f"{row['ok']}/{row['total']}", row["state"]))
        if row["error"]:
            out.append("      ! " + row["error"].replace("\n", " | ")[:200])
    if not report["measured"]:
        out += ["", "  NOTHING WAS MEASURED — every child failed. The numbers "
                    "above are not a profile of a fast CLI."]
    if report["failed"]:
        total = sum(r["total"] for r in report["rows"])
        out += ["", f"  ! {report['failed']} of {total} children failed — exit 1"]
    out += ["", "  The claim column is the MEB's figure, printed for comparison; "
                "it is not a gate.",
            "  This command is read-only and changed nothing."]
    return "\n".join(out)


def cmd_bench_cli_startup(args) -> int | None:
    """Time `import mindhive.__main__` and `python -m mindhive --help` as
    children of this interpreter. Read-only; writes no ledger event.

    Exits 2 when `--reps` is below 1, 1 when any child failed or timed out (a
    failed child is a failure, never a slow success), 0 otherwise -- including
    a median far past the 45 ms claim. Slow is a measurement."""
    reps = int(getattr(args, "reps", DEFAULT_STARTUP_REPS))
    if reps < 1:
        print("bench: --reps must be at least 1 (a median over no samples is "
              "not a measurement)")
        return 2
    report = run_cli_startup(
        reps=reps, timeout_s=float(getattr(args, "timeout", DEFAULT_CHILD_TIMEOUT_S)))
    print(json.dumps(report, indent=2) if getattr(args, "json", False)
          else render_cli_startup(report))
    return 1 if report["failed"] else None


# --- recall-semantic: the two search paths, in-process ------------------------

def default_lexical(query, project, limit):
    """The function behind `hive recall`: the word-boundary scorer over
    `LEDGER/*.md`, re-reading every entry on every call -- the "full ledger
    scan" of spec section 4.7."""
    from . import recall as recall_mod
    return recall_mod.recall(query, project=project, semantic=False, limit=limit)


#: The variable `paths.store_home` reads on every call -- which is what lets
#: the semantic path be pointed at a copy of the index one call at a time.
_STORE_HOME_ENV = "MINDHIVE_HOME"


@contextmanager
def _store_home_at(home):
    """`paths.store_home()` answers *home* inside the block; afterwards the
    variable is exactly what it was, unset included. Process-global for the
    block's length, which a CLI command running nothing concurrently can
    afford."""
    previous = os.environ.get(_STORE_HOME_ENV)
    os.environ[_STORE_HOME_ENV] = str(home)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_STORE_HOME_ENV, None)
        else:
            os.environ[_STORE_HOME_ENV] = previous


def _discard_copy(root) -> bool:
    """Remove the index copy under *root*; True when it is gone. chromadb
    caches one client per path and holds its SQLite file open, which on
    Windows makes the directory undeletable, so that cache is dropped first --
    only when chromadb was imported at all: importing it to clear it costs
    seconds."""
    import gc
    import shutil
    chroma_api = sys.modules.get("chromadb.api.client")
    shared = getattr(chroma_api, "SharedSystemClient", None)
    if shared is not None:
        try:
            shared.clear_system_cache()
        except Exception:                      # noqa: BLE001 -- the return value says whether it worked
            pass
    gc.collect()
    shutil.rmtree(root, ignore_errors=True)
    return not os.path.exists(root)


def _bind_copy(vectors, project):
    """``(path, query_fn, names, indexed_events)`` for the index copy the store
    currently points at: the collections that exist, and the function `hive
    recall --semantic` would dispatch to over them -- `semantic_recall_events`
    when the event index holds rows, else the file-level `semantic_recall`.
    Its own frame, so the client it opens is released when it returns."""
    client = vectors._client(project)
    # chroma returned Collection objects before 0.6 and names since.
    names = {str(getattr(c, "name", c)) for c in client.list_collections()}
    indexed_events = 0
    if vectors.EVENT_COLLECTION in names:
        # `get_collection`, not `_events_collection`: that one goes through
        # `get_or_create_collection`, and this function creates nothing.
        indexed_events = int(client.get_collection(vectors.EVENT_COLLECTION).count())
    if indexed_events > 0:
        return "events", vectors.semantic_recall_events, names, indexed_events
    if FILE_COLLECTION in names:
        return "files", vectors.semantic_recall, names, indexed_events
    raise RuntimeError(
        f"the vector index holds neither an `{vectors.EVENT_COLLECTION}` "
        f"collection with rows nor a `{FILE_COLLECTION}` collection: run "
        "`hive embed` first")


def default_semantic_open(project):
    """Open the semantic path for *project*: ``(facts, search)``, where
    ``search(query, limit)`` calls the function `hive recall --semantic`
    dispatches to, and ``search.close()`` removes the index copy it reads.
    Raises RuntimeError -- reported as `unavailable: <reason>`, exit 0 --
    when the path cannot be opened without writing: LIVE_LOCK
    (`guard.ensure_safe` against the real store, once, here; the bench has no
    `--force`), the `vectors` extra absent, no `chroma/` directory for the
    namespace, an index with neither collection, or a model that fails to load.

    Everything here -- importing chromadb and sentence-transformers, copying
    the index, loading the model, opening the SQLite-backed client -- is the
    `open_ms` figure the caller times around this call: what the first `hive
    recall --semantic` of a process waits for, and not a query. The copy's own
    share rides along as `copy_ms` and `copy_bytes`.

    Never writes to the store, which takes two precautions:

    * `recall.recall(semantic=True)` asks `vectors.indexed_event_count`, whose
      `get_or_create_collection` CREATES the chroma directory and an empty
      `events` collection on a namespace that was never embedded. So a
      namespace without `chroma/` is refused, and the collections are listed
      before a query function is bound.
    * chromadb is not read-only even for a query: its client re-saves the
      SQLite file and the HNSW segment when it opens. Measured on a scratch
      store: one `hive recall --semantic` against an index nothing was adding
      to changed the hashes of `chroma.sqlite3`, `data_level0.bin` and
      `length.bin`. So the namespace's `chroma/` is COPIED into a temporary
      store, and every open and query runs against the copy through the same
      `vectors` functions, with `paths.store_home()` pointed at it for the
      length of each call."""
    import tempfile
    from pathlib import Path
    from . import guard, vectors
    guard.ensure_safe()
    if not vectors.has_vectors_support():
        raise RuntimeError("the `vectors` extra is not installed (chromadb / "
                           "sentence-transformers not importable): "
                           "pip install -e .[vectors]")
    project = project or paths.detect_project()
    chroma_dir = paths.project_dir(project) / "chroma"
    if not chroma_dir.is_dir():
        raise RuntimeError(f"no vector index at {chroma_dir}: run `hive embed` "
                           "first (this bench never creates one)")
    vectors._require_deps()
    scratch = Path(tempfile.mkdtemp(prefix="mindhive-bench-"))
    failure = None
    try:
        import shutil
        with _store_home_at(scratch):
            copy = paths.project_dir(project) / "chroma"
        start = time.perf_counter()
        shutil.copytree(chroma_dir, copy)
        copy_ms = _round_ms((time.perf_counter() - start) * 1000.0)
        copy_bytes = sum(f.stat().st_size for f in copy.rglob("*") if f.is_file())
        with _store_home_at(scratch):
            path, query_fn, names, indexed_events = _bind_copy(vectors, project)
            vectors._encoder()   # the model load: paid here, not inside the first query
    except BaseException as e:
        if not isinstance(e, Exception):       # Ctrl-C: clean up and stop
            _discard_copy(scratch)
            raise
        failure = str(e) or e.__class__.__name__
    if failure is not None:
        # Outside the handler, so no traceback still holds a client whose open
        # SQLite file would keep the copy undeletable on Windows.
        _discard_copy(scratch)
        raise RuntimeError(failure)

    def search(query, limit):
        with _store_home_at(scratch):
            return query_fn(query, project=project, limit=limit)
    search.close = lambda: _discard_copy(scratch)
    facts = {"path": path, "indexed_events": indexed_events,
             "collections": sorted(names), "model": vectors.EMBEDDING_MODEL,
             "chroma_dir": str(chroma_dir), "copy_dir": str(scratch),
             "copy_ms": copy_ms, "copy_bytes": copy_bytes}
    return facts, search


def _search_row(samples, queries) -> dict:
    """The statistics of one path. The first sample is the cold figure and
    stays out of the warm median; per-query rows follow the same rule."""
    good = [s for s in samples if s["state"] == OK]
    cold = samples[0] if samples else None
    warm = [s["ms"] for s in samples[1:] if s["state"] == OK]
    per_query = []
    for query in queries:
        mine = [s for s in samples if s["query"] == query]
        warm_mine = [s["ms"] for s in samples[1:]
                     if s["query"] == query and s["state"] == OK]
        last_hits = next((s["hits"] for s in reversed(mine) if s["state"] == OK),
                         None)
        per_query.append({"query": query, "median_ms": _median(warm_mine),
                          "hits": last_hits,
                          "ok": sum(1 for s in mine if s["state"] == OK),
                          "total": len(mine)})
    return {
        "state": OK if len(good) == len(samples) else FAILED,
        "cold_ms": cold["ms"] if cold is not None and cold["state"] == OK else None,
        "median_ms": _median(warm),
        "min_ms": min(warm) if warm else None,
        "max_ms": max(warm) if warm else None,
        "ok": len(good),
        "total": len(samples),
        "per_query": per_query,
        "error": next((s["error"] for s in samples if s["error"]), ""),
        "samples": samples,
    }


def time_searches(search, queries, reps, clock=None) -> dict:
    """*reps* rounds over *queries*, in order, through ``search(query)``: one
    sample per call, every sample kept. A raise is the sample's state, not
    the run's: it is recorded with its message and excluded from every
    statistic, because a search that raised returns in microseconds and would
    otherwise be the fastest answer of the run."""
    clock = clock or time.perf_counter
    samples = []
    for rep in range(int(reps)):
        for query in queries:
            start = clock()
            try:
                hits = search(query)
            except Exception as e:             # noqa: BLE001 -- the raise IS the state
                elapsed_ms = (clock() - start) * 1000.0
                samples.append({"query": query, "rep": rep,
                                "ms": _round_ms(elapsed_ms), "state": FAILED,
                                "hits": None,
                                "error": (str(e) or e.__class__.__name__)[:MAX_ERROR_CHARS]})
                continue
            elapsed_ms = (clock() - start) * 1000.0
            samples.append({"query": query, "rep": rep, "ms": _round_ms(elapsed_ms),
                            "state": OK, "hits": len(list(hits or [])), "error": ""})
    return _search_row(samples, list(queries))


def _unavailable_row(reason) -> dict:
    return {"state": UNAVAILABLE, "reason": str(reason)[:MAX_ERROR_CHARS],
            "open_ms": None, "open": {}, "cold_ms": None, "median_ms": None,
            "min_ms": None, "max_ms": None, "ok": 0, "total": 0,
            "per_query": [], "error": "", "samples": []}


def run_recall_semantic(*, queries=None, reps=DEFAULT_RECALL_REPS, project=None,
                        limit=DEFAULT_RECALL_LIMIT, lexical=None,
                        semantic_open=None, clock=None, timestamp="") -> dict:
    """The whole report. The order is fixed and the seams rely on it: every
    lexical sample, then the semantic open (timed as `open_ms`), then every
    semantic sample. Raises ValueError for reps or limit below 1; never raises
    for anything a search or the open does."""
    reps, limit = int(reps), int(limit)
    if reps < 1:
        raise ValueError("reps must be >= 1")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    supplied = [str(q) for q in (queries or ()) if str(q).strip()]
    queries = supplied or list(DEFAULT_QUERIES)
    lexical = lexical or default_lexical
    semantic_open = semantic_open or default_semantic_open
    clock = clock or time.perf_counter

    ldir = paths.project_dir(project) / "LEDGER"
    files = sorted(ldir.glob("*.md")) if ldir.is_dir() else []

    lex_row = time_searches(lambda q: lexical(q, project, limit), queries, reps,
                            clock)

    start = clock()
    try:
        facts, search = semantic_open(project)
        reason = ""
    except Exception as e:                     # noqa: BLE001 -- "unavailable" IS the answer
        facts, search, reason = {}, None, (str(e) or e.__class__.__name__)
    open_ms = _round_ms((clock() - start) * 1000.0)  # read even on failure: the clock pairs stay aligned

    if search is None:
        sem_row = _unavailable_row(reason)
    else:
        close = getattr(search, "close", None)
        removed = None
        try:
            sem_row = time_searches(lambda q: search(q, limit), queries, reps, clock)
        finally:
            if callable(close):
                removed = bool(close())
        sem_row.update({"reason": "", "open_ms": open_ms,
                        "open": dict(facts) if isinstance(facts, dict)
                        else {"facts": str(facts)}})
        if removed is not None:
            sem_row["open"]["copy_removed"] = removed

    ratio = None
    if lex_row["median_ms"] is not None and sem_row["median_ms"]:
        ratio = round(lex_row["median_ms"] / sem_row["median_ms"], 4)

    return {
        "conditions": {
            "project": project or paths.detect_project(),
            "queries": list(queries),
            "query_set": ("caller-supplied via --query" if supplied
                          else "default (one query per memo kind)"),
            "reps": reps,
            "limit": limit,
            "samples_per_path": reps * len(queries),
            "ledger_files": len(files),
            "ledger_bytes": sum(f.stat().st_size for f in files),
            "ledger_dir": str(ldir),
            "method": RECALL_METHOD,
            "timestamp": timestamp or _now(),
            "claim": dict(RECALL_CLAIM),
        },
        "lexical": lex_row,
        "semantic": sem_row,
        "ratio": ratio,
        "measured": sum(1 for row in (lex_row, sem_row) if row["ok"]),
    }


def render_recall_semantic(report) -> str:
    c = report["conditions"]
    claim = c["claim"]
    rows = (("lexical", report["lexical"]), ("semantic", report["semantic"]))
    per_path = c["samples_per_path"]
    out = ["recall bench — %s" % c["project"],
           f"  ledger      {c['ledger_files']} files under LEDGER/, "
           f"{c['ledger_bytes']:,} bytes — what the lexical scan reads on every query",
           f"  queries     {len(c['queries'])} ({c['query_set']}): "
           + ", ".join(repr(q) for q in c["queries"]),
           f"  samples     {c['reps']} reps x {len(c['queries'])} queries = {per_path} "
           f"per path (1 cold + {per_path - 1} warm), limit {c['limit']}",
           f"  method      {c['method']}",
           f"  run at      {c['timestamp']}",
           f"  claim       {claim['source']}: full scan {claim['full_scan_ms']:g} ms "
           f"vs vector {claim['vector_ms']:g} ms = {claim['ratio']:g}x, on "
           f"{claim['ledger_events']:,} events / {claim['ledger_mb']} MB — printed "
           "for comparison, not a gate",
           "",
           "  path            cold  median(warm)      min      max       ok  state",
           "  " + "-" * 72]
    for name, row in rows:
        out.append("  {:<10} {:>9} {:>13} {:>8} {:>8} {:>8}  {}".format(
            name, _ms(row["cold_ms"]), _ms(row["median_ms"]), _ms(row["min_ms"]),
            _ms(row["max_ms"]), f"{row['ok']}/{row['total']}", row["state"]))
        if row["state"] == UNAVAILABLE:
            out.append("      ! unavailable: " + row["reason"])
        elif row["error"]:
            out.append("      ! " + row["error"].replace("\n", " | "))
        for q in row["per_query"]:
            out.append("      {:<26} median {:>9}  hits {:>4}  ok {}/{}".format(
                repr(q["query"])[:26], _ms(q["median_ms"]),
                "—" if q["hits"] is None else q["hits"], q["ok"], q["total"]))
    sem = report["semantic"]
    facts = sem.get("open") or {}
    detail = ", ".join(f"{k}={facts[k]}" for k in ("path", "indexed_events", "model")
                       if k in facts)
    ratio = _ratio_text(report["ratio"])
    out += ["",
            f"  semantic open  {_ms(sem.get('open_ms'))}  (deps import + index copy + "
            "model load + index open; paid once per process, not per query)"
            + (f"  [{detail}]" if detail else "")]
    if "copy_bytes" in facts:
        gone = {True: "removed",
                False: f"NOT removed -- delete {facts.get('copy_dir')} by hand"}.get(
            facts.get("copy_removed"), "removal not reported")
        out.append(f"  index copy     {facts['copy_bytes']:,} bytes in "
                   f"{_ms(facts.get('copy_ms'))}, {gone}. chromadb re-saves its "
                   "files on every open, so the queries ran against a temporary "
                   "copy and the store's index was not touched")
    out.append(f"  ratio          {ratio}  (lexical warm median / semantic warm "
               f"median; above 1.0 the semantic query answered faster; the spec's "
               f"figure is {claim['ratio']:g}x)")
    if not report["measured"]:
        out += ["", "  NOTHING WAS MEASURED — every search failed."]
    failed = [name for name, row in rows if row["state"] == FAILED]
    if failed:
        out += ["", "  ! a search raised on the %s path — exit 1" % " and ".join(failed)]
    out += ["", "  This command is read-only and changed nothing."]
    return "\n".join(out)


def cmd_bench_recall_semantic(args) -> int | None:
    """Time the lexical scan behind `hive recall` against the chroma path
    behind `hive recall --semantic`, in-process, on the same queries.
    Read-only; writes no ledger event and creates no index.

    Exits 2 when `--reps` or `--limit` is below 1, 1 when a search raised (a
    raised search is a failed sample, never a slow success), 0 otherwise --
    including when the semantic path is unavailable: an absent optional
    extra, an unembedded namespace or LIVE_LOCK is a reason in the report,
    not a failure of the measurement."""
    reps = int(getattr(args, "reps", DEFAULT_RECALL_REPS))
    limit = int(getattr(args, "limit", DEFAULT_RECALL_LIMIT))
    if reps < 1:
        print("bench: --reps must be at least 1 (a median over no samples is "
              "not a measurement)")
        return 2
    if limit < 1:
        print("bench: --limit must be at least 1 (a search for no hits measures "
              "nothing)")
        return 2
    project = getattr(args, "project", None) or None
    if project is None and paths.is_freshly_minted():
        print(f"warning: no memory for detected project "
              f"'{paths.detect_project()}' — measuring an empty namespace. Pass "
              f"--project or see `hive projects`.", file=sys.stderr)
    report = run_recall_semantic(queries=getattr(args, "query", None), reps=reps,
                                 project=project, limit=limit)
    print(json.dumps(report, indent=2) if getattr(args, "json", False)
          else render_recall_semantic(report))
    failed = any(report[path]["state"] == FAILED for path in ("lexical", "semantic"))
    return 1 if failed else None


# --- the CLI ------------------------------------------------------------------

def register(bnsub) -> None:
    """Wire `cli-startup` and `recall-semantic` onto the `bench` group's
    subparser set. Called from `bench.register`, so the group stays one call in
    `commands/parsers/__init__.py` and each module keeps the flags beside the
    code that reads them, for the reason `bench.register` gives."""
    startup_text = (
        f"median wall-clock of `python -c \"import mindhive.__main__\"` and "
        f"`python -m mindhive --help` as child processes of this interpreter, "
        f"{DEFAULT_STARTUP_REPS} each by default. The MEB's {CLI_STARTUP_CLAIM_MS:g} ms "
        f"claim is printed beside the measured median for comparison; it is not a "
        f"gate. Exit 1 only when a child failed or timed out")
    cs = bnsub.add_parser("cli-startup", help=startup_text, description=startup_text)
    cs.add_argument("--reps", type=int, default=DEFAULT_STARTUP_REPS,
                    help=f"children per scenario (default: {DEFAULT_STARTUP_REPS}; "
                         "at least 1)")
    cs.add_argument("--timeout", type=float, default=DEFAULT_CHILD_TIMEOUT_S,
                    help=f"seconds before one child is killed and reported as a "
                         f"timeout (default: {DEFAULT_CHILD_TIMEOUT_S:g})")
    cs.add_argument("--json", action="store_true", help="machine-readable report")
    cs.set_defaults(func=cmd_bench_cli_startup)

    recall_text = (
        "median query time of the lexical ledger scan (`hive recall`) against the "
        "chroma projection (`hive recall --semantic`) on the same queries, with "
        f"the speed ratio; spec section 4.7's {RECALL_CLAIM['ratio']:g}x is printed "
        "beside it for comparison, not enforced. Without the `vectors` extra, or "
        "on a namespace never embedded, the semantic row reads `unavailable` and "
        "the exit code stays 0. Never writes")
    rs = bnsub.add_parser("recall-semantic", help=recall_text, description=recall_text)
    rs.add_argument("--query", action="append", default=None,
                    help="repeatable; REPLACES the default set of one query per "
                         "memo kind: " + ", ".join(DEFAULT_QUERIES))
    rs.add_argument("--reps", type=int, default=DEFAULT_RECALL_REPS,
                    help=f"rounds over the query set per path (default: "
                         f"{DEFAULT_RECALL_REPS}; at least 1). The first sample of a "
                         "path is its cold figure; the median is over the rest")
    rs.add_argument("--limit", type=int, default=DEFAULT_RECALL_LIMIT,
                    help=f"hits requested per query, as `hive recall --limit` "
                         f"(default: {DEFAULT_RECALL_LIMIT})")
    # `default=None`, not `""`: `project_arg` refuses the empty string and
    # argparse runs a STRING default through `type=` (see `bench.register`).
    rs.add_argument("--project", type=cliparse.project_arg, default=None,
                    help="namespace to search (default: the detected project, as "
                         "`hive recall`)")
    rs.add_argument("--json", action="store_true", help="machine-readable report")
    rs.set_defaults(func=cmd_bench_recall_semantic)
