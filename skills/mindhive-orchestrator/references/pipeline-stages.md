# Pipeline Stages — the ten-stage sequence, its backbone skills, and the dynamic gate

The `/mindhive-orchestrator` pipeline runs ten stages **in order**. Its *main
execution engine is restricted to two plugins*: **`superpowers`** and
**`compound-engineering`**. Either may run a stage, or both may be combined —
but the backbone of every stage is one of these two. Every other installed
skill/plugin is a **complement/sub** that only raises quality (design, security,
accessibility, research, etc.); a complement never replaces a stage's backbone.

**Hard rule:** backbone and complement skill names are resolved **verbatim from
the session's available-skills list** at run time. Never invent a name or use a
remembered one. If a listed backbone skill is absent this session, say so and
fall back to the other allowed plugin.

## Stage → backbone-skill map

| # | Stage | Backbone (superpowers / compound-engineering only) | Typical complements (quality only) |
|---|-------|----------------------------------------------------|------------------------------------|
| 1 | **Brainstorm** | `superpowers:brainstorming` **or** `compound-engineering:ce-brainstorm` | `ce-ideate`, `ce-strategy`, `ce-web-researcher` |
| 2 | **Plan** | `compound-engineering:ce-plan` **or** `superpowers:writing-plans` | `ce-best-practices-researcher`, `ce-framework-docs-researcher` |
| 3 | **Observation** | codebase + ledger recon via `ce-plan`/`ce-work` research agents **and** `hive recall` / `hive state` / `hive graph related` | `ce-repo-research-analyst`, `ce-git-history-analyzer` |
| 4 | **Understand & converge** | `superpowers:brainstorming` convergence posture — resolve open forks with the user (see the dynamic gate below) | — (this is a user-facing stage, not a skill) |
| 5 | **PRD** | the requirements doc emitted by `compound-engineering:ce-brainstorm` / `ce-plan` (`docs/brainstorms/*-requirements.md`, `docs/plans/*-plan.md`) | a PRD-formatting skill, only to reshape — never to author intent |
| 6 | **Recommendation Matrix** | the orchestrator's own scored matrix — load `references/recommendation-matrix.md` | `hive recall "<skill>"` for prior outcomes |
| 7 | **Kanban** | work board built from the plan's Implementation Units (`TaskCreate`/`TaskList`), informed by `superpowers:executing-plans` | — |
| 8 | **Execution** | `compound-engineering:ce-work` (+ `superpowers:executing-plans`, `superpowers:subagent-driven-development`, `superpowers:dispatching-parallel-agents` for the parallel layer — see `references/parallel-orchestration.md`) | `ui-design:*`, `security-scanning:*`, domain skills |
| 9 | **Quality assessment** | `compound-engineering:ce-code-review` + `compound-engineering:ce-simplify-code` + `compound-engineering:ce-test-browser` + `superpowers:verification-before-completion` | `security-scanning:security-sast`, `ui-design:accessibility-audit` |
| 10 | **Learning** | `hive append …` + `hive sync` + `compound-engineering:ce-compound` | — |

Stages 8→9 form a **loop**: a failed verification in stage 9 returns to stage 8
(bounded by the caller's circuit breaker / iteration cap), not forward. Stage 10
only writes when something memorable actually happened — a clean, unremarkable
run appends nothing.

## The dynamic confidence gate (automatic vs. semi-automatic)

The pipeline is **dynamic**: at every stage boundary the orchestrator estimates
how well it understands the user's intent for the *next* stage, then either
**auto-advances** (automatic) or **pauses to converge** (semi-automatic). This is
where "the user's wishes and the AI's context understanding meet."

There is no exact meter in-session, so estimate **intent-confidence** from
observable proxies (same heuristic posture as `references/handoff-protocol.md`):

- **Recall coverage** — did `hive recall` / `state` / `graph` return decisions,
  caveats, or preferences that answer the open question for this stage?
- **Codebase answerability** — can the open question be settled by reading files
  rather than asking (the prime directive: *recall/read before you ask*)?
- **Scope/architecture/risk clarity** — are the decisions that change scope,
  architecture, or risk already pinned, or still open?

Decision at each boundary:

- **High confidence → automatic.** All scope/architecture/risk forks for the
  next stage are answered by recall + codebase. Announce the inferred decision
  in one line ("The ledger shows you settled X, so I'll proceed") and advance
  **without a blocking question**.
- **Low confidence → semi-automatic.** A fork that changes scope, architecture,
  or risk is unresolved and *not* answerable from recall/codebase. Pause and ask
  **one** question at a time via the blocking-question tool (`AskUserQuestion` in
  Claude Code), each **leading with the recommended answer**, until confidence
  is restored. Then advance.

Never ask a question answerable from the codebase or the ledger. Never block on
taste-only preferences that don't move scope, architecture, or risk — prefer a
stated default the user can override. The gate **converges; it does not hard-
stop** — mirroring the repo's handoff posture.
