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
# Every tool whose call feeds the session roadmap. Spelled out rather than
# imported from `tasktool` so this module keeps no module-scope dependency on
# it; `tests/test_hookcapture.py` asserts the two lists agree, because a name
# that drifts apart here is exactly how the feature went silent the first time.
_TODO_TOOLS = ("TodoWrite", "TaskCreate", "TaskUpdate")
# Shared truncation caps — harvest.py reuses these so both capture paths
# truncate identically (the dedup key depends on identical text).
CMD_MAX = 180
DESC_MAX = 120
_SKILL_TEXT_MAX = 200
SKILL_ARGS_MAX = 200


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


def _response_ok(resp) -> bool:
    """False only on explicit error markers in a Skill tool_response envelope
    (``is_error``/``isError`` truthy, or a non-empty ``error`` value)."""
    if isinstance(resp, dict):
        if resp.get("is_error") or resp.get("isError"):
            return False
        if resp.get("error"):
            return False
    return True


def _blocks_text(blocks) -> str:
    """Joined ``text`` parts of a content-block list, or ""."""
    parts = [b.get("text", "") for b in blocks
             if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return " ".join(x for x in parts if x).strip()


def _response_text(resp) -> str:
    """Best-effort short text from a tool_response: a plain string, the first
    non-empty string field, or joined ``text`` parts of a content-block list —
    whether that list is nested under a key or IS the response. Empty string
    when nothing readable is found.

    The bare-list case is here because a tool_response is not contractually a
    string: transcripts store results as exactly that block list, and two
    readers disagreeing about which shapes count is how a harness change goes
    unnoticed. `tasktool` reads task ids through this same function."""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return _blocks_text(resp)
    if isinstance(resp, dict):
        for key in ("text", "result", "output", "error", "content"):
            v = resp.get(key)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, list):
                joined = _blocks_text(v)
                if joined:
                    return joined
    return ""


def _skill_payload(inp, resp) -> dict | None:
    """Build the ``skill_used`` event payload from a Skill-tool PostToolUse
    payload: ``{name, plugin?, ok, text, args?}``. The skill name comes from
    ``tool_input`` (``plugin:skill`` namespacing is split into the two fields);
    ``ok`` reflects the tool_response error markers; ``args`` is the capped
    invocation ask when one was passed — the recorded "why" behind Oversee's
    per-session skill trail. None when unparseable."""
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
    args = inp.get("args")
    if isinstance(args, str) and args.strip():
        out["args"] = args.strip()[:SKILL_ARGS_MAX]
    return out


