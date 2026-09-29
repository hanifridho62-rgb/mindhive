# SPDX-License-Identifier: Apache-2.0
# src/mindhive/loop/task.py
import re
from dataclasses import dataclass
from pathlib import Path

_FUTURE_HEADING = re.compile(r"^\s*#+\s*(future|optional)", re.IGNORECASE)
_UNCHECKED = re.compile(r"^\s*[-*]\s*\[\s\]")
_CHECKED_ITEM = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*(.*)")
_UNCHECKED_ITEM = re.compile(r"^\s*[-*]\s*\[\s\]\s*(.*)")

# The provenance marker the replenishment injector writes, anchored to the start
# of the item text. This is the SAME marker `roadmap.NEW_MARKER` renders — one
# spelling for one fact, on both surfaces. It is re-expressed as a regex here
# rather than imported so `loop` stays free of a `roadmap` import;
# `tests/test_loop_task.py` asserts the two cannot drift.
_NEW_MARKER = re.compile(r"^\(\s*new\s*\)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class PlanItem:
    """One checklist line, parsed.

    `text` has the marker stripped and `is_new` remembers it was there. Keeping
    those apart is the point: the marker is provenance for a reader, while the
    text is what gets matched against roadmap steps and rendered. A marker left
    inline would make one item look like two different ones either side of an
    injection, and every consumer would have to strip it again."""

    text: str
    is_new: bool = False
    status: str = "pending"           # pending | done

    def as_record(self) -> dict:
        """The JSON-safe shape for the dashboard wire and the roadmap payload.

        A dataclass cannot cross either boundary, and converting at the boundary
        rather than never using a type keeps the parse self-describing where the
        reasoning happens."""
        return {"text": self.text, "new": self.is_new, "status": self.status}


def parse_item(raw: str, status: str = "pending") -> PlanItem:
    """Split one checklist item's text into its marker and its content.

    An unmarked item yields `is_new=False` and text untouched, which is what
    makes every historical `fix_plan.md` parse exactly as it did before."""
    text = (raw or "").strip()
    stripped = _NEW_MARKER.sub("", text, count=1)
    return PlanItem(text=stripped.strip(), is_new=stripped != text,
                    status=status)


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
    heading — deferred/optional work).

    Values are `PlanItem`s, not bare strings. The groups themselves are unchanged,
    so a caller that only wanted the text reads `.text`; a caller that needs to
    know the injector put an item there reads `.is_new`. A checked item keeps its
    marker, which is what makes injected work still identifiable after it is
    done."""
    done, planned, not_yet = [], [], []
    future = False
    for line in fix_plan.splitlines():
        if _FUTURE_HEADING.match(line):
            future = True
            continue
        m = _CHECKED_ITEM.match(line)
        if m:
            done.append(parse_item(m.group(1), status="done"))
            continue
        m = _UNCHECKED_ITEM.match(line)
        if m:
            (not_yet if future else planned).append(parse_item(m.group(1)))
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
