# MIND HIVE — Context Glossary

This file is the canonical glossary of domain terms for MIND HIVE. It is **not**
a spec or a scratchpad — only resolved terms and their precise meanings live
here. Implementation decisions belong in `docs/adr/`.

## Terms

### Hive mind
The shared, cross-session memory: the union of every Claude session and project
that reads from and writes to **one** MIND HIVE event store. "The hive mind sees
X" means *any* future session, in any project namespace, can retrieve X. It does
**not** mean multiple independent agents coordinating in real time.

### Memory
A markdown file under the user's memory store (`…/memory/*.md`) with frontmatter
(`node_type: memory`, `type: project|user|feedback|reference`). The durable,
authored intent layer. Distinct from an **event**.

### Event
An append-only signal captured by hooks via `python -m mindhive append <type>
<data>` (e.g. `file_touched`, `command_run`, `session_start`) — the machine-
recorded history of a coding session, distinct from an authored **memory**. The
**memory graph** is a projection of these.

### Memory graph
The existing `graph.json` projection of the event store (built by
`src/mindhive/graph.py`). Nodes: `event`, `session`, `file`, `tag`, `plugin`,
`skill`, `topic`, `recommendation`. This is the **intent layer** — what the hive
decided, learned, and questioned.

### Code graph
*(new — being designed)* A graph of code entities (functions, classes, modules)
and their structural relationships (calls, imports, defines), extracted from the
project's source. Distinct from the memory graph, which is about intent.

### Merge (memory ⨝ code)
The goal of the current work: linking memory-graph nodes to the code-graph
entities they concern, so the hive mind retrieves a decision/caveat *attached to
the specific function or module it was about* — not just "some file was touched."

### Edge provenance (EXTRACTED / INFERRED / AMBIGUOUS)
A tag on every graph edge recording how it was derived. **EXTRACTED** =
deterministic (tree-sitter AST facts; event-derived links) — trustworthy as
ground truth. **INFERRED** = produced by Opus reasoning (conceptual links) —
soft. **AMBIGUOUS** = Opus flagged it uncertain. Retrieval can filter to
EXTRACTED-only for deterministic structural answers. See `docs/adr/0002`.

### Structural spine vs conceptual overlay
The **structural spine** is the EXTRACTED subgraph (AST + event facts) — the
deterministic, verifiable core. The **conceptual overlay** is the INFERRED
edges Opus adds on top. "Trust the spine, weigh the overlay."

### Thermo-nuclear review
The `thermo-nuclear-code-quality-review` skill (adapted from Cursor's team-kit):
a strict maintainability reviewer enforcing seven standards — structural
simplification, a 1,000-line file cap, spaghetti prevention, design-over-
acceptance, direct-over-magic, type cleanliness, and canonical layering. It runs
as a queued, session-end-triggered review surfaced in the next session. See
`docs/adr/0004-queue-and-inject-thermo-nuclear-review.md`.

## Thermo-nuclear review — captured decisions

> These are the operating parameters settled while designing the thermo-nuclear
> review. The headline execution decision (queue-and-inject) is ADR 0004; the
> rest are its parameters, retained here so they are not lost.

- **Deliverable = both a skill artifact and an auto-triggering hook** (grill Q1,
  2026-06-24). Author the adapted `thermo-nuclear-code-quality-review` skill
  first, then wire a hook that auto-invokes it. The skill stays manually
  invokable; the hook fires it automatically.

- **Trigger = session-end batch** (grill Q2, 2026-06-24). The review is keyed off
  the session's `file_touched` event stream — not per-edit, not on a recall match.

- **Execution = queue-and-inject** (grill Q3, 2026-06-24 — see ADR 0004).
  `session_end.ps1` records a lightweight "review due" marker + touched-file list
  (cheap, no LLM). The next session's `session_start.ps1` injects the
  pending-review notice so interactive Claude runs the skill in-context. A
  fire-and-forget hook cannot reason, so the review happens where an LLM actually
  exists: the next interactive session.

- **Scope = first-party source + tests** (grill Q4, 2026-06-24). Queue only
  `src\mindhive\**\*.py` and `tests\**\*.py`; exclude generated (`*.pyc`,
  `*.egg-info`), `docs\`, `build\`, `dist\`, and the memory store. Dedup; skip
  queuing when the filtered list is empty.

- **Large scope => staged batches** (grill Q4 follow-up, 2026-06-24). When the
  queued file set is large, the review runs in sequential batches rather than one
  oversized pass.

- **Batch sizing = by total lines** (grill Q5, 2026-06-24). Trigger batching when
  the filtered set exceeds ~1,500 total lines; pack whole files into batches of
  ≤~1,500 lines each; never split a file across batches. All batches run
  sequentially in the next session (user-interruptible), not one-batch-per-session.

- **Marker = sidecar JSON, surfaced through hive memory** (grill Q6, 2026-06-24).
  Source of truth is a sidecar `~/.mindhive/pending_review.json`
  (`{files, batches, created_at, session_id}`) — written by `session_end.ps1`,
  deleted when the review completes. No change to the closed `VALID_TYPES` event
  schema. The `projector` renders a "⚠ Pending thermo-nuclear review" notice from
  the sidecar into `STATE.md`, which `session_start.ps1` already injects via
  `mindhive state`.

- **Skill artifact = project-level, Claude Code format** (grill Q7, 2026-06-24).
  Lives at `.claude/skills/thermo-nuclear-code-quality-review/SKILL.md` in the
  mindhive repo, versioned alongside the hooks that invoke it. Cursor frontmatter
  is rewritten to Claude Code `name`/`description` form; the seven standards
  become the skill body.

- **Verdict = advisory, findings compound into memory** (grill Q8, 2026-06-24).
  Standards stay strict, but output is ranked advisory findings, not a blocking
  gate — a post-hoc review has nothing to block. Only presumptive-blocker-level
  findings are written back as `caveat` events so they flow into `BRIEFING.md`,
  the timeline, and the graph; lower-severity findings are shown in-session only.
