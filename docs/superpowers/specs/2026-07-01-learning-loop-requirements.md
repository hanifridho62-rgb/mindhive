# Learning Loop — Requirements Document

**Status:** Phase 1 complete (ULT-IDEA). Awaiting user approval before Phase 2 (architecture).
**Date:** 2026-07-01
**Author:** Claude (Opus 4.8) with hanif

---

## Summary

An autonomous **capture → reflect → store → inject** loop that stops Claude (Opus 4.8, in Claude Code) from repeating its own recurring, root-caused mistakes across stateless sessions. Built as an **extension to MIND HIVE**, not a new platform.

## Problem Statement

Claude Code sessions are stateless. When Opus hits an execution error, fixes it, and the session ends, the fix is forgotten — so the *same class of error* is rediscovered in the next session. This is measured, not assumed. A grep across 195 real session transcripts found the same failures recurring:

- `ModuleNotFoundError: 'hive_pipeline'` ×14, `'mindhive'` ×13, `'tools'` ×10, `'ingest_hive'` ×9, `scripts.core.*` ×7 each, `mindhive.*` submodules ×4 each — **60+ recurrences with one dominant root cause:** running project code from the wrong working directory / without the venv / without `-m` from the repo root.
- `SyntaxError: unterminated string literal` (Windows here-string / quoting) ×2+, Unix-isms (`head`) and bare `hive` not on PATH in PowerShell.

These are few, stable, and enumerable — the ideal target for a prevention system: a handful of rules would eliminate the bulk of wasted cycles.

## User

**hanif** — solo operator running Opus 4.8 inside Claude Code on Windows 11 / PowerShell 5.1, across many projects (FABIO-v18 is the first tenant, but the system is general). The "user" whose behavior improves is **Claude itself**; hanif is the supervisor who approves promotions.

## Design

### Concept
Extend MIND HIVE's existing detect→store→inject spine with the two stages it lacks — **failure capture** and **reflection** — feeding a new, quarantined machine-rule store that is injected pre-flight.

### Flow
1. **Capture (Detection):** The `PostToolUse` hook (already live) is extended to read the tool *result* — exit code, stderr, and the command/action — and record a `command_failed` event. (Today it captures only the command string and discards success/failure.) No subprocess wrapper needed; Claude runs tools directly and the hook already receives the result.
2. **Reflect (Evaluator):** A separate Claude pass performs root-cause analysis on the captured failure and drafts a structured record: `failed_action`, `lesson_learned`, `prevention_rule`, `task_category`.
3. **Gate + Store:** Dedup by meaning (reuse `dedupe.py`). A candidate rule becomes **active only after its failure signature recurs ≥ N times** (N tunable; the recurrence count is the "touched fire twice" signal that filters one-off novel errors). Active rules are written **autonomously** to the **Quarantine Database** (machine-rule store: a new `RULES.md` + dedicated ChromaDB collection). Append-only, auditable, namespaced as machine-generated.
4. **Inject (Retrieval):** At session_start and/or pre-prompt, the top-K Quarantine rules matching the current intent (semantic match) are injected alongside the canonical directives.
5. **Feedback:** If a rule was injected but the same error still occurred, demote/decay it.

### Store split (the core safety invariant)
- **Canonical memory** (`SOUL.md` ≈ `directives.py` + `STATE.md`, LEDGER): human-owned, confirm-first. **The loop never writes here.**
- **Quarantine Database** (machine-rule store): the loop owns it, writes autonomously, rules active immediately.
- **Promotion** Quarantine → canonical `SOUL.md`: the **only** human gate — one-click approval by hanif.

This preserves the hands-off loop while honoring MIND HIVE's `OPERATING_SPEC` §4/§6 (append-only, confirm before mutating *canonical* memory, no auto-merge).

## Success Criteria

1. **Recurrence drops:** After a rule activates, its signature's recurrence in *new* sessions trends toward zero. Measured by re-running the same transcript grep, before vs. after.
2. **Signal stays high:** Active injected rule set stays small (target < ~20) — the recurrence gate + dedup keep noise out.
3. **No canonical contamination:** `SOUL.md`/directives change only via approved promotions; audit trail proves the loop never wrote there.
4. **Latency safe:** Capture and injection never block or slow a session (inherits MIND HIVE's hang-proof, fail-silent hook discipline).

## Out of Scope (v1)

- Async/socket-level failure capture (WebSocket price-feed timeouts, blueprint §II.1) — not a dev-loop error; defer.
- Auto-promotion into canonical `SOUL.md` — stays a manual one-click step, permanently by design.
- FABIO trading-logic rules (97%-reject, 30s incubation, circuit-breaker) — domain content, not dev-error prevention; separate concern.
- The `xiaomi/mimo-v2-pro` evaluator — replaced by Claude/Opus 4.8 (this environment's model).

## Dependencies

- MIND HIVE store, hooks, ChromaDB (`vectors.py`), `dedupe.py`, `directives.py`, `events.py` — all present.
- Claude Code `PostToolUse` payload must include tool result/stderr (to confirm in Phase 2).
- A promotion UI surface (the MIND HIVE dashboard already exists — candidate host for the one-click approval).

## Open Questions (resolve in Phase 2 / 2.5)

- Value of the recurrence threshold **N** (2? 3?) and how signature-equality is defined (exact stderr vs. normalized root cause).
- Where reflection runs: inline hook (risks latency), a scheduled batch pass, or the existing `hive loop`.
- Rule schema (blueprint's JSON schema, adapted) and its ChromaDB collection layout.
- Injection budget: top-K, token cap, and interaction with the existing Pensyve prompt-injection hook (avoid double-injection).
- Decay/demotion policy specifics.

## Assumptions

- Injection actually changes Opus's behavior (the load-bearing bet; success criterion #1 tests it directly).
- The dominant recurring errors remain environment/project-structural (wrong cwd/venv), which are rule-preventable.
- Windows/PowerShell 5.1 remains the runtime.

## Agent Considerations

The "agent" here *is* the consumer. Parity is trivial (no human UI to mirror) except for the **promotion approval**, which is deliberately human-gated. The loop must remain fail-silent and hang-proof per MIND HIVE hook discipline.
