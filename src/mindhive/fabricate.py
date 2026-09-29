# SPDX-License-Identifier: Apache-2.0
# src/mindhive/fabricate.py
"""The PRD assembly line: compile a PRD into an executable work order, put
it on an OS timer, and run it unattended with the loop engine.

Self-Reliant learn mode (research.py) already writes Executable PRDs — a
TITLE/SUMMARY header, a goal, an SOP whose every step names an installed
skill, and acceptance criteria. Until now a PRD was a *memory artifact*: a
human had to read it, hand-build a plan, and babysit the run. This module
closes that gap end to end:

- ``parse_prd`` / ``compile_work_order`` — parse the PRD (tolerant of
  markdown-heading and "(4) SOP —" contract styles), then materialize it as
  a loop goal dir (``.hive-fab/<fab_id>/`` with PROMPT.md carrying the goal
  + acceptance criteria and fix_plan.md carrying the SOP as the checklist)
  plus a roadmap plan (tagged ``fabricate``) whose steps are the SOP.
- ``dispatch`` — register the same per-user Windows scheduled task
  machinery autoresume uses, aimed at ``hive fabricate run <id>``, due
  immediately or at a chosen time ("throw it at the weekend").
- ``run`` — execute the loop engine on the goal dir with the fabrication's
  plan_id. From here the existing resilience does the rest: a usage cap
  checkpoints progress into THE fabrication roadmap (the fix_plan checklist
  is the source of truth) and re-arms a loop-mode auto-resume, so the cycle
  is loop -> sleep -> loop until the exit gate passes, or until
  autoresume.MAX_ATTEMPTS wake-ups in a row check off nothing new.
  Completion earns the roadmap its exit from every view.

Lifecycle is three ``fabrication`` events (deterministic telemetry;
``roadmap`` events remain the semantic plan, ``loop_*`` the run telemetry):
``compiled`` -> ``dispatched`` -> ``run`` (one per loop launch, recording
the run_id join and the exit reason). Current state is a total fold.
"""
import os
import re
import time
import calendar
from pathlib import Path

from . import events as ev_mod
from . import autoresume
from . import paths
from . import roadmap
from .sop import ID_RE

__all__ = ["ACTIONS", "FAB_TAG", "GOAL_ROOT", "TS_FMT", "MIN_LEAD_S",
           "MAX_TITLE_CHARS", "MAX_PATH_CHARS", "MAX_REASON_CHARS",
           "parse_prd", "slug", "validate_payload", "fold",
           "compile_work_order", "dispatch", "run", "ledger_body"]

ACTIONS = ("compiled", "dispatched", "run")

FAB_TAG = "fabricate"          # roadmap plans this module owns
GOAL_ROOT = ".hive-fab"        # per-fabrication goal dirs live under here

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
MIN_LEAD_S = 60

MAX_TITLE_CHARS = 120
MAX_PATH_CHARS = 500
MAX_REASON_CHARS = 60


# --- PRD parsing --------------------------------------------------------------

# Section heads in either style: "## SOP", "#### 4. SOP", "(4) SOP —", "4) SOP".
_SECTION_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:\(?\d+\)?[.)]?\s*)?"
    r"(problem|goal|requirements|sop|acceptance)\b.*$",
    re.IGNORECASE)
_TITLE_RE = re.compile(r"^\s*(?:#+\s*)?title\s*:\s*(.+?)\s*$", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"^\s*(?:#+\s*)?summary\s*:\s*(.+?)\s*$",
                         re.IGNORECASE)
_STEP_RE = re.compile(r"^\s*(?:\d+[.)]\s+|[-*]\s+(?:\[.\]\s*)?)(.+?)\s*$")


def _sections(text) -> dict:
    """Split a PRD body into {section_key: [lines]} — tolerant: whatever
    section heads exist are honored, everything before the first head is
    ignored (the header lines are parsed separately)."""
    out: dict = {}
    current = None
    for line in (text or "").splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).lower()
            out.setdefault(current, [])
            continue
        if current is not None:
            out[current].append(line)
    return out


