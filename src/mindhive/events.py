# src/mindhive/events.py
import json
import os
import time
from dataclasses import dataclass, field, asdict
from . import paths

SEMANTIC_TYPES = {
    "decision", "experiment_result", "caveat",
    "next_step", "open_question", "state_set",
}
DETERMINISTIC_TYPES = {
    "file_touched", "command_run", "git_diff",
    "session_start", "session_end", "compaction",
}
VALID_TYPES = SEMANTIC_TYPES | DETERMINISTIC_TYPES


@dataclass
class Event:
    ts: str
    session_id: str
    type: str
    payload: dict
    tags: list = field(default_factory=list)


def to_dict(ev: "Event") -> dict:
    return asdict(ev)


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def append_event(type, payload, session_id=None, tags=None, ts=None, project=None) -> "Event":
    if type not in VALID_TYPES:
        raise ValueError(f"unknown event type: {type}")
    session_id = session_id or os.environ.get("MINDHIVE_SESSION") or "manual"
    ev = Event(ts=ts or now_iso(), session_id=session_id, type=type,
               payload=payload, tags=list(tags or []))
    d = paths.events_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    seg = d / f"{session_id}.jsonl"
    with open(seg, "a", encoding="utf-8") as f:
        f.write(canonical_json(to_dict(ev)) + "\n")
    return ev


def read_all(project=None) -> list["Event"]:
    d = paths.events_dir(project)
    rows = []
    if not d.exists():
        return []
    for seg in sorted(d.glob("*.jsonl")):
        for i, line in enumerate(seg.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            rows.append((obj.get("ts", ""), seg.name, i, Event(**obj)))
    rows.sort(key=lambda t: (t[0], t[1], t[2]))
    return [r[3] for r in rows]
