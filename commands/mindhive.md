---
description: Turn an idea into executed work via the MindHive orchestrator pipeline
argument-hint: "[your idea]"
---

You are activating **`/mindhive`** — the MindHive Orchestrator. It turns the
idea below into finished, quality-checked work through a four-phase pipeline,
seeded by MindHive memory and chaining the skills already installed on this
machine.

The idea to orchestrate: `$ARGUMENTS`

> This will interview you, recommend the best skills with a scored matrix,
> execute through them, chain quality passes, and log what it learned back to
> MindHive. Confirm the idea before it starts building.

Run MindHive commands through the project venv:
`C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …`

Follow the full procedure in the **`mindhive-orchestrator`** skill. In short:

1. **Recall before asking.** Run `python -m mindhive state` and
   `python -m mindhive recall "<key terms>"` first; let the results pre-answer or
   skip interview questions. Explore the codebase/ledger instead of asking
   anything they can answer.
2. **Phase 1 — Interview.** Ask one question at a time, each with your
   recommended answer first, until scope, architecture, and risk are clear.
3. **Phase 2 — Recommendation Matrix.** Score candidate skills (resolved
   verbatim from the available-skills list) on suitability, compatibility,
   success probability (labeled est.), risk, speed, cost, and a verdict. Get
   approval on the selection.
4. **Phase 3 — Execute, then chain.** Invoke the selected skill(s), then
   proactively recommend follow-up skills (debug, security, quality) with a
   rationale each.
5. **Phase 4 — Learn.** `hive append` any mistakes, inefficiencies, or new
   preferences tagged `orchestrator`, then `hive sync`.

Throughout, monitor context at each phase boundary and offer a **Handoff** at
roughly 50% per the skill's handoff protocol.

If `$ARGUMENTS` is empty, ask the user for the idea before starting.
