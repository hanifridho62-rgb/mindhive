# src/mindhive/dashboard_post.py
"""Every POST route the dashboard serves, as a mixin on the request handler.

Extracted from ``dashboard.py`` when that file reached 2043 lines, and a mixin
for the same reason ``dashboard_get`` is one — see that module's docstring for
what ``self`` is here and why reading this file alone will not tell you.

``do_POST`` deliberately stayed in ``dashboard``, with ``do_GET``: it drains the
body, checks the token, and enforces the HIVE MIND read-only guard before any
handler here runs. That guard is why several handlers below can assume a concrete
target project without saying so.

The six long operations are NOT here. They answer 202 and hand the work to
``dashboard_jobs``; what remains in this file is the short mutating routes that
still answer inline.

The `jobs.start("harvest")` calls below therefore depend on a name this file
never imports: the six kinds exist because IMPORTING ``dashboard_jobs`` runs its
``register_kind`` calls, and ``dashboard`` — which is what assembles this mixin
into a handler at all — imports it. An unregistered kind is a ``KeyError`` out of
`jobs.start`, not a quiet no-op (``None`` there means "already running"), so
importing this module without ``dashboard_jobs`` would fail loudly on the first
click rather than silently."""
from urllib.parse import urlparse

from . import events, guard, jobs, projector
from . import invoker as invoker_mod
from . import plugins as plugins_mod
from . import research as research_mod
from . import sync as sync_mod
from . import workspace as workspace_mod
from .dashboard_data import _folder_for_project, default_project, is_all
from .dashboard_query import body_num
from .dashboard_locks import (REFLECT_BATCH_LIMIT, _INVOKE_LOCK, _PLUGINS_LOCK,
                              _PROJECT_LOCK, _REFLECT_LOCK, _SYNC_LOCK,
                              _one_at_a_time)


