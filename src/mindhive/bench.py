# SPDX-License-Identifier: Apache-2.0
# src/mindhive/bench.py
"""Timing the dashboard's own endpoints, so "it got faster" can be a number.

The perf work this measures has a before-profile and no after-profile, and the
reason is that the before-profile was a *ritual*: two servers, a hand-built
`curl -w` format string, four endpoints, a concurrency test, and a page of prose
describing the conditions. Nobody re-runs a ritual. So the ritual becomes a
command, and the conditions ride with the numbers instead of living in a plan
file next to them.

Three properties are load-bearing, and each of them is a decision that could
have gone the other way:

**It measures and never fixes.** No start, no stop, no restart, no write — not
even the dashboard token, which this module reads but will not create. A
benchmark that repairs its subject cannot be re-run to confirm a regression,
and one that mints a token would mint one the running server has never heard of.

**Cache misses and cache hits are separate rows, never averaged.** The thing
under measurement *is* a fingerprint-keyed memo layer (`dashboard._STATS_MEMO`
and friends). The first request per endpoint pays the fold; the rest are hits.
The before-profile was taken on code with no memos at all, so every one of its
samples was a miss — which makes the miss row the only one comparable to it, and
makes averaging the two a figure that moves with the hit/miss ratio. On a live
ledger an auto-harvest append invalidates a fingerprint mid-run, so a nominal
hit can be a miss; the sample counts are reported for exactly that reason.

**A failure is never timed as if it succeeded.** A 403 is fast, and four fast
403s look like a healthy server to anyone reading only the milliseconds. Every
route but `/` sits behind the token gate, checked *above* route dispatch, so a
missing token produces precisely that table. Non-200s are reported with their
status and excluded from every mean, and a 403 is called out by name.

The `transport` seam is how the error paths — unreachable, timed out, 403,
500 — are driven without a live server, the same bargain `repo.py` makes with
its `runner`. Its limit is worth stating: `repo.py`'s runner returns data with
no time dimension, and this module's entire output *is* the time dimension. So
the seam proves classification and arithmetic, never latency.

Stdlib only. This project ships no HTTP client dependency and a diagnostic is
the worst possible reason to add the first one.
"""
import concurrent.futures
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import cliparse
from .paths import store_file

# The four routes the before-profile measured, in its order. A default rather
# than a fixed list: a renamed route surfaces here as a non-200, but an ADDED
# slow route leaves no trace at all, and a hard-coded four would keep printing
# four healthy rows while a fifth regressed.
DEFAULT_ENDPOINTS = ("/api/projects", "/api/overview", "/api/oversee",
                     "/api/mindnet/cats")

# Low on purpose. The before-profile's own finding is that the 6.7x
# amplification IS concurrent request pressure against GIL-serialized folds, so
# this command is itself a third poller while it runs. `1 + reps` requests per
# endpoint, times the endpoint count, is the load it adds.
DEFAULT_REPS = 3

# `urllib.request` defaults to NO timeout, and the subject has known lock
# contention and 12s p50s. Without a ceiling one wedged endpoint hangs the whole
# run and the table is never printed.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Recorded in every report because it CHANGED. The before-profile used
# `curl -w` and reported ranges over repeated hand-run requests; this reports a
# mean over N from a different client. The comparison is approximate by
# construction and saying so is cheaper than a reader assuming otherwise.
CLIENT_METHOD = "urllib.request, mean over N samples (before-profile: curl -w, ranges)"

TOKEN_FILENAME = "dashboard.token"

# --- one sample's verdict -----------------------------------------------------
OK = "ok"
HTTP_ERROR = "http_error"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
SAMPLE_STATES = (OK, HTTP_ERROR, TIMEOUT, UNREACHABLE)

# Worst-wins when a row's samples disagree, so a row that answered 200 twice and
# then timed out does not report as `ok`.
_STATE_RANK = {OK: 0, HTTP_ERROR: 1, TIMEOUT: 2, UNREACHABLE: 3}

MAX_ERROR_CHARS = 200

_TOKEN_HINT = ("403 — probable token problem: every route but / is behind the "
               "token gate, checked above route dispatch")


