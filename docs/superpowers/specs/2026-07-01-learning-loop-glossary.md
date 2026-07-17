# CONTEXT.md — Learning Loop (glossary)

**Purpose:** one definition per term, used consistently across the project. Pure domain language, no code.
**Last Updated:** 2026-07-01

---

## Failure
A tool call where the transcript `tool_result` block has `is_error == true` **and** `toolUseResult.interrupted != true`. Interrupted (user-cancelled) calls are NOT failures. This is the ONLY trigger for capture. **Not** defined by exit code (unreliable/absent for many tools).

## Signature
The normalized **root-cause key** a failure maps to — not its raw stderr. E.g. `pymodule:mindhive`, `pscmd:head`, `pysyntax:unterminated-string`. Recurrence, dedup, and rules all key off the signature. Two failures with the same signature are "the same mistake."

## Incident / Incident Record
The single Markdown file per signature (`rules/incidents/<signature>.md`) holding the Raw Signal + both Evaluations + the projected Active rule. The human-editable **workspace**; the event log is canonical truth.

## Raw Signal
The immutable captured facts of an incident: failed action, stderr excerpt, sample session ids, occurrence count. Never edited.

## Auto Evaluation
The **Sonnet 4.6**-generated section of an incident: root cause, lesson, drafted prevention rule. Machine-owned; not hand-edited.

## Human Evaluation
The section **hanif** fills in the same file. **Authoritative:** when non-empty it overrides Auto Evaluation as the source of the Active rule. Blank = accept Auto.

## Active Prevention Rule
The projected one-line rule text that is actually injected. Derived: Human Evaluation if present, else Auto. Tracked by `rule_source` = `auto | human | merged`.

## Status (lifecycle)
- **candidate** — signature seen but not yet at the recurrence threshold; not injected.
- **active** — reached N recurrences; injected pre-flight.
- **demoted** — pulled from injection (auto rule that leaked ≥2×, or human-demoted). Retained for audit.
- **promoted** — its text has been copied into canonical `SOUL.md` by human approval.

## Recurrence gate (N)
A signature must occur **N = 2** times (tunable) before its rule goes `active`. The "touch fire twice → flinch" filter; blocks unpreventable one-offs.

## Prevented / Leaked
Efficacy counters on an active rule. **Prevented:** the rule was injected and its signature did not recur that session. **Leaked:** it recurred despite injection. Drive the success metric and auto-demotion.

## Quarantine (machine-rule store)
The store the loop writes to **autonomously**: `rules/` (incidents, `RULES.md`, the `prevention_rules` ChromaDB collection). Isolated from canonical memory. Append-only, auditable.

## Canonical memory (`SOUL.md`)
Human-owned permanent directives (`directives.py` + `STATE.md` + LEDGER). The loop **never** writes here.

## Promotion
The one-click human-approved copy of a rule's text from Quarantine into canonical `SOUL.md`. The **only** human gate in the loop; the only path to canonical.

## Capture / Reflection / Injection (the three built stages)
- **Capture** — deterministic, no-LLM recording of a Failure from the transcript.
- **Reflection** — deferred Sonnet pass that writes the Auto Evaluation and drafts the rule.
- **Injection** — session_start surfacing of the active rule set alongside directives.

## Reconcile
The loop-tick step (`hive rules reconcile`) that re-reads Human Evaluation edits and appends `rule_revised` events, keeping the file surface and the event log in sync.
