# src/mindhive/dashboard_get.py
"""Every GET route the dashboard serves, as a mixin on the request handler.

Extracted from ``dashboard.py`` when that file reached 2043 lines. The handlers
used to be methods of a class defined INSIDE ``make_handler(token)``, and the
nesting looked load-bearing; it was not. Exactly two of them needed the closure —
``_get_workspace`` and ``_get_research``, to render an iframe page with the
session token — and they now read ``self._token``, a class attribute
``make_handler`` stamps on the composed class. Nothing else in 600 lines touched
``token`` at all.

A MIXIN, not a base class, and it declares no ``__init__``: it is mixed into a
``BaseHTTPRequestHandler`` subclass, and every ``self._json`` / ``self._send`` /
``self.path`` here resolves on that composed class at call time. Reading this
file alone will not show you where those come from — ``dashboard.make_handler``
is the assembly point, and the one place the whole handler exists.

``do_GET`` deliberately stayed in ``dashboard``. It is dispatch and
authentication rather than a route, and its ``/`` branch calls ``render_page``
through ``dashboard``'s own globals, which is where ``test_dashboard_browser``
patches it to serve a deliberately mutated page.

``_get_enrich_settings`` and ``_get_job`` sit among the POST handlers in the
original file, beside the endpoints they belong to. They came here instead,
because ``_GET_ROUTES`` names them and a class body cannot reference a method
defined on a different class."""
import time
from urllib.parse import urlparse

from . import chain, dedupe, events, jobs, paths, taskview
from . import export as export_mod
from . import graph as graph_mod
from . import graphview
from . import plugins as plugins_mod
from . import recall as recall_mod
from . import research as research_mod
from . import workspace as workspace_mod
from .briefing import briefing as build_briefing
from .dashboard_data import (HIVEMIND_LABEL, HIVEMIND_PROJECT, _add_tallies,
                             _all_projects, _efficiency, _empty_tallies,
                             _project_stats, _safe_chain_ok, all_event_pairs,
                             default_project, event_pairs, overview_stats)
from .dashboard_locks import _VERIFY_LOCK, _one_at_a_time
from .dashboard_memo import (_CATS_MEMO, _ledger_fingerprint, _memo_get,
                             _memo_put, _sessions_cached, _with_online)
from .research_page import render_research
from .swarm_page import render_swarm
from .workspace_page import render_parallel