def _default_transport(url, timeout_s):
    """``(status, bytes_read, error)`` for one HTTP GET. Never raises: a refused
    connection, a timeout and a 500 all arrive as a return value, so callers
    have one shape to read.

    The body is read in every branch, error branches included, because the
    timing has to cover the same work the server did — a status line measured
    without its body is not the latency a browser sees."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            body = resp.read()
            return int(getattr(resp, "status", 0) or 0), len(body), ""
    except urllib.error.HTTPError as e:
        # A 403 or a 404 IS an answer, served at a cost. Timed like any other,
        # then classified as an error so no mean can absorb it.
        try:
            body = e.read()
        except Exception:
            body = b""
        return int(e.code), len(body), ""
    except Exception as e:                       # URLError, timeout, DNS, ...
        return 0, 0, str(e) or e.__class__.__name__


def _classify(status, error) -> str:
    """Which of the four states one transport answer represents."""
    if status:
        return OK if int(status) == 200 else HTTP_ERROR
    text = str(error or "").lower()
    return TIMEOUT if ("timed out" in text or "timeout" in text) else UNREACHABLE


def endpoint_url(origin, path, *, token="", project="") -> str:
    """The URL one sample requests. The token rides in the query string because
    that is where this server's own launcher puts it (`dashboard.stable_url`);
    it is therefore a secret inside a URL, and nothing in this module ever
    prints a built URL for that reason — reports carry the origin and the path
    separately."""
    query = {}
    if token:
        query["token"] = token
    if project:
        query["project"] = project
    origin = str(origin or "").rstrip("/")
    path = "/" + str(path or "").lstrip("/")
    suffix = ("?" + urllib.parse.urlencode(query)) if query else ""
    return origin + path + suffix


def _sample(url, timeout_s, transport, clock) -> dict:
    """One timed request. ``ms`` is always the measured elapsed time — including
    for a failure, where it is emphatically NOT a latency, which is why every
    mean in this module filters on ``state == OK`` instead of on presence."""
    start = clock()
    status, nbytes, error = transport(url, timeout_s)
    elapsed_ms = (clock() - start) * 1000.0
    return {"ms": round(elapsed_ms, 1), "status": int(status or 0),
            "bytes": int(nbytes or 0), "state": _classify(status, error),
            "error": str(error or "")[:MAX_ERROR_CHARS]}


def _mean_ms(samples):
    """Mean over the samples that actually answered 200, or None when none did.
    A row with no successful sample prints an em dash rather than a number that
    would read as a fast endpoint."""
    good = [s["ms"] for s in samples if s["state"] == OK]
    return round(sum(good) / len(good), 1) if good else None


def _worst(states) -> str:
    return max(states, key=lambda s: _STATE_RANK.get(s, 0), default=OK)


def time_endpoint(origin, path, *, token="", project="", reps=DEFAULT_REPS,
                  timeout_s=DEFAULT_TIMEOUT_SECONDS, transport=None,
                  clock=None) -> dict:
    """One endpoint's row: the cold/miss sample, then *reps* warm/hit samples.

    Nothing is discarded. The conventional benchmark move — throw the first
    request away as a warm-up — would here throw away the only measurement of
    the cache miss, which is the sample that compares to the before-profile and
    the sample that pays for the fold the perf work was about."""
    transport = transport or _default_transport
    clock = clock or time.perf_counter
    url = endpoint_url(origin, path, token=token, project=project)
    try:
        reps = max(0, int(reps))
    except (TypeError, ValueError):
        reps = DEFAULT_REPS

    cold = _sample(url, timeout_s, transport, clock)
    warm = [_sample(url, timeout_s, transport, clock) for _ in range(reps)]
    every = [cold] + warm
    statuses = sorted({s["status"] for s in every if s["status"]})
    note = _TOKEN_HINT if 403 in statuses else ""
    first_error = next((s["error"] for s in every if s["error"]), "")
    return {
        "path": path,
        "cold": cold,
        "warm": warm,
        "cold_ms": cold["ms"] if cold["state"] == OK else None,
        "warm_mean_ms": _mean_ms(warm),
        "warm_ok": sum(1 for s in warm if s["state"] == OK),
        "warm_total": len(warm),
        "samples": len(every),
        "statuses": statuses,
        "state": _worst([s["state"] for s in every]),
        "note": note,
        "error": first_error,
    }


def measure_serialization(origin, endpoints, *, token="", project="",
                          timeout_s=DEFAULT_TIMEOUT_SECONDS, transport=None,
                          clock=None) -> dict:
    """Serial total against concurrent wall-clock for the same set of requests.

    A ratio near 1.0 means the server answered them one at a time however they
    arrived — the GIL-serialized fold the before-profile identified. A ratio
    near the endpoint count would mean real parallelism.

    Both passes run after the warm phase, so both are hit-path measurements
    unless the ledger moved; the before-profile's equivalent figure was taken on
    unmemoized code and is therefore a miss-path measurement. The report says
    so rather than presenting the two as like for like."""
    transport = transport or _default_transport
    clock = clock or time.perf_counter
    paths = list(endpoints)
    urls = [endpoint_url(origin, p, token=token, project=project) for p in paths]

    start = clock()
    serial = [_sample(u, timeout_s, transport, clock) for u in urls]
    serial_ms = round((clock() - start) * 1000.0, 1)

    start = clock()
    if paths:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as ex:
            concurrent_samples = list(
                ex.map(lambda u: _sample(u, timeout_s, transport, clock), urls))
    else:
        concurrent_samples = []
    concurrent_ms = round((clock() - start) * 1000.0, 1)

    members = [{"path": p, "state": s["state"], "status": s["status"],
                "ms": s["ms"]} for p, s in zip(paths, concurrent_samples)]
    failed = [m["path"] for m in members if m["state"] != OK]
    return {
        "serial_ms": serial_ms,
        "concurrent_ms": concurrent_ms,
        # None rather than a divide-by-zero guard that quietly prints 0.0.
        "ratio": (round(serial_ms / concurrent_ms, 2)
                  if concurrent_ms > 0 else None),
        "members": members,
        # Named, not dropped: a ratio computed over three answers and one
        # refusal is not a four-way serialization measurement, and the
        # denominator has to say which member was missing.
        "failed": failed,
        "incomplete": bool(failed),
        "cache_state": "warm/hit path (the before-profile's equivalent was "
                       "all-miss: it ran on code with no memo layer)",
    }


def resolve_token(explicit="") -> tuple[str, str]:
    """``(token, source)``. Reads; never creates.

    `dashboard.load_or_create_token` mints and persists one when the file is
    absent — correct for a server that is about to serve, wrong here twice over:
    it would write to the store (this command mutates nothing) and the minted
    value would not be the token the running server is checking against."""
    if explicit:
        return str(explicit), "--token"
    env = os.environ.get("MINDHIVE_DASHBOARD_TOKEN")
    if env:
        return env, "MINDHIVE_DASHBOARD_TOKEN"
    path = store_file(TOKEN_FILENAME)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except Exception:
        token = ""
    return token, (str(path) if token else "")


def run(*, origin, token="", label="", project="", reps=DEFAULT_REPS,
        timeout_s=DEFAULT_TIMEOUT_SECONDS, endpoints=None, transport=None,
        clock=None, token_source="", timestamp="") -> dict:
    """The whole report: conditions, one row per endpoint, and the
    serialization figures. Never raises."""
    endpoints = list(endpoints) if endpoints else list(DEFAULT_ENDPOINTS)
    rows = [time_endpoint(origin, p, token=token, project=project, reps=reps,
                          timeout_s=timeout_s, transport=transport, clock=clock)
            for p in endpoints]
    serialization = measure_serialization(
        origin, endpoints, token=token, project=project, timeout_s=timeout_s,
        transport=transport, clock=clock)
    return {
        "conditions": {
            "url": str(origin),
            # The bench cannot know the server's own default namespace — that
            # is resolved from the server process's cwd — so an unpassed
            # --project is reported as unknown rather than guessed at.
            "project": project or "(server default — --project not passed)",
            "label": label,
            "method": CLIENT_METHOD,
            "timeout_s": timeout_s,
            "reps": reps,
            "samples_per_endpoint": 1 + int(reps),
            "timestamp": timestamp or _now(),
            "endpoints": endpoints,
            "endpoint_set": ("default (the four before-profile routes)"
                             if list(endpoints) == list(DEFAULT_ENDPOINTS)
                             else "caller-supplied via --endpoint"),
            # The source, never the value: the token is a secret and a report is
            # something people paste into plans and pull requests.
            "token_source": token_source or "(none)",
            # Nothing exposes the server's uptime, so the run stamp is all this
            # command can honestly offer; the caller records the restart beside it.
            "server_uptime": "not available — no endpoint exposes it; record the "
                             "restart time alongside this run",
        },
        "rows": rows,
        "serialization": serialization,
        "measured": sum(1 for r in rows if r["cold_ms"] is not None
                        or r["warm_mean_ms"] is not None),
    }


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- rendering ----------------------------------------------------------------

def _ms(value) -> str:
    return "—" if value is None else f"{value:.1f}ms"


def render(report) -> str:
    """The text table. Deliberately wide enough to paste into a plan file
    beside the before-profile it is meant to be compared against."""
    c = report["conditions"]
    out = ["dashboard bench — %s" % (c["label"] or "(unlabelled)"),
           f"  target      {c['url']}",
           f"  namespace   {c['project']}",
           f"  method      {c['method']}",
           f"  samples     {c['samples_per_endpoint']} per endpoint "
           f"(1 cold/miss + {c['reps']} warm/hit), timeout {c['timeout_s']:g}s",
           f"  endpoints   {c['endpoint_set']}",
           f"  token       {c['token_source']}",
           f"  run at      {c['timestamp']}",
           f"  uptime      {c['server_uptime']}",
           "",
           "  endpoint                  cold/miss     warm/hit  hits  state",
           "  " + "-" * 62]
    for row in report["rows"]:
        out.append("  {:<24} {:>10} {:>12} {:>5}  {}".format(
            row["path"][:24], _ms(row["cold_ms"]), _ms(row["warm_mean_ms"]),
            f"{row['warm_ok']}/{row['warm_total']}", row["state"]))
        if row["note"]:
            out.append(f"      ! {row['note']}")
        elif row["state"] != OK and row["error"]:
            out.append(f"      ! {row['error']}")
        elif row["state"] == HTTP_ERROR:
            out.append("      ! HTTP %s" % ", ".join(str(s) for s in row["statuses"]))
    if not report["measured"]:
        out += ["", "  NOTHING WAS MEASURED — every endpoint failed. The numbers "
                    "above are not a profile of a fast server."]
    s = report["serialization"]
    out += ["",
            "  serialization ({} requests)".format(len(s["members"])),
            f"    serial total     {_ms(s['serial_ms'])}",
            f"    concurrent wall  {_ms(s['concurrent_ms'])}",
            "    ratio            {}  (≈1.0 means the server answered them one "
            "at a time)".format("—" if s["ratio"] is None else f"{s['ratio']:.2f}x"),
            f"    cache state      {s['cache_state']}"]
    if s["incomplete"]:
        out.append("    ! incomplete — these members did not answer 200: "
                   + ", ".join(s["failed"]))
    return "\n".join(out)


# --- the CLI ------------------------------------------------------------------
# `register` lives here rather than inline in the parser module (`__main__.py`
# until the P9 split on 2026-09-14, `commands/parsers/__init__.py` since), which
# is where every other group is wired (`context`, `repo`, `packs`, `mindnet`).
# The deviation was deliberate and its only reason was size: __main__.py was
# past 4100 lines. Note for accuracy — the tech-debt lens did NOT flag that
# file, because `replenish._python_modules` skips `__`-prefixed names; the
# argument was the measured line count, not a tool's verdict.

def cmd_bench_dashboard(args) -> int | None:
    """Time the dashboard's endpoints. Read-only; writes no ledger event.

    Exits 2 on a refusal (no label, no token, no usable URL) and 1 when nothing
    was measured at all. Slow is not a failure — a diagnostic that exits
    non-zero for bad news gets pruned from whatever runs it — but a run that
    measured nothing is not bad news about the server, it is a broken run."""
    origin = str(getattr(args, "url", "") or "").strip()
    if not origin.startswith(("http://", "https://")):
        print("bench: --url needs a full origin, e.g. "
              "--url http://127.0.0.1:54461")
        return 2
    label = str(getattr(args, "label", "") or "").strip()
    if not label:
        print("bench: --label is required. The same code answers in 1.8s or "
              "12.4s depending on what else is hitting the server, so a number "
              "without its load condition is not evidence — name the condition "
              "(e.g. --label \"live, browser attached, both pollers\").")
        return 2
    token, source = resolve_token(getattr(args, "token", "") or "")
    if not token:
        print("bench: no dashboard token. Every route but / is behind the token "
              "gate, so without one this would time four 403s and call them "
              "fast. Pass --token, set MINDHIVE_DASHBOARD_TOKEN, or start the "
              f"dashboard once to create {store_file(TOKEN_FILENAME)}.")
        return 2

    report = run(origin=origin, token=token, label=label,
                 project=str(getattr(args, "project", "") or ""),
                 reps=getattr(args, "reps", DEFAULT_REPS),
                 timeout_s=getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS),
                 endpoints=getattr(args, "endpoint", None),
                 token_source=source)
    print(json.dumps(report, indent=2) if getattr(args, "json", False)
          else render(report))
    return 1 if not report["measured"] else None


def register(subparsers) -> None:
    """Wire the `bench` group onto the root subparser set built by
    `commands.parsers._build_parser`.

    A new top-level group rather than `hive dashboard bench`: `dashboard` is a
    leaf parser taking only `--port` and `--no-browser`, and
    `python -m mindhive dashboard` has to keep working as a bare invocation —
    that is the mechanism the project instructions protect.

    **This was the only command in the tree that registered itself** when it
    was written. Every other group — including the multi-subcommand ones —
    built its parser inline in `__main__.py`, which is how that file reached
    4115 lines (seven more groups have since taken the same trade, and the P9
    split moved the wiring itself to `commands/parsers/`). The deviation is
    deliberate and it is named here rather than left for the next reader to
    notice: the parser module gets two lines (an import and this call) instead
    of forty, and the flags stay beside the code that reads them, so a new flag
    cannot be added to one and forgotten in the other. Following the local
    convention would have been the more consistent choice and the worse one; if
    a second module ever registers this way, this comment stops being a
    deviation and the pattern should be written down as the convention."""
    bn = subparsers.add_parser(
        "bench", help="benchmarks that measure and never fix: the dashboard's "
                      "endpoints, CLI startup, the recall paths (read-only; "
                      "writes no events)")
    bnsub = bn.add_subparsers(dest="bench_cmd", required=True)
    bd = bnsub.add_parser(
        "dashboard", help="cold/miss and warm/hit timings for the dashboard "
                          "API, plus a serialization ratio")
    bd.add_argument("--url", required=True,
                    help="origin of the server to measure, e.g. "
                         "http://127.0.0.1:54461")
    bd.add_argument("--label", default="",
                    help="what load condition this run was taken under. "
                         "Required: the same code answers 6.7x apart between "
                         "two conditions, so a number without its label is not "
                         "evidence")
    bd.add_argument("--token", default="",
                    help="dashboard token (default: "
                         "MINDHIVE_DASHBOARD_TOKEN, then "
                         f"<store>/{TOKEN_FILENAME}). Every route but / is "
                         "behind the token gate")
    # `default=None`, not `""`: argparse runs a STRING default through `type=`,
    # and `project_arg` refuses the empty string, so an omitted `--project`
    # exited 2 before `cmd_bench_dashboard` ran. `None` is not passed through,
    # and the handler already reads the value as `str(... or "")`.
    bd.add_argument("--project", type=cliparse.project_arg, default=None,
                    help="namespace to fold (default: whatever the SERVER "
                         "resolves from its own cwd). Which namespace is "
                         "folded is the largest single determinant of cost")
    bd.add_argument("--reps", type=int, default=DEFAULT_REPS,
                    help=f"warm samples per endpoint after the cold one "
                         f"(default: {DEFAULT_REPS}; kept low because this "
                         f"command is itself load on the server it measures)")
    bd.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                    help=f"seconds before one request is abandoned (default: "
                         f"{DEFAULT_TIMEOUT_SECONDS:g}; urllib itself has no "
                         f"default and the subject has known lock contention)")
    bd.add_argument("--endpoint", action="append", default=None,
                    help="repeatable; REPLACES the default set of "
                         + ", ".join(DEFAULT_ENDPOINTS))
    bd.add_argument("--json", action="store_true", help="machine-readable report")
    bd.set_defaults(func=cmd_bench_dashboard)
    # Blueprint P8 (MEB K15, spec section 4.7): `cli-startup` and
    # `recall-semantic` live in `bench_scenarios` and register onto this same
    # group, so `hive bench` stays one call in `commands/parsers/__init__.py`
    # and each module keeps the flags beside the code that reads them.
    from . import bench_scenarios
    bench_scenarios.register(bnsub)
