# src/mindhive/failures.py
"""Deterministic capture of tool FAILURES from Claude Code transcripts.

A failure is a tool_result block with is_error==True that was NOT interrupted
(user cancels are excluded). The error text comes from the sibling
`toolUseResult.stderr`, falling back to the block's own content. Each failure is
keyed to a root-cause signature and stored as a `tool_failed` event.
"""
from . import events, paths, signature as sig

_EXCERPT_MAX = 4000
_ACTION_MAX = 500
_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
_CMD_TOOLS = ("Bash", "PowerShell")


def _block_text(content) -> str:
    """A tool_result block's content is a string or a list of {type,text} parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def extract_failures(obj) -> list:
    """Pure: map one transcript line to a list of failure dicts.

    Each dict: {tool_use_id, error_excerpt, signature}. Returns [] for
    non-failures, successes, interrupts, or malformed input — never raises.
    """
    if not isinstance(obj, dict):
        return []
    msg = obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    tur = obj.get("toolUseResult") or {}
    interrupted = isinstance(tur, dict) and tur.get("interrupted") is True
    stderr = tur.get("stderr") if isinstance(tur, dict) else ""
    out = []
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        if b.get("is_error") is not True or interrupted:
            continue
        text = (stderr or "").strip() or _block_text(b.get("content")).strip()
        out.append({
            "tool_use_id": b.get("tool_use_id"),
            "error_excerpt": text[:_EXCERPT_MAX],
            "signature": sig.signature(text),
        })
    return out


import json
from pathlib import Path


def action_from_tool_use(block) -> str:
    """Human-readable action string for a tool_use content block."""
    name = block.get("name")
    inp = block.get("input") or {}
    if name in _CMD_TOOLS and inp.get("command"):
        return str(inp["command"])[:_ACTION_MAX]
    if name in _FILE_TOOLS and inp.get("file_path"):
        return f"{name} {inp['file_path']}"[:_ACTION_MAX]
    return (name or "unknown")[:_ACTION_MAX]


def _iter_lines(path):
    for raw in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue


def _existing_failure_keys(project) -> set:
    """(session, signature, action) already stored — makes capture idempotent."""
    keys = set()
    for ev in events.read_all(project):
        if ev.type != "tool_failed":
            continue
        p = ev.payload if isinstance(ev.payload, dict) else {}
        keys.add((ev.session_id, p.get("signature"), p.get("action")))
    return keys


def capture_dir(transcript_dir, project=None, now=None) -> dict:
    """Scan a transcript directory, append a `tool_failed` event per new failure.

    Correlates each failing tool_use_id to its action (the assistant tool_use
    earlier in the same session). Idempotent: a (session, signature, action)
    already stored is skipped.
    """
    seen = _existing_failure_keys(project)
    new_failures = 0
    d = Path(transcript_dir)
    if not d.exists():
        return {"new_failures": 0, "files": 0}
    files = 0
    for f in sorted(d.glob("*.jsonl")):
        files += 1
        sid = f.stem
        actions = {}                    # tool_use_id -> action string
        for obj in _iter_lines(f):
            msg = obj.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        actions[b["id"]] = action_from_tool_use(b)
            for fail in extract_failures(obj):
                action = actions.get(fail["tool_use_id"], "unknown")
                key = (sid, fail["signature"], action)
                if key in seen:
                    continue
                seen.add(key)
                events.append_event(
                    "tool_failed",
                    {"text": fail["error_excerpt"], "signature": fail["signature"],
                     "action": action},
                    session_id=sid, tags=["auto", "failure"], ts=now, project=project)
                new_failures += 1
    return {"new_failures": new_failures, "files": files}
