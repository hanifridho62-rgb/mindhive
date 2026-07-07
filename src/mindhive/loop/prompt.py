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
When the entire task IS finished, also say so plainly in prose before the
block (e.g. "All tasks have been completed; nothing remains.") — the exit
gate requires the prose statement AND the structured signal to agree.
"""


def build_loop_context(briefing_text: str) -> str:
    """System-prompt context for an iteration: the compact MindHive briefing
    plus the standing completion contract."""
    return f"{briefing_text.rstrip()}\n\n---\n\n{CONTRACT_INSTRUCTION}"


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
