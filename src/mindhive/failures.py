# src/mindhive/failures.py
"""Deterministic capture of tool FAILURES from Claude Code transcripts.

A failure is a tool_result block with is_error==True that was NOT a bare
interrupt (a mere "stop" is excluded). Explicit user rejections/denials ARE
captured — as `judgment:rejected:*` signatures, not technical ones (KTD4).
The error text comes from the sibling `toolUseResult.stderr`, falling back to
the block's own content. Each failure is keyed to a root-cause signature and
stored as a `tool_failed` event.
"""
import re

from . import events, paths, signature as sig

_EXCERPT_MAX = 4000
_ACTION_MAX = 500
_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
_CMD_TOOLS = ("Bash", "PowerShell")

# is_error results that are NOT preventable coding mistakes. Transient infra
# stays excluded like `interrupted` (see Plan 1 Task 9 findings); explicit
# user rejections/denials are NOT noise — they are judgment signal (KTD4) and
# become `judgment:rejected:<tool>:<context-stem>` events in capture_dir.
# Both sets matched case-insensitively as substrings.
_INFRA_NOISE = (
    "auto mode cannot determine the safety",
    "is temporarily unavailable",
)
_REJECTION = (
    "the user doesn't want to proceed",
    "tool use was rejected",
    "the user chose not to",
)

# A failure whose ENTIRE text is a bare exit code carries no root cause at all
# (the real error went to the console, not stderr). It buckets into one giant
# raw:* signature that can never be reflected or resolved — pure noise that
# drowned the Improvements view (one such bucket reached 49+ occurrences).
_EXIT_ONLY = re.compile(r"^exit code \d+\.?$", re.IGNORECASE)


def _is_infra_noise(text: str) -> bool:
    low = (text or "").lower().strip()
    if _EXIT_ONLY.match(low):
        return True
    return any(n in low for n in _INFRA_NOISE)


def _is_rejection(text: str) -> bool:
    low = (text or "").lower()
    return any(n in low for n in _REJECTION)


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

    Each dict: {tool_use_id, error_excerpt, signature, rejected}. An explicit
    user rejection/denial is emitted with rejected=True and signature=None —
    the caller finalizes its `judgment:rejected:<tool>:<stem>` signature once
    the failing tool is known. Returns [] for non-failures, successes, bare
    interrupts (a mere "stop" carries no rejection semantics), transient-infra
    noise, or malformed input — never raises.
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
        if b.get("is_error") is not True:
            continue
        text = (stderr or "").strip() or _block_text(b.get("content")).strip()
        if _is_rejection(text):
            # Checked before the interrupted gate: a rejection sometimes rides
            # an interrupted result, and the rejection semantics win.
            out.append({
                "tool_use_id": b.get("tool_use_id"),
                "error_excerpt": text[:_EXCERPT_MAX],
                "signature": None,
                "rejected": True,
            })
            continue
        if interrupted or _is_infra_noise(text):
            continue
        out.append({
            "tool_use_id": b.get("tool_use_id"),
            "error_excerpt": text[:_EXCERPT_MAX],
            "signature": sig.signature(text),
            "rejected": False,
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


_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _normalize_ts(raw):
    """Reduce a transcript ISO-8601 timestamp to the store's second-resolution
    UTC form, or None when it can't be parsed. Mirrors harvest._normalize_ts so
    captured failures carry their REAL occurrence time (needed for leak timing),
    not capture wall-clock."""
    if not isinstance(raw, str):
        return None
    m = _TS_RE.match(raw.strip())
    return (m.group(1) + "Z") if m else None


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
        tools = {}                      # tool_use_id -> tool name
        for obj in _iter_lines(f):
            msg = obj.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        actions[b["id"]] = action_from_tool_use(b)
                        tools[b["id"]] = b.get("name") or "unknown"
            # Preserve the failure's REAL time from the transcript line; fall back
            # to `now` only when the line has no timestamp.
            line_ts = _normalize_ts(obj.get("timestamp")) or now
            for fail in extract_failures(obj):
                action = actions.get(fail["tool_use_id"], "unknown")
                if fail.get("rejected"):
                    # Judgment signal (KTD4): the context stem — a bounded slug
                    # of the rejected command/action — keeps unrelated
                    # rejections from aggregating into one mush incident.
                    tool = (tools.get(fail["tool_use_id"]) or "unknown").lower()
                    signature = f"judgment:rejected:{tool}:{sig.slug_id(action)}"
                    tags = ["auto", "failure", "judgment"]
                else:
                    signature = fail["signature"]
                    tags = ["auto", "failure"]
                key = (sid, signature, action)
                if key in seen:
                    continue
                seen.add(key)
                events.append_event(
                    "tool_failed",
                    {"text": fail["error_excerpt"], "signature": signature,
                     "action": action},
                    session_id=sid, tags=tags, ts=line_ts, project=project)
                new_failures += 1
    return {"new_failures": new_failures, "files": files}
