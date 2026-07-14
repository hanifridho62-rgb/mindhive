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
import os
import re
import time
from contextlib import contextmanager

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


# --- deterministic fallback verdicts -----------------------------------------
# The model runner is the preferred reflection engine, but it dies whole-hog on
# an expired `claude` CLI auth (401 -> empty verdict) — which silently froze
# the learning loop for weeks. For CURATED signature families the prevention
# rule is mechanical and known in advance, so a template verdict is strictly
# better than no verdict. Families with genuinely unknown root causes (raw:*,
# judgment:*) get NO fallback — fabricating rules there would mislead.

def _fb(root, lesson, rule, cat):
    return {"root_cause": root, "lesson": lesson + " (deterministic fallback rule)",
            "prevention_rule": rule, "task_category": cat}


_FALLBACK_EXACT = {
    "edit:not-read": _fb(
        "Edit/Write was attempted on a file never Read in this session — the "
        "harness read-gate rejects it.",
        "A Glob or Grep hit does not satisfy the read-gate; only Read does.",
        "Before any Edit or Write, call Read on that exact absolute path in "
        "this session first; when editing several files, Read them all up "
        "front in one batch, then edit.", "tool_use"),
    "edit:string-not-found": _fb(
        "Edit's old_string did not match the file — it was paraphrased or the "
        "file changed after the last Read.",
        "old_string must be copied verbatim from a fresh Read, including "
        "whitespace and indentation.",
        "Re-Read the target file immediately before each Edit and copy "
        "old_string exactly from that read; if the edit still fails, shrink "
        "old_string to a smaller unique snippet.", "tool_use"),
    "tool:ripgrep-timeout": _fb(
        "An unscoped Grep searched the whole tree from the root and timed out.",
        "Unbounded repo-wide searches are the known slow path here.",
        "Always bound Grep with a path, glob, or type filter — never run an "
        "unscoped repo-wide pattern.", "tool_use"),
    "read:file-too-large": _fb(
        "Read was called on a file exceeding the harness size limit.",
        "Whole-file reads of large artifacts always fail; ranged reads do not.",
        "Read large files in bounded chunks using offset and limit instead of "
        "one whole-file Read.", "tool_use"),
    "git:worktree-locked": _fb(
        "git worktree remove failed with Permission denied — a process still "
        "held a handle inside the tree (common on Windows).",
        "Plain worktree removal is unreliable on Windows while editors/AV "
        "hold handles.",
        "Use `git worktree remove --force <path>` from OUTSIDE the target "
        "tree, never cd into the tree being removed, then run "
        "`git worktree prune`.", "git_merge"),
    "pysyntax:unterminated-string": _fb(
        "Generated Python contained an unterminated string literal.",
        "Multi-line strings need triple quotes or a heredoc, not escaped "
        "single-line quoting.",
        "Balance every quote before executing generated Python; prefer "
        "triple-quoted strings or a heredoc for multi-line text.", "syntax"),
    "pysyntax:other": _fb(
        "Generated Python failed to parse (SyntaxError).",
        "Executing unchecked generated code wastes a failure cycle.",
        "Syntax-check generated Python (py_compile or an import smoke test) "
        "before executing it.", "syntax"),
}


