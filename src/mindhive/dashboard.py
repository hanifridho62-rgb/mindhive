# src/mindhive/dashboard.py
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import paths, chain
from . import guard
from . import recall as recall_mod
from . import graph as graph_mod
from . import graphview
from . import plugins as plugins_mod
from . import invoker as invoker_mod
from . import export as export_mod
from . import events, projector
from . import dedupe
from . import sync as sync_mod
from .briefing import briefing as build_briefing
from .dashboard_page import render_page

# A fixed default port + a token persisted under the store home keep the
# dashboard URL identical on every launch (http://127.0.0.1:54461/?token=…).
DEFAULT_PORT = 54461

# One reflection batch at a time: a second Reflect click while one is running
# would double token spend and race two writers over the same incident files.
_REFLECT_LOCK = threading.Lock()
# Serialize headless-invoke calls: each spawns a full `claude` subprocess, so
# concurrent clicks would fork a fleet of agents. One at a time, like reflect.
_INVOKE_LOCK = threading.Lock()

# Reflect batch size per request. Each incident is one `claude` CLI call (up
# to 120s), so an unbounded batch could outlive the browser's fetch timeout.
REFLECT_BATCH_LIMIT = 3


def _token_path():
    return paths.store_home() / "dashboard.token"


def load_or_create_token() -> str:
    """Return a stable dashboard token so the URL doesn't change between
    launches. Resolution order: MINDHIVE_DASHBOARD_TOKEN env → persisted file →
    freshly generated (and then persisted). Best-effort: a read/write failure
    falls back to an in-memory token rather than crashing the launch."""
    env = os.environ.get("MINDHIVE_DASHBOARD_TOKEN")
    if env:
        return env
    p = _token_path()
    try:
        if p.exists():
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except Exception:
        pass
    tok = secrets.token_urlsafe(16)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tok, encoding="utf-8")
    except Exception:
        pass
    return tok


# --- project / session / memory separation -------------------------------
# A "project" is a store namespace; a "session" is one append stream
# (session_id) within it; "memory" is the semantic subset of events (decisions,
# caveats, …) as opposed to deterministic session telemetry. Keeping the three
# distinct is what makes intent and context legible at a glance.

def _folder_for_project(proj: str) -> str:
    """The project's own folder (where its native MEMORY.md belongs), taken from
    the autosync registry. Falls back to the server's cwd only when the project
    isn't registered — so picking a project in the UI syncs THAT project's
    folder, not wherever the dashboard happens to be launched from."""
    from . import autosync
    for e in autosync.load_registry():
        if e.get("project") == proj and e.get("folder"):
            return e["folder"]
    return os.getcwd()


def _all_projects() -> list:
    """Names of every store namespace that has an events log."""
    home = paths.store_home()
    names = []
    if home.exists():
        for d in sorted(home.iterdir()):
            if d.is_dir() and (d / "events").is_dir():
                names.append(d.name)
    return names


def _project_stats(name: str) -> dict:
    evs = events.read_all(name)
    sessions, mem, auto = set(), 0, 0
    for ev in evs:
        sessions.add(ev.session_id)
        if ev.type in events.MEMORY_VIEW_TYPES:
            mem += 1
        else:
            auto += 1
    return {"name": name, "events": len(evs), "memory": mem, "auto": auto,
            "sessions": len(sessions), "last_ts": evs[-1].ts if evs else None}


# --- Improvements efficiency strip ----------------------------------------
# Failure rate = tool_failed per 100 tool actions. The denominator is tool
# activity only — user_prompt/assistant_response are logged for every exchange
# and would make the rate track conversation length instead of tool behavior.
_TOOL_ACTION_TYPES = ("command_run", "file_touched", "tool_failed")
_EFF_WINDOWS = (("day", 86400), ("week", 7 * 86400),
                ("month", 30 * 86400), ("quarter", 90 * 86400))
LOW_SAMPLE_FLOOR = 20     # tool actions per window below which a % is noise


def _iso_utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + "Z"


