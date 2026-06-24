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

- **Scope = first-party source + tests** (grill Q4, 2026-06-24). Queue only
  `src\mindhive\**\*.py` and `tests\**\*.py`; exclude generated (`*.pyc`, `*.egg-info`),
  `docs\`, `build\`, `dist\`, and the memory store. Dedup; skip queuing when the filtered
  list is empty.

- **Large scope => staged batches** (grill Q4 follow-up, 2026-06-24). When the queued file
  set is large, the review runs in sequential batches rather than one oversized pass.

- **Batch sizing = by total lines** (grill Q5, 2026-06-24). Trigger batching when the filtered
  set exceeds ~1,500 total lines; pack whole files into batches of ≤~1,500 lines each; never
  split a file across batches. All batches run sequentially in the next session
  (user-interruptible), not one-batch-per-session.

- **Marker = sidecar JSON, surfaced through hive memory** (grill Q6, 2026-06-24).
  Source of truth is a sidecar `~/.mindhive/pending_review.json`
  (`{files, batches, created_at, session_id}`) — written by `session_end.ps1`, deleted when
  the review completes. No change to the closed `VALID_TYPES` event schema. For Claude to read
  it as context with zero new wiring, `projector` renders a "⚠ Pending thermo-nuclear review"
  notice from the sidecar into `STATE.md`, which `session_start.ps1` already injects via
  `mindhive state`. The `file_touched` history stays in the event log; only the transient
  marker is a sidecar.

- **Skill artifact = project-level, Claude Code format** (grill Q7, 2026-06-24). Lives at
  `.claude/skills/thermo-nuclear-code-quality-review/SKILL.md` in the mindhive repo, versioned
  alongside the hooks that invoke it. Cursor frontmatter is rewritten to Claude Code
  `name`/`description` form (description is a real trigger string, not disabled-invocation);
  the seven standards become the skill body.

- **Verdict = advisory, findings compound into memory** (grill Q8, 2026-06-24). Standards stay
  strict (seven non-negotiables, 1,000-line flag, code-judo hunting) but output is ranked
  advisory findings, not a blocking gate — a post-hoc review has nothing to block. Only
  presumptive-blocker-level findings are written back as `caveat` events
  (`mindhive append caveat …`) so they flow into `BRIEFING.md`, the timeline, and the graph,
  and resurface next session; lower-severity findings are shown in-session only, to avoid
  flooding the timeline.
