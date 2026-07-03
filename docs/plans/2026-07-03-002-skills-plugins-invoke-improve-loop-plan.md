# Plan — Skills & Plugins: click-details, callable-via-MindHive, failure→improvement loop, circle+lfg loop

**Date:** 2026-07-03
**Branch:** feat/mindhive-orchestrator
**Author:** Claude (Fable 5 / Opus 4.8 session)
**Status:** in progress

## Origin

Extends the just-shipped live **Skills & plugins** view (registry-driven
`inventory` / `inventory_signature` / `live_inventory` in `plugins.py`, the
`/api/plugins/inventory` route, and the rebuilt dashboard view). User directive:
rich click-details, make skills/plugins *callable from MindHive*, turn call
outcomes green/red, feed failures into an improvement loop, integrate with the
`/mindhive` plugin, and fuse `circle` with `compound-engineering:lfg` into a
more detailed engineering loop.

## Confirmed decisions

1. **Invoke mechanism = headless `claude` CLI.** The dashboard shells out to
   `claude -p` per call (the pattern `reflect.py` already uses). Exit 0 → green;
   non-zero / `is_error` → red. Failures emit a `tool_failed` event so the
   existing `incidents.py` → `reflect.py` → Improvements pipeline learns from
   them. Rationale: real end-to-end invocation, self-contained, works with no
   agent already running. A Python process genuinely cannot call the Claude
   `Skill` tool, so this is the honest realization of "callable from MindHive".
2. **Phased delivery, TDD each phase, checkpoint at boundaries. No PR until it
   works.**

## Reality boundaries (stated plainly)

- The dashboard cannot invoke the in-process `Skill` tool. "Callable by
  MindHive" = the dashboard spawns a headless Claude that invokes the skill.
- Green/red reflects a *recorded call outcome*, not a live health probe.
- Headless invocation spawns a full agent per call — deliberately gated behind
  an explicit user click + a confirm, never auto-fired by the live poll.

## Phases

### P1 — Click-details (intended-use first, then date added, then full body)
- `plugins.skill_detail(kind, name, ...)` returns
  `{name, intended_use, added_at, body, source}`.
  - `intended_use` = SKILL.md `description` frontmatter (rendered at top).
  - `added_at` = registry `installedAt` for plugins; SKILL.md mtime for local.
  - `body` = full SKILL.md markdown minus frontmatter.
- Route `GET /api/skills/detail?kind=&name=&plugin=`.
- View: clicking a skill/plugin row expands a detail panel — intended use at the
  very top, then "added" timestamp, then the full description.

### P2 — Invoke channel (headless claude CLI)
- `invoker.py`: `invoke_skill(name, kind, runner=…)` builds a `use the <name>
  skill` prompt, calls an injectable runner (default = headless `claude`,
  reusing reflect's envelope parsing), returns `{ok, exit, excerpt, ms}`.
- Route `POST /api/skills/invoke` (token-gated, one-at-a-time lock like reflect).
- Records an `invoke_result` event on success; a `tool_failed` event (with a
  stable `skill:<name>` signature) on failure so incidents pick it up.

### P3 — Green/red status
- `plugins.call_status(project)` derives per-skill/plugin last-outcome from the
  `invoke_result` / `tool_failed` ledger → `{name: "ok"|"fail"|"unknown"}`.
- Route folds status into the inventory payload (or a sibling endpoint).
- View: status dot per row — green ok, red fail, grey never-called.

### P4 — Failure → improvement wiring
- Confirm `tool_failed` with `skill:<name>` signatures flow through
  `incidents.audit/project` and surface in `/api/improvements`. Add a
  `skill_invoke` category so reflect's prevention rules are scoped.
- View: a red row links to its Improvements lesson.

### P5 — `/mindhive` plugin integration
- `mindhive-orchestrator` (and `circle`) skills gain a documented contract:
  after selecting a skill they may drive it through `/api/skills/invoke`, and
  read `call_status` / Improvements to avoid known-bad skills.
- Every installed skill/plugin is reachable (name resolved from the live
  inventory), not a hardcoded subset.

### P6 — circle + lfg fused engineering loop
- New `skills/…/engineering-loop` (or extend `circle`): plan → work → review →
  fix → verify → learn, with MindHive recall/append between every stage and the
  P4 improvement loop closing the cycle. More detailed than lfg: adds explicit
  recall-preflight, per-stage failure capture, and a reflect pass on any red.

## Test strategy
Pure helpers unit-tested (detail parsing, status derivation, invoke result
shaping) with injected runners — never spawns a real `claude`. Routes tested via
the in-process dashboard server with monkeypatched invoker. Frontend hooks
asserted by page-substring tests, matching existing `test_dashboard.py` style.

## Out of scope / deferred
Real per-skill telemetry backend beyond last-outcome; auto-invocation on a
schedule; multi-call batching.
