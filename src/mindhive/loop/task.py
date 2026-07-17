# src/mindhive/loop/task.py
import re
from dataclasses import dataclass
from pathlib import Path

_FUTURE_HEADING = re.compile(r"^\s*#+\s*(future|optional)", re.IGNORECASE)
_UNCHECKED = re.compile(r"^\s*[-*]\s*\[\s\]")
_CHECKED_ITEM = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*(.*)")
_UNCHECKED_ITEM = re.compile(r"^\s*[-*]\s*\[\s\]\s*(.*)")


@dataclass
class TaskFiles:
    prompt: str
    fix_plan: str
    agent: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def load_tasks(loop_dir: Path) -> TaskFiles:
    loop_dir = Path(loop_dir)
    return TaskFiles(
        prompt=_read(loop_dir / "PROMPT.md"),
        fix_plan=_read(loop_dir / "fix_plan.md"),
        agent=_read(loop_dir / "AGENT.md"),
    )


def plan_items(fix_plan: str) -> dict:
    """Split fix_plan checklist items into Oversee progress groups: ``done``
    (checked, anywhere), ``planned`` (unchecked before a Future/Optional
    heading — the required work), ``not_yet`` (unchecked under such a
    heading — deferred/optional work)."""
    done, planned, not_yet = [], [], []
    future = False
    for line in fix_plan.splitlines():
        if _FUTURE_HEADING.match(line):
            future = True
            continue
        m = _CHECKED_ITEM.match(line)
        if m:
            done.append(m.group(1).strip())
            continue
        m = _UNCHECKED_ITEM.match(line)
        if m:
            (not_yet if future else planned).append(m.group(1).strip())
    return {"done": done, "planned": planned, "not_yet": not_yet}


def required_remaining_count(fix_plan: str) -> int:
    """Count unchecked checklist items that appear BEFORE any Future/Optional
    heading. Items under such a heading are non-blocking and never counted."""
    count = 0
    for line in fix_plan.splitlines():
        if _FUTURE_HEADING.match(line):
            break
        if _UNCHECKED.match(line):
            count += 1
    return count
