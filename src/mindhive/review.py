# src/mindhive/review.py
"""Thermo-nuclear review queue: turn a session's file_touched events into a
pending-review marker the next session can act on.

A fire-and-forget hook cannot run an LLM review itself, so session_end only does
the cheap bookkeeping here: filter touched files to first-party source/tests,
split them into line-bounded batches, and write a sidecar marker. The projector
renders that marker into STATE.md, which session_start injects — so the *next*
interactive session runs the review where an LLM actually exists. See
docs/adr/0004-queue-and-inject-thermo-nuclear-review.md.
"""
import json
from . import events as ev_mod, paths

# Batch when the filtered set exceeds this many total lines; pack whole files
# into batches no larger than this. A single file bigger than the limit becomes
# its own batch (we never split a file across batches — the 1,000-line and
# abstraction checks need the whole file in view).
BATCH_LINE_LIMIT = 1500


def pending_path(project=None):
    """Sidecar marker path. Per-project (under ~/.mindhive/<project>/), not the
    raw event log — 'review pending' is transient, consumable state."""
    return paths.project_dir(project) / "pending_review.json"


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def in_scope(path: str) -> bool:
    """First-party source and tests only; never generated artifacts."""
    p = _norm(path)
    if not p.endswith(".py"):
        return False
    if "__pycache__" in p or ".egg-info" in p:
        return False
    return "/src/mindhive/" in p or "/tests/" in p


def touched_files(evs, session_id=None):
    """Distinct in-scope file paths from file_touched events, preserving order.
    If session_id is given, restrict to that session."""
    seen, out = set(), []
    for ev in evs:
        if ev.type != "file_touched":
            continue
        if session_id is not None and ev.session_id != session_id:
            continue
        p = ev.payload.get("text") or ev.payload.get("path") if isinstance(ev.payload, dict) else None
        if not p or not in_scope(p):
            continue
        key = _norm(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _line_count(path: str) -> int:
    """Lines in the file as it exists now; 0 if it's gone (deleted this session)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def batch_files(files, limit=BATCH_LINE_LIMIT):
    """Greedily pack whole files into batches of <= `limit` total lines. A file
    larger than `limit` stands alone. Files that no longer exist are dropped."""
    sized = [(f, _line_count(f)) for f in files]
    sized = [(f, n) for f, n in sized if n > 0]
    batches, cur, cur_lines = [], [], 0
    for f, n in sized:
        if cur and cur_lines + n > limit:
            batches.append(cur)
            cur, cur_lines = [], 0
        cur.append(f)
        cur_lines += n
    if cur:
        batches.append(cur)
    return batches


def queue(evs, session_id=None, project=None):
    """Build (or clear) the pending-review marker from a session's events.
    Returns the marker dict, or None when nothing is in scope (and removes any
    stale marker so an empty session doesn't leave a phantom review)."""
    files = touched_files(evs, session_id=session_id)
    batches = batch_files(files)
    path = pending_path(project)
    if not batches:
        path.unlink(missing_ok=True)
        return None
    marker = {
        "files": [f for batch in batches for f in batch],
        "batches": batches,
        "batch_count": len(batches),
        "created_at": ev_mod.now_iso(),
        "session_id": session_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(marker, indent=2), encoding="utf-8")
    tmp.replace(path)
    return marker


def load_pending(project=None):
    path = pending_path(project)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear(project=None) -> bool:
    """Remove the marker once the review is done. True if one was present."""
    path = pending_path(project)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def notice(marker) -> str:
    """Render the pending marker as a Markdown block for STATE.md. Empty string
    when there is nothing pending, so the projector can append unconditionally."""
    if not marker:
        return ""
    files = marker.get("files") or []
    n_batches = marker.get("batch_count") or len(marker.get("batches") or [[]])
    out = [
        "## ⚠ Pending thermo-nuclear review",
        "",
        f"{len(files)} file(s) touched last session need a maintainability review "
        f"({n_batches} batch(es)). Run the `thermo-nuclear-code-quality-review` skill "
        "on them — re-read each file live (don't trust this snapshot), record "
        "presumptive-blocker findings with `hive append caveat \"…\"`, then clear this "
        "with `hive review clear`.",
        "",
        "**Files:**",
    ]
    for f in files:
        out.append(f"- {f}")
    out.append("")
    return "\n".join(out) + "\n"