class GetRoutes:
    """The GET half of ``DashboardHandler``. See the module docstring for why
    this is a mixin and where the other half of ``self`` comes from."""

    # --- GET route handlers -------------------------------------------
    # Each takes (proj, am, qs) and sends its own response. They are wired to
    # their path in _GET_ROUTES (bottom of the class) and dispatched once by
    # do_GET, so there is no long if/elif ladder. Routes that iterate events
    # get their (project, Event) source from event_pairs(), which is the one
    # place the HIVE MIND (am) fork lives.
    def _get_projects(self, proj, am, qs):
        stats = [_project_stats(n) for n in _all_projects()]
        stats.sort(key=lambda s: (s["events"], s["name"]), reverse=True)
        total = {"name": HIVEMIND_LABEL, "id": "*",
                 "events": sum(s["events"] for s in stats),
                 "memory": sum(s["memory"] for s in stats),
                 "auto": sum(s["auto"] for s in stats),
                 "sessions": sum(s["sessions"] for s in stats)}
        return self._json({"projects": stats, "hivemind": total,
                           "default": default_project(), "current": proj})

    def _get_state(self, proj, am, qs):
        if am:
            return self._send(200, "HIVE MIND unifies every project — pick a single "
                              "project to read its STATE.md.", "text/plain; charset=utf-8")
        p = paths.project_dir(proj) / "STATE.md"
        text = p.read_text(encoding="utf-8") if p.exists() else "(no STATE.md — run project)"
        return self._send(200, text, "text/plain; charset=utf-8")

    def _get_briefing(self, proj, am, qs):
        if am:
            return self._send(200, "HIVE MIND unifies every project — pick a single "
                              "project for its briefing.", "text/plain; charset=utf-8")
        bpath = build_briefing(proj)
        return self._send(200, bpath.read_text(encoding="utf-8"), "text/plain; charset=utf-8")

    @_one_at_a_time(_VERIFY_LOCK, "a chain verification is already running")
    def _get_verify(self, proj, am, qs):
        if am:
            per = {n: _safe_chain_ok(n) for n in _all_projects()}
            return self._json({"ok": all(per.values()) if per else False, "per_project": per})
        return self._json(chain.verify(proj))

    def _get_overview(self, proj, am, qs):
        # Both branches now go through the same per-project memo: HIVE MIND sums
        # folds instead of unifying every event in one pass, which is allowed
        # only because the two agree exactly — distinct (project, session) pairs
        # IS the sum of each project's distinct sessions, and the type tallies
        # add. `test_hive_mind_totals_match_a_directly_unified_fold` pins that.
        # The win is that the expensive branch stops being the uncacheable one:
        # 60 namespaces on this machine, one read each, and a second poll reads
        # none of them.
        if am:
            names = _all_projects()
            per = [overview_stats(n) for n in names]
            by_type = {}
            for stats in per:
                for etype, count in stats["by_type"].items():
                    by_type[etype] = by_type.get(etype, 0) + count
            stamps = [s["last_ts"] for s in per if s["last_ts"]]
            return self._json({
                "project": HIVEMIND_LABEL, "projects": len(names),
                "events": sum(s["events"] for s in per),
                "memory_events": sum(s["memory_events"] for s in per),
                "auto_events": sum(s["auto_events"] for s in per),
                "sessions": sum(s["sessions"] for s in per),
                "by_type": by_type, "last_ts": max(stamps) if stamps else None,
                "chain_ok": all(s["chain_ok"] for s in per) if names else False,
            })
        return self._json({"project": proj, **overview_stats(proj)})

    def _get_memory(self, proj, am, qs):
        # the semantic subset only — the intent/context layer. Build the
        # (project, Event) rows once so the projection + dedupe.collapse lives
        # in a single place instead of once per am / single-project branch.
        from . import roadmap as roadmap_mod
        session = (qs.get("session") or [None])[0]
        limit = int((qs.get("limit") or ["200"])[0])
        rows = [(n, e) for n, e in event_pairs(proj, am)
                if e.type in events.MEMORY_VIEW_TYPES]
        if session:
            rows = [(n, e) for n, e in rows if e.session_id == session]
        rows = list(reversed(rows))[:limit]      # newest first

        def _text(e):
            if not isinstance(e.payload, dict):
                return str(e.payload)
            if e.type == "roadmap":
                # roadmap payloads are structured (no "text"): render the
                # same self-describing body the LEDGER projection uses so
                # the Memory view never shows a blank plan row.
                return roadmap_mod.ledger_body(e.payload)
            return e.payload.get("text", "")
        # Collapse near-duplicate memories by meaning (same type + project),
        # keeping the newest; display-time only, ledger untouched.
        # similar_count rides along for the UI hint.
        return self._json(dedupe.collapse([
            {"ts": e.ts, "type": e.type, "session": e.session_id, "project": n,
             "text": _text(e), "tags": e.tags,
             "origin": events.origin_of(e.session_id, e.tags)}
            for n, e in rows
        ]))

    def _get_improvements(self, proj, am, qs):
        # Lessons learned: machine-learned incident lessons (auto) plus
        # user-dictated caveat notes (manual), kept as two shapes so long
        # status memos can't masquerade as lesson cards. One event pass per
        # project feeds the caveats, the per-signature enrichment
        # (timestamps, leaks, resolution glyph), and the efficiency tiles.
        from . import incidents
        names = _all_projects() if am else [proj]
        now = time.time()
        auto, manual = [], []
        tallies = _empty_tallies()
        for n in names:
            evs = events.read_all(n)
            _add_tallies(tallies, evs, now)
            stats_map = incidents.signal_stats(n, evs=evs)
            for s in incidents.summaries(n):
                st = stats_map.get(s["signature"], {})
                resolution = incidents.classify_resolution(s, st)
                auto.append(dict(
                    s, project=n, source="auto",
                    first_seen=st.get("first_seen"),
                    last_seen=st.get("last_seen"),
                    leaks=st.get("leaks", 0),
                    reroutes=st.get("reroutes", 0),
                    last_reroute=st.get("last_reroute"),
                    resolution=resolution,
                    **incidents.classify_triage(resolution, s)))
            for e in evs:
                if e.type != "caveat":
                    continue
                manual.append({
                    "ts": e.ts, "type": e.type, "session": e.session_id,
                    "project": n, "source": "manual", "tags": e.tags,
                    "origin": events.origin_of(e.session_id, e.tags),
                    "text": (e.payload.get("text", "")
                             if isinstance(e.payload, dict) else str(e.payload)),
                })
        auto.sort(key=lambda s: s.get("occurrences", 0), reverse=True)
        manual.sort(key=lambda m: m.get("ts") or "", reverse=True)
        return self._json({"auto": auto, "manual": dedupe.collapse(manual),
                           "stats": _efficiency(tallies)})

    def _get_sessions(self, proj, am, qs):
        by: dict = {}
        for name, e in event_pairs(proj, am):
            key = (name, e.session_id)
            s = by.get(key)
            if s is None:
                s = by[key] = {"session_id": e.session_id, "project": name,
                               "events": 0, "memory": 0,
                               "origin": None,
                               "first_ts": e.ts, "last_ts": e.ts, "_evs": []}
            s["events"] += 1
            s["_evs"].append(e)
            if not s["origin"]:
                s["origin"] = events.origin_of(e.session_id, e.tags)
            if e.type in events.MEMORY_VIEW_TYPES:
                s["memory"] += 1
            if e.ts and e.ts < s["first_ts"]:
                s["first_ts"] = e.ts
            if e.ts and e.ts > s["last_ts"]:
                s["last_ts"] = e.ts
        out = sorted(by.values(), key=lambda s: s["last_ts"] or "", reverse=True)
        for s in out:
            s["activity"] = taskview.tally(s.pop("_evs"))
            s["status"] = taskview.status_of(s["last_ts"])
        return self._json(out)

    def _get_changelog(self, proj, am, qs):
        # Mandatory code change-log. Shape and ordering delegate to
        # changelog.entries() — one source of truth, so this view can
        # never disagree with `hive log show`. The pending marker is a
        # per-project sidecar, so HIVE MIND shows entries without a
        # pending banner; categories ride along so the UI never hardcodes
        # the taxonomy.
        from . import changelog as changelog_mod
        if am:
            rows = []
            for n in _all_projects():
                rows.extend(changelog_mod.entries(events.read_all(n),
                                                  project=n))
            rows.sort(key=lambda r: r["ts"] or "", reverse=True)
            pending = None
        else:
            rows = changelog_mod.entries(events.read_all(proj),
                                         project=proj)
            pending = changelog_mod.load_pending(proj)
        return self._json({"entries": rows,
                           "categories": list(changelog_mod.CATEGORIES),
                           "pending": pending})

    def _get_sops(self, proj, am, qs):
        # Managed SOP registry. Active set and history both delegate to
        # sop.py (one source of truth for fold/ordering), so this view
        # can never disagree with `hive sop list` / `hive sop show`.
        from . import sop as sop_mod
        if am:
            active, hist = [], []
            for n in _all_projects():
                evs = events.read_all(n)
                for r in sop_mod.current(evs):
                    active.append(dict(r, project=n))
                hist.extend(sop_mod.entries(evs, project=n))
            active.sort(key=lambda r: (r.get("created") or "",
                                       r.get("sop_id") or ""))
            hist.sort(key=lambda r: r["ts"] or "", reverse=True)
        else:
            evs = events.read_all(proj)
            active = sop_mod.current(evs)
            hist = sop_mod.entries(evs, project=proj)
        return self._json({"active": active, "history": hist,
                           "actions": list(sop_mod.ACTIONS)})

    def _get_task(self, proj, am, qs):
        sid = (qs.get("session") or [""])[0]
        # In HIVE MIND mode each card carries its own project — a concrete
        # namespace is required to read the right ledger.
        tproj = (qs.get("session_project") or [None])[0]
        if am and not tproj:
            return self._json({"ok": False,
                               "error": "session_project required"}, 400)
        evs = [e for e in events.read_all(tproj or proj)
               if e.session_id == sid]
        return self._json(taskview.items(evs))

    def _get_task_detail(self, proj, am, qs):
        sid = (qs.get("session") or [""])[0]
        return self._json(taskview.detail(sid))

    def _get_recall(self, proj, am, qs):
        q = (qs.get("q") or [""])[0]
        semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
        limit = int((qs.get("limit") or ["10"])[0])
        force = (qs.get("force") or ["0"])[0] in ("1", "true")
        if am:
            hits = []
            for n in _all_projects():
                for h in recall_mod.recall(q, project=n, semantic=semantic, limit=limit, force=force):
                    h = dict(h)
                    h["project"] = n
                    hits.append(h)
            return self._json(hits)
        return self._json(list(recall_mod.recall(q, project=proj, semantic=semantic, limit=limit, force=force)))

    def _get_graph(self, proj, am, qs):
        # Serve the persisted graph.json when it is still fresh (autosync
        # keeps it current), rebuilding only when events actually changed —
        # so re-opening the Graph tab is a cheap cache read, not a full
        # rebuild + 60 MB serialize + disk write. Semantic edges fold in the
        # embedding-derived semantic_near links but cost an embedding search
        # per event, so they are the opt-in path (?semantic=1), never the
        # default load.
        semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
        if am:
            # HIVE MIND aggregates every namespace; freshness is the newest
            # event across all of them (a cheap stat sweep), and the full
            # aggregate log is only read on a cache miss.
            src_mtime = max((graph_mod._events_mtime(n) for n in _all_projects()),
                            default=0.0)
            graph = graph_mod.load_or_build(
                project=HIVEMIND_PROJECT, semantic=semantic,
                source_mtime=src_mtime,
                build_evs=lambda: [e for _, e in all_event_pairs()])
        else:
            graph = graph_mod.load_or_build(proj, semantic=semantic)
        html = graphview.render_html(graph)
        return self._send(200, html, "text/html; charset=utf-8")

    def _get_plugins_recommend(self, proj, am, qs):
        if am:
            for n in _all_projects():
                cat = plugins_mod.latest_catalog(n)
                if cat:
                    return self._json(cat)
            return self._json({})
        return self._json(plugins_mod.latest_catalog(proj) or {})

    def _get_plugins_inventory(self, proj, am, qs):
        # machine-level (what THIS Claude install has), so project-agnostic
        return self._json(plugins_mod.live_inventory())

    def _get_skills_detail(self, proj, am, qs):
        name = (qs.get("name") or [""])[0]
        plugin = (qs.get("plugin") or [None])[0] or None
        detail = plugins_mod.skill_detail(name, plugin=plugin)
        if detail is None:
            return self._json({"ok": False, "error": "not found"}, 404)
        return self._json(detail)

    def _get_skills_status(self, proj, am, qs):
        # status: green/red last outcome per skill; counts: total invokes —
        # both derived from the ledger in a single pass. HIVE MIND merges
        # every namespace's ledger, mirroring /api/improvements — skills
        # telemetry must not read "*" as a literal (empty) namespace.
        evs = [e for _, e in all_event_pairs()] if am else None
        return self._json(plugins_mod.call_stats(project=proj, evs=evs))

    def _get_skills_history(self, proj, am, qs):
        name = (qs.get("name") or [""])[0]
        limit = int((qs.get("limit") or ["20"])[0])
        evs = [e for _, e in all_event_pairs()] if am else None
        return self._json(plugins_mod.call_history(name, project=proj,
                                                   limit=limit, evs=evs))

    def _get_mindnet_cats(self, proj, am, qs):
        # MN-5's cat board: which faculties are awake, dimming, or gone.
        # Imported lazily like the other mindnet touchpoints so a dashboard
        # in a project that has never used the phase spine pays nothing.
        #
        # `now` is resolved HERE and passed in, because `ui_cats.board` is
        # pure by design — that is what makes its 180 s and 6 h boundaries
        # testable at the boundary instead of approximately.
        from datetime import datetime, timezone

        from . import oversee as oversee_mod
        from .mindnet import telemetry as tel_mod
        from .mindnet import ui_cats as cats_mod
        from .mindnet import ui_sessions as sess_mod
        # ONE clock for both boards. The faculty side ages rows from an ISO
        # string and `oversee` decides liveness from an epoch float; reading
        # the clock twice would let a session straddle the two and render as
        # working on one axis and stale on the other in the same payload.
        stamp = time.time()
        now = datetime.fromtimestamp(stamp, timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        # THE gate. Measured on the real store this endpoint returned 503
        # bytes in 6.0 s under load — all of it `read_all` plus the two
        # folds, on a route every open page polls every 2.5 s. An unchanged
        # ledger cannot change either fold, so a hit reads nothing and folds
        # nothing.
        #
        # `segment_stats` is the same stat-only fingerprint the phase sidecar
        # already answers staleness with: `os.scandir`'s cached stat data,
        # microseconds, never opens or parses a byte.
        #
        # THREE properties are load-bearing, and the cache is worse than no
        # cache without any one of them:
        #   * An EMPTY fingerprint is an unconditional MISS, and so is a
        #     NON-FILE BACKEND. Both now live in `_ledger_fingerprint`, which
        #     returns None for either — one place, so the pair cannot be
        #     half-applied by the next caller that wants this gate. Their
        #     reasoning is there; the gap that shipped is recorded below.
        #   * HIVE MIND (am) bypasses the gate outright. It reads EVERY
        #     namespace through `all_event_pairs`, and `segment_stats` is
        #     per-project — a single-project stamp would miss an append in
        #     any other namespace, and would miss a whole new namespace
        #     appearing. Stamping every namespace is possible but is a
        #     scandir each plus a re-listing to notice new ones; bypassing is
        #     unconditionally correct, and this is the rarer path.
        #
        # The non-file-backend guard was specified alongside the {} one and
        # did not ship: the comment here said "two properties are
        # load-bearing" and named a different second, so the gap read as
        # completeness. That is the failure mode this whole plan exists to
        # remove, so it is named rather than fixed quietly — and both guards
        # now sit behind one helper so the next gate inherits them.
        #
        # What is memoized is the two FOLDS, never the response: `now` and
        # `online` are functions of the wall clock and are re-derived below
        # on every call, hit or miss (see `_with_online`).
        fp = None if am else _ledger_fingerprint(proj)
        memo = _memo_get(_CATS_MEMO, proj) if fp else None
        if memo is not None and memo[0] == fp:
            tel_state, records, have_sessions = memo[1], memo[2], True
        else:
            # ONE ledger read for both folds. They ask different questions of
            # the SAME events, and `interactive_sessions` takes `evs` for
            # exactly this reason — re-reading would double the cost of the
            # miss against a store measured in tens of thousands of events.
            evs = [e for _, e in all_event_pairs()] if am else \
                events.read_all(proj)
            tel_state = tel_mod.fold(evs)
            # The session half is isolated. `oversee.interactive_sessions` is
            # not documented as total and it folds every event twice
            # internally — letting it raise would 500 the whole endpoint and
            # take the faculty strip, which was reliable before this, down
            # with it. The page's own rule is that a failing board must not
            # take out the page; the server owes it the same.
            try:
                records = _sessions_cached(proj, am, evs, stamp)
                have_sessions = True
            except Exception:               # noqa: BLE001
                records, have_sessions = [], False
            # Only a fold that SUCCEEDED is memoized. Storing the degraded
            # answer would pin "cannot know" behind the gate until the ledger
            # next moved; re-attempting costs exactly what this endpoint cost
            # before the gate existed, which is the honest price of a fault.
            if fp and have_sessions:
                _memo_put(_CATS_MEMO, proj, (fp, tel_state, records))
        # Both boards render from THIS call's clock, whatever the gate did.
        # `_with_online` runs unconditionally — on a miss it is a redundant
        # (idempotent) second pass over a few dozen rows, which is the price
        # of there being exactly one place that can freeze `online`.
        cats = cats_mod.board(tel_state, now)
        try:
            sessions = sess_mod.board(_with_online(records, stamp), now)
        except Exception:                       # noqa: BLE001
            sessions = {"cats": (), "total": 0, "hidden": 0}
            have_sessions = False
        producers = tel_mod.producers()
        # Report the session signal as unobservable when the fold that feeds
        # it just failed, so the strip says "cannot know" instead of the
        # confident "nothing running" — the exact distinction this map exists
        # to make.
        producers["session"] = producers.get("session", False) and have_sessions
        # The thresholds ride along so the page renders "idle 4h" without a
        # second copy of 180/21600 in JavaScript (invariant 10).
        return self._json({"cats": list(cats),
                           "sessions": list(sessions["cats"]),
                           "sessions_total": sessions["total"],
                           "sessions_hidden": sessions["hidden"],
                           # Which signals this BUILD can observe at all, so
                           # an empty strip can say "nothing running" rather
                           # than being mistaken for "cannot know".
                           "producers": producers,
                           "dim_after_secs": cats_mod.DIM_AFTER_SECS,
                           "hide_after_secs": cats_mod.HIDE_AFTER_SECS,
                           "idle_vanish_secs": cats_mod.IDLE_VANISH_SECS,
                           "online_after_secs":
                               oversee_mod.ONLINE_WINDOW_SECONDS})

    def _get_skills_stats(self, proj, am, qs):
        # per-skill completion-rate telemetry aggregated from the ledger
        # (skill_used / invoke_result / tool_failed skill:*) — U5/KTD5
        evs = [e for _, e in all_event_pairs()] if am else None
        return self._json({"stats": plugins_mod.skill_stats(project=proj,
                                                            evs=evs)})

    def _get_skills_recommend(self, proj, am, qs):
        # deterministic most-needed-skills ranking (U6/KTD6): one ledger
        # read feeds both the stats and the need extraction. HIVE MIND
        # merges events AND incident summaries across every namespace.
        top = (qs.get("top") or [None])[0]
        evs = summ = None
        if am:
            from . import incidents
            evs = [e for _, e in all_event_pairs()]
            summ = [s for n in _all_projects()
                    for s in incidents.summaries(project=n)]
        return self._json(plugins_mod.recommend_for(
            project=proj, evs=evs, top=int(top) if top else None,
            incident_summaries=summ))

    def _get_skills_trail(self, proj, am, qs):
        # Durable per-session skill trail (agent-native parity with the CLI
        # and dashboard cards) — reads the SAME shared fold Oversee uses, so
        # ended/stale sessions stay auditable after they leave the board.
        # HIVE MIND merges every namespace like the other skills endpoints.
        from . import oversee as oversee_mod
        iu = oversee_mod.intended_use_map()
        session = (qs.get("session") or [None])[0] or None
        names = _all_projects() if am else [proj]
        out = {}
        for n in names:
            for sid, entries in oversee_mod.skill_trails(
                    project=n, intended_use=iu).items():
                if session and sid != session:
                    continue
                out[sid] = entries
        return self._json({"trails": out})

    def _get_oversee(self, proj, am, qs):
        # Oversee: every kind of live Claude work — hive loop runs AND
        # interactive sessions (straight from hook telemetry, no /circle
        # needed). Dismissed/finished/stale entries are already excluded
        # by the projections. Each card carries its roadmap plan (the ask
        # + plan steps + progress, see roadmap.py) when one exists for its
        # run/session id; plans with no live card — notably interrupted
        # ones whose run already left the board — ride the top-level
        # ``plans`` list so a roadmap is never orphaned.
        #
        # DELIBERATELY NOT MEMOIZED, unlike its neighbour /api/overview, and the
        # reason is worth having here rather than rediscovering. The fingerprint
        # is not the problem — both producers below ARE pure folds over
        # `read_all`, so `segment_stats` would key this correctly. The problem is
        # that the WALL CLOCK decides three separate things INSIDE the fold, and
        # a cached fold freezes a board rather than a number:
        #   1. a loop run's `online` — `status == "running"` and last event
        #      within that run's own `heartbeat_seconds` (default 15 min + 60 s),
        #      decided in `loop.state.list_runs`;
        #   2. a chat session's `online` — last event within
        #      `oversee.ONLINE_WINDOW_SECONDS` (180 s);
        #   3. whether a chat session is on the board AT ALL — idle past
        #      `ACTIVE_WINDOW_SECONDS` (6 h) and it is dropped by the fold.
        # `dashboard_memo._with_online` is the seam for (2) only. (1) and (3)
        # need their own, and `list_runs` additionally reads each run's fix-plan
        # file off `goal_dir` — the one genuine off-ledger input here. All of it
        # is fixable; none of it is fixed, so this endpoint is still a full read
        # per poll. TODOS item 17 carries the plan.
        from . import oversee as oversee_mod
        from . import roadmap as roadmap_mod
        from .loop import state as loop_state
        names = _all_projects() if am else [proj]
        # Built once per request (60 s memoized) so every card's skill trail
        # gets the intended-use "why" fallback without a per-session rescan.
        iu = oversee_mod.intended_use_map()
        runs, plans, dismissed = [], [], 0
        for n in names:
            evs = events.read_all(n)
            res = loop_state.list_runs(n)
            rmap = roadmap_mod.fold(evs)
            live_ids = set()
            for r in res["runs"] + oversee_mod.interactive_sessions(
                    n, evs=evs, intended_use=iu):
                r["project"] = n
                plan = rmap.get(r["run_id"])
                if plan is not None and plan["status"] != "completed":
                    r["roadmap"] = plan
                live_ids.add(r["run_id"])
                runs.append(r)
            for p in roadmap_mod.plans(evs):
                if p["plan_id"] not in live_ids:
                    plans.append(dict(p, project=n))
            dismissed += res["dismissed"]
        runs.sort(key=lambda r: r.get("started") or "", reverse=True)
        plans.sort(key=lambda p: p.get("updated") or "", reverse=True)
        return self._json({"runs": runs, "dismissed": dismissed,
                           "plans": plans,
                           "any_online": any(r["online"] for r in runs)})

    def _get_workspace(self, proj, am, qs):
        # /api/workspace/<action>: the two iframe pages render HTML; every
        # other action is JSON, delegated to workspace.api_get. HIVE MIND
        # (am) falls back to the default project — workspace agents always
        # need a concrete namespace to log their I/O into.
        action = urlparse(self.path).path.rsplit("/", 1)[-1]
        if action in ("parallel", "swarm"):
            html = (render_parallel(self._token) if action == "parallel"
                    else render_swarm(self._token))
            return self._send(200, html, "text/html; charset=utf-8")
        return self._json(workspace_mod.api_get(
            action, proj=default_project() if am else proj, qs=qs))

    def _get_research(self, proj, am, qs):
        # /api/research/<action>: "page" renders the Self-Reliant iframe
        # view; everything else is JSON via research.api_get. Like the
        # workspace, HIVE MIND falls back to the default project so run
        # events land in a concrete namespace.
        action = urlparse(self.path).path.rsplit("/", 1)[-1]
        if action == "page":
            return self._send(200, render_research(self._token),
                              "text/html; charset=utf-8")
        return self._json(research_mod.api_get(
            action, proj=default_project() if am else proj, qs=qs))

    def _get_export(self, proj, am, qs):
        path = export_mod.export()
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="handoff.md"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return

    def _get_enrich_settings(self, proj, am, qs):
        # Prompt enrichment toggle (default ON — see enrich.py). HIVE MIND
        # reads the default project's setting: the panel is hidden under
        # "*", but the fetch must still answer coherently.
        from . import enrich as enrich_mod
        return self._json(enrich_mod.settings(
            default_project() if am else proj))

    def _get_job(self, proj, am, qs):
        """Poll one job. `offset` is how many output bytes the caller already
        has, so a 2.5s poll carries only what is new — the whole buffer used
        to be re-sent every tick (T16).

        An unknown id is ``ok:False`` with a 200, not a 404: the honest reason
        is almost always that the server restarted, and the page needs to say
        that rather than render a transport error.
        """
        job_id = (qs.get("id") or [""])[0]
        job = jobs.get(job_id)
        if job is None:
            return self._json({"ok": False, "error":
                "no such job — the dashboard may have restarted since it "
                "started"}, 200)
        offset = (qs.get("offset") or ["0"])[0]
        return self._json({"ok": True, "job": job.snapshot(offset)})

    # --- route tables (path -> handler) -------------------------------
    # Defined after the handlers so the names resolve; dispatched once by
    # do_GET / do_POST in place of a long if/elif ladder.
    _GET_ROUTES = {
        "/api/projects": _get_projects,
        "/api/state": _get_state,
        "/api/briefing": _get_briefing,
        "/api/verify": _get_verify,
        "/api/overview": _get_overview,
        "/api/memory": _get_memory,
        "/api/improvements": _get_improvements,
        "/api/sessions": _get_sessions,
        "/api/task": _get_task,
        "/api/task_detail": _get_task_detail,
        "/api/oversee": _get_oversee,
        "/api/changelog": _get_changelog,
        "/api/sops": _get_sops,
        "/api/recall": _get_recall,
        "/api/graph": _get_graph,
        "/api/mindnet/cats": _get_mindnet_cats,
        "/api/plugins/recommend": _get_plugins_recommend,
        "/api/plugins/inventory": _get_plugins_inventory,
        "/api/skills/detail": _get_skills_detail,
        "/api/skills/status": _get_skills_status,
        "/api/skills/history": _get_skills_history,
        "/api/skills/stats": _get_skills_stats,
        "/api/skills/trail": _get_skills_trail,
        "/api/skills/recommend": _get_skills_recommend,
        "/api/enrich/settings": _get_enrich_settings,
        "/api/export": _get_export,
        "/api/job": _get_job,
    }
    for _p in ("parallel", "swarm", "agents", "output", "profiles",
               "auth", "board"):
        _GET_ROUTES[f"/api/workspace/{_p}"] = _get_workspace
    del _p
    for _p in ("page", "status", "leaderboard", "output", "learnings",
               "prds", "sources"):
        _GET_ROUTES[f"/api/research/{_p}"] = _get_research
    del _p
