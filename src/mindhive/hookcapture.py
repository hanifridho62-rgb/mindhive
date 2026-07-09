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
# Shared truncation caps — harvest.py reuses these so both capture paths
# truncate identically (the dedup key depends on identical text).
CMD_MAX = 180
DESC_MAX = 120


def line_stats(tool, inp, resp=None):
    """(plus, minus) line counts for a file-tool call, or (None, None) when
    they can't be derived. Prefers the exact structuredPatch from the tool
    response; falls back to new/old string line counts, then Write content.
    The old/new-string fallback may overcount when old_string/new_string
    include unchanged context lines carried for uniqueness.
    Unknown is NOT zero — callers only skip on a computed (0, 0)."""
    patch = resp.get("structuredPatch") if isinstance(resp, dict) else None
    if isinstance(patch, list) and patch:
        # Only trust the patch when every hunk's "lines" is a real list — a
        # malformed value (e.g. a string) would otherwise be iterated
        # char-by-char and silently misreport counts. Malformed -> treat the
        # whole patch as unusable and fall through to the string fallbacks.
        hunk_lines = [h.get("lines") if isinstance(h, dict) else None for h in patch]
        if all(isinstance(ls, list) for ls in hunk_lines):
            plus = minus = 0
            for ls in hunk_lines:
                for ln in ls:
                    if isinstance(ln, str) and ln.startswith("+"):
                        plus += 1
                    elif isinstance(ln, str) and ln.startswith("-"):
                        minus += 1
            return plus, minus
    old, new = inp.get("old_string"), inp.get("new_string")
    if isinstance(old, str) and isinstance(new, str):
        return len(new.splitlines()), len(old.splitlines())
    if tool == "Write" and isinstance(inp.get("content"), str):
        return len(inp["content"].splitlines()), 0
    return None, None


def extract_events(payload) -> list:
    """Pure extraction: map one PostToolUse payload to ``(type, payload)``
    pairs. The payload's ``text`` field is byte-identical to the historical
    value (path / command[:180]) — harvest's dedup key depends on it. Extra
    keys (plus/minus/tool/desc) ride alongside. A file change that computes
    to +0 −0 is not a change and is skipped (locked design rule).

    Returns ``[]`` for unknown tools or missing fields — never raises on a
    well-formed dict with absent keys.
    """
    if not isinstance(payload, dict):
        return []
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") or {}
    if tool in _FILE_TOOLS:
        path = inp.get("file_path")
        if not path:
            return []
        plus, minus = line_stats(tool, inp, payload.get("tool_response"))
        p = {"text": path, "tool": tool}
        if plus is not None:
            if plus == 0 and minus == 0:
                return []
            p["plus"], p["minus"] = plus, minus
        return [("file_touched", p)]
    if tool in _CMD_TOOLS:
        cmd = inp.get("command")
        if not cmd:
            return []
        p = {"text": cmd[:CMD_MAX]}
        desc = inp.get("description")
        if isinstance(desc, str) and desc.strip():
            p["desc"] = desc.strip()[:DESC_MAX]
        return [("command_run", p)]
    if tool == "Skill":
        name = inp.get("skill")
        if isinstance(name, str) and name.strip():
            name = name.strip()
            # PostToolUse fires after the tool ran; a skill whose execution
            # later fails still surfaces via the tool_failed pipeline, so this
            # records the invocation outcome the green/red view reads.
            return [("invoke_result", {"text": name, "name": name})]
        return []
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
        for etype, ep in extract_events(payload):
            events.append_event(etype, ep, session_id=sid,
                                tags=["auto"], ts=now, project=project)
            appended += 1
        return {"appended": appended}
    except Exception as e:
        log_hook_error("post_tool_use", e)
        return {"error": f"{type(e).__name__}: {e}"}
