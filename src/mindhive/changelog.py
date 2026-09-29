# SPDX-License-Identifier: Apache-2.0
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
    """One spelling of a path, via the shared primitive.

    Kept as a one-line alias rather than inlined at the five call sites so the
    name this module has always used still reads locally — but it delegates now.
    It used to convert backslashes and nothing else, which left a THIRD definition
    of path equality in the tree beside `paths.normalise_path` and `review._norm`,
    and the whole point of moving the primitive to `paths` was that the leading-dot
    bug must not have to be fixed in more than one place."""
    return paths.normalise_path(p)


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
    out = []
    for p in order:
        cov = _covered_at(covered, p)
        if not cov or cov < first_touch[_norm(p)]:
            out.append(p)
    return out


def _covered_map(evs):
    """Every SPELLING of each logged file path -> newest code_change ts listing it.

    Expanded on both sides rather than compared literally, because the two sides
    are written by different producers with different conventions: `file_touched`
    rows carry worktree-absolute paths (3716 of them against 3 relative, measured
    on the live store) while `code_change` entries carry repo-relative ones (1026
    against 17). Compared as written, the sets essentially never intersect — so
    coverage subtracted nothing, every touched file stayed "unlogged" no matter
    how many entries named it, and the pending marker grew without bound."""
    covered = {}
    for ev in evs:
        if ev.type != "code_change" or not isinstance(ev.payload, dict):
            continue
        ts = ev.ts or ""
        for f in ev.payload.get("files") or []:
            if not isinstance(f, str):
                continue
            for k in paths.path_spellings(f):
                if ts >= covered.get(k, ""):
                    covered[k] = ts
    return covered


def _covered_at(covered, path) -> str:
    """The newest entry ts covering *path* under any of its spellings, or ""."""
    return max((covered.get(k, "") for k in paths.path_spellings(path)),
               default="")


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
        # Spelling-aware on both axes: a carried-forward path must not be added
        # twice because this session touched it under a different spelling, and
        # must not survive because the entry that covered it spelled it the other
        # way.
        seen = set()
        for f in files:
            seen |= paths.path_spellings(f)
        for f in prev["files"]:
            spellings = paths.path_spellings(f)
            if spellings & seen or _covered_at(covered, f) >= floor:
                continue
            files.append(f)
            seen |= spellings
    path = pending_path(project)
    if not files:
        path.unlink(missing_ok=True)
        return None
    marker = {
        "files": files,
        "created_at": ev_mod.now_iso(),
        "session_id": session_id,
    }
    # One shared idiom (paths.write_text_atomic), not a fifth hand-rolled copy.
    # The private-tmp-name and Windows publish-retry both live there; the copies
    # here all used `<name>.json.tmp`, the shared name whose clobber that helper
    # was written to end.
    paths.write_text_atomic(path, json.dumps(marker, indent=2))
    return marker


def load_pending(project=None):
    """The marker, or None when it is absent, unreadable, or the wrong SHAPE.

    Shape is checked, not assumed. `discharge()` reaches into `marker["files"]`,
    and a sidecar that is valid JSON of the wrong shape — a legacy format, a
    hand-edit, an externally corrupted file — would raise `AttributeError` or
    `TypeError` there. Neither is an `OSError`, so neither is caught by the guard
    `record()` wraps the discharge in, and the exception would escape AFTER the
    `code_change` event was already durably appended: the CLI reports failure on a
    change that actually succeeded, and the operator re-runs `hive log` and writes
    a duplicate. Reads stay total, as everywhere else in this store."""
    path = pending_path(project)
    if not path.exists():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or not isinstance(marker.get("files"), list):
        return None
    return marker


def clear(project=None) -> bool:
    """Drop the marker entirely. `queue()`'s empty case only — `record()` used to
    call this too, and that was the bug `discharge` replaces."""
    path = pending_path(project)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def _write_marker(marker, project=None) -> None:
    """Persist the marker through the module's own temp-file-plus-replace idiom."""
    path = pending_path(project)
    paths.write_text_atomic(path, json.dumps(marker, indent=2))


