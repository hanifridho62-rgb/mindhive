# SPDX-License-Identifier: Apache-2.0
# src/mindhive/oversee.py
"""Oversee projection for INTERACTIVE Claude sessions.

The Oversee dashboard section shows every kind of Claude work: autonomous
``hive loop`` runs (projected by ``loop.state.list_runs``) and — this module —
ordinary interactive sessions, straight from the deterministic telemetry the
hooks/auto-harvest already capture (session_start/end, file_touched,
command_run, user_prompt, assistant_response, skill_used). No slash command or
loop is needed for a session to appear: if events flow, the session is on the
board.

Each card also carries a skill trail: which skills the session invoked, when,
whether they succeeded, and why — where "why" is recorded fact, never a
synthesized motive: the args the skill was invoked with, else a same-session
prompt-gate direction (consumed once), else the skill's labelled intended-use
purpose; plus the prompt it was serving ("during").

Lifecycle mirrors the loop cards: a session leaves the view automatically when
it finishes (session_end) or goes stale (idle past ACTIVE_WINDOW_SECONDS);
manual delete appends the same ``loop_run_dismissed`` tombstone keyed by the
session id, and a dismissed-but-still-active session resurfaces on new events
(never silently hide live work). ``online`` means Claude is actively working —
tool events within ONLINE_WINDOW_SECONDS."""
import time

from . import events as ev_mod
from . import initiatives as initiatives_mod
from . import worksessions
from .briefing import headline
from .loop.state import _ts_epoch

ONLINE_WINDOW_SECONDS = 180.0        # tool events this fresh = Claude is working
ACTIVE_WINDOW_SECONDS = 6 * 3600.0   # idle longer than this = session aged out
_DONE_CAP = 8                        # per detail group, keep the card readable
_WS_TAG_CAP = 4                      # the active [WS] tag + the three before it

# Work-session rows (blueprint P1b) are bookkeeping ABOUT a session, not
# activity OF one: they attach to a card real telemetry already opened, and
# never open a card or bump its liveness -- `hive worksession migrate` copies
# old roadmap plans, and counting those rows put every past plan owner back on
# the board as "online". One set with the Sessions view (`events.BOOKKEEPING_TYPES`).
_WS_TYPES = ev_mod.BOOKKEEPING_TYPES

# telemetry that counts as "the session is doing something"
#: What `verification.scope` says for a native CI run (`ci.runner.SCOPE`).
#: Spelled here rather than imported: `oversee` is on the dashboard's poll
#: path and must not pull the runner in to read one string.
_CI_SCOPE = "ci"

_ACTIVITY_TYPES = {
    "session_start", "file_touched", "command_run", "user_prompt",
    "assistant_response", "compaction", "skill_used",
} | (ev_mod.SEMANTIC_TYPES - _WS_TYPES)


def _text(payload) -> str:
    if isinstance(payload, dict):
        return payload.get("text", "") or payload.get("path", "") or ""
    return str(payload)


_IU_CACHE = {"ts": 0.0, "map": {}}
_IU_TTL = 60.0                        # inventory rescans at most once a minute


def intended_use_map(now_fn=time.time) -> dict:
    """``{skill_name: intended-use one-liner}`` from the live plugin inventory,
    memoized for ``_IU_TTL`` seconds so the 2.5 s dashboard refresh never
    re-scans the plugin cache. This is the honest never-empty fallback for a
    trail row's "why" when the invocation carried no args and no gate
    direction — the skill's stated PURPOSE, not a claimed motive. Best-effort:
    any inventory error keeps the last good map (or ``{}``)."""
    now = now_fn()
    if _IU_CACHE["map"] and (now - _IU_CACHE["ts"]) < _IU_TTL:
        return _IU_CACHE["map"]
    m: dict = {}
    try:
        from . import plugins, recommend
        for s in recommend.flatten_inventory(
                plugins.live_inventory()["inventory"]):
            name = s.get("name")
            desc = headline(str(s.get("description") or ""))
            if name and desc and name not in m:
                m[name] = desc
    except Exception:
        return _IU_CACHE["map"] or {}
    _IU_CACHE.update(ts=now, map=m)
    return m


