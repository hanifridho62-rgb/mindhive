# SPDX-License-Identifier: Apache-2.0
"""The v1.1 GET routes, as a second mixin beside `dashboard_get.GetRoutes`.

`dashboard_get.py` stands three lines under the 1000-line cap
(`tests/test_file_size_cap.py`), so every route the blueprint adds lands here
instead of there. `dashboard.make_handler` composes

    class DashboardHandler(GetRoutesV1, GetRoutes, PostRoutes, BaseHTTPRequestHandler)

and gives that class its own merged ``_GET_ROUTES`` (the base table, then
`_GET_ROUTES_V1`). `do_GET` reads ``self._GET_ROUTES`` and calls
``handler(self, proj, am, qs)``, so the dispatcher, the token guard, the
project validation and the gzip path are all untouched: a route here is a plain
function in `_GET_ROUTES_V1` and receives the same four arguments as the base
table's entries.

``/api/worksessions`` (blueprint P1b, BAB 5.5) is the Oversee section's fourth
fetch: per namespace, the `worksessions.session_rows` summaries, every
initiative (`initiatives.initiatives`: declarations and ``[INITIATIVE]`` steps,
newest first), and the context-occupancy figures the card bar draws. The ledger
fold is gated the way ``/api/changelog`` is (`dashboard_memo._fingerprinted`):
an unchanged ledger is never re-read. A MOVED one is, and during live work hook
telemetry moves it between nearly every 2.5 s poll, so the fold reads through
`events.read_types` -- only the two work-session types survive the parse, not
the whole store `/api/oversee` has just folded. The usage figures come from an off-ledger
file (the synced MEMORY.md) that a ledger fingerprint cannot see move, so they
are computed on every request -- one directory walk
(`worksession_usage.memory_path_index`) and one autosync-registry read shared by
every namespace, and one small file read per namespace that has a digest.

``/static/dashboard.js`` (blueprint P5, Pillar 4) serves
`dashboard_page.DASHBOARD_JS` -- the same string the page inlines at the top of
its one script block -- as ``application/javascript``, behind the token check
every route but ``/`` gets.
"""
from . import events, initiatives, worksession_usage, worksessions
from .dashboard_data import _all_projects
from .dashboard_memo import _OVERSEE_MEMO_MAX, _BoundedMemo, _fingerprinted
from .dashboard_page import DASHBOARD_JS

# {project: (fingerprint, {"sessions": [...], "initiatives": [...]})} -- bounded
# for the namespace count, like every memo HIVE MIND fills in one request.
_WORKSESSIONS_MEMO = _BoundedMemo(_OVERSEE_MEMO_MAX)


def worksession_fold(project) -> dict:
    """One namespace's ledger fold for the route, each row stamped with the
    namespace so HIVE MIND can merge them. Both lists are newest first; the
    initiatives are the ledger order reversed, so the route's stable merge sort
    keeps same-second rows in reversed LEDGER order -- within one session that
    is newest first; across sessions the ledger orders a same-second tie by
    segment name, because no row records sub-second append order."""
    evs = events.read_types(events.BOOKKEEPING_TYPES, project)
    return {"sessions": worksessions.session_rows(evs, project),
            "initiatives": [dict(i, project=project)
                            for i in reversed(initiatives.initiatives(evs))]}


def memory_text(e) -> str:
    """The Memory view's body for a work-session row, whose structured payload
    has no ``text``: the same LEDGER body the projection writes, so the view
    never shows a blank step, status move or declaration. "" for any other
    type (`dashboard_get._get_memory` lives at the 1000-line cap, so the
    rendering lives here)."""
    if e.type == "work_session" and isinstance(e.payload, dict):
        return worksessions.ledger_body(e.payload)
    if e.type == "initiative_declared":
        return initiatives.ledger_body(e.payload)
    return ""


class GetRoutesV1:
    """Mixin: the v1.1 GET routes. Mounted FIRST in `make_handler`; the two
    route tables are disjoint (`tests/test_dashboard_get_v1.py` pins that)."""

    def _get_worksessions(self, proj, am, qs):
        from . import autosync
        known = _all_projects()
        names = known if am else [proj]
        sessions, found, usage = [], [], {}
        index = worksession_usage.memory_path_index()
        registry = autosync.load_registry()
        for n in names:
            fold = _fingerprinted(_WORKSESSIONS_MEMO, n,
                                  lambda n=n: worksession_fold(n))
            sessions.extend(fold["sessions"])
            found.extend(fold["initiatives"])
            usage[n] = worksession_usage.usage_for_project(
                n, index=index, registry=registry, namespaces=known)
        sessions.sort(key=lambda s: s.get("last_ts") or "", reverse=True)
        found.sort(key=lambda i: i.get("ts") or "", reverse=True)
        return self._json({"ok": True, "sessions": sessions,
                           "initiatives": found, "usage": usage,
                           "context_budget_tokens": worksessions.CONTEXT_BUDGET_TOKENS})

    def _get_static_dashboard_js(self, proj, am, qs):
        """`static/dashboard.js`, the same string the page inlines."""
        return self._send(200, DASHBOARD_JS, "application/javascript; charset=utf-8")

    _GET_ROUTES_V1 = {
        "/api/worksessions": _get_worksessions,
        "/static/dashboard.js": _get_static_dashboard_js,
    }


__all__ = ["GetRoutesV1", "worksession_fold", "memory_text"]
