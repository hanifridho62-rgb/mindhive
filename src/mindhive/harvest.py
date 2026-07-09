# src/mindhive/harvest.py
# Deterministic, hook-free auto-capture: turn Claude Code session transcripts
# (~/.claude/projects/<encoded>/<uuid>.jsonl) into MIND HIVE events.
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from . import events, hookcapture, paths


def _harvest_lock_path() -> Path:
    return paths.store_home() / "harvest.lock"


@contextmanager
def _harvest_lock(stale_secs: float = 120.0):
    """Cross-process mutex so concurrent harvesters (the dashboard's fast loop
    and the scheduled autosync task) never process the same transcript lines at
    once and double-write events. Yields True if acquired, False if another
    harvest already holds it. A lock older than ``stale_secs`` is presumed
    orphaned (crashed process) and stolen."""
    p = _harvest_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.exists() and (time.time() - p.stat().st_mtime) > stale_secs:
            p.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:               # already held by another harvester
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        yield True
    finally:
        try:
            p.unlink()
        except OSError:
            pass

_MARKERS = ("remember:", "memo:", "decision:", "caveat:")
# Hint words only disambiguate the GENERIC markers (remember:/memo:). An explicit
# decision:/caveat: marker is authoritative and never reclassified.
_CAVEAT_HINT = ("bug", "fail", "lesson", "incident", "careful", "don't", "avoid", "broke")
_AMBIGUOUS_MARKERS = ("remember:", "memo:")

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

# Noise the Claude Code transcript wraps around (or injects beside) a real prompt.
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_SYS_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_CMD_TAG_RE = re.compile(r"</?command-(?:message|name|args)>", re.DOTALL)


def _clean_prompt(text: str) -> str:
    """Reduce a user-message body to just what the user actually typed: pull the
    real input out of a slash command's <command-args>, and drop injected
    <system-reminder> blocks and stray command tags."""
    m = _CMD_ARGS_RE.search(text)
    if m:                                 # slash command -> the args ARE the input
        text = m.group(1)
    text = _SYS_REMINDER_RE.sub("", text)
    text = _CMD_TAG_RE.sub("", text)
    return text.strip()


def _is_injected_prompt(text: str) -> bool:
    """Skill bodies arrive as user-role messages but aren't prompts the user typed."""
    return text.lstrip()[:40].lower().startswith("base directory for this skill:")


_MAX_WORDS = 5000


def _cap_words(text: str, limit: int = _MAX_WORDS) -> str:
    """Bound a single captured memory (one bubble) to ``limit`` words so a giant
    paste can't bloat the store/UI, while still allowing very long entries."""
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]) + " …[truncated]"


def _normalize_ts(raw) -> str | None:
    """Reduce a transcript ISO-8601 timestamp (e.g. '2026-06-20T09:15:30.500Z')
    to the canonical second-resolution UTC form the store uses, or None when it
    can't be parsed."""
    if not isinstance(raw, str):
        return None
    m = _TS_RE.match(raw.strip())
    return (m.group(1) + "Z") if m else None


def _user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _items_from_obj(o) -> list:
    items = []
    t = o.get("type")
    msg = o.get("message") or {}
    if t == "assistant":
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name")
                inp = b.get("input") or {}
                if name in ("Edit", "Write", "NotebookEdit"):
                    fp = inp.get("file_path")
                    if fp:
                        plus, minus = hookcapture.line_stats(name, inp)
                        item = {"kind": "file_touched", "value": fp, "tool": name}
                        if plus is not None:
                            if plus == 0 and minus == 0:
                                continue      # +0 −0 is not a change (locked rule)
                            item["plus"], item["minus"] = plus, minus
                        items.append(item)
                elif name in ("Bash", "PowerShell"):
                    cmd = inp.get("command")
                    if cmd:
                        item = {"kind": "command_run",
                                "value": cmd[:hookcapture.CMD_MAX]}
                        desc = inp.get("description")
                        if isinstance(desc, str) and desc.strip():
                            item["desc"] = desc.strip()[:hookcapture.DESC_MAX]
                        items.append(item)
        # Capture Claude's prose response too, so the OUTPUT enters the hive
        # alongside the prompt. Tool-only turns have no text and are skipped.
        resp = _user_text(content).strip()
        if resp:
            items.append({"kind": "assistant_response", "value": _cap_words(resp)})
    elif t == "user":
        text = _user_text(msg.get("content")).strip()
        low = text.lower()
        # Pick the marker that appears FIRST in the text (smallest position),
        # not whichever happens to come first in _MARKERS.
        best = None  # (position, marker)
        for m in _MARKERS:
            i = low.find(m)
            if i != -1 and (best is None or i < best[0]):
                best = (i, m)
        if best is not None:
            pos, m = best
            val = text[pos + len(m):].strip()
            if val:
                if m == "caveat:":
                    mtype = "caveat"
                elif m == "decision:":
                    mtype = "decision"            # explicit marker is authoritative
                else:                              # remember:/memo: -> infer from hints
                    mtype = "caveat" if any(h in low for h in _CAVEAT_HINT) else "decision"
                # Store the full memo — no truncation. Redundant re-phrasings
                # are collapsed by meaning at display time (see dedupe.py),
                # so the ledger keeps every memory verbatim and complete.
                items.append({"kind": "memo", "value": _cap_words(val), "memo_type": mtype})
        # Capture the typed prompt itself so EVERY prompt enters the hive — not
        # only marker-bearing ones. Skip injected skill bodies (not user-typed).
        if text and not _is_injected_prompt(text):
            prompt = _clean_prompt(text)
            if prompt:
                items.append({"kind": "user_prompt", "value": _cap_words(prompt)})
    return items


