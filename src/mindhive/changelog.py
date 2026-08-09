# src/mindhive/changelog.py
"""Mandatory code change-log: every change to mindhive's own code gets a
categorized entry with a brief human-written explanation.

Entries are `code_change` events in the ledger (semantic, so they ride the
LEDGER, hash chain, and Memory view). Enforcement mirrors the thermo-nuclear
review gate in review.py: session_end queues the session's touched source
files into a sidecar marker (`pending_changelog.json`), the projector renders
an "Unlogged code changes" block into STATE.md, and `hive log <category>
"<explanation>"` records the entry and clears the marker.

One entry covers one change-set (a logical piece of work), not one file or
one commit.
"""
import json
import subprocess
from . import events as ev_mod, paths, review
from .sop import ID_RE

CATEGORIES = (
    "update", "minor_change", "deletion",
    "reduction", "major_change", "replacement",
)

# An "explanation" shorter than this is a placeholder, not an explanation.
MIN_EXPLANATION_CHARS = 10


def pending_path(project=None):
    """Sidecar marker path, sibling to review's pending_review.json."""
    return paths.project_dir(project) / "pending_changelog.json"


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def in_scope(path: str) -> bool:
    """Same first-party filter as the review gate. A leading slash is added so
    repo-relative paths (git output like `src/mindhive/x.py`) match review's
    substring check, which expects `/src/mindhive/`."""
    return review.in_scope("/" + _norm(path).lstrip("/"))


def unlogged_files(evs, session_id=None):
    """In-scope files touched this session with no covering code_change entry.

    Coverage is session-blind but time-aware: an entry from ANY session counts
    (a manual `hive log` run in a plain terminal records under session
    "manual", not the coding session's id — filtering coverage by session
    would wrongly re-fire the gate at session_end after a proper log), but
    only when it was recorded at-or-after this session's FIRST touch of the
    file, so a stale entry from an earlier change-set never exempts new work.
    """
    first_touch, order = {}, []
    for ev in evs:
        if ev.type != "file_touched":
            continue
        if session_id is not None and ev.session_id != session_id:
            continue
        p = (ev.payload.get("text") or ev.payload.get("path")
             if isinstance(ev.payload, dict) else None)
        if not p or not in_scope(p):
            continue
        key = _norm(p)
        if key not in first_touch:
            first_touch[key] = ev.ts or ""
            order.append(p)
    covered = _covered_map(evs)
    return [p for p in order
            if _norm(p) not in covered
            or covered[_norm(p)] < first_touch[_norm(p)]]


def _covered_map(evs):
    """Normalized file path -> newest code_change ts listing it."""
    covered = {}
    for ev in evs:
        if ev.type != "code_change" or not isinstance(ev.payload, dict):
            continue
        for f in ev.payload.get("files") or []:
            k = _norm(f)
            if (ev.ts or "") >= covered.get(k, ""):
                covered[k] = ev.ts or ""
    return covered


