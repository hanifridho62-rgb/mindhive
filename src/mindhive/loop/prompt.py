# src/mindhive/loop/prompt.py

CONTRACT_INSTRUCTION = """\
When you finish this iteration, end your response with this exact block:

LOOP_STATUS:
  summary: <one line of what you did this iteration>
  progress: true|false
  STATUS: IN_PROGRESS|COMPLETE
  EXIT_SIGNAL: true|false

Set EXIT_SIGNAL: true ONLY when the ENTIRE task is finished and verified.
If more work remains, set EXIT_SIGNAL: false even if this iteration went well.
When the entire task IS finished, also state that outcome plainly in your own
words in the prose before the block — describe what is complete and confirm
no work remains. The exit gate requires your prose account and the structured
signal to agree; a bare signal without the prose statement will not end the run.
"""


# How many items of any one kind ride the handoff. A cap, not a budget: the
# delta seeds a FRESH session, and a seed that approaches the smart-zone
# ceiling itself is what `safeguards.RotationGuard` halts on.
MAX_DELTA_ITEMS = 12

_GLYPH = {"done": "[x]", "active": "[>]", "pending": "[ ]", "skipped": "[-]"}

_UNVERIFIED_HEADING = (
    "## The outgoing session's own note — UNVERIFIED\n"
    "Nothing below has been checked against the ledger. Treat it as a hint "
    "about intent, never as a record of what happened; where it disagrees "
    "with the status above, the status above is correct."
)


def _rows(items):
    """Only mappings, only non-empty — malformed rows vanish rather than raise.
    A projection fault must not become a crash in a loop driving subprocesses."""
    out = []
    for it in items or ():
        if isinstance(it, dict):
            out.append(it)
    return out


def _capped(items, render):
    lines = [render(i) for i in items[:MAX_DELTA_ITEMS]]
    lines = [ln for ln in lines if ln]
    dropped = len(items) - MAX_DELTA_ITEMS
    if dropped > 0:
        lines.append(f"- ... and {dropped} more (truncated)")
    return lines


def build_progress_delta(*, steps=(), changes=(), phases=(),
                         agent_note=None) -> str:
    """What the previous slice accomplished, for the session that replaces it.

    Every input here is MECHANICAL — closed roadmap steps, recorded change-log
    entries, satisfied phases — and is folded by the caller from the existing
    registries rather than re-derived here. `agent_note` is the one input that
    is not, so it is rendered last and under an explicit unverified heading:
    `mindnet/agent.py` already establishes that an agent's exit-0 reply proves
    only that the model answered, and a handoff is exactly the place where a
    confident wrong claim would be adopted as state by the next session.

    Returns "" when nothing happened. An empty scaffold of headers would assert
    "we looked and found nothing", which is a different and stronger claim than
    "there is nothing to say"."""
    parts = []

    step_rows = [s for s in _rows(steps) if str(s.get("text") or "").strip()]
    if step_rows:
        parts.append("## Plan steps\n" + "\n".join(_capped(
            step_rows,
            lambda s: f"- {_GLYPH.get(s.get('status'), '[ ]')} "
                      f"{str(s.get('text') or '').strip()}")))

    change_rows = [c for c in _rows(changes) if str(c.get("text") or "").strip()]
    if change_rows:
        parts.append("## Changes recorded\n" + "\n".join(_capped(
            change_rows,
            lambda c: f"- {str(c.get('category') or 'change')}: "
                      f"{str(c.get('text') or '').strip()}")))

    phase_rows = [p for p in _rows(phases)
                  if str(p.get("task_id") or "").strip()
                  and str(p.get("phase") or "").strip()]
    if phase_rows:
        parts.append("## Phases satisfied\n" + "\n".join(_capped(
            phase_rows,
            lambda p: f"- {str(p.get('task_id')).strip()}: "
                      f"{str(p.get('phase')).strip()}")))

    note = str(agent_note or "").strip()
    if note:
        parts.append(f"{_UNVERIFIED_HEADING}\n\n{note}")

    return "\n\n".join(parts)


def build_loop_context(briefing_text: str, progress_delta: str = "") -> str:
    """System-prompt context for an iteration: the compact MindHive briefing,
    the progress carried across a session rotation when there is one, and the
    standing completion contract."""
    head = briefing_text.rstrip()
    delta = (progress_delta or "").strip()
    if delta:
        head = f"{head}\n\n---\n\n# Carried across a session rotation\n\n{delta}"
    return f"{head}\n\n---\n\n{CONTRACT_INSTRUCTION}"


def build_task_prompt(tasks, recall_hits) -> str:
    parts = []
    if tasks.prompt.strip():
        parts.append("# Goal\n" + tasks.prompt.strip())
    if tasks.fix_plan.strip():
        parts.append("# Fix plan (work the unchecked items, top priority first)\n"
                     + tasks.fix_plan.strip())
    if tasks.agent.strip():
        parts.append("# Build / test / run commands\n" + tasks.agent.strip())
    if recall_hits:
        lines = ["# Relevant prior memory"]
        for h in recall_hits:
            lines.append(f"- {h.get('snippet', '')}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "Continue the task."
