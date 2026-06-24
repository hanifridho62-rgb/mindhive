# 1. Queue-and-inject the thermo-nuclear review instead of running it in the hook

Date: 2026-06-24
Status: Accepted

## Context

We want an automatic, strict maintainability review (the `thermo-nuclear-code-quality-review`
skill) to run over the code touched during a coding session, triggered "when this memory is
used" — i.e. off the `file_touched` event stream the hooks already capture.

The obstacle: a review is an **LLM reasoning task** — it must read changed files and produce a
maintainability judgement. But mindhive hooks are fire-and-forget PowerShell scripts that shell
out to `python -m mindhive …`; they run *code*, not a model. And by the time `session_end`
fires, the interactive Claude session that *could* reason is already closing. So a hook cannot
"run the review" itself.

## Decision

The `session_end` hook does only the cheap bookkeeping it is capable of: it filters the
session's `file_touched` events to first-party source/tests, splits them into ≤~1,500-line
batches, and writes a sidecar marker `~/.mindhive/pending_review.json`. The `projector` renders
a "⚠ Pending thermo-nuclear review" notice from that sidecar into `STATE.md`, which the existing
`session_start.ps1` already injects via `mindhive state`. The **next interactive session** reads
that notice and runs the skill in-context — where an LLM actually exists. Presumptive-blocker
findings are written back as `caveat` events; the sidecar is deleted once the review completes.

## Alternatives considered

- **Headless background review.** `session_end.ps1` spawns a detached `claude -p
  "/thermo-nuclear-code-quality-review <files>"` (the pattern used for `plugins autoscan`).
  Fully zero-touch, but spends Opus tokens unattended on every session end, and the output goes
  unread until someone opens the report file. Rejected: cost and unread-output for a review that
  is meant to be acted on.

- **A `review_due` event type** in the append-only ledger. Rejected: "review pending" is mutable,
  consumable state, not a historical fact; clearing it would require appending a second
  reconciling event and fighting the append-only model. A sidecar file is the honest shape for
  transient state and needs no change to the closed `VALID_TYPES` schema.

## Consequences

- The review lands at the **start of the next** session, not the end of the current one — a
  deliberate delay. If the code changes again before that session, batch findings can go stale;
  the review prompt must re-read files at run time, not trust the queued snapshot.
- The hook stays hang-proof and LLM-free, consistent with every other mindhive hook.
- Findings compound: repeat offenders (e.g. a file creeping toward 1,000 lines across sessions)
  surface in the briefing trend rather than being re-discovered cold each time.
