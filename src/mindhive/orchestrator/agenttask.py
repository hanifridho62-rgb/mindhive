# src/mindhive/orchestrator/agenttask.py
"""`AgentTask` -- everything one child is told, and nothing else -- plus the
normalised outcome the scheduler reads back.

Carved out of `lintester.py` at the size cap, the way `mission.py` and
`preview.py` were. The pairing is deliberate: this module is the whole closed
surface between the orchestrator and one child. `AgentTask` is what goes in
(frozen, so no lane can be handed a task another is mutating, and so the field
set is a thing a test can assert is CLOSED -- that is what keeps global repo
state from creeping back into a per-unit prompt), and `_RunOutcome` is what
comes back. Nothing here spawns anything or touches the filesystem.

Every name stays reachable as `lintester.AgentTask` / `lintester._RunOutcome`
-- those are the names in use, in the tests, in `orchestrator/__init__.py` and
in `gate.py`'s local import -- and `lintester.py` re-imports them.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import NamedTuple

MUST_USE_LEAD = ("Skills the record says work here. Use them via the Skill tool "
                 "before the first edit:")

# How many skills one task's clause may name. `advice.resolve` is documented in
# this project's ledger as noisy — a dashboard task once matched
# `market-sizing-analysis` — and a directive that names eight skills directs
# nothing. Three is a clause a child can act on.
MAX_MUST_USE = 3

# `advice` returns each skill as "<name> — <the catalog's whole description>",
# which is right for the fenced context block and wrong for a directive: the
# clause is an instruction naming a tool, and pasting a 200-character blurb into
# it buries the name it exists to say.
_SKILL_SPLITS = (" — ", " - ", ": ")


def _skill_name(value) -> str:
    text = _clean(value)
    for sep in _SKILL_SPLITS:
        text = text.split(sep, 1)[0]
    return text.strip()


class _RunOutcome(NamedTuple):
    """A child's result, normalised to what the scheduler and repair loop read."""

    ok: bool = False
    reason: str = ""
    stderr: str = ""
    result: str = "fail"
    # Whether a child process was created at all. `False` is what separates a
    # task that failed from a machine that could not start one, and
    # `environment.environment_failure` reads it -- so it must survive the
    # normalisation rather than be folded into `result`.
    executed: bool = True
    exit_code: object = None


@dataclass(frozen=True)
class AgentTask:
    """One independently-executable unit, carrying its own scope and nothing else.

    Frozen on purpose, and the frozen-ness is load-bearing twice: a child cannot
    be handed a task another lane is mutating, and the closed field set is what
    `test_lintester.py` asserts against to keep global repo state from creeping
    back in. Lifecycle lives on `queue.QueueEntry`, which is the mutable half."""

    task_id: str
    title: str = ""
    description: str = ""
    brief_text: str = ""
    must_use: tuple = ()
    weight: int = 0
    lane_id: int = -1
    worktree_path: str = ""
    files: tuple = ()

    def with_repair(self, suffix) -> "AgentTask":
        """A copy carrying a repair block appended to its description.

        *suffix* has already been through `resilience.defused_stderr` by the time
        it reaches here — `RepairLoop` composes it — so this does no sanitising
        of its own and must not be called with raw child output."""
        text = str(suffix or "").strip()
        if not text:
            return self
        joined = f"{self.description}\n\n{text}".strip()
        return dataclasses.replace(self, description=joined)

    @property
    def must_use_block(self) -> str:
        lines = [f"- MUST USE: {s} via the Skill tool" for s in self.must_use if s]
        return "\n".join([MUST_USE_LEAD, *lines]) if lines else ""

    @property
    def prompt(self) -> str:
        """Everything this child is told, in a fixed order.

        Fixed so two runs of one task read the same way; an irreproducible prompt
        is one nobody can diff when the run goes wrong."""
        parts = [f"# {self.title}" if self.title else "", self.description,
                 self.must_use_block, self.brief_text]
        return "\n\n".join(p for p in parts if p).strip()


def _clean(text) -> str:
    try:
        return str(text or "").strip()
    except (ValueError, TypeError):
        return ""