def fallback_verdict(signature: str) -> dict:
    """Deterministic verdict for a curated signature family, or {} when the
    family is unknown (raw:*/judgment:*/anything unrecognized) — the fallback
    must never fabricate a rule for a root cause it cannot actually know."""
    sig = str(signature or "")
    if sig in _FALLBACK_EXACT:
        return dict(_FALLBACK_EXACT[sig])
    if sig.startswith("pymodule:"):
        pkg = sig[len("pymodule:"):] or "the missing package"
        return _fb(
            f"Code imported '{pkg}' but it is not installed in the "
            "environment that ran it.",
            "Environment-dependent imports must be verified before use.",
            f"Before running code that imports '{pkg}', verify it is "
            "installed in the target environment (quick import check) and "
            "install it or guard the import if not.", "dependency")
    if sig.startswith("pscmd:"):
        cmd = sig[len("pscmd:"):] or "that command"
        return _fb(
            f"'{cmd}' is not recognized by Windows PowerShell — it is a "
            "Unix/other-shell command or is not on PATH.",
            "PowerShell and POSIX shells do not share a command vocabulary.",
            f"Do not call '{cmd}' in PowerShell — use the PowerShell-native "
            "equivalent, or run it through the Bash tool instead.", "tool_use")
    if sig.startswith("skill:"):
        name = sig[len("skill:"):] or "that skill"
        return _fb(
            f"Invoking the '{name}' skill failed.",
            "A skill that keeps failing must stop being auto-invoked until "
            "the failure is understood.",
            f"Before invoking '{name}' again, confirm it exists in the "
            "installed inventory and its required inputs are provided; after "
            "repeated failures, stop auto-invoking it and investigate.",
            "tool_use")
    return {}


def _auto_section(verdict: dict) -> str:
    return (f"- Root cause: {verdict.get('root_cause', '').strip()}\n"
            f"- Lesson: {verdict.get('lesson', '').strip()}\n"
            f"- Prevention rule: {verdict.get('prevention_rule', '').strip()}\n"
            f"- Category: {verdict.get('task_category', '').strip()}")


def reflect_incident(signature, project=None, runner=_claude_runner) -> dict:
    """Reflect one incident: build a prompt from its Raw Signal, run the model,
    write the Auto Evaluation section, and re-derive the Active rule. When the
    model runner fails or returns nothing usable (dead CLI, 401, timeout), a
    deterministic fallback verdict covers the curated signature families so
    the learning loop keeps moving without manual input."""
    d = incidents._incident_dir(project)
    path = d / f"{incidents._safe_name(signature)}.md"
    if not path.exists():
        return {"reflected": False, "reason": "no such incident"}
    secs = incidents._split_sections(path.read_text(encoding="utf-8"))
    raw = secs.get("Raw Signal", "")
    fail_reason = "empty verdict"
    try:
        verdict = parse_reflection(runner(build_prompt(signature, raw, [])))
    except Exception as e:         # keep the class for the health marker
        verdict = {}
        fail_reason = type(e).__name__
    if not verdict.get("prevention_rule"):
        verdict = fallback_verdict(signature)
    if not verdict.get("prevention_rule"):
        return {"reflected": False, "reason": fail_reason}
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
    """Atomic marker write (temp file + os.replace) so a concurrent reader —
    another session_end batch, the health notice — never sees a torn file."""
    p = _marker_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data or {}, indent=2), encoding="utf-8")
    os.replace(tmp, p)


_GATE_STALE_SECS = 600.0        # a crashed batch may hold the gate ~10 min max


def _gate_lock_path(project=None):
    return paths.project_dir(project) / "rules" / ".reflect.lock"


@contextmanager
def _cooldown_gate(project=None, timeout: float = 10.0,
                   stale_secs: float = _GATE_STALE_SECS):
    """Cross-process lock (O_CREAT|O_EXCL lockfile) around reflect_batch's
    cooldown read-check → marker-write gate so two racing session_end batches
    can't both pass the check and double-spend the window. Steals a lock older
    than ``stale_secs`` (crashed batch). Best-effort, mirroring incidents'
    policy: on timeout proceed unlocked rather than dropping the batch."""
    p = _gate_lock_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if p.exists() and (time.time() - p.stat().st_mtime) > stale_secs:
                p.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            break
        except OSError:                 # held by another process
            if time.monotonic() >= deadline:
                break                   # proceed unlocked (best-effort guard)
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            p.unlink()
        except OSError:
            pass