def consumed_directions(project=None, evs=None) -> dict:
    """``{skill_name: times a direction was followed}`` — consume-once pairing.

    One ``skill_directed`` pairs with the next matching ``skill_used`` in the SAME
    session, then is spent. That is the same rule :func:`_skill_why` applies, and it
    exists here separately because the two ask different questions:

    - **display** (``_skill_why``): "what is the most informative why for this use?"
      The invocation args win when present, so a use that carries args returns the args
      and never reaches the direction branch. Correct for a trail a person reads.
    - **learning** (this function): "was this suggestion ever followed?" Whether the
      caller happened to pass args is irrelevant to that, and letting it matter biases
      the answer badly — 78 of 216 real ``skill_used`` rows (36%) carry args, so a
      genuinely-followed suggestion would look ignored and any consumer that demotes on
      "never taken" would demote a skill that IS being taken.

    Sharing the pairing RULE while keeping the two answers separate is the point; the
    alternative is a second implementation of the pairing itself, which would let the
    trail and the learner disagree about what happened.

    Total and ledger-shaped: skips ``manual``/blank sessions and malformed rows exactly
    as `skill_trails` does, so the two folds see the same events."""
    if evs is None:
        evs = ev_mod.read_all(project)
    pending: dict = {}
    out: dict = {}
    for e in (evs or []):
        sid = getattr(e, "session_id", None)
        if not sid or sid == "manual":
            continue
        p = e.payload if isinstance(getattr(e, "payload", None), dict) else {}
        if e.type == "skill_directed":
            skills = p.get("skills")
            if not isinstance(skills, list):
                continue
            for s in skills:
                if isinstance(s, dict) and s.get("name"):
                    # A set per event: one injection naming a skill twice is still one
                    # direction, and counting it twice would make it undercountable.
                    pending.setdefault(sid, {}).setdefault(str(s["name"]), 0)
                    pending[sid][str(s["name"])] = 1
        elif e.type == "skill_used":
            name = p.get("name")
            if not name:
                continue
            name = str(name)
            if pending.get(sid, {}).pop(name, 0):
                out[name] = out.get(name, 0) + 1
    return out


# Who issued a direction, and how that renders. An absent or empty `source` is
# the prompt gate: it was the only writer of `skill_directed` before the
# replenishment injector, so every historical row folds exactly as it did.
DIRECTION_SOURCES = {
    "": "directed by prompt-gate",
    "replenish_injector": "directed by autonomous replenish loop",
}


def _skill_why(payload, directed, intended_use) -> str:
    """The recorded "why" for one ``skill_used``, honesty-ordered: the
    invocation args (the ask) -> a same-session direction, attributed to
    whoever issued it (causal) -> the skill's intended-use one-liner (labelled
    as PURPOSE, not motive) -> empty. Never a synthesized motive.

    A direction is CONSUMED (one ``skill_directed`` -> the next matching
    invocation): stamping every later same-session call "directed by
    prompt-gate" would over-attribute — a skill the gate suggested once but
    that was later invoked independently did not happen *because* of the gate.
    ``directed`` is the caller's live per-session dict, so the pop persists
    across entries in the fold.

    An UNRECOGNISED source renders a bare "directed". The honesty rule is that a
    trail never attributes a call to something that did not cause it, and that
    cannot hold if every unknown writer falls back to the gate's wording.
    Echoing the raw value would be worse still — it puts an unvalidated string
    from the ledger straight into the rendered trail."""
    args_why = headline(str(payload.get("args") or ""))
    if args_why:
        return args_why
    name = str(payload.get("name") or "")
    if name in directed:
        entry = directed.pop(name)        # consume: one direction, one invocation
        measured, source = entry if isinstance(entry, tuple) else (entry, "")
        if source not in DIRECTION_SOURCES:
            return "directed"
        why = DIRECTION_SOURCES[source]
        # The measured suffix is the prompt gate's own catalog scoring; a source
        # that carries none renders the bare reason.
        if source == "" and isinstance(measured, dict) and measured.get("calls"):
            return (f"{why} "
                    f"(measured {measured.get('ok', 0)}/{measured['calls']})")
        return why
    use = (intended_use or {}).get(name)
    return f"purpose: {use}" if use else ""


