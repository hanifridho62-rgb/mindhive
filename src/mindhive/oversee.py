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
whether they succeeded, and why — where "why" is recorded fact only (the args
the skill was invoked with, plus the prompt it was serving), never a
synthesized motive.

Lifecycle mirrors the loop cards: a session leaves the view automatically when
it finishes (session_end) or goes stale (idle past ACTIVE_WINDOW_SECONDS);
manual delete appends the same ``loop_run_dismissed`` tombstone keyed by the
session id, and a dismissed-but-still-active session resurfaces on new events
(never silently hide live work). ``online`` means Claude is actively working —
tool events within ONLINE_WINDOW_SECONDS."""
import time

from . import events as ev_mod
from .briefing import headline
from .loop.state import _ts_epoch

ONLINE_WINDOW_SECONDS = 180.0        # tool events this fresh = Claude is working
ACTIVE_WINDOW_SECONDS = 6 * 3600.0   # idle longer than this = session aged out
_DONE_CAP = 8                        # per detail group, keep the card readable

# telemetry that counts as "the session is doing something"
_ACTIVITY_TYPES = {
    "session_start", "file_touched", "command_run", "user_prompt",
    "assistant_response", "compaction", "skill_used",
} | ev_mod.SEMANTIC_TYPES


def _text(payload) -> str:
    if isinstance(payload, dict):
        return payload.get("text", "") or payload.get("path", "") or ""
    return str(payload)


def interactive_sessions(project=None, *, now_fn=time.time, evs=None,
                         online_seconds=ONLINE_WINDOW_SECONDS,
                         active_seconds=ACTIVE_WINDOW_SECONDS) -> list[dict]:
    """Active interactive-session records, newest first, shaped like the loop
    run records (plus ``kind: "chat"``) so the dashboard renders one list."""
    if evs is None:
        evs = ev_mod.read_all(project)
    sessions: dict[str, dict] = {}
    order: list[str] = []
    dismissed: dict[str, str] = {}
    prompt_now: dict[str, str] = {}      # latest prompt headline per session
    for e in evs:
        if e.type == "loop_run_dismissed":
            rid = (e.payload or {}).get("run_id")
            if rid:
                dismissed[rid] = e.ts
            continue
        sid = e.session_id
        if not sid or sid == "manual" or e.type.startswith("loop_"):
            continue
        s = sessions.get(sid)
        if s is None:
            s = sessions[sid] = {
                "run_id": sid, "kind": "chat", "title": "", "goal_dir": "",
                "started": e.ts, "status": "running", "iterations": 0,
                "total_cost_usd": 0.0, "total_tokens": 0, "last_event_ts": e.ts,
                "ended": False, "doing": None, "done": [], "planned": [],
                "not_yet": [], "files": [], "task": "", "skills": [],
                # provenance: Self-Reliant runs log into research-<id> streams;
                # the label rides the card so Oversee shows whose work this is
                # (filled by the per-event pass below — same idiom as
                # dashboard._get_sessions)
                "origin": None,
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
                prompt_now[sid] = t
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
        elif e.type == "skill_used" and isinstance(e.payload, dict):
            name = e.payload.get("name")
            if name:
                s["skills"].append({
                    "name": str(name),
                    "plugin": str(e.payload.get("plugin") or ""),
                    "ts": e.ts,
                    "ok": bool(e.payload.get("ok", True)),
                    "why": headline(str(e.payload.get("args") or "")),
                    "during": prompt_now.get(sid, ""),
                })

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
        s["skills_total"] = len(s["skills"])
        s["skills"] = s["skills"][-_DONE_CAP:]
        s.pop("task", None)
        out.append(s)
    out.reverse()
    return out
