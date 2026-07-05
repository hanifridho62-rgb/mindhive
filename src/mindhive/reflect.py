# src/mindhive/reflect.py
"""Reflection pass: turn one incident into a structured prevention rule.

Pure helpers (build_prompt/parse_reflection) are unit-tested. reflect_incident
calls an injectable runner; the default shells out to the shared headless
`claude` helper (claude_cli) and is NOT unit-tested.

Also home to the auto-reflect batch machinery (KTD3): the per-project cooldown
marker `rules/.last_reflect`, the reflection-health notice the projector renders
into STATE.md, and reflect_batch — the bounded, cooldown-gated pass session_end
spawns detached.
"""
import calendar
import json
import time

from . import claude_cli, paths

_SYSTEM = (
    "You are the Reflection Engine. Analyze ONE execution failure and output ONLY "
    "a JSON object — no prose, no code fences — with keys: root_cause, lesson, "
    "prevention_rule, task_category. task_category is one of: architecture, syntax, "
    "git_merge, engine_logic, dependency, tool_use. prevention_rule is a single "
    "imperative sentence a future agent can follow to avoid this failure."
)


def build_prompt(signature, error_excerpt, actions) -> str:
    acts = "\n".join(f"- {a}" for a in (actions or [])) or "- (unknown)"
    return (f"{_SYSTEM}\n\nSIGNATURE: {signature}\nFAILING ACTIONS:\n{acts}\n"
            f"ERROR:\n{error_excerpt}\n\nReturn the JSON now.")


def parse_reflection(text: str) -> dict:
    """Extract the JSON verdict from a model response, tolerating fences/prose."""
    if not text:
        return {}
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return {}
    try:
        obj = json.loads(text[s:e + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _claude_runner(prompt: str, timeout_s: int = 120) -> str:
    """Default runner: one-shot headless `claude` call, returns the result text
    (empty on any spawn failure or an is_error envelope)."""
    return claude_cli.result_text(
        claude_cli.run_headless(prompt, timeout_s=timeout_s)["stdout"])


from . import incidents


def _auto_section(verdict: dict) -> str:
    return (f"- Root cause: {verdict.get('root_cause', '').strip()}\n"
            f"- Lesson: {verdict.get('lesson', '').strip()}\n"
            f"- Prevention rule: {verdict.get('prevention_rule', '').strip()}\n"
            f"- Category: {verdict.get('task_category', '').strip()}")


def reflect_incident(signature, project=None, runner=_claude_runner) -> dict:
    """Reflect one incident: build a prompt from its Raw Signal, run the model,
    write the Auto Evaluation section, and re-derive the Active rule."""
    d = incidents._incident_dir(project)
    path = d / f"{incidents._safe_name(signature)}.md"
    if not path.exists():
        return {"reflected": False, "reason": "no such incident"}
    secs = incidents._split_sections(path.read_text(encoding="utf-8"))
    raw = secs.get("Raw Signal", "")
    verdict = parse_reflection(runner(build_prompt(signature, raw, [])))
    if not verdict.get("prevention_rule"):
        return {"reflected": False, "reason": "empty verdict"}
    # Hold the incident write guard across re-read → replace → write → project so
    # no other writer — another thread OR another process (a `hive rules reflect`
    # CLI run, a second dashboard) — can clobber this verdict mid-flight. The
    # model call above is deliberately OUTSIDE the guard — it can take 120s.
    with incidents._write_guard(project):
        txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                         "Auto Evaluation", _auto_section(verdict))
        incidents._atomic_write(path, txt)
        incidents.project(project)      # re-derive Active + rule_source, preserve sections
    return {"reflected": True}


# --- auto-reflect batch: cooldown marker + health + bounded pass (KTD3) ------

HEALTH_LAG_HOURS = 48


def _marker_path(project=None):
    return paths.project_dir(project) / "rules" / ".last_reflect"


def read_marker(project=None) -> dict:
    """The per-project reflection marker (last_attempt / last_success /
    last_error / cooldown_started). Empty dict when missing or corrupt."""
    try:
        obj = json.loads(_marker_path(project).read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_marker(project=None, data=None) -> None:
    p = _marker_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data or {}, indent=2), encoding="utf-8")