def iter_transcript_events(path) -> list:
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        out.extend(_items_from_obj(o))
    return out


def _hook_captured_sessions(project) -> set:
    """session_ids that already carry a NON-harvest event in this project's
    store — i.e. the live hooks recorded them. For these we dedup at the
    event level (see _existing_event_keys) rather than re-adding actions the
    hooks already wrote. This is what makes harvest safe to run alongside the
    live hooks."""
    out = set()
    for ev in events.read_all(project):
        if "harvest" not in (ev.tags or []):
            out.add(ev.session_id)
    return out


def _existing_event_keys(project) -> set:
    """(session_id, type, text) for every event already in the store.

    Used to skip re-adding an event the live hooks already captured WITHOUT
    skipping the whole session — so a session the hooks only partially captured
    (e.g. the hook crashed mid-session) still gets its missed events backfilled
    instead of being fast-forwarded past forever."""
    keys = set()
    for ev in events.read_all(project):
        text = ev.payload.get("text") if isinstance(ev.payload, dict) else None
        keys.add((ev.session_id, ev.type, text))
    return keys


def _event_for_item(item):
    """Map a parsed transcript item to (event_type, payload, tags)."""
    kind = item["kind"]
    if kind == "file_touched":
        payload = {"text": item["value"]}
        for k in ("plus", "minus", "tool"):
            if k in item:
                payload[k] = item[k]
        return "file_touched", payload, ["harvest", "auto"]
    if kind == "command_run":
        payload = {"text": item["value"]}
        if "desc" in item:
            payload["desc"] = item["desc"]
        return "command_run", payload, ["harvest", "auto"]
    if kind == "memo":
        return item["memo_type"], {"text": item["value"]}, ["harvest", "memo"]
    if kind == "user_prompt":
        return "user_prompt", {"text": item["value"]}, ["harvest", "prompt"]
    if kind == "assistant_response":
        return "assistant_response", {"text": item["value"]}, ["harvest", "response"]
    return None


def _state_path(project):
    return paths.project_dir(project) / "harvest_state.json"


def _load_state(project) -> dict:
    p = _state_path(project)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(project, state):
    paths.project_dir(project).mkdir(parents=True, exist_ok=True)
    _state_path(project).write_text(events.canonical_json(state), encoding="utf-8")


