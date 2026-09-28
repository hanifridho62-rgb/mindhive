# SPDX-License-Identifier: Apache-2.0
# src/mindhive/dashboard_jobs.py
"""The six long dashboard operations, as background jobs (T13), and the harvest
they share.

Extracted from ``dashboard.py`` when that file reached 2043 lines. These are the
operations that used to hold a lock for the whole length of an HTTP response;
each is now started by a 202 and polled at ``/api/job``. The lock is still per
operation — it is just held by the JOB for as long as the work runs, instead of
by the handler for as long as the response takes.

``run_catch_up`` lives here rather than in ``dashboard`` because both of its real
callers do: ``_job_harvest`` right below it, and the background harvest loop in
``dashboard``, which reaches it through this module. That also makes this the one
address a test can patch — ``tests/test_dashboard_locks`` fakes it here to prove
the harvest job runs its work exactly once."""
from . import jobs, outcomes, projector
from . import plugins as plugins_mod
from .dashboard_data import default_project, is_all
from .dashboard_locks import (REFLECT_BATCH_LIMIT, _EMBED_LOCK, _HARVEST_LOCK,
                              _PLUGINS_LOCK, _REFLECT_LOCK, _SELFTEST_LOCK)

def run_catch_up() -> dict:
    """Smart-harvest every transcript dir into its project and refresh the
    touched projections. Used by the manual endpoint and the background loop."""
    from . import harvest as harvest_mod
    res = harvest_mod.catch_up(skip_captured=True)
    for proj in res.get("projects", {}):
        try:
            projector.project(proj)
        except Exception:
            pass
    return res


# --- the six long operations, as jobs (T13) ----------------------------------
# Each body answers ONE question the endpoint could not: what did the run
# actually do? `ok: True` said only that the code reached the end, so a harvest
# that found nothing and a harvest that ingested forty sessions reported
# identically — and that ambiguity was the whole complaint. Every body below has
# to choose between DID_WORK and NOTHING_TO_DO, which is the one place the
# distinction can be made honestly, because it is the only place that has the
# counts.
#
# Module level, not inside `make_handler`: registration happens once at import,
# and a runner that closed over a handler instance could not be tested without
# an HTTP server. The endpoints reach them by kind NAME, so nothing here needs to
# be defined before the handler class.
#
# Validation and refusals stay SYNCHRONOUS at the endpoint (a bad `limit`, an
# active LIVE_LOCK): they are instant, and answering them with a job id the
# caller then has to poll to learn it was rejected would be a worse contract.

def _job_harvest(project, request, emit) -> outcomes.Report:
    res = run_catch_up()
    if res.get("locked"):
        # Another harvester — the background loop, or the CLI — holds the file
        # lock. Not NOTHING_TO_DO: there may well have been work, and this run
        # simply did not get to look.
        return outcomes.Report(outcomes.DEGRADED,
                               "another harvest was already running — nothing "
                               "was captured by this one", res)
    new = int(res.get("total_new") or 0)
    touched = len(res.get("projects") or {})
    skipped = res.get("skipped_unknown") or []
    tail = (f"; skipped {len(skipped)} untracked namespace(s)" if skipped else "")
    if not new:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               f"no sessions the hooks missed{tail}", res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"harvested {new} session(s) into {touched} project(s){tail}", res)


def _job_embed(project, request, emit) -> outcomes.Report:
    """Index BOTH collections, exactly as `hive embed` does.

    The button used to run the FILE pass alone, so an operator who never
    touched the CLI had a semantic layer that silently half-worked: the
    Memory view could find the file a decision lived in and `hive recall
    --semantic` could not find the decision (blueprint P1h)."""
    from . import vectors
    force = bool(request.get("force", False))
    rebuild = bool(request.get("rebuild", False))
    try:
        res = vectors.embed(project, force=force, rebuild=rebuild)
    except RuntimeError as e:
        # The semantic layer is optional by invariant (DATA_MODEL §5), so absent
        # extras are a DEGRADED store rather than a broken dashboard — reporting
        # that as FAILED would send someone hunting a bug in a dependency they
        # chose not to install.
        #
        # Narrowed to the chained ImportError rather than every RuntimeError, and
        # that distinction is load-bearing: `vectors._require_deps` is the ONLY
        # path that raises from an ImportError, so anything else reaching here is
        # a real fault and must stay FAILED. A blanket `except RuntimeError` had
        # exactly the flattening this module exists to stop — it labelled a
        # genuine crash as "optional dependency missing".
        if isinstance(e.__cause__, ImportError):
            return outcomes.Report(outcomes.DEGRADED, str(e))
        raise
    evres = vectors.embed_events(project, force=force, rebuild=rebuild)
    out = {"files": res, "events": evres}
    embedded = int(res.get("embedded") or 0)
    indexed = int(evres.get("embedded") or 0)
    if not embedded and not indexed:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "no LEDGER entries to embed yet", out)
    return outcomes.Report(outcomes.DID_WORK,
                           f"embedded {embedded} LEDGER entr(ies) and "
                           f"{indexed} event(s)", out)


