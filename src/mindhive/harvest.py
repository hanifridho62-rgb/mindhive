# src/mindhive/harvest.py
# Deterministic, hook-free auto-capture: turn Claude Code session transcripts
# (~/.claude/projects/<encoded>/<uuid>.jsonl) into MIND HIVE events.
import json
from pathlib import Path
from . import events, paths

_MARKERS = ("remember:", "memo:", "decision:", "caveat:")
_CAVEAT_HINT = ("bug", "fail", "lesson", "incident", "careful", "don't", "avoid", "broke")


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
                        items.append({"kind": "file_touched", "value": fp})
                elif name in ("Bash", "PowerShell"):
                    cmd = inp.get("command")
                    if cmd:
                        items.append({"kind": "command_run", "value": cmd[:180]})
    elif t == "user":
        text = _user_text(msg.get("content")).strip()
        low = text.lower()
        for m in _MARKERS:
            if m in low:
                val = text[low.index(m) + len(m):].strip()
                mtype = "caveat" if (m == "caveat:" or any(h in low for h in _CAVEAT_HINT)) else "decision"
                if val:
                    # Store the full memo — no truncation. Redundant re-phrasings
                    # are collapsed by meaning at display time (see dedupe.py),
                    # so the ledger keeps every memory verbatim and complete.
                    items.append({"kind": "memo", "value": val, "memo_type": mtype})
                break
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
    store — i.e. the live hooks recorded them. Re-harvesting those would double
    the same actions, so skip them. This is what makes harvest safe to run
    alongside the live hooks."""
    out = set()
    for ev in events.read_all(project):
        if "harvest" not in (ev.tags or []):
            out.add(ev.session_id)
    return out


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

    With ``skip_captured=True`` any session the live hooks already recorded is
    left untouched, so harvest is safe to run alongside the hooks without
    double-counting the same tool actions."""
    d = Path(transcript_dir)
    state = _load_state(project)
    captured = _hook_captured_sessions(project) if skip_captured else set()
    new_events = 0
    files = 0
    skipped = 0
    if d.exists():
        for f in sorted(d.glob("*.jsonl")):
            files += 1
            if skip_captured and f.stem in captured:
                skipped += 1
                state[f.name] = max(int(state.get(f.name, 0)),
                                    len(f.read_text(encoding="utf-8", errors="ignore").splitlines()))
                continue
            try:
                all_lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            cursor = int(state.get(f.name, 0))
            if len(all_lines) <= cursor:
                continue
            sid = f.stem
            for line in all_lines[cursor:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                for item in _items_from_obj(o):
                    if item["kind"] == "file_touched":
                        events.append_event("file_touched", {"text": item["value"]},
                                            session_id=sid, tags=["harvest", "auto"], ts=now, project=project)
                    elif item["kind"] == "command_run":
                        events.append_event("command_run", {"text": item["value"]},
                                            session_id=sid, tags=["harvest", "auto"], ts=now, project=project)
                    elif item["kind"] == "memo":
                        events.append_event(item["memo_type"], {"text": item["value"]},
                                            session_id=sid, tags=["harvest", "memo"], ts=now, project=project)
                    new_events += 1
            state[f.name] = len(all_lines)
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
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or '"cwd"' not in line:
                    continue
                cwd = (json.loads(line) or {}).get("cwd")
                if cwd:
                    return paths.detect_project(Path(cwd))
        except Exception:
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
            if r["new_events"]:
                acc = per_project.setdefault(proj, {"new_events": 0, "dirs": 0})
                acc["new_events"] += r["new_events"]
                acc["dirs"] += 1
                total_new += r["new_events"]
    return {"projects": per_project, "total_new": total_new,
            "skipped_unknown": sorted(skipped_unknown)}