class PostRoutes:
    """The POST half of ``DashboardHandler``. See the module docstring."""

    # --- POST route handlers ------------------------------------------
    # Each takes (proj, body) and sends its own response, wired via
    # _POST_ROUTES. The HIVE MIND read-only guard and the mutating-action
    # target check live in do_POST, before dispatch.
    def _start(self, kind, proj, body, busy):
        """Accept a long operation and hand back a job to poll.

        **202, not 200.** The work has not been done when this returns, and a
        200 would be a claim that it had. The refusal below stays ``ok:False``
        with a 200 — this file's convention for an expected domain rejection
        (see `_one_at_a_time`), and unchanged from when these ran inline, so
        "already running" reads the same to the page either way.

        No `_one_at_a_time` decorator on the routes that come through here:
        the JOB holds the operation's lock for as long as the work runs. A
        decorator would hold it only for the length of this response and
        release it before the work began.
        """
        job = jobs.start(kind, proj, body)
        if job is None:
            return self._json({"ok": False, "error": busy}, 200)
        return self._json({"ok": True, "job": job.snapshot(0)}, 202)

    def _post_harvest(self, proj, body):
        # self-healing backfill: capture any session the hooks missed
        return self._start("harvest", proj, body,
                           "a harvest is already running")

    def _post_append(self, proj, body):
        etype = body.get("type")
        raw = body.get("payload", "")
        tags = [t for t in (body.get("tags") or "").split(",") if t]
        if etype == "sop":
            # The server holding the SOP process token says nothing about who
            # sent this request, so this door never stamps `sop-validated` and
            # drops one the caller supplied (`advice.write_tags`).
            from . import advice as authorship
            # ...and with a secret configured it can verify NOTHING about its
            # caller, while an unstamped row reads as the operator's: refuse.
            refusal = authorship.http_door_refusal()
            if refusal:
                return self._json({"ok": False, "error": refusal}, 200)
            tags = authorship.write_tags(tags, stamp=False)
        if not etype:
            return self._json({"ok": False, "error": "type is required"}, 200)
        # A structured body IS the payload; only a plain string is free
        # text needing the {"text": …} wrapper. Wrapping unconditionally
        # meant this endpoint could never produce a valid registry payload
        # (a mindnet_task/roadmap/sop/code_change/autoresume contract is
        # never one text field) — so validating without this first would
        # have rejected every registry append instead of gating it.
        payload = raw if isinstance(raw, dict) else {"text": raw}
        try:
            # The same dispatch `hive append` runs (events._PAYLOAD_
            # CONTRACTS), so the CLI and this endpoint cannot drift.
            events.validate_typed_payload(etype, payload)
        except ValueError as e:
            # An expected domain rejection, not a server fault: ok:False,
            # 200 like _post_embed's LIVE_LOCK. Nothing is appended, and
            # nothing raises out of the handler.
            return self._json({"ok": False, "error": str(e)}, 200)
        ev = events.append_event(etype, payload, tags=tags, project=proj)
        projector.project(proj)
        return self._json({"ok": True, "type": ev.type, "ts": ev.ts, "project": proj})

    @_one_at_a_time(_PROJECT_LOCK, "a projection rebuild is already running")
    def _post_project(self, proj, body):
        return self._json({"ok": True, "result": projector.project(proj)})

    @_one_at_a_time(_SYNC_LOCK, "a sync is already running")
    def _post_sync(self, proj, body):
        folder = _folder_for_project(proj)
        path = sync_mod.sync(folder, project=proj)
        return self._json({"ok": True, "path": str(path), "folder": folder})

    def _post_embed(self, proj, body):
        # The guard stays SYNCHRONOUS: it is instant, and answering an active
        # LIVE_LOCK with a job id the caller must poll to discover it was
        # refused would be a worse contract than saying so now.
        try:
            guard.ensure_safe(force=bool(body.get("force", False)))
        except RuntimeError as e:
            # LIVE_LOCK is an expected, safe-to-report rejection — not a
            # server fault — so keep it ok:False, 200.
            return self._json({"ok": False, "error": str(e)}, 200)
        return self._start("embed", proj, body,
                           "an embedding pass is already running")

    def _post_plugins_scan(self, proj, body):
        # The all-projects fallback lives in the job body, which is the one
        # place that knows where the catalog is written.
        return self._start("plugins_scan", proj, body,
                           "a plugin scan is already running")

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

    # Shares _PLUGINS_LOCK with /api/plugins/scan on purpose: `ensure_fresh`
    # runs that same scan when the catalog is stale, so a lock per route
    # would leave two scans reachable over one cache through two doors.
    @_one_at_a_time(_PLUGINS_LOCK, "a plugin scan is already running")
    def _post_plugins_autoscan(self, proj, body):
        if is_all(proj):                       # global scan; see _post_plugins_scan
            proj = default_project()
        res = plugins_mod.ensure_fresh(
            top_n=body_num(body, "top", 5),
            max_age_hours=body_num(body, "max_age_hours", 24, float),
            force=bool(body.get("force", False)),
            project=proj,
        )
        return self._json({"ok": True, "scanned": res["scanned"],
                           "age_hours": res["age_hours"], "result": res["payload"]})

    def _bad_limit(self, body):
        """The `limit` check both reflection routes share, kept synchronous:
        a non-numeric limit is a caller mistake to report now, not a job to
        start and then fail. Returns the rejection response, or None."""
        try:
            int(body.get("limit", REFLECT_BATCH_LIMIT))
        except (TypeError, ValueError):
            return self._json({"ok": False,
                               "error": "limit must be an integer"}, 200)
        return None

    def _post_rules_reflect(self, proj, body):
        return (self._bad_limit(body)
                or self._start("rules_reflect", proj, body,
                               "reflection already running"))

    def _post_rules_resolve(self, proj, body):
        return (self._bad_limit(body)
                or self._start("rules_resolve", proj, body,
                               "resolution already running"))

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

    def _post_enrich_toggle(self, proj, body):
        # Concrete project required — "*" is already rejected by the
        # do_POST read-only guard before this handler runs.
        from . import enrich as enrich_mod
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            return self._json({"ok": False,
                               "error": "enabled must be true or false"}, 200)
        st = enrich_mod.set_enabled(enabled, project=proj)
        return self._json({"ok": True, "settings": st})

    def _post_enrich_preview(self, proj, body):
        # Deterministic enrichment of one draft prompt. ``record`` appends
        # the prompt_enriched audit event — set only when the user actually
        # uses the result (copy), so previews never spam the ledger.
        from . import enrich as enrich_mod
        text = body.get("prompt")
        if not isinstance(text, str) or not text.strip():
            return self._json({"ok": False, "error": "prompt is required"}, 200)
        answers = body.get("answers")
        res = enrich_mod.enrich(
            text, project=proj,
            answers=answers if isinstance(answers, dict) else None)
        if body.get("record"):
            enrich_mod.record(text, res, project=proj)
        return self._json({"ok": True, "result": res})

    def _post_selftest(self, proj, body):
        return self._start("selftest", proj, body,
                           "a self test is already running")

    # The workspace and research POSTs, twins of the GET handlers of the same
    # name in `dashboard_get`. They live here and not beside those twins
    # because `_POST_ROUTES` below names them, and a class body cannot
    # reference a method defined on a different class.
    def _post_workspace(self, proj, body):
        action = urlparse(self.path).path.rsplit("/", 1)[-1]
        return self._json(workspace_mod.api_post(action, proj=proj, body=body))

    def _post_research(self, proj, body):
        action = urlparse(self.path).path.rsplit("/", 1)[-1]
        return self._json(research_mod.api_post(action, proj=proj, body=body))

    # The POST half of the route tables. The comment explaining why both are
    # defined after their handlers is in `dashboard_get`.
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
        "/api/rules/resolve": _post_rules_resolve,
        "/api/rules/dismiss": _post_rules_dismiss,
        "/api/oversee/dismiss": _post_oversee_dismiss,
        "/api/enrich/toggle": _post_enrich_toggle,
        "/api/enrich/preview": _post_enrich_preview,
        "/api/selftest": _post_selftest,
    }
    for _p in ("spawn", "send", "run_recipe", "set_model", "stop",
               "remove", "clear_idle", "profile_add", "profile_remove",
               "profile_login", "profile_trust", "auth_verify",
               "board_save", "board_dispatch", "mission", "mission_stop",
               "command"):
        _POST_ROUTES[f"/api/workspace/{_p}"] = _post_workspace
    del _p
    for _p in ("start", "stop"):
        _POST_ROUTES[f"/api/research/{_p}"] = _post_research
    del _p
