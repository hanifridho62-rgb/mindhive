# src/mindhive/loop/task.py
import re
from dataclasses import dataclass
from pathlib import Path

_FUTURE_HEADING = re.compile(r"^\s*#+\s*(future|optional)", re.IGNORECASE)
_UNCHECKED = re.compile(r"^\s*[-*]\s*\[\s\]")
_CHECKED_ITEM = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*(.*)")
_UNCHECKED_ITEM = re.compile(r"^\s*[-*]\s*\[\s\]\s*(.*)")

# The provenance marker on a checklist item the replenishment injector added.
# It is the SAME marker `roadmap.NEW_MARKER` puts on a roadmap step, written as
# an independent literal here so `loop/` does not import the registry for one
# string — `tests/test_loop_task.py` pins the two together, which is what makes
# "one marker" checkable rather than a convention someone can quietly break.
NEW_MARKER = "(new)"
_NEW_PREFIX = re.compile(r"^\(new\)\s*", re.IGNORECASE)


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


def item(text: str) -> dict:
    """One checklist item as ``{text, is_new}``, with the provenance marker
    PARSED rather than displayed raw.

    The marker is stripped from the text and carried as a flag, so a renderer
    shows a badge instead of literal punctuation, and so `is_new` survives the
    hops where a substring would not — `roadmap.steps_from_plan_items` and the
    interruption snapshot both rebuild the text.

    Provenance, not recency: nothing clears the flag, including checking the
    item off. Only a LEADING marker counts — a line whose own wording contains
    "(new)" later on is describing its work, not claiming to be injected."""
    text = str(text or "").strip()
    stripped = _NEW_PREFIX.sub("", text, count=1)
    return {"text": stripped.strip(), "is_new": stripped != text}


def plan_items(fix_plan: str) -> dict:
    """Split fix_plan checklist items into Oversee progress groups: ``done``
    (checked, anywhere), ``planned`` (unchecked before a Future/Optional
    heading — the required work), ``not_yet`` (unchecked under such a
    heading — deferred/optional work).

    Each group holds ``{text, is_new}`` dicts (see :func:`item`). The marker is
    orthogonal to the grouping: an injected item under a Future heading is still
    deferred, and `required_remaining_count` does not care about markers at
    all — the COUNT is what forces the loop to continue, the marker is
    provenance for whoever reads the plan."""
    done, planned, not_yet = [], [], []
    future = False
    for line in fix_plan.splitlines():
        if _FUTURE_HEADING.match(line):
            future = True
            continue
        m = _CHECKED_ITEM.match(line)
        if m:
            done.append(item(m.group(1)))
            continue
        m = _UNCHECKED_ITEM.match(line)
        if m:
            (not_yet if future else planned).append(item(m.group(1)))
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
