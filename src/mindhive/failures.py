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
