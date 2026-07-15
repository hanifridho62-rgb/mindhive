---
description: Turn an idea into a validated spec and executed work via the MindHive Orchestrator (ULT-IDEA canvas + memory)
argument-hint: "[your idea]"
---

You are activating **`/mindhive`** — the MindHive Orchestrator. It runs the full
**ULT-IDEA** design canvas (validate the idea → agent-native architecture review →
sharpen into an implementation-ready spec) **fused with MindHive memory**, then
selects and chains the skills already installed on this machine to execute it, and
logs what it learned back to the ledger.

The idea to orchestrate: `$ARGUMENTS`

> This validates and sharpens the idea with rigor, recommends the best execution
> skills with a scored matrix, executes through them (green/red, self-healing), and
> logs lessons back to MindHive. It confirms the spec before it starts building —
> the anti-leak HARD-GATE means no code until the spec is approved.

**Resolve the MindHive interpreter once, first.** Run the first form that returns real
output (not the Windows Store "Python was not found" message), then reuse that prefix:
`python -m mindhive state` → else the project venv
`C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive state` (known-good on
this machine) → else `hive state`. If no shell exists, read `STATE.md`/`CONTEXT.md`/
`MEMORY.md` and the code directly.

**Follow the full procedure in the `mindhive-orchestrator` skill.** In short:

0. **Recall before you ask (Requirement 1).** Run `… -m mindhive state`,
   `… -m mindhive recall "<key terms>"` (add `--semantic` if thin), and
   `… -m mindhive graph related "<main entity>"` when the idea touches a subsystem.
   Let the results pre-answer or skip questions; explore the codebase/ledger instead
   of asking anything they already answer.
1. **ULT-IDEA canvas.** Diagnose scope (Lightweight/Standard/Deep), then run Phase 1
   (five-gap validation → 2–3 approaches → durable requirements doc), Phase 2 +
   2.5 for systems (agent-native review → locked architecture decisions), and Phase 3
   (eliminate fuzzy language → CONTEXT.md, edge-case scenarios, testing strategy).
   One question at a time, each led by your recommended answer. **Do not write code or
   invoke an execution skill until the spec is approved** (the HARD-GATE).
2. **Recommendation Matrix.** On approval, score candidate skills — resolved verbatim
   from `… -m mindhive skills list` and grounded in `… -m mindhive plugins recommend`
   and `… -m mindhive skills status` (green/red history) — on suitability,
   compatibility, success probability (labeled `est.`), risk, speed, cost, verdict.
   Get approval on the selection.
3. **Execute, then chain.** Invoke the selected skill(s) (Skill tool, or
   `… -m mindhive skills invoke <name>` when MindHive should drive it), then
   proactively recommend follow-up passes (debug, security, quality) with a rationale.
   Before invoking a skill that shows red, read its lesson with
   `… -m mindhive rules show "skill:<name>"`.
4. **Learn.** `… -m mindhive append` any mistakes/inefficiencies/new preferences tagged
   `orchestrator`, then `… -m mindhive sync`.

Throughout, monitor context at each phase boundary and offer a **Handoff** at roughly
50% per the skill's handoff protocol. Works in Claude Code, Cowork, and NewTask — see
the skill's `references/compatibility.md` for how asking and command-running adapt per
surface.

If `$ARGUMENTS` is empty, ask the user for the idea before starting.