def _job_plugins_scan(project, request, emit) -> outcomes.Report:
    # Plugins/skills are installed machine-wide, so a scan is global. In HIVE
    # MIND mode there is no concrete namespace to store the catalog under — fall
    # back to the default project so the recommend route can still find it.
    target = default_project() if is_all(project) else project
    res = plugins_mod.scan_and_store(top_n=int(request.get("top", 5)),
                                     project=target)
    counts = res.get("counts") or {}
    total = int(counts.get("plugins_total") or 0)
    skills = int(counts.get("skills_total") or 0)
    if not total and not skills:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "no plugins or skills found in the cache", res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"catalogued {total} plugin(s) and {skills} skill(s)", res)


def _reflect_limit(request) -> int:
    """Clamp to [0, REFLECT_BATCH_LIMIT]. A direct API caller must not be able to
    request an unbounded batch of 120s-per-incident claude calls."""
    return max(0, min(int(request.get("limit", REFLECT_BATCH_LIMIT)),
                      REFLECT_BATCH_LIMIT))


def _job_rules_reflect(project, request, emit) -> outcomes.Report:
    # Reflect a bounded batch of active, not-yet-reflected incidents into
    # structured lessons. Candidates below the recurrence threshold wait until
    # they recur; already-reflected records are skipped (the CLI's `hive rules
    # reflect` re-reflects everything — this deliberately does not).
    import shutil
    from . import incidents, reflect as reflect_mod
    limit = _reflect_limit(request)
    incidents.audit(project=project)       # self-heal a stale projection first
    incidents.project(project=project)
    eligible = [s["signature"] for s in incidents.summaries(project)
                if s["status"] == "active" and s["rule_source"] == "none"]
    if not eligible:
        # Names the filter, not just its result. "no active failure without a
        # lesson already" was true and unreadable: it reported that the count
        # was zero without saying what had been counted, so a healthy store and
        # a filter looking at the wrong fields produced the identical sentence.
        # The two fields are the ones on the line above.
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "nothing to reflect: no incident with "
                               "status=active and rule_source=none. Zero is "
                               "the healthy answer here — it means every live "
                               "failure has already produced a lesson, not "
                               "that the search came up empty",
                               {"reflected": 0, "failed": 0, "pending": 0})
    reflected = failed = 0
    for signature in eligible[:limit]:
        emit(f"reflecting {signature}\n")
        if reflect_mod.reflect_incident(signature, project=project).get("reflected"):
            reflected += 1
        else:
            failed += 1
    # pending = eligible not yet turned into a lesson, excluding the ones tried
    # this round: they were attempted, not skipped.
    res = {"reflected": reflected, "failed": failed,
           "pending": max(0, len(eligible) - reflected - failed)}
    if failed and not reflected and not shutil.which("claude"):
        return outcomes.Report(
            outcomes.FAILED,
            "claude CLI unavailable — reflection needs it on PATH", res)
    if failed:
        return outcomes.Report(
            outcomes.DEGRADED,
            f"reflected {reflected}, {failed} failed, {res['pending']} pending",
            res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"reflected {reflected} lesson(s), {res['pending']} pending", res)


def _job_rules_resolve(project, request, emit) -> outcomes.Report:
    # Active resolution over the red (unresolved) set: reflect incidents with no
    # rule yet, reroute leaking auto rules to the best distinct alternative
    # (human rules are never touched). Same clamp as Reflect — each red incident
    # can cost a 120s claude call.
    import shutil
    from . import resolve as resolve_mod
    res = resolve_mod.resolve_batch(project=project, limit=_reflect_limit(request))
    rerouted = int(res.get("rerouted") or 0)
    reflected = int(res.get("reflected") or 0)
    failed = int(res.get("failed") or 0)
    done = rerouted + reflected
    if failed and not done and not shutil.which("claude"):
        return outcomes.Report(
            outcomes.FAILED,
            "claude CLI unavailable — resolution needs it on PATH", res)
    if failed:
        return outcomes.Report(
            outcomes.DEGRADED,
            f"rerouted {rerouted}, reflected {reflected}, {failed} failed", res)
    if not done:
        return outcomes.Report(outcomes.NOTHING_TO_DO,
                               "nothing red left to resolve", res)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"rerouted {rerouted}, reflected {reflected}", res)


