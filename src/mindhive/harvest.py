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
                    items.append({"kind": "memo", "value": val[:300], "memo_type": mtype})
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


def harvest(transcript_dir, project, now=None) -> dict:
    """Append events for NEW transcript lines only (cursor in harvest_state.json)."""
    d = Path(transcript_dir)
    state = _load_state(project)
    new_events = 0
    files = 0
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
    return {"processed_files": files, "new_events": new_events}
