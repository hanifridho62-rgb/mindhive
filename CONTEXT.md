# CONTEXT — mindhive

A glossary of the project's domain language. Not a spec. Not a scratchpad.

## Glossary

### Thermo-nuclear review
The `thermo-nuclear-code-quality-review` skill (adapted from Cursor's team-kit): a strict
maintainability reviewer enforcing seven standards — structural simplification, a 1,000-line
file cap, spaghetti prevention, design-over-acceptance, direct-over-magic, type cleanliness,
and canonical layering. Produces a blocking-style review with presumptive blockers.

### Memory
A markdown file under the user's memory store (`…/memory/*.md`) with frontmatter
(`node_type: memory`, `type: project|user|feedback|reference`). Distinct from an **event**.

### Event
An append-only signal captured by hooks via `python -m mindhive append <type> <data>`.
Examples: `file_touched`, `command_run`, `session_start`. This is the "memory history"
the hooks record during coding.

## Decisions captured here

- **Deliverable = both a skill artifact and an auto-triggering hook** (grill Q1, 2026-06-24).
  Author the adapted `thermo-nuclear-code-quality-review` skill first, then wire a hook that
  auto-invokes it. The skill stays manually invokable; the hook fires it automatically.

- **Trigger = session-end batch** (grill Q2, 2026-06-24). The review is keyed off the
  session's `file_touched` event stream — not per-edit, not on a recall match.

- **Execution = queue-and-inject** (grill Q3, 2026-06-24 — ADR candidate). `session_end.ps1`
  records a lightweight "review due" marker + touched-file list (cheap, no LLM). The next
  session's `session_start.ps1` injects the pending-review notice so interactive Claude runs
  the skill in-context. A fire-and-forget hook cannot reason, so the review happens where an
  LLM actually exists: the next interactive session.
