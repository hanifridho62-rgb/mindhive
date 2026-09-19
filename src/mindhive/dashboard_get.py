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

from . import chain, dashboard_get_v1, dedupe, events, jobs, paths, taskview
from . import export as export_mod
from . import graph as graph_mod
from . import graphview
from . import plugins as plugins_mod
from . import recall as recall_mod
from . import research as research_mod
from . import workspace as workspace_mod
from .briefing import briefing as build_briefing
from .dashboard_data import (HIVEMIND_LABEL, HIVEMIND_PROJECT, _add_tallies,
                             _add_tallies_rows, _all_projects, _efficiency,
                             _empty_tallies, _project_stats, _safe_chain_ok,
                             _tool_action_rows, all_event_pairs,
                             default_project, event_pairs, injection_status,
                             overview_stats)
from .dashboard_locks import _VERIFY_LOCK, _one_at_a_time
from .dashboard_query import is_safe_project, qs_int
from .dashboard_memo import (_CATS_MEMO, _changelog_cached, _improve_cached,
                             _intended_use_digest, _ledger_fingerprint,
                             _manual_collapsed_cached, _memo_get, _memo_put,
                             _memory_rows_cached, _oversee_cached,
                             _recommend_cached, _session_rows_cached,
                             _sessions_cached, _skill_calls_cached,
                             _skill_history_cached, _skill_stats_cached,
                             _sops_cached, _task_cards_cached, _with_liveness,
                             _with_online)
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
        # `injection` rides the response but NOT the memo: it is a fact about the
        # SOP sidecar, not about this namespace's ledger, so a cached copy would
        # freeze the one field whose job is to report a degradation. None when
        # healthy — see `injection_status`.
        injection = injection_status()
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
                "injection": injection,
            })
        return self._json({"project": proj, **overview_stats(proj),
                           "injection": injection})

    def _get_memory(self, proj, am, qs):
        # the semantic subset only — the intent/context layer. Build the
        # (project, Event) rows once so the projection + dedupe.collapse lives
        # in a single place instead of once per am / single-project branch.
        #
        # Gated per project on the ledger fingerprint. The TYPE filter is a pure
        # fold, and it was reading the whole ledger on every 2.5 s tick with the
        # Memory section open — measured warm at 482 ms, the same as cold.
        # `session` and `limit` cut the cached list afterwards, so they stay out
        # of the key.
        from . import roadmap as roadmap_mod
        session = (qs.get("session") or [None])[0]
        limit = qs_int(qs, "limit", 200)

        def memory_pairs(n):
            return [(n, e) for e in events.read_all(n)
                    if e.type in events.MEMORY_VIEW_TYPES]

        if am:
            rows = []
            for n in _all_projects():
                rows.extend(_memory_rows_cached(n, lambda n=n: memory_pairs(n)))
            # `all_event_pairs` sorts the unified stream by ts and the filter
            # preserved that order; sorting the concatenation restores it. Python
            # sorts stably, so ties still fall in `_all_projects()` order — which
            # is the order they were concatenated in before, too.
            rows.sort(key=lambda pe: pe[1].ts or "")
        else:
            rows = _memory_rows_cached(proj, lambda: memory_pairs(proj))
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
            return e.payload.get("text", "") or dashboard_get_v1.memory_text(e)
        # Collapse near-duplicate memories by meaning (same type + project),
        # keeping the newest; display-time only, ledger untouched.
        # similar_count rides along for the UI hint.
        return self._json(dedupe.collapse([
            {"ts": e.ts, "type": e.type, "session": e.session_id, "project": n,
             "text": _text(e), "tags": e.tags,
             "origin": events.origin_of(e.session_id, e.tags)}
            for n, e in rows
        ]))

    def _improvements_fold(self, n):
        """One project's Improvements fold, and every part of it is a pure
        function of that project's ledger and incident records — no clock, so it
        is cacheable as a whole. `signal_stats`, `classify_resolution` and
        `classify_triage` were each checked for a clock read before this was
        split out; the one time-dependent part of the response, the efficiency
        tiles, is cut from `rows` by the caller at request time."""
        from . import incidents
        evs = events.read_all(n)
        stats_map = incidents.signal_stats(n, evs=evs)
        auto = []
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
        manual = []
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
        return {"auto": auto, "manual": manual,
                "rows": _tool_action_rows(evs)}

    def _get_improvements(self, proj, am, qs):
        # Lessons learned: machine-learned incident lessons (auto) plus
        # user-dictated caveat notes (manual), kept as two shapes so long
        # status memos can't masquerade as lesson cards. One event pass per
        # project feeds the caveats, the per-signature enrichment
        # (timestamps, leaks, resolution glyph), and the efficiency tiles.
        #
        # Gated, because this was the last polled route reading the WHOLE ledger
        # on every tick: measured on the real store, 415 ms of `read_all` over
        # 44951 events plus 118 ms of `summaries` over 155 signatures, and
        # `renderPlugins` asks for it every 2.5 s while Skills is open. The
        # fingerprint carries the incident FILES as well as the ledger, because
        # a reflection pass rewrites a lesson without appending an event.
        names = _all_projects() if am else [proj]
        now = time.time()
        auto, manual = [], []
        tallies = _empty_tallies()
        for n in names:
            fold = _improve_cached(n, lambda n=n: self._improvements_fold(n))
            # Copies out of the cached lists: the sort below is in place, and
            # the entry is shared with every later poll.
            auto.extend(fold["auto"])
            manual.extend(fold["manual"])
            _add_tallies_rows(tallies, fold["rows"], now)
        auto.sort(key=lambda s: s.get("occurrences", 0), reverse=True)
        manual.sort(key=lambda m: m.get("ts") or "", reverse=True)
        # The collapse is the last expensive thing on this route: 76 ms of the
        # ~108 ms it still cost warm once the fold itself was gated. On the
        # single-project path its input is a deterministic sort of an
        # already-cached list, so it carries the same stamp.
        #
        # HIVE MIND collapses the MERGED list and is left uncached. Not because
        # the representatives would differ — `dedupe.collapse` clusters on
        # `(type, project)` and every manual row carries its project, so two
        # namespaces can never merge — but because of their ORDER: collapse
        # emits representatives in input order, so the merged list yields one
        # global newest-first sequence while a per-namespace concatenation
        # yields them grouped by project. Same lessons, different list.
        collapsed = (dedupe.collapse(manual) if am else
                     _manual_collapsed_cached(proj,
                                              lambda: dedupe.collapse(manual)))
        return self._json({"auto": auto, "manual": collapsed,
                           "stats": _efficiency(tallies)})

    def _get_sessions(self, proj, am, qs):
        # Gated per project on the ledger fingerprint: this fold read the whole
        # ledger on every 2.5 s tick with the Sessions section open — measured
        # warm at 444 ms, the same as cold. Everything cached here is a pure
        # function of one project's events; `status` is not, and is re-derived
        # below onto a copy.
        def fold(n):
            by: dict = {}
            for e in events.read_all(n):
                s = by.get(e.session_id)
                if s is None:
                    s = by[e.session_id] = {
                        "session_id": e.session_id, "project": n,
                        "events": 0, "memory": 0, "origin": None,
                        "first_ts": e.ts, "last_ts": e.ts, "_evs": []}
                s["events"] += 1
                s["_evs"].append(e)
                if not s["origin"]:
                    s["origin"] = events.origin_of(e.session_id, e.tags)
                if e.type in events.MEMORY_VIEW_TYPES:
                    s["memory"] += 1
                if e.type in events.BOOKKEEPING_TYPES and s["events"] > 1:
                    continue           # never re-dates a session (as on Oversee)
                if e.ts and e.ts < s["first_ts"]:
                    s["first_ts"] = e.ts
                if e.ts and e.ts > s["last_ts"]:
                    s["last_ts"] = e.ts
            rows = list(by.values())
            for s in rows:
                s["activity"] = taskview.tally(s.pop("_evs"))
            return rows

        # Grouping was already keyed on (project, session_id), so folding one
        # project at a time groups identically.
        rows = []
        for n in (_all_projects() if am else [proj]):
            rows.extend(_session_rows_cached(n, lambda n=n: fold(n)))
        # The ORDER is reproduced, not approximated. `sorted(reverse=True)` is
        # stable, so the old list's ties fell in the dict's insertion order —
        # which was each session's FIRST appearance in one globally ts-sorted
        # stream, i.e. `first_ts`, ties broken by namespace order and then by
        # position within that namespace. These rows arrive concatenated in
        # exactly that nesting, so one stable sort on `first_ts` restores the
        # same sequence before the `last_ts` sort runs over it.
        #
        # Skipping this reordered the HIVE MIND list at row 508 of 2101 on the
        # real store: two sessions sharing `2026-08-10T00:27:10Z` swapped, which
        # is a visible change to a view whose contents did not change.
        rows.sort(key=lambda s: s["first_ts"] or "")
        out = sorted(rows, key=lambda s: s["last_ts"] or "", reverse=True)
        # `status` is a function of the WALL CLOCK, so it is derived per response
        # onto a copy — storing it would freeze a session as active forever.
        return self._json([dict(s, status=taskview.status_of(s["last_ts"]))
                           for s in out])

    def _get_changelog(self, proj, am, qs):
        # Mandatory code change-log. Shape and ordering delegate to
        # changelog.entries() — one source of truth, so this view can
        # never disagree with `hive log show`. The pending marker is a
        # per-project sidecar, so HIVE MIND shows entries without a
        # pending banner; categories ride along so the UI never hardcodes
        # the taxonomy.
        #
        # Gated per project: the fold is pure over one ledger, and it was reading
        # all of it on every 2.5 s tick while the Log section is open — measured
        # warm at 407 ms against the real store, the same as cold. The pending
        # marker stays outside the gate; it is a sidecar the fingerprint cannot
        # see move.
        from . import changelog as changelog_mod

        def fold(n):
            return changelog_mod.entries(events.read_all(n), project=n)

        if am:
            rows = []
            for n in _all_projects():
                rows.extend(_changelog_cached(n, lambda n=n: fold(n)))
            rows.sort(key=lambda r: r["ts"] or "", reverse=True)
            pending = None
        else:
            # A copy, because the entry is shared with every later poll and the
            # response is handed on to callers that may sort it in place.
            rows = list(_changelog_cached(proj, lambda: fold(proj)))
            pending = changelog_mod.load_pending(proj)
        return self._json({"entries": rows,
                           "categories": list(changelog_mod.CATEGORIES),
                           "pending": pending})

    def _get_sops(self, proj, am, qs):
        # Managed SOP registry. Active set and history both delegate to
        # sop.py (one source of truth for fold/ordering), so this view
        # can never disagree with `hive sop list` / `hive sop show`.
        #
        # Gated for `_get_changelog`'s reason and by the same measurement: 433 ms
        # warm, identical to cold, every 2.5 s while the SOPs section is open.
        from . import sop as sop_mod

        def fold(n):
            evs = events.read_all(n)
            return sop_mod.current(evs), sop_mod.entries(evs, project=n)

        if am:
            active, hist = [], []
            for n in _all_projects():
                cur, ents = _sops_cached(n, lambda n=n: fold(n))
                active.extend(dict(r, project=n) for r in cur)
                hist.extend(ents)
            active.sort(key=lambda r: (r.get("created") or "",
                                       r.get("sop_id") or ""))
            hist.sort(key=lambda r: r["ts"] or "", reverse=True)
        else:
            # Copies for `_get_changelog`'s reason: both lists are shared with
            # every later poll of this project.
            cur, ents = _sops_cached(proj, lambda: fold(proj))
            active, hist = list(cur), list(ents)
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
        # Refused by name, like `project=` at the router: this parameter picks
        # the ledger `events.read_all` folds below, and it was the one project
        # value on the route that reached the store without the check.
        if tproj and not is_safe_project(tproj):
            return self._json({"ok": False,
                               "error": "invalid session_project"}, 400)
        # One grouped fold serves every card in the namespace (see
        # `_task_cards_cached`). A session the fold never saw answers exactly
        # as the old per-session filter did — `taskview.items([])` — so an
        # unknown id still gets an empty card rather than a 404.
        name = tproj or proj

        def fold(n):
            by_sid = {}
            for ev in events.read_all(n):
                by_sid.setdefault(ev.session_id, []).append(ev)
            return {k: taskview.items(rows) for k, rows in by_sid.items()}

        cards = _task_cards_cached(name, lambda: fold(name))
        return self._json(cards.get(sid) or taskview.items([]))

    def _get_task_detail(self, proj, am, qs):
        sid = (qs.get("session") or [""])[0]
        return self._json(taskview.detail(sid))

    def _get_recall(self, proj, am, qs):
        q = (qs.get("q") or [""])[0]
        semantic = (qs.get("semantic") or ["0"])[0] in ("1", "true")
        limit = qs_int(qs, "limit", 10)
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
        #
        # Gated on the single-project path, which is the one the board takes:
        # measured at 378 ms warm for a 28-byte response, all of it `read_all`.
        # HIVE MIND folds a MERGED event list and cannot be composed from
        # per-project folds — "last outcome per skill" across namespaces is not
        # the merge of each namespace's last outcome — so that path is left
        # reading, and says so rather than pretending to be covered.
        if am:
            evs = [e for _, e in all_event_pairs()]
            return self._json(plugins_mod.call_stats(project=proj, evs=evs))
        return self._json(_skill_calls_cached(
            proj, lambda: plugins_mod.call_stats(project=proj)))

    def _get_skills_history(self, proj, am, qs):
        # One fold per namespace serves every expanded skill. The board
        # re-fetches this for EACH expanded skill on every 2.5 s tick, and each
        # one used to be its own full `read_all` — three skills open meant three
        # whole-ledger reads a tick. `limit` slices the cached list rather than
        # being part of the key, so a second limit costs nothing either.
        name = (qs.get("name") or [""])[0]
        limit = qs_int(qs, "limit", 20)
        if am:
            evs = [e for _, e in all_event_pairs()]
            return self._json(plugins_mod.call_history(name, project=proj,
                                                       limit=limit, evs=evs))

        def fold(n):
            by_name = {}
            for ev in events.read_all(n):
                p = ev.payload if isinstance(ev.payload, dict) else {}
                if ev.type == "invoke_result":
                    key = p.get("name")
                    row = {"ts": ev.ts, "ok": True, "text": p.get("text", ""),
                           "session": ev.session_id}
                elif ev.type == "tool_failed":
                    sig = str(p.get("signature") or "")
                    key = sig[6:] if sig.startswith("skill:") else None
                    row = {"ts": ev.ts, "ok": False, "text": p.get("text", ""),
                           "session": ev.session_id}
                else:
                    continue
                if key is None:
                    continue
                by_name.setdefault(key, []).append(row)
            # `call_history` folds chronologically and reverses, which keeps
            # append order on equal timestamps. Reversing each list here is the
            # same operation on the same sequence.
            for rows in by_name.values():
                rows.reverse()
            return by_name

        hist = _skill_history_cached(proj, lambda: fold(proj))
        return self._json(hist.get(name, [])[:max(0, limit)])

    def _get_injection(self, proj, am, qs):
        # What the six UserPromptSubmit injectors delivered, per turn.
        #
        # ITS OWN ROUTE rather than a field on /api/oversee, and the reason is
        # not tidiness: that board is a fold over the SESSIONS on it, while an
        # injection belongs to a TURN, and the newest turn routinely belongs to
        # a session with no tool events yet -- one that the board has therefore
        # not drawn. Folding this in would drop exactly the most recent case,
        # which is the one the operator opened the page to see.
        #
        # No gate needed and none wanted: both reads below are one O(1) sidecar
        # file. That is the whole point of keeping the ring off the ledger --
        # `/api/mindnet/cats` had to be gated only because it folded `read_all`
        # on a route every open page polls.
        from . import injectors as inj_mod
        return self._json({"ok": True,
                           "current": inj_mod.read_last(proj),
                           "recent": inj_mod.recent_turns(proj)})

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
        from . import roadmap as roadmap_mod
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
            tel_state, records, plan_index = memo[1], memo[2], memo[3]
            directions = memo[4]
            have_sessions = True
        else:
            # ONE ledger read for both folds. They ask different questions of
            # the SAME events, and `interactive_sessions` takes `evs` for
            # exactly this reason — re-reading would double the cost of the
            # miss against a store measured in tens of thousands of events.
            evs = [e for _, e in all_event_pairs()] if am else \
                events.read_all(proj)
            tel_state = tel_mod.fold(evs)
            # THIRD fold on the same read, and the cheapest of them: it is
            # what lets a session cat answer how far along its plan is. The
            # plan lives two API calls away from this strip, and a phase that
            # is only correct on the Oversee card is the kind of half-wired
            # surface this endpoint's own comments were written about.
            rmap = roadmap_mod.fold(evs)
            idx = roadmap_mod.by_session(rmap)
            # FOURTH fold on the same read, and it must be memoized WITH the
            # others: the panel's `skill` row is a join between a faculty and
            # what its own session directed, and computing it outside the memo
            # would fill the row on a miss and blank it on every hit after —
            # a field that flickers empty on a 2.5 s timer.
            directions = cats_mod.directions_by_session(evs)
            # Resolved exactly as the Oversee card resolves it, through the
            # one helper -- the cat used to consult the adoption index alone,
            # so a session with a plan of its own got a phase on the card and
            # nothing (or a different plan) on the cat.
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
            # Built from `records`, so it has to come after the fold that
            # produces them -- and before the memo that stores the pair, so
            # a hit serves a plan_index that matches its own session list.
            plan_index = {}
            for row in records:
                # `run_id`, not `session_id`: these rows are the union of
                # loop runs and chat sessions, and `ui_sessions.board`
                # reads the same key to look the plan back up. Keying this
                # index on anything else builds it empty in silence.
                sid = row.get("run_id")
                if not sid:
                    continue
                plan = roadmap_mod.plan_for_session(rmap, sid, idx)
                if plan is not None:
                    plan_index[sid] = plan
            # Only a fold that SUCCEEDED is memoized. Storing the degraded
            # answer would pin "cannot know" behind the gate until the ledger
            # next moved; re-attempting costs exactly what this endpoint cost
            # before the gate existed, which is the honest price of a fault.
            if fp and have_sessions:
                _memo_put(_CATS_MEMO, proj,
                          (fp, tel_state, records, plan_index, directions))
        # Both boards render from THIS call's clock, whatever the gate did.
        # `_with_online` runs unconditionally — on a miss it is a redundant
        # (idempotent) second pass over a few dozen rows, which is the price
        # of there being exactly one place that can freeze `online`.
        # `skills=` is the overlay `board` has documented since MN-5 and that no
        # production caller ever supplied, so the click-through panel's `skill`
        # row rendered an em-dash for every faculty on both pages — a field that
        # reads "nothing to report" while meaning "never wired", which is the
        # one failure this board exists to avoid.
        cats = cats_mod.board(tel_state, now,
                             skills=cats_mod.skill_overlay(tel_state, directions))
        try:
            sessions = sess_mod.board(_with_online(records, stamp),
                                      now, plans=plan_index)
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
        #
        # Gated for `_get_skills_status`'s reason and by the same measurement:
        # 407 ms warm, identical to cold, on every visit to the section.
        if am:
            evs = [e for _, e in all_event_pairs()]
            return self._json({"stats": plugins_mod.skill_stats(project=proj,
                                                                evs=evs)})
        return self._json({"stats": _skill_stats_cached(
            proj, lambda: plugins_mod.skill_stats(project=proj))})

    def _get_skills_recommend(self, proj, am, qs):
        # deterministic most-needed-skills ranking (U6/KTD6): one ledger
        # read feeds both the stats and the need extraction. HIVE MIND
        # merges events AND incident summaries across every namespace.
        #
        # Gated on the single-project path: `renderPlugins` is the Skills
        # section's LIVE function, so this ran every 2.5 s at 641 ms a tick.
        # The stamp covers all three of its inputs (see `_recommend_fingerprint`),
        # not just the ledger. HIVE MIND merges events and incidents across every
        # namespace and is left reading, like the other skills endpoints.
        top = (qs.get("top") or [None])[0]
        top = int(top) if top else None
        if am:
            from . import incidents
            evs = [e for _, e in all_event_pairs()]
            summ = [s for n in _all_projects()
                    for s in incidents.summaries(project=n)]
            return self._json(plugins_mod.recommend_for(
                project=proj, evs=evs, top=top, incident_summaries=summ))
        return self._json(_recommend_cached(
            proj, top,
            lambda: plugins_mod.recommend_for(project=proj, top=top)))

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
        # GATED like its neighbour /api/overview, but the gate needed more than
        # a fingerprint to be safe here, and that is why this endpoint was the
        # last one to get one. Both producers below ARE pure folds over
        # `read_all`, so `segment_stats` keys them correctly; the obstacle was
        # that the WALL CLOCK decides three separate things INSIDE the fold, so
        # a fold cached as-is freezes a BOARD rather than a number:
        #   1. a loop run's `online` — `status == "running"` and last event
        #      within that run's OWN `heartbeat_seconds` (default 15 min + 60 s);
        #   2. a chat session's `online` — last event within
        #      `oversee.ONLINE_WINDOW_SECONDS` (180 s);
        #   3. whether a chat session is on the board AT ALL — idle past
        #      `ACTIVE_WINDOW_SECONDS` (6 h) and the fold drops it.
        # All three are re-derived on the way out by `_with_liveness`, which
        # DISPATCHES on `kind`: `_with_online` was the seam for (2) alone, and
        # applying it to a loop row would call a live 15-minute run offline
        # twelve times too early. (3) is a drop rather than a field, which is
        # safe over a cached fold in one direction only — ageing can only remove
        # a card — so `_oversee_cached` refuses an entry folded after this
        # stamp. The plan split has to be computed from the SURVIVING rows for
        # the same reason.
        #
        # Two off-ledger inputs join the fingerprint because nothing appends an
        # event when they change: each run's `fix_plan.md`, read off `goal_dir`
        # by `list_runs`, and the plugin inventory's intended-use map, which
        # feeds `skill_trails` inside the fold.
        from . import oversee as oversee_mod
        from . import roadmap as roadmap_mod
        from .loop import state as loop_state
        names = _all_projects() if am else [proj]
        # Built once per request (60 s memoized) so every card's skill trail
        # gets the intended-use "why" fallback without a per-session rescan.
        iu = oversee_mod.intended_use_map()
        iu_digest = _intended_use_digest(iu)
        stamp = time.time()

        def fold_board(n):
            evs = events.read_all(n)
            # ONE read, folded four ways. `list_runs` had no `evs=` parameter,
            # so this endpoint read the same ledger twice per namespace.
            res = loop_state.list_runs(n, evs=evs, now_fn=lambda: stamp)
            rmap = roadmap_mod.fold(evs)
            # A plan belongs to whoever is WORKING it, and that is not
            # always the session that created it: `roadmap.auto_plan_id`
            # lets a later session continue a paused plan, and the fold
            # stamps `session_id` with whoever last wrote to it. Matching on
            # plan_id alone made one plan invisible on the live card and
            # stranded in the awaiting-resume list at the same time -- the
            # pile that grows while nothing appears to move.
            by_session = roadmap_mod.by_session(rmap)
            rows = []
            for r in res["runs"] + oversee_mod.interactive_sessions(
                    n, evs=evs, intended_use=iu, now_fn=lambda: stamp):
                r["project"] = n
                plan = roadmap_mod.plan_for_session(
                    rmap, r["run_id"], by_session)
                # The helper already refuses a retired plan; the guard that
                # stood here tested the string "completed" and let an
                # ARCHIVED one through.
                if plan is not None:
                    r["roadmap"] = plan
                rows.append(r)
            return {"runs": rows,
                    "plans": [dict(p, project=n)
                              for p in roadmap_mod.plans(evs)],
                    "dismissed": res["dismissed"],
                    # The entry's own fingerprint input: these are the files
                    # `_fix_plan_items` read to build it.
                    "goal_dirs": tuple(sorted({r["goal_dir"] for r in res["runs"]
                                               if r.get("goal_dir")}))}

        runs, plans, dismissed = [], [], 0
        for n in names:
            board = _oversee_cached(n, iu_digest, stamp,
                                    lambda n=n: fold_board(n))
            live = _with_liveness(board["runs"], stamp)
            # Keyed by PLAN id, not run id — the two coincided only while the
            # match was identity — and computed from the rows that SURVIVED the
            # clock. When ageing takes a card off the board it has to take its
            # plan out of this set too, or the roadmap is orphaned in neither
            # list: gone from the board with its card, and absent from the
            # awaiting-resume list that exists to catch exactly that.
            live_plans = {r["roadmap"]["plan_id"] for r in live
                          if r.get("roadmap")}
            runs.extend(live)
            plans.extend(p for p in board["plans"]
                         if p["plan_id"] not in live_plans)
            dismissed += board["dismissed"]
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
        """The handoff for the project the panel is SHOWING.

        `export_mod.export()` was called with no argument, so it defaulted to
        `paths.project_dir(None)` -> `detect_project()` — the namespace derived
        from the dashboard PROCESS's cwd, which has nothing to do with the
        picker. Reproduced against a live server with two namespaces: the
        control `/api/state?project=beta` answered beta's STATE.md while
        `/api/export?project=beta` returned alpha's, silently, under a 200 and
        an attachment filename. It also WRITES `handoff.md` into whichever
        project it picked, so the wrong namespace grew a file as well.

        HIVE MIND answers in words rather than a file, and NOT as an
        attachment: `project_dir("*")` is not a directory, and a downloaded
        handoff.md whose body explains itself is worse than a page that does.
        Same sentence and same shape as `_get_state` / `_get_briefing`.
        """
        if am:
            return self._send(200, "HIVE MIND unifies every project — pick a single "
                              "project to export its handoff.", "text/plain; charset=utf-8")
        path = export_mod.export(proj)
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
        "/api/injection": _get_injection,
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
