---
title: "feat: Prompt-quality triggers — deterministic skill directives for vague prompts"
date: 2026-07-14
type: feat
depth: standard
origin: user request (headless ce-plan run)
---

# feat: Prompt-quality triggers — deterministic skill directives for vague prompts

## Summary

Give MindHive a per-prompt quality gate: when the user's typed prompt is
underspecified — vague, ambiguous, target-less, or method-skipping — a
UserPromptSubmit hook injects deterministic "use this skill" directives so the
agent front-loads the proven method (planning, debugging, review) instead of
improvising on a weak instruction. The trigger rules are **permanent** (a
sidecar manifest, injected on every matching prompt) and **trained from the
ledger**: the winning skill per quality-gap dimension is chosen from the live
skills/plugins inventory ranked by measured completion telemetry
(`plugins.skill_stats`) with the same learning-gated provenance rules the
recommend engine already enforces.

## Problem Frame

- The user's settings already register `hooks/user_prompt_submit.ps1`, but the
  script exists nowhere — the UserPromptSubmit hook is a dead registration
  today. Nothing reacts to prompt quality.
- The briefing's SUGGESTED SKILLS block is session-scoped and generic; it
  cannot react to the *current prompt* being vague.
- The ledger already measures which skills complete reliably (448 past
  prompts, 81 skill calls with outcomes), but nothing converts that history
  into per-prompt guidance.

## Requirements

- R1: Deterministic, dependency-free prompt assessment — named quality
  dimensions (too-short, vague-verb without target, no concrete target,
  dangling reference, ambiguity markers, method-skip), pure functions, no
  network/LLM calls.
- R2: Trigger rules are *trained* from the past ledger: skill choice per
  dimension is ranked from the live inventory using measured completion
  telemetry (recommend-engine tie-break: measured beats estimated, higher rate
  wins), and the manifest records how often each dimension actually occurred
  in past `user_prompt` history (observed counts) plus full provenance.
- R3: Rules are permanent: stored in a `rules/triggers.json` sidecar
  (enforce.json precedent), applied by the hook on every prompt until
  retrained; retraining is idempotent and auditable (one `trigger_trained`
  ledger event per train).
- R4: A `hive hook user_prompt_submit` stage that reads the hook payload from
  stdin and prints a UserPromptSubmit `additionalContext` JSON only when the
  prompt fails the gate AND matching rules exist. Silent (no output) on clear
  prompts. Fail-open: any error means no injection, never a blocked prompt.
- R5: CLI surface: `hive triggers train | list | check "<prompt>"`.
- R6: A hang-proof `hooks/user_prompt_submit.ps1` thin pipe (pre_tool_use.ps1
  pattern, MINDHIVE_HEADLESS guard), plus a fire-and-forget stale-gated train
  in session_start.ps1 so rules stay fresh without slowing prompts.
- R7: Directive text mirrors the learning-gated wording: a measured skill at or
  above the auto gate is "MUST USE"; anything else is "TRY & MEASURE" phrasing
  (never presenting an estimate as proven).
- R8: Suite green before PR; merged to master to finish.

## Key Technical Decisions

- **New module `src/mindhive/promptgate.py`**, mirroring `enforce.py` shape:
  pure predicates, manifest load/save with atomic write, `hook_context()`
  fail-open entry point. Keeps promptgate importable without the plugins stack
  at hook time (manifest read + regex assessment only — cheap).
- **Dimensions are a curated constant** (CURATED_NOTES precedent) mapping each
  quality gap to remediation-need keywords; the *skill* filling each dimension
  is not hardcoded — training scores every inventory skill against the
  dimension's need text via `recommend.score_skill` and picks by the existing
  `_winner_key` chain. Data decides, not the author.
- **Sidecar, not ledger, for the manifest** (plugin_catalog precedent):
  derived, retrainable data must not bloat the append-only chain. One small
  `trigger_trained` event (new DETERMINISTIC_TYPES member) records each train
  for audit; DETERMINISTIC_TYPES are outside LEDGER_TYPES so no chain impact.
