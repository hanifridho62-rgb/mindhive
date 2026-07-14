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
_SKILL_TOOL = "Skill"
_CMD_MAX = 180
_SKILL_TEXT_MAX = 200


def _response_ok(resp) -> bool:
    """False only on explicit error markers in a Skill tool_response envelope
    (``is_error``/``isError`` truthy, or a non-empty ``error`` value)."""
    if isinstance(resp, dict):
        if resp.get("is_error") or resp.get("isError"):
            return False
        if resp.get("error"):
            return False
    return True


def _response_text(resp) -> str:
    """Best-effort short text from a Skill tool_response: a plain string, the
    first non-empty string field, or joined ``text`` parts of a content-block
    list. Empty string when nothing readable is found."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        for key in ("text", "result", "output", "error", "content"):
            v = resp.get(key)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, list):
                parts = [b.get("text", "") for b in v
                         if isinstance(b, dict) and isinstance(b.get("text"), str)]
                joined = " ".join(x for x in parts if x).strip()
                if joined:
                    return joined
    return ""


def _skill_payload(inp, resp) -> dict | None:
    """Build the ``skill_used`` event payload from a Skill-tool PostToolUse
    payload: ``{name, plugin?, ok, text}``. The skill name comes from
    ``tool_input`` (``plugin:skill`` namespacing is split into the two fields);
    ``ok`` reflects the tool_response error markers. None when unparseable."""
    raw = None
    for key in ("skill", "name", "skill_name"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            raw = v.strip()
            break
    if not raw:
        return None
    plugin, _, name = raw.rpartition(":")
    out = {"name": name or raw, "ok": _response_ok(resp),
           "text": _response_text(resp).strip()[:_SKILL_TEXT_MAX]}
    if plugin:
        out["plugin"] = plugin
    return out


def extract_events(payload) -> list:
    """Pure extraction: map one PostToolUse payload to ``(type, value)`` pairs.
    ``value`` is a string for file/command tools and a payload dict for the
    Skill tool (``skill_used`` carries structured fields, not just text).

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
    if tool == _SKILL_TOOL:
        skill = _skill_payload(inp, payload.get("tool_response"))
        return [("skill_used", skill)] if skill else []
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
            body = value if isinstance(value, dict) else {"text": value}
            events.append_event(etype, body, session_id=sid,
                                tags=["auto"], ts=now, project=project)
            appended += 1
        return {"appended": appended}
    except Exception as e:
        log_hook_error("post_tool_use", e)
        return {"error": f"{type(e).__name__}: {e}"}
