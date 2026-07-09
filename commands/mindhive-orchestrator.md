---
description: Turn an idea into executed work via the MindHive orchestrator — the one centralized pipeline
argument-hint: "[your idea]"
---

You are activating **`/mindhive-orchestrator`** — the single centralized MindHive
pipeline. It turns the idea below into finished, quality-checked work through a
**ten-stage sequential, dynamic** workflow, seeded by MindHive memory. Its main
execution engine is restricted to the **`superpowers`** and **`compound-engineering`**
plugins (either may run, or both combined); every other installed skill is a
**complement** that only raises quality.

This is the **only** trigger for the centralized pipeline. `/circle` and
`mindhive:circle` are the headless-loop primitive; `/engineering-loop` is a
subordinate execution engine this pipeline may drive — none of them host the
pipeline.

The idea to orchestrate: `$ARGUMENTS`

> This will interview you only when it must, recommend the best skills with a
> scored matrix, execute (fanning out to parallel sub-agents when the work
> allows), run quality passes, and log what it learned back to MindHive. Confirm
> the idea before it starts building.

Run MindHive commands through the project venv:
`C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …`

Follow the full procedure in the **`mindhive-orchestrator`** skill. In short:

1. **Recall before you ask.** Run `python -m mindhive state` and
   `python -m mindhive recall "<key terms>"` first; let the results pre-answer or
   skip questions. Explore the codebase/ledger instead of asking anything they
   can answer.
2. **Run the ten stages in order:** brainstorm → plan → observation →
   understand-and-converge → PRD → recommendation matrix → kanban → execution →
   quality assessment → learning. Each stage's backbone is a `superpowers` or
   `compound-engineering` skill (resolved verbatim from the available-skills
   list); other skills attach only as complements.
3. **Advance dynamically.** At each stage boundary, auto-advance when intent is
   clear from recall + codebase (automatic); pause to converge with the user only
   when a scope/architecture/risk fork is genuinely unresolved (semi-automatic).
4. **Fan out at execution.** Use `python -m mindhive clusters plan` to classify
   file complexity into file-ownership-safe lanes, then dispatch parallel
   sub-agents under `superpowers:dispatching-parallel-agents`.
5. **Learn.** `hive append` any mistakes, inefficiencies, or new preferences
   tagged `orchestrator`, then `hive sync`.

If `$ARGUMENTS` is empty, ask the user for the idea before starting.
