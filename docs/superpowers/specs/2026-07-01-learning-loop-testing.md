# Learning Loop — Testing Strategy (Phase 3)

**Date:** 2026-07-01. Defines what proves the system works before implementation.

## Tier 1 — Capability tests (does each stage do its job?)
1. **Capture detects a real failure:** feed a transcript line with `tool_result.is_error=true` + `ModuleNotFoundError: 'mindhive'` → a `tool_failed` event is recorded with signature `pymodule:mindhive`.
2. **Capture ignores non-failures:** `is_error=false` line, and an `interrupted=true` line → **no** event recorded.
3. **Signature normalization:** stderr strings differing only in path/line-number map to the SAME signature; two genuinely different errors map to DIFFERENT signatures.
4. **Reflection produces schema-valid output:** given a captured failure, the Sonnet pass returns `{root_cause, lesson, prevention_rule, task_category}` — no prose, valid enum.
5. **Injection surfaces active rules:** with one active rule, session_start context contains the `# PREVENTION RULES` block with that rule's text.

## Tier 2 — Parity / authority tests
6. **Human overrides Auto:** fill Human Evaluation → `reconcile` → Active Prevention Rule = human text; `rule_source=human`.
7. **Blank Human = accept Auto:** empty Human section → Active rule = Auto text; `rule_source=auto`.
8. **Human rule not auto-demoted:** a human-sourced rule leaks 2× → status becomes `needs_review`, NOT `demoted`.
9. **Auto rule auto-demotes:** an auto-sourced rule leaks 2× → `demoted`, leaves injection.
10. **Promotion is gated:** no code path writes to `SOUL.md`/`directives` except `hive rules promote` after explicit approval. (Audit the write sites.)

## Tier 3 — Edge cases (the scenarios we surfaced)
11. **Recurrence gate:** 1st occurrence → `candidate`, not injected. 2nd → `active`, injected. 3rd → prevented-count increments if absent.
12. **Dedup:** the same signature captured across 14 sessions yields ONE incident record, `occurrences=14` — not 14 rules.
13. **Injection bloat guard:** >20 active rules → only top-N by occurrence injected, and the drop is logged (no silent truncation).
14. **Benign `is_error`:** a tool that sets `is_error` for "no matches" does not create a persisted rule (filtered by gate/allowlist).
15. **Store isolation:** a wrong Auto rule in Quarantine never appears in canonical `SOUL.md` without promotion.

## Tier 4 — Integration (end-to-end, on real data)
16. **Replay the corpus:** run capture over the existing 195 transcripts → expect incident records for `pymodule:mindhive/hive_pipeline/tools`, `pscmd:head`, `pysyntax:unterminated-string`, matching the counts already measured (13/14/10…).
17. **Efficacy loop:** simulate: activate the `pymodule:mindhive` rule → inject into a fresh session → confirm `prevented` increments when that session runs mindhive correctly; force a leak → confirm counter + demotion path.
18. **Success metric moves:** aggregate `prevented/(prevented+leaked)` computed and rising across a simulated multi-session run.

## What "done" looks like
Tiers 1–2 green = the mechanism is sound and safe. Tier 3 green = the edge behavior matches the spec. Tier 16 (corpus replay) reproducing the known recurrence counts = the strongest proof the system would have caught the real, historical mistakes.