def extract_events(payload) -> list:
    """Pure extraction: map one PostToolUse payload to ``(type, payload)``
    pairs. The payload's ``text`` field is byte-identical to the historical
    value (path / command[:180]) — harvest's dedup key depends on it. Extra
    keys (plus/minus/tool/desc) ride alongside, and the Skill tool yields a
    structured ``skill_used`` payload (``{name, plugin?, ok, text, args?}``). A file
    change that computes to +0 −0 is not a change and is skipped (locked
    design rule).

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
        skill = _skill_payload(inp, payload.get("tool_response"))
        return [("skill_used", skill)] if skill else []
    return []


def log_hook_error(stage: str, exc) -> None:
    """Append a one-line failure record so silent drops become detectable.

    Best-effort: a logging failure must never propagate out of a hook.
    """
    try:
        log = paths.store_file("hooks_errors.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"
        line = f"{ts}\t{stage}\t{type(exc).__name__}: {exc}\n"
        with open(log, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# --- live failure capture (PostToolUseFailure) --------------------------------
# Failure categories, matched case-insensitively as substrings, first hit wins.
# The point is the MACHINE-READABLE payload: the ledger already holds 260
# legacy tool_failed events whose payloads carry no parseable `tool` field
# (SOP tool-mcp-integration §1) — every live-captured failure fixes that
# pattern by carrying {tool, category, retryable} alongside the transcript
# contract {text, signature, action}.
_FAILURE_CATEGORIES = (
    ("permission_denied", ("permission denied", "access denied",
                           "access is denied", "eacces", "eperm",
                           "operation not permitted"), False),
    ("not_found", ("no such file", "not found", "enoent",
                   "cannot find"), False),
    ("timeout", ("timed out", "timeout", "etimedout"), True),
    ("network", ("econnrefused", "econnreset", "enetunreach",
                 "getaddrinfo", "network is unreachable"), True),
    ("syntax", ("syntaxerror", "parse error", "parser error",
                "unexpected token", "invalid syntax"), False),
)


def categorize_failure(text) -> dict:
    """Deterministic ``{category, retryable}`` for a failure's error text.

    Retryable means a verbatim retry can plausibly succeed (transient infra);
    a permission denial or contract violation is NOT retryable — adjust the
    call instead (SOP tool-mcp-integration §2)."""
    low = (text or "").lower()
    for category, needles, retryable in _FAILURE_CATEGORIES:
        if any(n in low for n in needles):
            return {"category": category, "retryable": retryable}
    return {"category": "error", "retryable": False}


def _failure_text(payload) -> str:
    """Best-effort error text from a PostToolUseFailure payload. The field
    layout is defensive on purpose — this hook's schema is exactly the kind
    of coupling that silently renamed fields have broken before."""
    err = payload.get("error")
    if isinstance(err, str) and err.strip():
        return err.strip()
    if isinstance(err, dict):
        for key in ("message", "text"):
            v = err.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    text = _response_text(payload.get("tool_response"))
    if text.strip():
        return text.strip()
    v = payload.get("stderr")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return ""


def record_post_tool_use_failure(raw: str, project=None, now=None) -> dict:
    """Parse a raw PostToolUseFailure payload and append one structured
    ``tool_failed`` event.

    Reuses the transcript-harvest semantics from failures.py — infra noise is
    skipped, explicit rejections become ``judgment:rejected:*`` signatures
    (KTD4), and the action string is byte-identical to
    ``failures.action_from_tool_use`` so the (session, signature, action)
    idempotency key lets the transcript back-harvest dedup the live event.
    Events carry the ``live`` tag to distinguish hook capture from transcript
    harvest. Never raises."""
    if not raw or not raw.strip():
        return {"appended": 0}
    try:
        from . import failures, signature as sig_mod
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return {"appended": 0}
        tool = payload.get("tool_name") or payload.get("toolName") or "unknown"
        inp = payload.get("tool_input") or payload.get("toolInput") or {}
        text = _failure_text(payload)
        if not text or failures._is_infra_noise(text):
            return {"appended": 0}
        action = failures.action_from_tool_use({"name": tool, "input": inp})
        if failures._is_rejection(text):
            signature = (f"judgment:rejected:{(tool or 'unknown').lower()}:"
                         f"{sig_mod.slug_id(action)}")
            cat = {"category": "rejected", "retryable": False}
            tags = ["auto", "failure", "judgment", "live"]
        else:
            signature = sig_mod.signature(text)
            cat = categorize_failure(text)
            tags = ["auto", "failure", "live"]
        p = {"text": text[:failures._EXCERPT_MAX], "signature": signature,
             "action": action, "tool": tool, **cat}
        events.append_event("tool_failed", p,
                            session_id=payload.get("session_id"),
                            tags=tags, ts=now, project=project)
        return {"appended": 1}
    except Exception as e:
        log_hook_error("post_tool_use_failure", e)
        return {"error": f"{type(e).__name__}: {e}"}


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
        tool = payload.get("tool_name") if isinstance(payload, dict) else None
        if tool in _TODO_TOOLS:
            # Session roadmap auto-sync (roadmap.py): the todo list IS the
            # session's plan + progress. Deliberately NOT in extract_events —
            # that pure mapping is mirrored by harvest.py, and replaying
            # historical todo snapshots from transcripts could only reorder the
            # registry, never improve it. Best-effort: a sync failure is
            # logged, never breaks capture.
            #
            # TWO tools reach the same writer because two shapes exist and only
            # one of them is what this harness sends. `TodoWrite` posts a whole
            # list, so its payload IS the snapshot. `TaskCreate`/`TaskUpdate`
            # post one item at a time and carry no list at all, so the snapshot
            # is accumulated across calls by `tasktool` — which is why gating
            # on the name `TodoWrite` alone left the whole feature with 0 real
            # inputs in 61 namespaces while this hook appended 23,973 events.
            try:
                from . import roadmap, tasktool
                inp = payload.get("tool_input") or {}
                if tool == "TodoWrite":
                    todos = inp.get("todos")
                else:
                    todos = tasktool.record(sid, tool, inp,
                                            payload.get("tool_response"),
                                            project=project)
                if roadmap.sync_from_todos(sid, todos,
                                           project=project, ts=now):
                    appended += 1
            except Exception as e:
                log_hook_error("todo_roadmap", e)
        for etype, ep in extract_events(payload):
            events.append_event(etype, ep, session_id=sid,
                                tags=["auto"], ts=now, project=project)
            appended += 1
        return {"appended": appended}
    except Exception as e:
        log_hook_error("post_tool_use", e)
        return {"error": f"{type(e).__name__}: {e}"}