def _job_selftest(project, request, emit) -> outcomes.Report:
    """Run the repo's own pytest suite, from the web server, on a browser click.

    That sentence is alarming on purpose, so here is the justification the
    five-surfaces review asked for — including the parts that weaken it.

    **What it actually is.** Not pytest in-process. `verify.default_argv`
    (`verify.py:69`) builds `[sys.executable, "-m", "pytest", "-q", <tests>]` and
    `verify.stream` spawns it through `subprocess.Popen` (`verify.py:141`) from a
    jobs worker thread, argv-list only — `shell=True` appears nowhere in that
    module and a test pins its absence. Output is streamed line by line into the
    job artifact rather than captured wholesale, so a four-minute suite is not
    four minutes of an empty pane, and a process that dies mid-run is sealed as
    `job_abandoned` on the next boot instead of staying "running" forever.

    **What holds it safe — three things, stated at their real strength:**

    * **The token.** One token for the WHOLE server, not per route:
      `make_handler(token)` (`dashboard.py:86`) closes over a single value and
      `_check_token` (`dashboard.py:157-162`) compares every request against it
      (constant-time, so it does not leak by timing). It is normally delivered in
      the URL query string — `stable_url` (`dashboard.py:378-384`) builds
      `http://127.0.0.1:<port>/?token=<tok>`, which is what the launcher opens. A
      URL-borne bearer token is a materially weaker hold than "per-route auth"
      would imply: it lands in shell history, in whatever the browser keeps, and
      in any log that records a request line. It is stated that way here rather
      than oversold, because a hold nobody has described accurately is a hold
      nobody can audit.
    * **The single-flight lock.** `_SELFTEST_LOCK` (`dashboard_locks.py:64`) is
      passed to `jobs.register_kind` at the bottom of this module, so a second
      click while a run is in flight is refused rather than queued. The worst case
      it prevents is concrete: two pytest processes over the same working tree,
      writing the same caches and temp files, each reporting results the other
      invalidated.
    * **Local-only binding.** The server binds `127.0.0.1` explicitly
      (`ThreadingHTTPServer(("127.0.0.1", port), handler)`, `dashboard.py:495`),
      so nothing off this machine can reach the route to begin with.

    **What it costs when someone does click it.** A multi-minute, CPU-heavy
    subprocess starts, on the machine already running the dashboard whose own
    latency this project has spent a plan measuring. That is the honest price and
    it is not hidden behind the word "selftest".

    **Revocation.** Remove any ONE of those three holds and this justification is
    void, not merely weaker: bind to `0.0.0.0` and the local-only argument is
    gone; drop the lock and concurrent runs corrupt each other's tree; make the
    token optional and an unauthenticated request starts a process. Writing that
    down is the point of the docstring — the next person to touch any of the
    three should find the condition stated before they trip it."""
    from . import selftest as selftest_mod
    # echo=False: the server's stdout is the terminal the dashboard was launched
    # from, and the person who wants this output is looking at the browser. `emit`
    # is what puts it there, line by line, so a four-minute suite is not four
    # minutes of an empty pane.
    res = selftest_mod.run(emit=emit, echo=False)
    code = res["exit_code"]
    counts = res.get("counts") or {}
    tally = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts parsed"
    if code == 0:
        return outcomes.Report(outcomes.DID_WORK, f"suite green — {tally}", res)
    return outcomes.Report(outcomes.FAILED,
                           f"suite exited {code} — {tally}", res)


# Opt-in, one line each. The lock is the pre-existing per-operation lock: the
# same work is reachable from the CLI and, for plugins, from a sibling endpoint,
# so "one job per kind" alone would have quietly dropped that guarantee.
jobs.register_kind("harvest", "Catching up on missed sessions",
                   _job_harvest, _HARVEST_LOCK)
jobs.register_kind("embed", "Embedding the ledger", _job_embed, _EMBED_LOCK)
jobs.register_kind("plugins_scan", "Scanning plugins and skills",
                   _job_plugins_scan, _PLUGINS_LOCK)
jobs.register_kind("rules_reflect", "Reflecting failures into lessons",
                   _job_rules_reflect, _REFLECT_LOCK)
jobs.register_kind("rules_resolve", "Resolving red lessons",
                   _job_rules_resolve, _REFLECT_LOCK)
jobs.register_kind("selftest", "Running the test suite",
                   _job_selftest, _SELFTEST_LOCK)