- **Hook output contract**: `{"hookSpecificOutput": {"hookEventName":
  "UserPromptSubmit", "additionalContext": ...}}` — additive context only; we
  never emit `decision: block`.
- **Noise guards**: assessment skips machine-generated prompts (leading `<`,
  slash-commands, injected skill bodies) and only fires on ≥1 hard finding
  (method-skip, too-short) or ≥2 soft findings; training filters the same
  noise out of historical prompts.

## Implementation Units

### U1. promptgate module: assessment + training + hook context

**Goal:** The deterministic core — assess a prompt, train the manifest from
inventory + ledger, and produce the hook's additionalContext payload.
**Requirements:** R1, R2, R3, R4, R7
**Files:** `src/mindhive/promptgate.py`, `src/mindhive/events.py` (add
`trigger_trained` to DETERMINISTIC_TYPES), `tests/test_promptgate.py`
**Approach:** `DIMENSIONS` constant (id → {label, need text, hard/soft});
`assess(prompt)` → `{skip|clear|findings}`; `train(...)` pure over
(inventory, stats, evs) + `train_and_store(project)` wrapper; manifest I/O in
`rules/triggers.json`; `hook_context(raw, project)` parses payload, assesses,
loads manifest, renders directives.
**Test scenarios:** vague prompt fires expected dimensions; detailed prompt
(paths + acceptance criteria) is clear; machine/slash prompts skipped;
method-skip is a hard single-finding trigger; training picks the
measured-better skill over an estimated one; observed counts computed from
filtered historical prompts; manifest round-trips atomically; hook_context is
silent on clear prompts, silent on empty manifest, emits well-formed
additionalContext JSON with MUST USE vs TRY & MEASURE wording per provenance;
malformed payload/garbage manifest → `{}` (fail-open).

### U2. CLI wiring: hook stage + triggers subcommands

**Goal:** `hive hook user_prompt_submit` and `hive triggers train|list|check`.
**Requirements:** R4, R5
**Dependencies:** U1
**Files:** `src/mindhive/__main__.py`, `tests/test_rules_cli.py` (or a new
`tests/test_triggers_cli.py`)
**Approach:** extend `cmd_hook` stage choices; new `cmd_triggers_*` handlers
bound via set_defaults, lazy imports, `--project` flags per convention.
**Test scenarios:** hook stage prints JSON only on a firing prompt; train
writes manifest + appends the audit event; list renders rules with provenance;
check classifies a sample prompt.

### U3. Hook scripts

**Goal:** Fill the dead `user_prompt_submit.ps1` registration; keep rules
fresh via session-start background training.
**Requirements:** R6
**Dependencies:** U2
**Files:** `hooks/user_prompt_submit.ps1`, `hooks/session_start.ps1`
**Approach:** copy pre_tool_use.ps1 shape (headless guard, stdin pipe, print
stdout only when non-empty, exit 0 always); session_start gains one detached
`hive triggers train --max-age-hours 24` Start-Process line next to the
autoscan one.
**Test expectation:** none — PowerShell thin pipes with no logic beyond the
tested Python entry points (existing hooks carry no tests either).

### U4. Ship

**Goal:** Suite green, PR opened, CI green, merged.
**Requirements:** R8
**Dependencies:** U1–U3
**Approach:** run pytest with `PYTHONPATH=<worktree>/src` (editable-install
caveat), ce-code-review before PR, ce-commit-push-pr flow, watch CI, merge.
Known caveat: the harness may refuse self-merge of own PRs to master — if so,
report the blocker plainly.

## Scope Boundaries

**In scope:** the promptgate module, CLI, hook scripts, tests, docs touch-ups
inside those files.
**Out of scope / deferred to follow-up:** dashboard surface for trigger rules;
live capture of `user_prompt` events at hook time; per-dimension
enable/disable UX; LLM-based prompt rewriting.

## Risks

- False-positive nagging on terse-but-clear prompts → conservative thresholds,
  hard/soft split, machine-prompt skip; `hive triggers check` gives a fast
  offline probe.
- Hook latency → hook path reads one small JSON + regex pass; training never
  runs inline with a prompt.
- Live checkout activation only happens post-merge (settings already point at
  the script path) — explicitly the desired rollout.