def _empty_tallies() -> dict:
    # window -> [cur_failures, cur_actions, prev_failures, prev_actions]
    return {key: [0, 0, 0, 0] for key, _ in _EFF_WINDOWS}


def _add_tallies(tallies: dict, evs, now: float) -> None:
    """Accumulate one project's tool-action counts into the window tallies.
    Called once per project; HIVE MIND sums across projects before rating."""
    cuts = {key: (_iso_utc(now - span), _iso_utc(now - 2 * span))
            for key, span in _EFF_WINDOWS}
    for ev in evs:
        if ev.type not in _TOOL_ACTION_TYPES or not ev.ts:
            continue          # guard a null ts (corrupt segment) like signal_stats
        for key, _ in _EFF_WINDOWS:
            cur_cut, prev_cut = cuts[key]
            if ev.ts >= cur_cut:
                i = 0
            elif ev.ts >= prev_cut:
                i = 2
            else:
                continue
            t = tallies[key]
            t[i + 1] += 1
            if ev.type == "tool_failed":
                t[i] += 1


def _efficiency(tallies: dict) -> dict:
    """Rates, deltas, and honesty dispositions per window: an empty current
    window and thin samples render as neutral states, never fake percents.
    delta_pct is signed relative change — negative means fewer failures."""
    out = {}
    for key, _ in _EFF_WINDOWS:
        cf, ca, pf, pa = tallies[key]
        cur_rate = round(cf * 100.0 / ca, 1) if ca else None
        prev_rate = round(pf * 100.0 / pa, 1) if pa else None
        row = {"cur_failures": cf, "cur_actions": ca,
               "prev_failures": pf, "prev_actions": pa,
               "cur_rate": cur_rate, "prev_rate": prev_rate, "delta_pct": None}
        if ca == 0:
            row["disposition"] = "no_current_data"
        elif pa == 0:
            row["disposition"] = "no_baseline"
        elif ca < LOW_SAMPLE_FLOOR or pa < LOW_SAMPLE_FLOOR:
            row["disposition"] = "low_sample"
        elif prev_rate == 0 and cur_rate == 0:
            row["disposition"], row["delta_pct"] = "no_change", 0.0
        elif prev_rate == 0:
            row["disposition"] = "regression_from_zero"
        else:
            row["delta_pct"] = round((cur_rate - prev_rate) * 100.0 / prev_rate, 1)
            row["disposition"] = "ok"
        out[key] = row
    return out


def default_project() -> str:
    """The project the panel opens on: the one with the most events (so a fresh
    launch shows real data, not an empty cwd-derived namespace). Falls back to
    the cwd-detected project when the store is empty."""
    stats = [s for s in (_project_stats(n) for n in _all_projects()) if s["events"] > 0]
    if stats:
        stats.sort(key=lambda s: (s["events"], s["name"]), reverse=True)
        return stats[0]["name"]
    return paths.detect_project()


# --- HIVE MIND: one unified view across every project --------------------
# Selecting project="*" in the panel aggregates all namespaces into a single
# stream — every session, log, memory and bit of context unified. It's a
# read-only lens; mutating actions still need a concrete target project.
ALL_TOKENS = {"*", "__all__", "all"}
HIVEMIND_LABEL = "⨁ HIVE MIND (all projects)"
HIVEMIND_PROJECT = "__hivemind__"   # synthetic namespace for the unified graph cache


def is_all(proj) -> bool:
    return proj in ALL_TOKENS


def all_event_pairs() -> list:
    """(project_name, Event) across every namespace, ordered oldest→newest."""
    pairs = []
    for name in _all_projects():
        for ev in events.read_all(name):
            pairs.append((name, ev))
    pairs.sort(key=lambda pe: pe[1].ts or "")
    return pairs


def event_pairs(proj: str, am: bool) -> list:
    """(project_name, Event) for the current selection, oldest→newest — the one
    place the HIVE MIND (am) fork lives for event-iterating routes. am → every
    project's events unified; otherwise → just ``proj``'s, each labelled ``proj``."""
    if am:
        return all_event_pairs()
    return [(proj, e) for e in events.read_all(proj)]