def _steps_of(lines) -> list:
    steps = []
    for ln in lines or []:
        m = _STEP_RE.match(ln)
        if m and m.group(1).strip():
            steps.append(m.group(1).strip())
    return steps


def parse_prd(text) -> dict:
    """PRD text -> {title, summary, goal, sop, acceptance}. ``sop`` and
    ``acceptance`` are lists of step strings; the rest are strings. Raises
    ValueError when there is no SOP — a PRD without a build procedure is
    not executable, and silently fabricating from one would run nothing."""
    text = (text or "").strip()
    title = summary = ""
    for ln in text.splitlines()[:10]:
        tm, sm = _TITLE_RE.match(ln), _SUMMARY_RE.match(ln)
        if tm and not title:
            title = tm.group(1).strip()
        elif sm and not summary:
            summary = sm.group(1).strip()
    secs = _sections(text)
    sop = _steps_of(secs.get("sop"))
    if not sop:
        raise ValueError(
            "PRD has no SOP steps — an executable PRD needs a numbered SOP "
            "section (the Self-Reliant learn-mode contract emits one)")
    goal = "\n".join(ln for ln in (secs.get("goal") or []) if ln.strip())
    acceptance = _steps_of(secs.get("acceptance"))
    if not acceptance:
        acceptance = [ln.strip() for ln in (secs.get("acceptance") or [])
                      if ln.strip()]
    if not title:
        for ln in text.splitlines():
            if ln.strip():
                title = ln.strip().lstrip("# ").strip()
                break
    return {"title": title[:MAX_TITLE_CHARS], "summary": summary,
            "goal": goal.strip(), "sop": sop, "acceptance": acceptance}


def slug(title) -> str:
    """`fab-<kebab-of-title>` fitting the shared ID_RE contract."""
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return ("fab-" + (s or "prd"))[:64].rstrip("-")


# --- payload contract ---------------------------------------------------------

def _ts_ok(ts) -> bool:
    try:
        time.strptime(str(ts), TS_FMT)
        return True
    except (ValueError, TypeError):
        return False


def validate_payload(payload) -> None:
    """The fabrication event contract, enforced at every append surface
    (this module's writers and the raw ``hive append fabrication`` path)."""
    if not isinstance(payload, dict):
        raise ValueError("fabrication payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError(
            "fabrication requires an action — one of: " + ", ".join(ACTIONS))
    if not ID_RE.match(payload.get("fab_id") or ""):
        raise ValueError(
            "fabrication requires a kebab-case fab_id, e.g. fab-auth-refactor")
    if action == "compiled":
        if not str(payload.get("goal_dir") or "").strip():
            raise ValueError("fabrication compiled requires the goal_dir")
        if len(str(payload.get("goal_dir"))) > MAX_PATH_CHARS:
            raise ValueError(
                f"fabrication goal_dir exceeds {MAX_PATH_CHARS} chars")
        if len(str(payload.get("title") or "")) > MAX_TITLE_CHARS:
            raise ValueError(
                f"fabrication title exceeds {MAX_TITLE_CHARS} chars")
        steps = payload.get("steps")
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
            raise ValueError(
                "fabrication compiled requires an integer steps count >= 1")
    elif action == "dispatched":
        if not _ts_ok(payload.get("due")):
            raise ValueError(
                "fabrication dispatched requires a due timestamp "
                "(UTC, e.g. 2026-07-18T15:00:00Z)")
        timer = payload.get("timer")
        if timer is not None and timer not in ("windows_task", "none"):
            raise ValueError(
                "fabrication timer must be 'windows_task' or 'none'")
        if len(str(payload.get("cwd") or "")) > MAX_PATH_CHARS:
            raise ValueError(
                f"fabrication cwd exceeds {MAX_PATH_CHARS} chars")
    elif action == "run":
        if not str(payload.get("run_id") or "").strip():
            raise ValueError("fabrication run requires the loop run_id join")
        if len(str(payload.get("reason") or "")) > MAX_REASON_CHARS:
            raise ValueError(
                f"fabrication reason exceeds {MAX_REASON_CHARS} chars")
        its = payload.get("iterations")
        if its is not None and (not isinstance(its, int)
                                or isinstance(its, bool) or its < 0):
            raise ValueError("fabrication iterations must be an int >= 0")


