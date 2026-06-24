# src/mindhive/hookcapture.py
"""Python-side handler for the live Claude Code hooks.

The PostToolUse hook previously parsed the Claude payload in PowerShell and
swallowed every failure with `catch {}`, so a renamed field (e.g.
``tool_input`` -> ``toolInput``) would silently stop all live capture with no
signal. Moving the extraction here makes the fragile field-name coupling
unit-testable and records failures to ``hooks_errors.log`` instead of dropping
them. The ``.ps1`` hook becomes a thin pipe: it just forwards raw stdin.
"""
import json
import time
from . import events, paths

# Mirror harvest.py's tool->event mapping so live capture and transcript
# back-harvest agree on what each tool means.
_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
_CMD_TOOLS = ("Bash", "PowerShell")
_CMD_MAX = 180


def extract_events(payload) -> list:
    """Pure extraction: map one PostToolUse payload to ``(type, value)`` pairs.

    Returns ``[]`` for unknown tools or missing fields — never raises on a
    well-formed dict with absent keys.
    """
    if not isinstance(payload, dict):
        return []
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") or {}
    if tool in _FILE_TOOLS:
        path = inp.get("file_path")
        return [("file_touched", path)] if path else []
    if tool in _CMD_TOOLS:
        cmd = inp.get("command")
        return [("command_run", cmd[:_CMD_MAX])] if cmd else []
    return []


def log_hook_error(stage: str, exc) -> None:
    """Append a one-line failure record so silent drops become detectable.

    Best-effort: a logging failure must never propagate out of a hook.
    """
    try:
        home = paths.store_home()
        home.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
        line = f"{ts}\t{stage}\t{type(exc).__name__}: {exc}\n"
        with open(home / "hooks_errors.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def record_post_tool_use(raw: str, project=None, now=None) -> dict:
    """Parse a raw PostToolUse payload and append the captured events.

    Returns ``{"appended": n}`` on success (``n`` may be 0 for empty input or
    unknown tools), or ``{"error": "..."}`` after logging — it never raises, so
    a hook can call it without risking the session.
    """
    if not raw or not raw.strip():
        return {"appended": 0}
    try:
        payload = json.loads(raw)
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        appended = 0
        for etype, value in extract_events(payload):
            events.append_event(etype, {"text": value}, session_id=sid,
                                tags=["auto"], ts=now, project=project)
            appended += 1
        return {"appended": appended}
    except Exception as e:
        log_hook_error("post_tool_use", e)
        return {"error": f"{type(e).__name__}: {e}"}