def _overview_counts(pairs) -> dict:
    """Tally overview stats from an iterable of (session_key, Event). Shared by
    the single-project and HIVE MIND overviews so the counting lives in one
    place; the caller chooses the session key (a bare id, or (project, id) when
    unifying across projects)."""
    by_type, sessions, mem, auto, total, last_ts = {}, set(), 0, 0, 0, None
    for skey, ev in pairs:
        by_type[ev.type] = by_type.get(ev.type, 0) + 1
        sessions.add(skey)
        if ev.type in events.MEMORY_VIEW_TYPES:
            mem += 1
        else:
            auto += 1
        total += 1
        if ev.ts and (last_ts is None or ev.ts > last_ts):
            last_ts = ev.ts
    return {"events": total, "memory_events": mem, "auto_events": auto,
            "sessions": len(sessions), "by_type": by_type, "last_ts": last_ts}


def _safe_chain_ok(name: str, evs=None) -> bool:
    try:
        return bool(chain.verify(name, evs=evs).get("ok"))
    except Exception:
        return False


def make_handler(token: str):
    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        # --- helpers ---
        def _check_token(self) -> bool:
            qs = parse_qs(urlparse(self.path).query)
            supplied = self.headers.get("X-Hive-Token") or (qs.get("token") or [None])[0]
            if supplied is None:
                return False
            # constant-time compare to avoid leaking the token via timing
            return secrets.compare_digest(str(supplied), str(token))

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        # Bodies are tiny JSON control messages; cap the read so an
        # unauthenticated caller (do_POST drains the body before the token
        # check to avoid a WinError 10053 reset) can't force an arbitrarily
        # large allocation or hold a worker thread on a slow/oversized body.
        MAX_BODY_BYTES = 1 << 20  # 1 MiB

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > self.MAX_BODY_BYTES:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

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

        def _get_verify(self, proj, am, qs):
            if am:
                per = {n: _safe_chain_ok(n) for n in _all_projects()}
                return self._json({"ok": all(per.values()) if per else False, "per_project": per})
            return self._json(chain.verify(proj))

        def _get_overview(self, proj, am, qs):
            if am:
                names = _all_projects()
                # read each project's log ONCE, then derive stats AND the chain
                # check from the same events (no second full read)
                evs_by_proj = {n: events.read_all(n) for n in names}
                stats = _overview_counts(
                    ((name, ev.session_id), ev)
                    for name, evs in evs_by_proj.items() for ev in evs)
                return self._json({
                    "project": HIVEMIND_LABEL, "projects": len(names), **stats,
                    "chain_ok": (all(_safe_chain_ok(n, evs_by_proj[n]) for n in names)
                                 if names else False),
                })
            evs = events.read_all(proj)
            stats = _overview_counts((ev.session_id, ev) for ev in evs)
            return self._json({
                "project": proj, **stats,
                # reuse the events we just read — no second full read
                "chain_ok": bool(chain.verify(proj, evs=evs).get("ok")),
            })

        def _get_memory(self, proj, am, qs):
            # the semantic subset only — the intent/context layer. Build the
            # (project, Event) rows once so the projection + dedupe.collapse lives
            # in a single place instead of once per am / single-project branch.
            session = (qs.get("session") or [None])[0]
            limit = int((qs.get("limit") or ["200"])[0])
            rows = [(n, e) for n, e in event_pairs(proj, am)
                    if e.type in events.MEMORY_VIEW_TYPES]
            if session:
                rows = [(n, e) for n, e in rows if e.session_id == session]
            rows = list(reversed(rows))[:limit]      # newest first
            # Collapse near-duplicate memories by meaning (same type + project),
            # keeping the newest; display-time only, ledger untouched.
            # similar_count rides along for the UI hint.
            return self._json(dedupe.collapse([
                {"ts": e.ts, "type": e.type, "session": e.session_id, "project": n,
                 "text": e.payload.get("text", "") if isinstance(e.payload, dict) else str(e.payload),
                 "tags": e.tags}
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
                    auto.append(dict(
                        s, project=n, source="auto",
                        first_seen=st.get("first_seen"),
                        last_seen=st.get("last_seen"),
                        leaks=st.get("leaks", 0),
                        resolution=incidents.classify_resolution(s, st)))
                for e in evs:
                    if e.type != "caveat":
                        continue
                    manual.append({
                        "ts": e.ts, "type": e.type, "session": e.session_id,
                        "project": n, "source": "manual", "tags": e.tags,
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
                    s = by[key] = {"session_id": e.session_id, "project": name, "events": 0,
                                   "memory": 0, "first_ts": e.ts, "last_ts": e.ts}
                s["events"] += 1
                if e.type in events.MEMORY_VIEW_TYPES:
                    s["memory"] += 1
                if e.ts and e.ts < s["first_ts"]:
                    s["first_ts"] = e.ts
                if e.ts and e.ts > s["last_ts"]:
                    s["last_ts"] = e.ts
            out = sorted(by.values(), key=lambda s: s["last_ts"] or "", reverse=True)
            return self._json(out)

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
            # semantic=True folds the embedding-derived semantic_near edges into
            # the interactive graph (degrades to deterministic edges if
            # embeddings/deps are absent). Cost scales with ledger size, so a
            # large namespace's graph takes longer to build on demand.
            if am:
                evs = [e for _, e in all_event_pairs()]
                html = graphview.render_html(graph_mod.build(project=HIVEMIND_PROJECT, _evs=evs, semantic=True))
            else:
                html = graphview.render_html(graph_mod.build(proj, semantic=True))
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
            # both derived from the ledger in a single pass
            return self._json(plugins_mod.call_stats(project=proj))

        def _get_skills_history(self, proj, am, qs):
            name = (qs.get("name") or [""])[0]
            limit = int((qs.get("limit") or ["20"])[0])
            return self._json(plugins_mod.call_history(name, project=proj, limit=limit))

        def _get_skills_stats(self, proj, am, qs):
            # per-skill completion-rate telemetry aggregated from the ledger
            # (skill_used / invoke_result / tool_failed skill:*) — U5/KTD5
            return self._json({"stats": plugins_mod.skill_stats(project=proj)})

        def _get_skills_recommend(self, proj, am, qs):
            # deterministic most-needed-skills ranking (U6/KTD6): one ledger
            # read feeds both the stats and the need extraction
            top = (qs.get("top") or [None])[0]
            return self._json(plugins_mod.recommend_for(
                project=proj, top=int(top) if top else None))

        def _get_oversee(self, proj, am, qs):
            # Oversee: every kind of live Claude work — hive loop runs AND
            # interactive sessions (straight from hook telemetry, no /circle
            # needed). Dismissed/finished/stale entries are already excluded
            # by the projections.
            from . import oversee as oversee_mod
            from .loop import state as loop_state
            names = _all_projects() if am else [proj]
            runs, dismissed = [], 0
            for n in names:
                evs = events.read_all(n)
                res = loop_state.list_runs(n)
                for r in res["runs"] + oversee_mod.interactive_sessions(n, evs=evs):
                    r["project"] = n
                    runs.append(r)
                dismissed += res["dismissed"]
            runs.sort(key=lambda r: r.get("started") or "", reverse=True)
            return self._json({"runs": runs, "dismissed": dismissed,
                               "any_online": any(r["online"] for r in runs)})

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

        # --- routing ---
        def do_GET(self):
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/":
                return self._send(200, render_page(token), "text/html; charset=utf-8")
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            handler = self._GET_ROUTES.get(route)
            if handler is None:
                return self._json({"ok": False, "error": "not found"}, 404)
            qs = parse_qs(parsed.query)
            proj = (qs.get("project") or [None])[0] or default_project()
            am = is_all(proj)
            try:
                return handler(self, proj, am, qs)
            except Exception as e:  # never crash the server
                # Unexpected failure -> 500 so monitoring and clients can tell a
                # server fault apart from a normal empty/negative result. Expected
                # domain rejections return their own ok:False, 200 inside handlers.
                return self._json({"ok": False, "error": str(e)}, 500)

        # --- POST route handlers ------------------------------------------
        # Each takes (proj, body) and sends its own response, wired via
        # _POST_ROUTES. The HIVE MIND read-only guard and the mutating-action
        # target check live in do_POST, before dispatch.
        def _post_harvest(self, proj, body):
            # self-healing backfill: capture any session the hooks missed
            res = run_catch_up()
            return self._json({"ok": True, "result": res})

        def _post_append(self, proj, body):
            etype = body.get("type")
            text = body.get("payload", "")
            tags = [t for t in (body.get("tags") or "").split(",") if t]
            if not etype:
                return self._json({"ok": False, "error": "type is required"}, 200)
            ev = events.append_event(etype, {"text": text}, tags=tags, project=proj)
            projector.project(proj)
            return self._json({"ok": True, "type": ev.type, "ts": ev.ts, "project": proj})

        def _post_project(self, proj, body):
            return self._json({"ok": True, "result": projector.project(proj)})

        def _post_sync(self, proj, body):
            folder = _folder_for_project(proj)
            path = sync_mod.sync(folder, project=proj)
            return self._json({"ok": True, "path": str(path), "folder": folder})

        def _post_embed(self, proj, body):
            force = bool(body.get("force", False))
            try:
                guard.ensure_safe(force=force)  # raises when LIVE_LOCK active and not force
            except RuntimeError as e:
                # LIVE_LOCK is an expected, safe-to-report rejection — not a
                # server fault — so keep it ok:False, 200.
                return self._json({"ok": False, "error": str(e)}, 200)
            from . import vectors
            return self._json({"ok": True, "result": vectors.embed(proj, force=force)})

        def _post_plugins_scan(self, proj, body):
            # Plugins/skills are installed machine-wide, so a scan is global. In
            # HIVE MIND (all-projects) mode there's no concrete namespace to store
            # the catalog under — fall back to the default project so the snapshot
            # stays retrievable by the recommend route.
            if is_all(proj):
                proj = default_project()
            result = plugins_mod.scan_and_store(top_n=int(body.get("top", 5)), project=proj)
            return self._json({"ok": True, "result": result})

        def _post_skills_invoke(self, proj, body):
            name = (body.get("name") or "").strip()
            plugin = (body.get("plugin") or None)
            if not name:
                return self._json({"ok": False, "error": "name is required"}, 200)
            if not _INVOKE_LOCK.acquire(blocking=False):
                return self._json({"ok": False,
                                   "error": "another skill call is running"}, 200)
            try:
                res = invoker_mod.invoke_skill(name, plugin=plugin, project=proj)
            finally:
                _INVOKE_LOCK.release()
            return self._json({"ok": res["ok"], "name": name, "plugin": plugin,
                               "exit": res["exit"], "excerpt": res["excerpt"]})

        def _post_plugins_autoscan(self, proj, body):
            if is_all(proj):                       # global scan; see _post_plugins_scan
                proj = default_project()
            res = plugins_mod.ensure_fresh(
                top_n=int(body.get("top", 5)),
                max_age_hours=float(body.get("max_age_hours", 24)),
                force=bool(body.get("force", False)),
                project=proj,
            )
            return self._json({"ok": True, "scanned": res["scanned"],
                               "age_hours": res["age_hours"], "result": res["payload"]})

        def _post_rules_reflect(self, proj, body):
            # Reflect a bounded batch of active, not-yet-reflected incidents
            # into structured lessons. Candidates (below the recurrence
            # threshold) wait until they recur; already-reflected records are
            # skipped (the CLI's `hive rules reflect` re-reflects everything —
            # this endpoint deliberately does not).
            import shutil
            from . import incidents, reflect as reflect_mod
            # Clamp to [0, REFLECT_BATCH_LIMIT]: a direct API caller must not be
            # able to request an unbounded batch of 120s-per-incident claude
            # calls while holding _REFLECT_LOCK. Non-numeric is a domain error.
            try:
                limit = int(body.get("limit", REFLECT_BATCH_LIMIT))
            except (TypeError, ValueError):
                return self._json({"ok": False,
                                   "error": "limit must be an integer"}, 200)
            limit = max(0, min(limit, REFLECT_BATCH_LIMIT))
            if not _REFLECT_LOCK.acquire(blocking=False):
                return self._json({"ok": False,
                                   "error": "reflection already running"}, 200)
            try:
                # self-heal a stale or missing incident projection first
                incidents.audit(project=proj)
                incidents.project(project=proj)
                eligible = [
                    s["signature"] for s in incidents.summaries(proj)
                    if s["status"] == "active" and s["rule_source"] == "none"
                ]
                reflected = failed = 0
                for signature in eligible[:limit]:
                    if reflect_mod.reflect_incident(signature, project=proj).get("reflected"):
                        reflected += 1
                    else:
                        failed += 1
                if failed and not reflected and not shutil.which("claude"):
                    return self._json({"ok": False, "error":
                        "claude CLI unavailable — reflection needs it on PATH"}, 200)
                # pending = eligible not yet turned into a lesson: exclude the
                # ones that failed this round (they were attempted, not skipped).
                return self._json({"ok": True, "reflected": reflected,
                                   "failed": failed,
                                   "pending": max(0, len(eligible) - reflected - failed)})
            finally:
                _REFLECT_LOCK.release()

        def _post_rules_reflect_one(self, proj, body):
            # Reflect ONE named failure signature on demand — the "reflect this
            # failure" action on a failed skill call. Unlike the batch endpoint,
            # this bypasses the recurrence-threshold gate: the user is explicitly
            # asking to learn from this single failure now.
            import shutil
            from . import incidents, reflect as reflect_mod
            signature = (body.get("signature") or "").strip()
            if not signature:
                return self._json({"ok": False, "error": "signature is required"}, 200)
            if not _REFLECT_LOCK.acquire(blocking=False):
                return self._json({"ok": False,
                                   "error": "reflection already running"}, 200)
            try:
                # ensure the incident projection exists (even a single-occurrence
                # candidate gets a file), then reflect that specific signature.
                incidents.audit(project=proj)
                incidents.project(project=proj)
                if not incidents.load(signature, project=proj):
                    return self._json({"ok": False,
                                       "error": f"no such incident: {signature}"}, 200)
                res = reflect_mod.reflect_incident(signature, project=proj)
                if not res.get("reflected"):
                    if not shutil.which("claude"):
                        return self._json({"ok": False, "error":
                            "claude CLI unavailable — reflection needs it on PATH"}, 200)
                    return self._json({"ok": False, "signature": signature,
                        "error": res.get("reason", "reflection produced no rule")}, 200)
                return self._json({"ok": True, "reflected": True,
                                   "signature": signature})
            finally:
                _REFLECT_LOCK.release()

        def _post_rules_dismiss(self, proj, body):
            # Explicit demotion from a lesson card — the only way the gray
            # "dismissed" state is reachable without dropping to the CLI.
            # demote() appends the rule_demoted event and re-projects the
            # record, so the next Improvements fetch classifies it dismissed.
            from . import incidents
            sig = (body.get("signature") or "").strip()
            if not sig or not incidents.load(sig, project=proj):
                return self._json({"ok": False, "error": "unknown signature"}, 200)
            incidents.demote(sig, project=proj)
            return self._json({"ok": True, "signature": sig})

        def _post_oversee_dismiss(self, proj, body):
            # Manual session delete: append-only tombstone, so the hash chain
            # stays intact and every projection rebuild keeps the run hidden.
            # Deleting an already-dismissed run (e.g. the engine auto-dismissed
            # it between poll and click) is a benign no-op, not an error.
            from . import oversee as oversee_mod
            from .loop import state as loop_state
            run_id = (body.get("run_id") or "").strip()
            if not run_id:
                return self._json({"ok": False, "error": "run_id is required"}, 200)
            known = {r["run_id"] for r in loop_state.list_runs(proj)["runs"]}
            known |= {s["run_id"] for s in oversee_mod.interactive_sessions(proj)}
            if run_id not in known:
                already = any(
                    e.type == "loop_run_dismissed"
                    and (e.payload or {}).get("run_id") == run_id
                    for e in events.read_all(proj))
                if already:
                    return self._json({"ok": True, "run_id": run_id,
                                       "already_dismissed": True})
                return self._json({"ok": False,
                                   "error": f"unknown run: {run_id}"}, 200)
            events.append_event("loop_run_dismissed",
                                {"run_id": run_id, "cause": "manual"}, project=proj)
            return self._json({"ok": True, "run_id": run_id})

        def _post_selftest(self, proj, body):
            from . import selftest as selftest_mod
            code = selftest_mod.run()
            return self._json({"ok": code == 0, "exit_code": code})

        def do_POST(self):
            route = urlparse(self.path).path
            # Drain the request body BEFORE any early response: replying 403
            # while the client is still sending aborts the connection on
            # Windows (WinError 10053) instead of delivering the status.
            try:
                body = self._body()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
            if not self._check_token():
                return self._json({"ok": False, "error": "forbidden"}, 403)
            try:
                proj = body.get("project") or default_project()
                if is_all(proj):
                    if route == "/api/project":
                        res = {n: projector.project(n) for n in _all_projects()}
                        return self._json({"ok": True, "result": res})
                    # selftest + harvest + plugin scans are global (machine-wide,
                    # not tied to one project); every other mutating action needs
                    # a concrete target. The plugin handlers substitute the
                    # default project for storage when in all-mode.
                    if route not in ("/api/selftest", "/api/harvest",
                                     "/api/plugins/scan", "/api/plugins/autoscan"):
                        return self._json({"ok": False, "error":
                            "HIVE MIND is a unified read-only view across all projects — "
                            "pick a specific project for this action."}, 200)
                handler = self._POST_ROUTES.get(route)
                if handler is None:
                    return self._json({"ok": False, "error": "not found"}, 404)
                return handler(self, proj, body)
            except Exception as e:
                # Unexpected failure -> 500 (see do_GET). The HIVE MIND read-only
                # guard and the "type is required" check return ok:False, 200
                # explicitly as expected domain rejections.
                return self._json({"ok": False, "error": str(e)}, 500)

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
            "/api/oversee": _get_oversee,
            "/api/recall": _get_recall,
            "/api/graph": _get_graph,
            "/api/plugins/recommend": _get_plugins_recommend,
            "/api/plugins/inventory": _get_plugins_inventory,
            "/api/skills/detail": _get_skills_detail,
            "/api/skills/status": _get_skills_status,
            "/api/skills/history": _get_skills_history,
            "/api/skills/stats": _get_skills_stats,
            "/api/skills/recommend": _get_skills_recommend,
            "/api/export": _get_export,
        }
        _POST_ROUTES = {
            "/api/harvest": _post_harvest,
            "/api/append": _post_append,
            "/api/project": _post_project,
            "/api/sync": _post_sync,
            "/api/embed": _post_embed,
            "/api/plugins/scan": _post_plugins_scan,
            "/api/skills/invoke": _post_skills_invoke,
            "/api/plugins/autoscan": _post_plugins_autoscan,
            "/api/rules/reflect": _post_rules_reflect,
            "/api/rules/reflect_one": _post_rules_reflect_one,
            "/api/rules/dismiss": _post_rules_dismiss,
            "/api/oversee/dismiss": _post_oversee_dismiss,
            "/api/selftest": _post_selftest,
        }

    return DashboardHandler


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


def _transcripts_fingerprint(root=None) -> float:
    """Cheap activity signal: the newest mtime across all transcript .jsonl
    files. Advances whenever any session file is created or appended, so the
    harvest loop can skip work entirely when nothing has changed. Stat-only —
    no file contents are read."""
    if root is None:
        from . import harvest as harvest_mod
        root = harvest_mod.claude_projects_root()
    newest = 0.0
    try:
        for d in os.scandir(root):
            if not d.is_dir():
                continue
            try:
                for f in os.scandir(d.path):
                    if f.name.endswith(".jsonl"):
                        m = f.stat().st_mtime
                        if m > newest:
                            newest = m
            except OSError:
                continue
    except OSError:
        pass
    return newest


def _auto_harvest_loop(stop_event) -> None:
    """Low-latency background capture: poll a cheap mtime fingerprint of the
    transcripts and harvest ONLY when something actually changed. Idle ticks cost
    just stat() calls, so the poll can be fast without wasting CPU/IO; a freshly
    typed prompt is captured within one poll interval.

    Off via MINDHIVE_AUTOHARVEST=0; poll seconds via MINDHIVE_AUTOHARVEST_SECS
    (default 3, floor 1). The cross-process harvest lock keeps this safe to run
    alongside the scheduled autosync task."""
    if os.environ.get("MINDHIVE_AUTOHARVEST", "1").lower() in ("0", "false", "off", "no"):
        return
    try:
        poll = max(1, int(os.environ.get("MINDHIVE_AUTOHARVEST_SECS", "3")))
    except Exception:
        poll = 3
    # Catch up once on startup so an already-running session is captured promptly.
    try:
        run_catch_up()
    except Exception:
        pass
    last_fp = _transcripts_fingerprint()
    while not stop_event.wait(poll):
        try:
            fp = _transcripts_fingerprint()
            if fp == last_fp:
                continue            # nothing changed — skip the harvest entirely
            last_fp = fp
            res = run_catch_up()
            if res.get("total_new"):
                print(f"MIND HIVE auto-harvest: +{res['total_new']} events "
                      f"across {len(res['projects'])} project(s)", flush=True)
        except Exception:
            pass   # never let the healer disturb the server


def stable_url(port: int | None = None, token: str | None = None) -> str:
    """The dashboard's stable URL for the given (or default) port and token.
    Kept in one place so the launcher, the server, and any caller all report the
    identical http://127.0.0.1:<port>/?token=<tok> that a browser will open."""
    port = DEFAULT_PORT if port is None else port
    token = token or load_or_create_token()
    return f"http://127.0.0.1:{port}/?token={token}"


def launch_background(port: int | None = None) -> dict:
    """Spawn `hive dashboard` as a *detached* background process so a caller
    (e.g. the autonomous loop) can bring the panel up without blocking on
    serve()'s serve_forever(). The child outlives this process and — thanks to
    serve()'s fixed-port reuse — a second launch just reopens the same stable URL
    instead of erroring.

    Best-effort by contract: any spawn failure is swallowed and reported in the
    return value rather than raised, so a dashboard problem can never abort the
    caller. Returns {"launched": True, "url": ..., "pid": ...} on success or
    {"launched": False, "reason": ...} when the spawn could not start.
    """
    url = stable_url(port)
    argv = [sys.executable, "-m", "mindhive", "dashboard"]
    if port is not None:
        argv += ["--port", str(port)]
    # Detach so the child survives this process and doesn't hold its console.
    kwargs: dict = {"stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as e:   # never let a launch failure disturb the caller
        return {"launched": False, "reason": str(e)}
    return {"launched": True, "url": url, "pid": proc.pid}


def serve(port: int | None = None, open_browser: bool = True,
          token: str | None = None) -> None:
    """Start the dashboard on a fixed port with a persisted token so the URL is
    stable across launches, open the browser, and serve until interrupted.

    ``port=None`` uses the fixed DEFAULT_PORT; pass ``port=0`` for an ephemeral
    port. If the fixed port is already bound (the dashboard is likely already
    running), this reopens that same stable URL and returns instead of crashing.

    A background loop also auto-harvests sessions the live hooks miss, so the
    store self-heals even where hooks never fire (disable: MINDHIVE_AUTOHARVEST=0).
    """
    if port is None:
        port = DEFAULT_PORT
    token = token or load_or_create_token()
    handler = make_handler(token)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        url = f"http://127.0.0.1:{port}/?token={token}"
        print(f"MIND HIVE dashboard could not bind port {port}: {e}", flush=True)
        print(f"It may already be running — opening {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    actual = server.server_address[1]
    url = f"http://127.0.0.1:{actual}/?token={token}"
    print(f"MIND HIVE dashboard: {url}", flush=True)
    print("Close this window or press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    stop_event = threading.Event()
    threading.Thread(target=_auto_harvest_loop, args=(stop_event,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.shutdown()