def skill_trails(project=None, evs=None, intended_use=None) -> dict:
    """``{session_id: [entry, ...]}`` skill trail for EVERY session — live,
    ended, or stale — with no live-board lifecycle filtering. This is the
    durable fold behind the ``hive skills trail`` CLI and the read API; the
    live ``interactive_sessions`` view reuses it too, so the surfaces can never
    disagree. Each entry is ``{name, plugin, ts, ok, why, during}``: ``during``
    is the headline of the most recent preceding ``user_prompt`` in the
    session; ``why`` follows :func:`_skill_why`. Chronological (``read_all`` is
    ordered) and total — skips ``manual``/junk rows, never raises."""
    if evs is None:
        evs = ev_mod.read_all(project)
    trails: dict = {}
    prompt_now: dict = {}
    directed: dict = {}                   # sid -> {skill_name: measured|None}
    for e in evs:
        sid = e.session_id
        if not sid or sid == "manual":
            continue
        p = e.payload if isinstance(e.payload, dict) else {}
        if e.type == "user_prompt":
            t = headline(_text(p))
            if t:
                prompt_now[sid] = t
        elif e.type == "skill_directed":
            # ``.get(k, [])`` only defaults on a MISSING key — a present but
            # non-list value ({"skills": null} / a number from a hand-edited or
            # future-shaped row) would be returned as-is and raise on iteration,
            # breaking the total-read invariant this fold promises. Guard the
            # container type explicitly, not just the items.
            skills = p.get("skills")
            if isinstance(skills, list):
                # The source is per EVENT and the measured score per SKILL, so
                # the pair travels together — reading the source off the skill
                # dict would find nothing.
                source = p.get("source")
                source = source if isinstance(source, str) else ""
                for s in skills:
                    if isinstance(s, dict) and s.get("name"):
                        directed.setdefault(sid, {})[str(s["name"])] = (
                            s.get("measured"), source)
        elif e.type == "skill_used":
            if not p.get("name"):
                continue
            trails.setdefault(sid, []).append({
                "name": str(p["name"]),
                "plugin": str(p.get("plugin") or ""),
                "ts": e.ts,
                "ok": bool(p.get("ok", True)),
                "why": _skill_why(p, directed.get(sid, {}), intended_use),
                "during": prompt_now.get(sid, ""),
            })
    return trails