# --- fold ---------------------------------------------------------------------

def fold(evs) -> dict:
    """Deterministic fold of fabrication events into fab_id -> record.
    Total — malformed rows are skipped; a run/dispatch on an uncompiled id
    still folds (history written by other code versions)."""
    recs: dict = {}
    for ev in evs:
        if ev.type != "fabrication" or not isinstance(ev.payload, dict):
            continue
        p = ev.payload
        action, fab_id = p.get("action"), p.get("fab_id")
        if action not in ACTIONS or not fab_id:
            continue
        rec = recs.get(fab_id) or {
            "fab_id": fab_id, "status": "", "title": "", "goal_dir": "",
            "cwd": "", "due": "", "timer": "none", "steps": 0,
            "run_id": "", "reason": "", "iterations": 0,
            "created": ev.ts, "updated": ev.ts}
        rec = dict(rec)
        if action == "compiled":
            goal_dir = str(p.get("goal_dir") or "")
            if not goal_dir:
                continue
            rec.update(status="compiled",
                       goal_dir=goal_dir[:MAX_PATH_CHARS],
                       title=str(p.get("title") or "")[:MAX_TITLE_CHARS],
                       steps=p.get("steps") if isinstance(p.get("steps"), int)
                       and not isinstance(p.get("steps"), bool) else 0)
        elif action == "dispatched":
            if not _ts_ok(p.get("due")):
                continue
            rec.update(status="dispatched", due=str(p["due"]),
                       cwd=str(p.get("cwd") or "")[:MAX_PATH_CHARS],
                       timer=p.get("timer") if p.get("timer") in
                       ("windows_task", "none") else "none")
        elif action == "run":
            reason = str(p.get("reason") or "")[:MAX_REASON_CHARS]
            rec.update(status="completed" if reason == "completed"
                       else "stopped",
                       run_id=str(p.get("run_id") or ""), reason=reason,
                       iterations=p.get("iterations") if
                       isinstance(p.get("iterations"), int)
                       and not isinstance(p.get("iterations"), bool) else 0)
        rec["updated"] = ev.ts
        recs[fab_id] = rec
    return recs


# --- compile ------------------------------------------------------------------

_PROMPT_TMPL = """# {title}

{summary}

## Goal

{goal}

## Acceptance criteria (the exit gate — ALL must hold before EXIT_SIGNAL)

{acceptance}

## Discipline

- Work through fix_plan.md in order; check items off as they land.
- Tests must be green before any item is checked off as done.
- Record a `hive log` change-log entry for changes to src/ or tests/.
"""


