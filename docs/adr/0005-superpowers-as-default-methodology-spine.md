# 5. Superpowers as the default methodology spine

Date: 2026-06-24
Status: Accepted

## Context

The user runs at least four overlapping software-development methodology stacks:
the **superpowers** plugin (brainstorming → writing-plans →
subagent-driven-development → TDD → debugging → review → finishing-a-branch),
**compound-engineering** (`/ce-brainstorm`, `/ce-plan`, `/ce-work`, `/ce-debug`,
`/ce-code-review`, …), **anthropic-skills** (`brainstorming`, `grill-with-docs`,
`tdd`, `diagnose`, `qa`), and the project's own conventions (CONTEXT.md glossary,
`docs/adr`, the thermo-nuclear maintainability review).

Each covers the same phases. Without a stated winner, Claude picks a different
tool for the same phase from session to session — the inconsistency the user
explicitly wanted removed ("so that Claude can use it effectively"). The user is
already on superpowers in practice: prior worktrees carry
`docs/superpowers/specs/` and `docs/superpowers/plans/` artifacts, and its plan
location matches the project's existing convention.

## Decision

**Superpowers is the default end-to-end methodology spine**, encoded as `§8` of
the injected operating directives (`src/mindhive/directives.py`, mirrored in the
operating-spec doc) so it reaches every session. Specifics:

1. **Spine, auto-engaged:** brainstorming (gate) → writing-plans →
   subagent-driven-development → TDD → verification-before-completion →
   requesting-code-review; with using-git-worktrees and
   dispatching-parallel-agents.
2. **Triviality bypass:** trivial mechanical edits skip the brainstorm→plan gate.
3. **Debugging:** systematic-debugging is default; ce-debug only for
   issue-tracker-sourced bugs.
4. **Execution envelope:** an approved plan in an isolated worktree is batched
   consent for autonomous in-scope execution; out-of-scope / unplanned-destructive
   / memory-network-deploy actions halt (reconciles superpowers' "don't pause"
   with operating-spec §4 "confirm before mutating").
5. **Close-out:** verify → requesting-code-review → thermo-nuclear review → then
   branch-finish/push/PR halts for confirmation.
6. **Carve-outs:** thermo-nuclear review is the maintainability gate;
   grill-with-docs sharpens plans against the glossary/ADRs. Other `/ce-*` and
   anthropic-skills are supplements, invoked only on explicit request — never
   auto-triggered alongside the spine.

## Consequences

- Claude reaches for one consistent toolchain by default; the overlapping stacks
  no longer compete non-deterministically for the same phase.
- The spine ships via the existing session-start directive injection, so it is in
  context every session without the user re-stating it — at the cost of a longer
  injected spec.
- The user's two genuinely-additive tools (thermo-nuclear review, grill-with-docs)
  are preserved as named carve-outs rather than being crowded out by superpowers'
  own review/brainstorm skills.
- The directive must be kept in sync across `directives.py` and the operating-spec
  doc (the module already requires this).

## Alternatives considered

- **Compound-engineering as primary:** rejected — inverts the user's already
  established `docs/superpowers/` usage and discards a coherent, already-in-use
  methodology.
- **Peers, pick per-task:** rejected — maximum flexibility but reintroduces the
  exact cross-session inconsistency this decision removes.
- **Pure superpowers, no carve-outs:** rejected — would drop the thermo-nuclear
  review (the project's standing maintainability gate) and grill-with-docs, both
  actively used and additive rather than redundant.
