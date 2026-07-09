# src/mindhive/taskview.py
"""Backend + page fragments for the Sessions notification center.

Three disclosure levels: L1 per-session tallies (the summary chip), L2 the
ordered activity rows capped to the last ITEM_CAP entries, and L3 granular
detail (full diffs / command output) parsed lazily from the Claude Code
transcript so the append-only ledger never stores heavy payloads. Nothing
here deletes anything — the Sessions page is a deliberately auditable track
record. Also owns the cards' CSS/JS fragments, injected into the page by
dashboard.py so dashboard_page.py stays sibling-free."""
import calendar
import difflib
import json
import re
import time
from pathlib import Path

ACTIVE_WINDOW_SECS = 600      # newest event younger than this -> "ongoing"
ITEM_CAP = 100                # locked: detail history shows the last 100 rows
DIFF_CAP_LINES = 400
OUTPUT_CAP_CHARS = 10_000
_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NOTE_TYPES = ("caveat", "decision", "experiment_result",
               "next_step", "open_question")


def _epoch(ts):
    if not isinstance(ts, str):
        return None
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def status_of(last_ts, now=None) -> str:
    """"ongoing" when the session's newest event is fresher than
    ACTIVE_WINDOW_SECS, else "completed". Unparseable -> completed."""
    e = _epoch(last_ts)
    if e is None:
        return "completed"
    return "ongoing" if (now or time.time()) - e < ACTIVE_WINDOW_SECS else "completed"


def tally(evs) -> dict:
    """L1 aggregate for one session: row-kind counts plus summed line stats.
    Zero-valued keys are kept — the JS omits them from the chip."""
    t = {"files": 0, "plus": 0, "minus": 0, "commands": 0,
         "notes": 0, "failures": 0, "loops": 0}
    seen_files = set()
    for ev in evs:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.type == "file_touched":
            seen_files.add(p.get("text"))
            t["plus"] += int(p.get("plus") or 0)
            t["minus"] += int(p.get("minus") or 0)
        elif ev.type == "command_run":
            t["commands"] += 1
        elif ev.type in _NOTE_TYPES:
            t["notes"] += 1
        elif ev.type == "tool_failed":
            t["failures"] += 1
        elif ev.type == "loop_iteration":
            t["loops"] += 1
    t["files"] = len(seen_files)
    return t


def items(evs, cap=ITEM_CAP) -> dict:
    """L2 rows in ledger order, capped to the LAST ``cap``. The ledger keeps
    everything; ``truncated`` tells the UI how many older rows exist."""
    rows = []
    for ev in evs:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.type == "file_touched":
            row = {"kind": "touched", "text": p.get("text", ""), "ts": ev.ts}
            for k in ("plus", "minus", "tool"):
                if k in p:
                    row[k] = p[k]
        elif ev.type == "command_run":
            row = {"kind": "ran", "text": p.get("text", ""), "ts": ev.ts}
            if p.get("desc"):
                row["desc"] = p["desc"]
        elif ev.type in _NOTE_TYPES:
            row = {"kind": ev.type, "text": p.get("text", ""), "ts": ev.ts}
        elif ev.type == "tool_failed":
            row = {"kind": "failed", "ts": ev.ts,
                   "text": p.get("text") or p.get("signature", "")}
        elif ev.type == "loop_iteration":
            row = {"kind": "loop", "ts": ev.ts,
                   "text": p.get("summary") or f"iteration {p.get('iteration')}",
                   "iteration": p.get("iteration"),
                   "progress": p.get("progress"),
                   "breaker_state": p.get("breaker_state"),
                   "cost_usd": p.get("cost_usd"),
                   "total_tokens": p.get("total_tokens"),
                   "context_fill_ratio": p.get("context_fill_ratio")}
        else:
            continue
        rows.append(row)
    total = len(rows)
    return {"items": rows[-cap:], "total": total,
            "truncated": max(0, total - cap)}


# --- L3: lazy granular detail from the Claude Code transcript --------------

def transcripts_root() -> Path:
    return Path.home() / ".claude" / "projects"


def find_transcript(session_id, root=None):
    """Locate ``<root>/<any dir>/<session_id>.jsonl``. The id becomes a
    filename, so anything but a strict token is rejected (path traversal)."""
    if not isinstance(session_id, str) or not _SID_RE.match(session_id):
        return None
    root = Path(root) if root else transcripts_root()
    if not root.exists():
        return None
    for d in sorted(root.iterdir()):
        f = d / f"{session_id}.jsonl"
        if d.is_dir() and f.exists():
            return f
    return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _diff(old, new, path) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=path, tofile=path, lineterm=""))
    if len(lines) > DIFF_CAP_LINES:
        extra = len(lines) - DIFF_CAP_LINES
        lines = lines[:DIFF_CAP_LINES] + [f"… +{extra} more lines"]
    return "\n".join(lines)


def _detail_for_use(block):
    name = block.get("name")
    inp = block.get("input") or {}
    if name in ("Edit", "Write", "NotebookEdit"):
        fp = inp.get("file_path")
        if not fp:
            return None
        d = {"kind": "touched", "text": fp, "tool": name}
        old, new = inp.get("old_string"), inp.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            d["diff"] = _diff(old, new, fp)
        elif name == "Write" and isinstance(inp.get("content"), str):
            head = inp["content"].splitlines()[:DIFF_CAP_LINES]
            d["diff"] = "\n".join("+" + ln for ln in head)
        return d
    if name in ("Bash", "PowerShell"):
        cmd = inp.get("command")
        if not cmd:
            return None
        d = {"kind": "ran", "text": cmd[:180], "command": cmd[:2000]}
        desc = inp.get("description")
        if isinstance(desc, str) and desc.strip():
            d["desc"] = desc.strip()[:120]
        return d
    return None


def detail(session_id, root=None) -> dict:
    """L3: ordered granular details parsed from the session transcript.
    Items carry the same (kind, text) identity as the L2 rows so the UI can
    zip them in order. A missing transcript degrades — old sessions must
    render, not 500."""
    f = find_transcript(session_id, root=root)
    if f is None:
        return {"error": "transcript_unavailable", "items": []}
    try:
        lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {"error": "transcript_unavailable", "items": []}
    uses, order = {}, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if o.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    d = _detail_for_use(b)
                    if d is not None:
                        uses[b.get("id")] = d
                        order.append(b.get("id"))
        elif o.get("type") == "user":
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                d = uses.get(b.get("tool_use_id"))
                if d is not None and d["kind"] == "ran":
                    d["output"] = _result_text(b.get("content"))[:OUTPUT_CAP_CHARS]
    out = [uses[i] for i in order if i in uses]
    return {"items": out[-ITEM_CAP:], "total": len(out)}