def interactive_sessions(project=None, *, now_fn=time.time, evs=None,
                         intended_use=None,
                         online_seconds=ONLINE_WINDOW_SECONDS,
                         active_seconds=ACTIVE_WINDOW_SECONDS) -> list[dict]:
    """Active interactive-session records, newest first, shaped like the loop
    run records (plus ``kind: "chat"``) so the dashboard renders one list."""
    if evs is None:
        evs = ev_mod.read_all(project)
    # One shared fold owns the per-session skill trail (live + durable views
    # never disagree — KTD2); attached per card in the output pass below.
    trails = skill_trails(evs=evs, intended_use=intended_use)
    sessions: dict[str, dict] = {}
    order: list[str] = []
    dismissed: dict[str, str] = {}
    ws_rows: dict[str, list] = {}
    for e in evs:
        if e.type == "loop_run_dismissed":
            rid = (e.payload or {}).get("run_id")
            if rid:
                dismissed[rid] = e.ts
            continue
        sid = e.session_id
        if not sid or sid == "manual" or e.type.startswith("loop_"):
            continue
        if e.type in _WS_TYPES:
            ws_rows.setdefault(sid, []).append(e)
            continue
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = {
                "run_id": sid, "kind": "chat", "title": "", "goal_dir": "",
                "started": e.ts, "status": "running", "iterations": 0,
                "total_cost_usd": 0.0, "total_tokens": 0, "last_event_ts": e.ts,
                "ended": False, "doing": None, "done": [], "planned": [],
                "not_yet": [], "files": [], "task": "",
                # The two questions this card is FOR, and neither had an
                # answer on it: `task` names what the session is working on
                # (folded from state_set, then deleted on the way out), and
                # `blockers` names what is in the way. A session with no
                # open questions still carries the empty list, because a key
                # that appears only sometimes is how a renderer starts
                # failing on the quiet case rather than the loud one.
                "blockers": [],
                # provenance: Self-Reliant runs log into research-<id> streams;
                # the label rides the card so Oversee shows whose work this is
                # (filled by the per-event pass below — same idiom as
                # dashboard_get._get_sessions)
                "origin": None,
                # Work sessions (blueprint P1b, BAB 5.5): the ACTIVE step's
                # phase, the [WS] tags up to and including it, and the
                # session's initiatives. Present, and empty, on every card.
                "phase": "", "ws_tags": [], "initiatives": [],
                # The session's last NATIVE CI run (blueprint P1g), or None.
                # Present and None on every card: the renderer reads `ci` to
                # decide whether to show the gate at all, and a key that
                # appears only after a run is one it starts failing on.
                "ci": None,
            }
            order.append(sid)
        if not s["origin"]:              # any event may carry the tag
            s["origin"] = ev_mod.origin_of(sid, e.tags)
        if e.type in _ACTIVITY_TYPES or e.type == "session_end":
            s["last_event_ts"] = e.ts
        if e.type == "session_end":
            s["ended"] = True
        elif e.type == "user_prompt":
            t = headline(_text(e.payload))
            if t:
                s["title"] = t
                s["doing"] = t
        elif e.type == "assistant_response":
            t = headline(_text(e.payload))
            if t:
                s["doing"] = t
        elif e.type == "state_set" and isinstance(e.payload, dict):
            s["task"] = str(e.payload.get("task") or s["task"])
            ns = e.payload.get("next_step")
            if ns:
                s["planned"].append(str(ns))
        elif e.type == "next_step":
            t = headline(_text(e.payload))
            if t:
                s["planned"].append(t)
        elif e.type == "open_question":
            # Deliberately its own list rather than folded into `planned`:
            # an unanswered question is not a next step, and a card that
            # renders them together tells the reader work is queued when
            # what is actually queued is a decision.
            t = headline(_text(e.payload))
            if t:
                s["blockers"].append(t)
        elif e.type in ("decision", "experiment_result", "caveat"):
            t = headline(_text(e.payload))
            if t:
                s["done"].append(f"[{e.type}] {t}")
        elif e.type == "file_touched":
            p = _text(e.payload)
            if p and p not in s["files"]:
                s["files"].append(p)
        elif e.type == "command_run":
            s["iterations"] += 1          # activity count for the pill
        elif (e.type == "verification" and isinstance(e.payload, dict)
                and e.payload.get("scope") == _CI_SCOPE):
            # The LAST one wins, deliberately: a card still red after the fix
            # landed is a card the operator learns to ignore. Scope is
            # checked rather than assumed, because a failing `hive selftest`
            # is a red SUITE, which is a different claim about a different
            # run than a red CI gate.
            s["ci"] = {"exit_code": int(e.payload.get("exit_code") or 0),
                       "pr_id": int(e.payload.get("pr_id") or 0),
                       "gate": str(e.payload.get("gate") or ""),
                       "branch": str(e.payload.get("branch") or ""),
                       "ts": e.ts}

    now = now_fn()
    out = []
    for sid in order:
        s = sessions[sid]
        if s.pop("ended"):
            continue                      # finished -> removed automatically
        last = _ts_epoch(s["last_event_ts"])
        if last is None or (now - last) >= active_seconds:
            continue                      # stale ghost -> aged out
        # same resurrection rule as loop runs: a dismissal only holds while it
        # stays the session's last word
        if sid in dismissed and dismissed[sid] >= s["last_event_ts"]:
            continue
        s["online"] = (now - last) < online_seconds
        if not s["title"]:
            # research streams have no user_prompt to title from — name the run
            s["title"] = s["task"] or (
                f"Self-Reliant run {sid[len(ev_mod.SELF_RELIANT_SESSION_PREFIX):]}"
                if s.get("origin") == ev_mod.SELF_RELIANT_TAG
                and sid.startswith(ev_mod.SELF_RELIANT_SESSION_PREFIX)
                else f"session {sid[:12]}")
        files = s.pop("files")
        s["done"] = s["done"][-_DONE_CAP:] + [f"touched {p}" for p in files[-_DONE_CAP:]]
        s["planned"] = s["planned"][-_DONE_CAP:]
        s["blockers"] = s["blockers"][-_DONE_CAP:]
        ci = s["ci"]
        if ci and ci["exit_code"] != 0:
            # `failed` rather than a note on a running card: the gate that
            # decides whether work lands went red, and a card that still
            # reads "running" is telling the operator the opposite.
            s["status"] = "failed"
            s["blockers"] = ([f"CI \u2717 PR #{ci['pr_id']} "
                              f"({ci['gate'] or 'unknown gate'} exit "
                              f"{ci['exit_code']})"]
                             + s["blockers"])[-_DONE_CAP:]
        # The same fold `hive worksession list` and /api/worksessions read, so
        # the card head is the step `fold` calls active, never merely the last
        # one appended, and the three surfaces cannot disagree.
        rows = ws_rows.get(sid, [])
        w = worksessions.fold(rows).get(sid)
        if w is not None and w.steps:
            head = w.active_step_index
            s["phase"] = w.steps[head].phase_tag
            s["ws_tags"] = [st.technical_action for st in w.steps[:head + 1]][-_WS_TAG_CAP:]
        s["initiatives"] = [{k: v for k, v in i.items() if k != "session_id"}
                            for i in initiatives_mod.initiatives(rows)][-_DONE_CAP:]
        sk = trails.get(sid, [])
        s["skills_total"] = len(sk)
        s["skills"] = sk[-_DONE_CAP:]
        out.append(s)
    out.reverse()
    return out