def _live_lock_blocks(project=None) -> bool:
    """True when the global LIVE_LOCK file names this namespace as a whole
    token. The lock text is tokenized on whitespace/punctuation separators and
    each token normalized like paths.normalize; only whole-token equality with
    the namespace matches — a namespace that is merely a substring of a longer
    token does not. Missing/unreadable lock -> False (proceed)."""
    try:
        text = (paths.store_home() / "LIVE_LOCK").read_text(encoding="utf-8")
    except OSError:
        return False
    name = paths.normalize(project or paths.detect_project())
    tokens = set()
    for t in re.split(r"[\s,;:]+", text):
        slug = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
        if slug:                        # never fall back to "default" here
            tokens.add(slug)
    return name in tokens


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
    last_error for the health notice. Never raises for a failing runner.

    Honors the global LIVE_LOCK (whole-token namespace match) BEFORE touching
    the marker, so both entry points (`hive rules reflect` and the detached
    session_end spawn) refuse while that namespace is live-trading. The
    cooldown read-check → marker-write gate runs under a cross-process lock,
    and the zero-reflected rollback is compare-and-swap: it only restores the
    previous cooldown_started when the marker still holds the value THIS
    process wrote (a newer batch's window is never clobbered)."""
    from . import events as ev_mod
    runner = runner or _claude_runner       # late-bound so tests can stub _claude_runner
    if _live_lock_blocks(project):
        return {"reflected": 0, "attempted": 0, "skipped": "live_lock"}
    with _cooldown_gate(project):
        marker = read_marker(project)
        prev_cooldown = marker.get("cooldown_started")
        cd = _ts_epoch(prev_cooldown)
        if cd is not None and (time.time() - cd) < cooldown_hours * 3600:
            return {"reflected": 0, "attempted": 0, "skipped": "cooldown"}
        incidents.audit(project=project)
        incidents.project(project=project)
        targets = [(r["signature"], "reflect")
                   for r in incidents.summaries(project=project)
                   if r["status"] == "active" and r["rule_source"] == "none"]
        # Active resolution: red leaking AUTO rules get an alternative route
        # in the same bounded, cooldown-gated pass (attempt-capped; human
        # rules are authoritative and never rerouted). Lazy import — resolve
        # imports this module at load time.
        from . import resolve as resolve_mod
        seen = {sig for sig, _ in targets}
        for r in resolve_mod.red_summaries(project):
            if r.get("rule_source") == "auto" and r["signature"] not in seen \
                    and int(r.get("reroutes") or 0) < incidents.MAX_REROUTES:
                targets.append((r["signature"], "reroute"))
        targets = targets[:max(0, int(limit))]
        if not targets:
            return {"reflected": 0, "attempted": 0, "skipped": "nothing to reflect"}
        now = ev_mod.now_iso()
        marker.update({"cooldown_started": now, "last_attempt": now})
        write_marker(project, marker)       # consume the window BEFORE the first call
    done, rerouted, last_error = 0, 0, ""
    for signature, mode in targets:
        try:
            if mode == "reroute":
                r = resolve_mod.reroute_incident(signature, project=project,
                                                 runner=runner)
                ok = bool(r.get("resolved"))
            else:
                r = reflect_incident(signature, project=project, runner=runner)
                ok = bool(r.get("reflected"))
        except Exception as e:              # runner blew up: record class, keep going
            r, ok = {"reason": type(e).__name__}, False
        if ok:
            done += 1
            rerouted += mode == "reroute"
        else:
            last_error = r.get("reason") or "unknown"
    marker = read_marker(project)
    if done:
        marker["last_success"] = ev_mod.now_iso()
        marker["last_error"] = ""
        write_marker(project, marker)
        try:
            # A just-written rule whose signature already has post-activation
            # exposure and zero leaks is proven on arrival — promote it now.
            incidents.auto_promote(project=project)
        except Exception:
            pass
        try:
            # Nudge->enforce ladder: a rerouted signature with a matcher may
            # have crossed the leak threshold — flip the hard-force rung too.
            from . import enforce
            enforce.auto_escalate(project=project)
        except Exception:
            pass
        _refresh_projections(project)       # rules land in the very next session
    else:
        if marker.get("cooldown_started") == now:    # CAS: our write, roll it back
            marker["cooldown_started"] = prev_cooldown
        marker["last_error"] = last_error
        write_marker(project, marker)
    return {"reflected": done - rerouted, "rerouted": rerouted,
            "attempted": len(targets)}