def harvest(transcript_dir, project, now=None, skip_captured=False) -> dict:
    """Append events for NEW transcript lines only (cursor in harvest_state.json).

    With ``skip_captured=True`` any event the live hooks already recorded is
    deduped at the event level, so harvest is safe to run alongside the hooks
    without double-counting — while still backfilling events in a captured
    session that the hooks missed (e.g. after a mid-session hook crash)."""
    d = Path(transcript_dir)
    state = _load_state(project)
    captured = _hook_captured_sessions(project) if skip_captured else set()
    # (session, type, text) keys already in the store — only needed to dedup
    # against hook-captured sessions; grows as we write so in-transcript repeats
    # aren't lost.
    seen_keys = _existing_event_keys(project) if captured else set()
    new_events = 0
    files = 0
    skipped = 0
    if d.exists():
        for f in sorted(d.glob("*.jsonl")):
            files += 1
            try:
                all_lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            cursor = int(state.get(f.name, 0))
            if len(all_lines) <= cursor:
                continue
            sid = f.stem
            is_captured = sid in captured
            wrote = 0
            for line in all_lines[cursor:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                # Preserve the real time the action happened (from the transcript
                # line), falling back to `now` only when the line has no timestamp.
                line_ts = _normalize_ts(o.get("timestamp")) or now
                for item in _items_from_obj(o):
                    mapped = _event_for_item(item)
                    if mapped is None:
                        continue
                    etype, payload, tags = mapped
                    if is_captured:
                        # Skip what the hooks already wrote for this session, but
                        # let through anything they missed.
                        key = (sid, etype, payload.get("text"))
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                    events.append_event(etype, payload, session_id=sid,
                                        tags=tags, ts=line_ts, project=project)
                    new_events += 1
                    wrote += 1
            state[f.name] = len(all_lines)
            if is_captured and wrote == 0:
                skipped += 1
    _save_state(project, state)
    return {"processed_files": files, "new_events": new_events, "skipped_captured": skipped}


def claude_projects_root() -> Path:
    """Where Claude Code stores per-folder session transcripts."""
    return Path.home() / ".claude" / "projects"


def project_for_transcript_dir(d) -> str | None:
    """Infer the MIND HIVE project namespace for a Claude transcript directory
    by reading the first ``cwd`` recorded in any transcript and applying the
    normal project detection. None when no cwd is found (can't be placed)."""
    for f in sorted(Path(d).glob("*.jsonl")):
        try:
            # Stream and stop at the first cwd (it's near the top) instead of
            # loading the whole transcript — these files can be multi-MB.
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = (json.loads(line) or {}).get("cwd")
                    except (json.JSONDecodeError, ValueError):
                        # A malformed line (e.g. a transcript caught mid-write)
                        # must skip to the next LINE, not abandon the whole file
                        # — otherwise the session is mis-detected as None and
                        # dropped from catch_up entirely.
                        continue
                    if cwd:
                        return paths.detect_project(Path(cwd))
        except OSError:
            continue
    return None


def existing_projects() -> list:
    """Store namespaces that already have an events log — i.e. projects the user
    already tracks in MIND HIVE."""
    home = paths.store_home()
    out = []
    if home.exists():
        for d in sorted(home.iterdir()):
            if d.is_dir() and (d / "events").is_dir():
                out.append(d.name)
    return out


def catch_up(now=None, skip_captured=True, known_only=True) -> dict:
    """Self-healing fallback: scan every Claude Code transcript directory and
    harvest any session the live hooks didn't capture, into the project its cwd
    maps to. Safe to run repeatedly and alongside the hooks (skip_captured).
    This is what backfills sessions in environments where the hooks never fire
    (e.g. the Agent SDK harness).

    ``known_only`` (default) bounds the blast radius to namespaces the user
    already tracks, so incidental sessions (e.g. run from the home folder) never
    spawn surprise projects. Returns a per-project summary plus any skipped
    namespaces."""
    with _harvest_lock() as got:
        if not got:               # another harvester is running — skip this tick
            return {"projects": {}, "total_new": 0, "skipped_unknown": [], "locked": True}
        return _catch_up_unlocked(now=now, skip_captured=skip_captured, known_only=known_only)


def _catch_up_unlocked(now=None, skip_captured=True, known_only=True) -> dict:
    # Local import: the Learning-Loop capture stage. Kept local so importing
    # harvest stays cheap and there is no import-time coupling.
    from . import failures, incidents
    root = claude_projects_root()
    known = set(existing_projects()) if known_only else None
    per_project: dict = {}
    skipped_unknown: set = set()
    total_new = 0
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            proj = project_for_transcript_dir(d)
            if not proj:
                continue
            if known is not None and proj not in known:
                skipped_unknown.add(proj)
                continue
            r = harvest(d, proj, now=now, skip_captured=skip_captured)
            # Also capture tool FAILURES and advance the incident lifecycle, so
            # the Learning Loop self-feeds from the same automatic sweep instead
            # of requiring a manual `hive failures capture`.
            fr = failures.capture_dir(str(d), project=proj, now=now)
            if fr.get("new_failures"):
                incidents.audit(project=proj)
                incidents.project(project=proj)
            if r["new_events"] or fr.get("new_failures"):
                acc = per_project.setdefault(
                    proj, {"new_events": 0, "new_failures": 0, "dirs": 0})
                acc["new_events"] += r["new_events"]
                acc["new_failures"] += fr.get("new_failures", 0)
                acc["dirs"] += 1
                total_new += r["new_events"]
    return {"projects": per_project, "total_new": total_new,
            "skipped_unknown": sorted(skipped_unknown)}
