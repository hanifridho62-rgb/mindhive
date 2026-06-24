# src/mindhive/loop/task.py
import re
from dataclasses import dataclass
from pathlib import Path

_FUTURE_HEADING = re.compile(r"^\s*#+\s*(future|optional)", re.IGNORECASE)
_UNCHECKED = re.compile(r"^\s*[-*]\s*\[\s\]")


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