def compile_work_order(prd_text, *, fab_id=None, base_dir=None, source="",
                       project=None, evs=None, session_id=None) -> dict:
    """PRD text -> goal dir + roadmap + compiled event. Refuses to stomp a
    roadmap this module does not own (no ``fabricate`` tag); re-compiling an
    owned id is a deliberate re-plan (roadmap ``set`` upserts). Returns
    {fab_id, goal_dir, title, steps}."""
    prd = parse_prd(prd_text)
    fab_id = fab_id or slug(prd["title"])
    if not ID_RE.match(fab_id):
        raise ValueError(
            "fab_id must be kebab-case (lowercase letters, digits, hyphens)")
    if evs is None:
        evs = ev_mod.read_all(project)
    existing = roadmap.get(evs, fab_id)
    # Ownership is the fabrication record itself, not the roadmap tag: the
    # engine's interruption snapshot re-``set``s the plan with bare roadmap
    # tags, and that must never lock this module out of its own plan.
    if existing is not None and FAB_TAG not in (existing.get("tags") or []) \
            and fab_id not in fold(evs):
        raise ValueError(
            f"roadmap '{fab_id}' exists and is not fabricate-owned — pick "
            "another id with --id")
    base = Path(base_dir or os.getcwd())
    goal_dir = base / GOAL_ROOT / fab_id
    from .loop import scaffold as scaffold_mod
    scaffold_mod.scaffold(goal_dir)         # AGENT.md + loop.toml defaults
    steps = [s[:roadmap.MAX_STEP_CHARS]
             for s in prd["sop"][:roadmap.MAX_STEPS]]
    acceptance = "\n".join(f"- {a}" for a in prd["acceptance"]) \
        or "- The SOP is fully executed and the suite is green."
    (goal_dir / "PROMPT.md").write_text(
        _PROMPT_TMPL.format(title=prd["title"] or fab_id,
                            summary=prd["summary"],
                            goal=prd["goal"] or prd["summary"]
                            or prd["title"],
                            acceptance=acceptance),
        encoding="utf-8")
    (goal_dir / "fix_plan.md").write_text(
        "# Fix plan (compiled from the PRD's SOP)\n\n"
        + "\n".join(f"- [ ] {s}" for s in steps) + "\n",
        encoding="utf-8")
    prompt = (prd["title"] + (" — " + prd["summary"] if prd["summary"]
                              else ""))[:roadmap.MAX_PROMPT_CHARS]
    roadmap.record("set", fab_id, prompt=prompt,
                   title=prd["title"][:roadmap.MAX_TITLE_CHARS],
                   steps=steps, tags=[FAB_TAG], session_id=session_id,
                   project=project, evs=evs)
    payload = {"action": "compiled", "fab_id": fab_id,
               "goal_dir": str(goal_dir)[:MAX_PATH_CHARS],
               "title": prd["title"], "steps": len(steps),
               "source": str(source or "")[:MAX_PATH_CHARS]}
    validate_payload(payload)
    ev_mod.append_event("fabrication", payload, session_id=session_id,
                        tags=[FAB_TAG], project=project)
    return {"fab_id": fab_id, "goal_dir": str(goal_dir),
            "title": prd["title"], "steps": len(steps)}


# --- dispatch -----------------------------------------------------------------

def _iso(epoch: float) -> str:
    return time.strftime(TS_FMT, time.gmtime(epoch))


def _task_name(fab_id) -> str:
    return f"MindHive Fabricate {fab_id}"


def dispatch(fab_id, *, project=None, evs=None, at=None, in_hours=None,
             session_id=None, register=None, now=None) -> dict:
    """Arm the OS timer that runs ``hive fabricate run <fab_id>`` — due
    almost immediately by default, or at ``at`` / now+``in_hours`` for a
    deferred start ("throw it at the weekend"). The dispatched event is the
    source of truth; a failed timer registration degrades to timer "none"
    with `hive autoresume wait`-style manual delivery via ``run``."""
    if evs is None:
        evs = ev_mod.read_all(project)
    rec = fold(evs).get(fab_id)
    if rec is None or not rec.get("goal_dir"):
        raise ValueError(
            f"no compiled fabrication '{fab_id}' — compile the PRD first: "
            "hive fabricate compile <prd.md>")
    if not (Path(rec["goal_dir"]) / "PROMPT.md").exists():
        raise ValueError(
            f"fabrication '{fab_id}' goal dir vanished ({rec['goal_dir']}) — "
            "re-compile the PRD")
    now = time.time() if now is None else now
    if at is not None:
        try:
            due_e = calendar.timegm(time.strptime(str(at), TS_FMT))
        except (ValueError, TypeError):
            raise ValueError("fabricate --at must be a UTC timestamp like "
                             "2026-07-18T15:00:00Z")
        due = _iso(max(due_e, now + MIN_LEAD_S))
    elif in_hours is not None:
        due = _iso(now + float(in_hours) * 3600)
    else:
        due = _iso(now + MIN_LEAD_S)
    cwd = str(Path(rec["goal_dir"]).parent.parent)   # the repo root
    try:
        effective = project or paths.detect_project()
    except Exception:
        effective = project
    arg = f"-m mindhive fabricate run {fab_id}"
    if effective:
        arg += f" --project {effective}"
    try:
        timer = (register or autoresume.register_windows_task)(
            _task_name(fab_id), due, cwd, arg) or "none"
    except Exception:
        timer = "none"
    payload = {"action": "dispatched", "fab_id": fab_id, "due": due,
               "timer": timer, "cwd": cwd[:MAX_PATH_CHARS]}
    validate_payload(payload)
    ev_mod.append_event("fabrication", payload, session_id=session_id,
                        tags=[FAB_TAG], project=project)
    return payload