def queue(evs, session_id=None, project=None):
    """Build (or clear) the pending-changelog marker from a session's events.
    Returns the marker dict, or None when nothing is unlogged.

    The gate is CUMULATIVE, unlike review.py's per-session marker: a later
    session that touched nothing must not evaporate an earlier session's
    unlogged debt — only a covering code_change entry clears a file. Files
    from an existing marker are carried forward unless an entry recorded
    since that marker's creation covers them (record() would have cleared
    the marker for anything logged before it was created)."""
    files = unlogged_files(evs, session_id=session_id)
    prev = load_pending(project)
    if prev and prev.get("files"):
        floor = prev.get("created_at") or ""
        covered = _covered_map(evs)
        seen = {_norm(f) for f in files}
        for f in prev["files"]:
            k = _norm(f)
            if k in seen or covered.get(k, "") >= floor:
                continue
            files.append(f)
            seen.add(k)
    path = pending_path(project)
    if not files:
        path.unlink(missing_ok=True)
        return None
    marker = {
        "files": files,
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
    path = pending_path(project)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _git_changed_files(cwd=None) -> list:
    """Fallback file detection for manual use outside a hooked session: files
    changed on this branch vs the merge-base with master, plus uncommitted
    changes. Empty list on any git failure — the caller errors out asking for
    explicit --files instead of guessing.

    *cwd* names the checkout to ask. It defaults to the process's own directory,
    which is right for `hive log`; a caller that knows which tree it is talking
    about (the replenishment audit lens, running for a loop in another project)
    passes it, because the answer is per-checkout and running git in the wrong
    one reports another repo's changes as this one's."""
    files = []
    run_kw = {"capture_output": True, "text": True, "timeout": 15}
    if cwd is not None:
        run_kw["cwd"] = str(cwd)
    try:
        base = subprocess.run(
            ["git", "merge-base", "master", "HEAD"], **run_kw).stdout.strip()
        cmds = [["git", "diff", "--name-only", "HEAD"]]
        if base:
            cmds.append(["git", "diff", "--name-only", base, "HEAD"])
        seen = set()
        for cmd in cmds:
            out = subprocess.run(cmd, **run_kw).stdout
            for line in out.splitlines():
                line = line.strip()
                if line and line not in seen and in_scope(line):
                    seen.add(line)
                    files.append(line)
    except (OSError, subprocess.SubprocessError):
        return []
    return files


def validate_payload(payload) -> None:
    """Enforce the code_change contract on the raw-append surface. cmd_append
    in __main__.py calls this for the type, so `hive append code_change …`
    cannot smuggle an uncategorized or unexplained entry past the mandatory-
    log rule; the event store itself stays type-agnostic."""
    if not isinstance(payload, dict):
        raise ValueError("code_change payload must be an object")
    if payload.get("category") not in CATEGORIES:
        raise ValueError(
            "code_change requires a category — one of: " + ", ".join(CATEGORIES))
    if len((payload.get("text") or "").strip()) < MIN_EXPLANATION_CHARS:
        raise ValueError(
            "code_change requires a real explanation "
            f"(at least {MIN_EXPLANATION_CHARS} characters)")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("code_change requires a non-empty files list")
    # OPTIONAL MindNetwork join key (KTD16). Absent on every historical event
    # and on every entry that is not part of a tracked task, so it is checked
    # only when present — requiring it would invalidate the whole existing
    # change-log, and reads must stay total either way.
    #
    # It is checked at all because `mindnet` satisfies the `execution` phase by
    # scanning for a code_change whose task_id equals the task's: a typo'd or
    # non-string id joins to nothing, and the operator sees "no code_change
    # carries this task_id" long after the entry was written, with no clue
    # which of the two spellings was wrong.
    task_id = payload.get("task_id")
    if task_id is not None and not ID_RE.match(str(task_id)):
        raise ValueError(
            "code_change task_id must be a kebab-case task id (lowercase "
            "letters, digits, hyphens; max 64 chars) — it joins this entry to "
            "a mindnet task's 'execution' phase evidence")


def record(category, text, files=None, session_id=None, project=None,
           evs=None, task_id=None):
    """Append one code_change entry covering a change-set, then clear the
    pending marker. Raises ValueError on an invalid category, a too-short
    explanation, an empty resolved file list, or a malformed *task_id*.

    *task_id* is the optional MindNetwork join key (KTD16): when the change-set
    belongs to a declared `mindnet` task, stamping it here is what lets that
    task's `execution` phase find machine-checkable evidence that code was
    actually written. The key is OMITTED from the payload entirely when not
    given, so an untracked entry stays byte-identical to every historical one
    and the hash chain sees no schema change.

    The join already worked without this argument — `changelog.validate_payload`
    rejects no extra key, so `hive append code_change '{…,"task_id":"x"}'
    --json` has always landed a valid joined entry. This is the ergonomic
    producer for it, not an unblock."""
    if category not in CATEGORIES:
        raise ValueError(
            f"unknown category: {category!r} — one of: {', '.join(CATEGORIES)}")
    text = (text or "").strip()
    if len(text) < MIN_EXPLANATION_CHARS:
        raise ValueError(
            "explanation is mandatory: briefly describe what changed "
            f"(at least {MIN_EXPLANATION_CHARS} characters)")
    if not files:
        pending = load_pending(project)
        if pending and pending.get("files"):
            files = pending["files"]
        elif session_id:
            # Only sweep the ledger when a concrete session bounds the sweep —
            # session_id=None would return every in-scope file ever touched
            # across all sessions, silently attributing history to this entry.
            if evs is None:
                evs = ev_mod.read_all(project)
            files = unlogged_files(evs, session_id=session_id)
        if not files:
            files = _git_changed_files()
    if not files:
        raise ValueError(
            "no changed files detected — pass them explicitly with --files")
    payload = {"text": text, "category": category, "files": list(files)}
    if task_id is not None:
        if not ID_RE.match(str(task_id)):
            raise ValueError(
                f"invalid --task id {task_id!r}: it must be a kebab-case task "
                "id (lowercase letters, digits, hyphens; max 64 chars), and it "
                "must match the id the task was declared under — a near-miss "
                "joins to nothing and the phase never satisfies")
        payload["task_id"] = str(task_id)
    ev = ev_mod.append_event(
        "code_change", payload,
        session_id=session_id, tags=["changelog"], project=project)
    try:
        clear(project)
    except OSError:
        # The entry is already in the ledger; a locked marker file (AV or
        # indexer holding pending_changelog.json) must not make the CLI
        # report failure and invite a duplicate entry. The marker self-heals:
        # the next queue() re-derives coverage from the ledger and drops it.
        pass
    return ev


def entries(evs, project=None):
    """code_change events as display dicts, newest first. Ties on ts (two
    entries in the same second) break by append order, later first. This is
    the ONE source of truth for entry shape and ordering — the dashboard's
    /api/changelog delegates here so it can never disagree with
    `hive log show`. ``project`` stamps each dict when given (HIVE MIND)."""
    out = []
    for i, ev in enumerate(evs):
        if ev.type != "code_change":
            continue
        p = ev.payload if isinstance(ev.payload, dict) else {}
        d = {
            "ts": ev.ts,
            "session": ev.session_id,
            "category": p.get("category", ""),
            "text": p.get("text", ""),
            "files": p.get("files") or [],
        }
        # Only when present: every historical entry lacks it, and materialising
        # an empty key would make the display shape claim a join that is not
        # there.
        if p.get("task_id"):
            d["task_id"] = p["task_id"]
        if project is not None:
            d["project"] = project
        out.append((i, d))
    out.sort(key=lambda pair: (pair[1]["ts"] or "", pair[0]), reverse=True)
    return [e for _i, e in out]


def notice(marker) -> str:
    """Render the pending marker as a Markdown block for STATE.md. Empty string
    when nothing is pending."""
    if not marker:
        return ""
    files = marker.get("files") or []
    out = [
        "## ⚠ Unlogged code changes",
        "",
        f"{len(files)} mindhive source file(s) were changed without a "
        "change-log entry. Logging is MANDATORY before this work is called "
        "done: run `hive log <category> \"<brief explanation>\"` where "
        f"category is one of {', '.join(CATEGORIES)}. Recording the entry "
        "clears this notice.",
        "",
        "**Files:**",
    ]
    for f in files:
        out.append(f"- {f}")
    out.append("")
    return "\n".join(out) + "\n"