def _ts_epoch(ts):
    """Epoch seconds for an events.now_iso() timestamp, or None."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def health_note(project=None) -> str:
    """One-line reflection-health warning for STATE.md, or "". Warns when the
    last attempt has no success within HEALTH_LAG_HOURS behind it — an expired
    CLI auth must not silently regress the learning loop."""
    m = read_marker(project)
    attempt = _ts_epoch(m.get("last_attempt"))
    if attempt is None:
        return ""
    success = _ts_epoch(m.get("last_success"))
    if success is not None and attempt - success <= HEALTH_LAG_HOURS * 3600:
        return ""
    err = m.get("last_error") or "unknown"
    return (f"⚠ Reflection health: auto-reflection is failing (last error: {err}; "
            f"last attempt {m.get('last_attempt')}, last success "
            f"{m.get('last_success') or 'never'}). Check `claude` CLI auth, then "
            f"run `hive rules reflect`.")


def _refresh_projections(project=None) -> None:
    """After ≥1 successful reflection, re-run projection (+ native sync when the
    namespace is registered with a folder) so new rules reach STATE.md/MEMORY.md
    before the next session, not two sessions later. Best-effort: a sync failure
    degrades to a plain projection rebuild. Local imports keep the module import
    graph cycle-free (projector imports this module for health_note)."""
    from . import projector
    name = project or paths.detect_project()
    folder = None
    try:
        from . import autosync
        for e in autosync.load_registry():
            if e.get("project") == name and e.get("folder"):
                folder = e["folder"]
                break
    except Exception:
        folder = None
    if folder:
        try:
            from . import sync as sync_mod
            sync_mod.sync(folder, project=name)
            return
        except Exception:
            pass
    projector.project(name)


def reflect_batch(project=None, limit=3, cooldown_hours=6.0, runner=None) -> dict:
    """Bounded, cooldown-gated reflection pass over active un-reflected
    incidents. Marker semantics (KTD3): the cooldown timestamp is written BEFORE
    the first runner call; a batch that reflects zero incidents rolls the
    cooldown back (failures don't consume the window) but keeps last_attempt +
    last_error for the health notice. Never raises for a failing runner."""
    from . import events as ev_mod
    runner = runner or _claude_runner       # late-bound so tests can stub _claude_runner
    marker = read_marker(project)
    prev_cooldown = marker.get("cooldown_started")
    cd = _ts_epoch(prev_cooldown)
    if cd is not None and (time.time() - cd) < cooldown_hours * 3600:
        return {"reflected": 0, "attempted": 0, "skipped": "cooldown"}
    incidents.audit(project=project)
    incidents.project(project=project)
    targets = [r["signature"] for r in incidents.summaries(project=project)
               if r["status"] == "active" and r["rule_source"] == "none"]
    targets = targets[:max(0, int(limit))]
    if not targets:
        return {"reflected": 0, "attempted": 0, "skipped": "nothing to reflect"}
    now = ev_mod.now_iso()
    marker.update({"cooldown_started": now, "last_attempt": now})
    write_marker(project, marker)           # consume the window BEFORE the first call
    done, last_error = 0, ""
    for signature in targets:
        try:
            res = reflect_incident(signature, project=project, runner=runner)
        except Exception as e:              # runner blew up: record class, keep going
            res = {"reflected": False, "reason": type(e).__name__}
        if res.get("reflected"):
            done += 1
        else:
            last_error = res.get("reason") or "unknown"
    marker = read_marker(project)
    if done:
        marker["last_success"] = ev_mod.now_iso()
        marker["last_error"] = ""
        write_marker(project, marker)
        _refresh_projections(project)       # rules land in the very next session
    else:
        marker["cooldown_started"] = prev_cooldown   # roll back: window not consumed
        marker["last_error"] = last_error
        write_marker(project, marker)
    return {"reflected": done, "attempted": len(targets)}