# --- run ----------------------------------------------------------------------

def _run_loop(project, goal_dir, plan_id, force=False):
    from .loop import config as loop_config
    from .loop import driver as loop_driver
    from .loop import engine as loop_engine
    if not loop_driver.preflight("claude"):
        return {"reason": "spawn_failed", "iterations": 0, "run_id": "none"}
    cfg = loop_config.load_config(Path(goal_dir) / "loop.toml")
    return loop_engine.run_loop(
        project=project, loop_dir=Path(goal_dir), cfg=cfg,
        driver=loop_driver.CLIDriver("claude"), force=force, plan_id=plan_id)


def run(fab_id, *, project=None, session_id=None, run_loop_fn=None,
        force=False, unregister=None) -> dict:
    """Execute one loop launch for the fabrication (what the dispatch timer
    runs). A usage-capped launch leaves the roadmap interrupted and a
    loop-mode auto-resume armed (the engine does both; inside an auto-resume
    wake-up the wake-up itself re-arms and counts the attempt), so the next
    window continues it; a completed launch earns the roadmap its exit.
    Returns the recorded run payload."""
    evs = ev_mod.read_all(project)
    rec = fold(evs).get(fab_id)
    if rec is None or not rec.get("goal_dir") \
            or not (Path(rec["goal_dir"]) / "PROMPT.md").exists():
        raise ValueError(
            f"no runnable fabrication '{fab_id}' — compile the PRD first: "
            "hive fabricate compile <prd.md>")
    try:
        result = (run_loop_fn or _run_loop)(project, rec["goal_dir"], fab_id,
                                            force=force)
    except RuntimeError as e:              # LIVE_LOCK guard refusal
        result = {"reason": "guard_refused", "iterations": 0,
                  "run_id": "none", "error": str(e)}
    reason = str(result.get("reason") or "")[:MAX_REASON_CHARS]
    payload = {"action": "run", "fab_id": fab_id,
               "run_id": str(result.get("run_id") or "none"),
               "reason": reason,
               "iterations": result.get("iterations")
               if isinstance(result.get("iterations"), int) else 0}
    validate_payload(payload)
    ev_mod.append_event("fabrication", payload, session_id=session_id,
                        tags=[FAB_TAG], project=project)
    if reason == "completed":
        # The exit gate passed: close the plan (idempotent — skip when a
        # human already did) and drop the dispatch timer.
        cur = roadmap.get(ev_mod.read_all(project), fab_id)
        if cur is not None and cur["status"] != "completed":
            try:
                roadmap.record("complete", fab_id, tags=[FAB_TAG],
                               session_id=session_id, project=project)
            except ValueError:
                pass
        try:
            (unregister or autoresume.unregister_windows_task)(
                _task_name(fab_id))
        except Exception:
            pass
    return payload


def ledger_body(payload) -> str:
    """Self-describing markdown body should a fabrication event ever be
    projected — symmetry with the other registries."""
    action = payload.get("action", "")
    head = f"[{action}] fabrication {payload.get('fab_id', '')}"
    parts = [head]
    if payload.get("title"):
        parts.append(str(payload["title"]))
    if payload.get("goal_dir"):
        parts.append(f"goal dir: {payload['goal_dir']}")
    if payload.get("due"):
        parts.append(f"due: {payload['due']} (timer: "
                     f"{payload.get('timer', 'none')})")
    if payload.get("reason"):
        parts.append(f"loop run {payload.get('run_id', '')}: "
                     f"{payload['reason']} "
                     f"({payload.get('iterations', 0)} iteration(s))")
    return "\n\n".join(parts)