def discharge(files, project=None) -> dict | None:
    """Remove *files* from the pending marker, leaving everything else queued.

    `record()` used to `clear()` the whole marker regardless of what the entry
    covered, so logging one file silently discharged debt for every other file in
    the queue — and the next `session_end` rebuilt it, which made the gate look
    flaky rather than wrong.

    `created_at` and `session_id` SURVIVE the rewrite, and that is the part worth
    guarding. `queue()` uses `created_at` as the coverage floor and only drops a
    carried-forward file when a covering entry postdates it; a fresh stamp would
    invalidate every entry recorded before this run and re-queue files that were
    genuinely logged — reintroducing the phantom debt this function exists to
    stop. Returns the surviving marker, or None once it empties."""
    marker = load_pending(project)
    if not marker or not marker.get("files"):
        return None
    discharged = set()
    for f in files or []:
        if isinstance(f, str):
            discharged |= paths.path_spellings(f)
    survivors = [f for f in marker["files"]
                 if not (paths.path_spellings(f) & discharged)]
    if survivors == marker["files"]:
        return marker
    if not survivors:
        clear(project)
        return None
    marker = dict(marker, files=survivors)
    _write_marker(marker, project)
    return marker


def _git_changed_files(root=None) -> list:
    """Fallback file detection for manual use outside a hooked session: the
    in-scope files this WORKING TREE has changed.

    The working tree, not the branch. This used to run
    `git diff --name-only <merge-base master HEAD> HEAD`, which is the whole
    committed history of the branch — measured on one ordinary feature branch,
    that returned 81 files where the working tree had 0, and it is why a 14-file
    change-set was recorded as 61 files including modules the change never opened.
    An entry naming files a change never touched is a false record, and the ledger
    is append-only: it can be corrected by a following entry but never edited.

    `git diff` also cannot list untracked paths in any form, which is why three
    brand-new test files were absent from that same entry. `--porcelain -uall` is
    what fixes both. `-uall` rather than the default `-unormal` is load-bearing:
    `-unormal` collapses a new untracked directory into one `?? src/mindhive/pkg/`
    entry, and `in_scope` rejects it for not ending in `.py`, so every source file
    in a new package would stay invisible — the same silent omission in a new
    costume. This repo adds packages (`loop/`, `mindnet/`, `rotation/`).

    *root* is explicit because the resolution has to be a decision rather than an
    accident: this used to inherit the process working directory, which is not the
    repo root for every caller. `None` resolves through `paths.worktree_root()`,
    and when THAT is `None` — running outside any worktree — the answer is an
    empty list rather than a guess made from wherever the process happens to be.

    Empty list on any git failure; the caller reports which of the three empty
    cases it was."""
    root = root or paths.worktree_root()
    if root is None:
        return []
    files, seen = [], set()
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=str(root),
            capture_output=True, text=True, timeout=15)
        if completed.returncode != 0:
            return []
        for line in (completed.stdout or "").splitlines():
            # Porcelain v1: two status characters, a space, then the path. A
            # rename carries "old -> new"; the new name is the one that exists.
            entry = line[3:].strip().strip('"')
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1].strip().strip('"')
            entry = _norm(entry)
            if entry and entry not in seen and in_scope(entry):
                seen.add(entry)
                files.append(entry)
    except (OSError, subprocess.SubprocessError):
        return []
    return files


def _no_files_message() -> str:
    """Say WHICH empty case this was.

    All three used to collapse into "no changed files detected", which is
    unhelpful precisely when it fires most: an operator who logs after committing
    has a clean tree, and the generic message reads as a malfunction rather than
    as "there is nothing here to detect"."""
    if paths.worktree_root() is None:
        return ("not inside a git worktree, so the change-set cannot be "
                "detected — pass the files explicitly with --files")
    return ("the working tree is clean, so there is nothing to detect — log "
            "before committing, or pass the files explicitly with --files")


def validate_payload(payload) -> None:
    """Enforce the code_change contract on the raw-append surface. cmd_append
    in commands/ledger.py calls this for the type, so `hive append code_change …`
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
        # THIS session's own unlogged work first, and the marker only as the
        # fallback for a session-less invocation. The order used to be the other
        # way round, which was harmless only because the wholesale clear kept the
        # marker usually absent. Now that the marker legitimately RETAINS another
        # session's undischarged debt, preferring it would make the next bare
        # `hive log` name files this change-set never touched — the same false
        # record, arriving by a route the subtractive fix created.
        if session_id:
            # Only sweep the ledger when a concrete session bounds the sweep —
            # session_id=None would return every in-scope file ever touched
            # across all sessions, silently attributing history to this entry.
            if evs is None:
                evs = ev_mod.read_all(project)
            files = unlogged_files(evs, session_id=session_id)
        if not files:
            pending = load_pending(project)
            if pending and pending.get("files"):
                files = pending["files"]
        if not files:
            files = _git_changed_files()
    if not files:
        raise ValueError(_no_files_message())
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
        discharge(payload["files"], project)
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
