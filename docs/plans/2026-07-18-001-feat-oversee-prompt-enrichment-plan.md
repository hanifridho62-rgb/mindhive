---
title: "feat: Oversee prompt enrichment (default-on, toggleable)"
type: feat
date: 2026-07-18
status: planned
---

# feat: Oversee prompt enrichment (default-on, toggleable)

## Summary

Add a **Prompt enrichment** feature to the dashboard's Oversee section: paste a
draft prompt, get back a deterministic enrichment package — clarifying
questions to answer, likely source files (from the ledger's own record of the
project's code plus an optional bounded repo scan), skill directives from the
trained triggers manifest, an Anthropic prompt-engineering SOP compliance
checklist, and a composed, copy-ready enriched prompt. The feature is
**enabled by default** and toggleable per project; when enabled it also
extends the existing UserPromptSubmit promptgate injection with the clarifying
questions. Everything is deterministic (no model calls), mirroring
`promptgate.py` / `roadmap.py` idioms.

## Problem Frame

The hive already *gates* weak prompts (promptgate injects skill directives
when a prompt is underspecified) but offers no place to *improve* a prompt
before spending a session on it. The user wants an Oversee-resident tool that
enriches context three ways — (1) asking the user, (2) checking the source
code, (3) injecting skills — and composes a prompt that complies with the
Anthropic prompt-engineering SOP (project SOP 5: specification-shaped prompts
with scope, constraints, and verification).

## Requirements

- R1: Enrichment via clarifying questions derived from the prompt's measured
  quality gaps (user answers fold into the composed prompt).
- R2: Enrichment via source code: candidate target files from the ledger's
  `file_touched` history ranked by keyword overlap/recency, plus a bounded
  filename scan of the project's registered folder (fail-open).
- R3: Enrichment via skills: directives from the trained triggers manifest +
  inventory suggestions ranked by measured completion telemetry (never present
  an unproven skill as proven — same provenance rules as promptgate).
- R4: Anthropic-SOP compliance checklist (concrete target, sufficient detail,
  unambiguous intent, clear antecedents, method intact, verification stated)
  and a composed enriched prompt that is specification-shaped.
- R5: Default-ON, per-project toggle; persisted; audited in the ledger; when
  ON the promptgate hook injection is extended with clarifying questions.
- R6: Ledger evidence: `prompt_enriched` / `enrich_toggled` deterministic
  events, registered per DATA_MODEL.md §6.

## Key Technical Decisions

- **Deterministic engine, no LLM**: mirrors promptgate/roadmap — pure
  functions over the prompt text, ledger events, and manifest. Testable,
  fail-open everywhere.
- **Toggle sidecar** `<project_dir>/enrich.json` (atomic write via
  `paths.write_text_atomic`), missing file ⇒ enabled (default-on). The toggle
  is user config, not derived data, but follows the triggers.json sidecar
  precedent; each change appends an `enrich_toggled` audit event.
- **Source candidates from the ledger first** (`file_touched` fold — the
  project's real code surface), then a bounded, pruned filesystem scan of the
  autosync-registered folder. Both fail-open; scan capped.
- **Skills from the existing learning pipeline**: firing-dimension directives
  reuse the trained manifest (promptgate rules); general suggestions rank the
  live inventory with `recommend.need_match` + `plugins.skill_stats`
  provenance gating.
- **Hook extension is additive and cheap**: only when the gate already fired,
  only reads the sidecar (no ledger read on the prompt path), appends
  question lines to the existing additionalContext. Fail-open.
- **New event types are DETERMINISTIC_TYPES** (telemetry, like
  `trigger_trained`): exempt from consolidation by construction, no
  cmd_append contract needed (they are appended by the module, not by users).

## Implementation Units

### U1. `src/mindhive/enrich.py` — deterministic enrichment engine
Goal: settings (default-on toggle + audit), clarifying questions, source
candidates, SOP checklist, skill suggestions, composed prompt, `enrich()`
aggregate, `record()`, `extend_hook_context()`.
Files: `src/mindhive/enrich.py`, `tests/test_enrich.py`.
Test scenarios: default-on when sidecar missing/corrupt; toggle persists and
appends audit event; questions per dimension + verification question; source
ranking (keyword overlap beats recency; recency breaks ties; non-matching
excluded); scan honors prune dirs and cap; checklist met/unmet per criterion;
compose contains Task/Context/Clarifications/Skills/Definition-of-done and
folds answers; enrich() deterministic for fixed inputs; record caps text and
appends `prompt_enriched`; extend_hook_context appends questions when
enabled, returns context unchanged when disabled, fail-open on garbage.

### U2. `src/mindhive/events.py` — register event types
Goal: `prompt_enriched`, `enrich_toggled` in DETERMINISTIC_TYPES with
why-comments (DATA_MODEL.md §6 step 1).
Files: `src/mindhive/events.py`, covered by `tests/test_enrich.py`.

### U3. `src/mindhive/__main__.py` — CLI + hook wiring
Goal: `hive enrich status|on|off|preview` subcommands; user_prompt_submit
hook stage extends promptgate context via `enrich.extend_hook_context`.
Files: `src/mindhive/__main__.py`, `tests/test_enrich.py`.
Test scenarios: status prints enabled; on/off round-trip; preview prints
composed prompt (and `--json` full result); hook stdout gains question lines
when enabled, unchanged when disabled.

### U4. `src/mindhive/dashboard.py` — API endpoints
Goal: GET `/api/enrich/settings`; POST `/api/enrich/toggle`,
`/api/enrich/preview` (optional `record`); route table entries. HIVE MIND
(`*`): settings falls back to default project; mutating routes stay behind
the existing read-only guard.
Files: `src/mindhive/dashboard.py`, `tests/test_dashboard.py`.
Test scenarios: settings default true; toggle off → settings false + audit
event in ledger; preview returns questions/sources/skills/sop/
enriched_prompt; preview with record appends `prompt_enriched`; toggle under
`*` rejected read-only; endpoints require token.

### U5. `src/mindhive/dashboard_page.py` — Oversee panel
Goal: "Prompt enrichment" panel at the top of Oversee: on/off toggle pill,
textarea, Enrich button, results (SOP checklist, questions with answer
inputs, source chips, skill directives, composed prompt + copy). State
survives the 2.5 s live refresh (module Sets/vars idiom); refresh skips
repaint while the user is typing in the panel; hidden under HIVE MIND `*`.
Files: `src/mindhive/dashboard_page.py`, `tests/test_dashboard.py`
(served-page hook greps: `renderEnrich`, `/api/enrich`).

### U6. Docs + change-log
Goal: DATA_MODEL.md event-type note; `hive log update "…"` entry; full suite
green (worktree PYTHONPATH discipline).
Files: `docs/DATA_MODEL.md`.

## Scope Boundaries

- No LLM-powered rewriting — enrichment is deterministic composition.
- No change to promptgate's gating thresholds or manifest format.
- The toggle governs enrichment only; promptgate's own injection stays
  independent (disabling enrichment never disables the quality gate).
- Deferred: semantic (vector) source matching; enrichment history view;
  per-dimension toggle granularity.

## Verification

Full pytest suite green from the worktree (`PYTHONPATH=<worktree>/src`);
served dashboard page greps for the new panel hooks; CLI round-trip
(`hive enrich off` → `status` → `on`); change-log entry recorded.
