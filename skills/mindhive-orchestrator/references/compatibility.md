# Compatibility & Portability

This skill is written to run on **all parts of Claude** — Claude Code, Claude
Cowork, and Claude NewTask. ULT-IDEA rigor is pure prose and runs anywhere; the
MindHive machinery layers on where the CLI is present and degrades cleanly where
it is not. This reference expands the summary in [SKILL.md](../SKILL.md).

---

## The two portability seams

Only two things differ across Claude surfaces. Handle both explicitly and the
skill is portable everywhere.

### Seam 1 — the MindHive CLI

**Present** when `python -m mindhive`, `hive`, or the venv path resolves (usually
Claude Code on the machine where MindHive is installed). **Absent** in sandboxes
without the local install (often Cowork / NewTask).

**Contract:** if absent, announce **once** — "MindHive memory unavailable —
running ULT-IDEA in stateless mode" — and run full ULT-IDEA rigor. Every
`python -m mindhive …` block in the skill is best-effort; a missing memory command
never hard-fails the workflow. In stateless mode:

- Replace `recall` / `graph related` with direct codebase exploration (grep, read
  files) — the HARD-GATE "explore before asking" still holds against the code.
- Skip Phases S/X/L's MindHive-specific commands, but still run the *logic* of
  those phases: reason about which skills fit, invoke them with the in-agent Skill
  tool, and offer the follow-up quality chain.
- Skip ledger log-back; there is nowhere to write.

### Seam 2 — the blocking-question tool

Used to ask one question at a time and wait for the answer.

| Surface | Blocking-question tool | Fallback |
|---------|------------------------|----------|
| Claude Code | `AskUserQuestion` (load via `ToolSearch select:AskUserQuestion`) | Numbered options in chat |
| Claude Cowork | Platform blocking prompt | Numbered options in chat |
| Claude NewTask | Platform blocking prompt | Numbered options in chat |

Where no blocking tool exists, ask a **single** numbered-options question in chat
and wait for the reply. Never stack multiple questions to compensate.

---

## Full surface matrix

| Concern | Claude Code | Claude Cowork | Claude NewTask |
|---------|-------------|---------------|----------------|
| Blocking question | `AskUserQuestion` | Platform prompt / numbered chat | Platform prompt / numbered chat |
| Skill invocation | `Skill` tool + `skills invoke` | Platform skill primitive; in-agent if no CLI | Platform skill primitive; in-agent if no CLI |
| MindHive CLI | Usually present (local) | Often absent → stateless mode | Often absent → stateless mode |
| Visual companion (1.3) | Local browser URL | Inline diagrams / text fallback | Inline diagrams / text fallback |
| Ledger log-back (Phase L) | Full | Skipped in stateless mode | Skipped in stateless mode |

---

## Degradation ladder

Each rung still delivers a validated idea:

1. **Full fusion** — CLI + blocking tool + Skill tool. Memory-grounded, scored
   skill selection, execution chaining, learning loop. (Typical: Claude Code.)
2. **ULT-IDEA + in-agent chaining** — no CLI, but the Skill tool is present.
   Full validation rigor; skills selected by reasoning and invoked in-agent; no
   ledger grounding or telemetry. (Typical: Cowork/NewTask with skills.)
3. **Pure ULT-IDEA** — no CLI, no skill primitive. Validation, architecture
   review, language sharpening, testing strategy, and a written spec handed off.
   (Any surface.)

**Announce the mode once, at the top of the run.** Don't repeat it every phase.

---

## Frontmatter portability

Keep frontmatter to `name` + `description` only — the lowest common denominator
every surface parses reliably. All platform-variant mechanics live in the skill
body and this reference, never in frontmatter. Do not add platform-specific
frontmatter keys; they risk parse failures on surfaces that don't recognize them.
