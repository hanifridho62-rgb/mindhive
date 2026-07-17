---
title: "feat: Active rule resolution — reroute red rules until they hold green"
date: 2026-07-13
type: feat
depth: standard
origin: user request (no brainstorm doc)
---

# feat: Active rule resolution — reroute red rules until they hold green

## Summary

Red (unresolved) lessons in the Improvements view are currently a dead end: once a
prevention rule leaks (the failure recurs despite the rule), the signature can
**never** classify green again, because leaks are counted forever from the first
`rule_activated` timestamp and the reflection machinery only ever targets
incidents with *no* rule yet. This plan adds an **active resolution pass** that
automatically filters the red set, generates and ranks *alternative prevention
routes* for leaking rules, deploys the best route, and re-anchors leak counting
at the reroute — so a rerouted rule that holds turns green (and then graduates
to SOUL.md via the existing `auto_promote`, making the resolution permanent).
The pass is wired into the existing session-end auto batch, the CLI, and the
Improvements dashboard view.

## Problem Frame

`incidents.classify_resolution` marks a lesson `unresolved` (red ✗) when either
(a) it is at threshold with `rule_source == none`, or (b) its rule has `leaks > 0`.
Three structural gaps keep red rules red:

1. **Leaks never reset.** `signal_stats` / `incidents.leaks` count every
   post-activation failure forever. Even a later, better rule can never prove
   itself — green is unreachable after one leak.
2. **Leaking rules are never re-reflected.** `reflect_batch` and the dashboard
   Reflect endpoint select only `status == active and rule_source == none`.
   A rule that failed is never replaced with an alternative route.
3. **Auto-demotion is a dead end.** At `LEAK_LIMIT` leaks an auto rule silently
   drops to `demoted` — out of injection, still red, never revisited.

"Force Claude to choose the best suggested route" = the chosen route becomes the
*only* injected rule text for that signature (nudge rung), flagged as a reroute
so the briefing marks it as mandatory; for signatures with an enforcement
matcher, the existing `enforce.auto_escalate` hard-force rung is triggered from
the same pass.

## Requirements

- R1: Automatically identify ("filter") all red/unresolved lessons per project.
- R2: For a red lesson with a failed (leaking) **auto** rule, generate several
  distinct alternative prevention routes, rank them, and deploy the best one.
  Human-sourced rules are never overwritten (human is authoritative, ADR 0007).
- R3: Re-anchor leak/exposure accounting at the reroute so a holding rerouted
  rule classifies `watching` → `resolved` (green) and can auto-promote to
  SOUL.md. Un-demotes an auto-demoted rule (it re-enters injection).
- R4: Red lessons with *no* rule keep using the existing reflect path from the
  same resolution pass (portion-automation: whatever can be automated, is).
- R5: The pass runs automatically (session-end batch), manually via CLI
  (`hive rules resolve`), and from the Improvements view (Resolve button +
  per-card reroute visibility).
- R6: Bounded and safe: attempt cap per signature, cooldown shared with
  reflection, deterministic fallbacks only when they offer a *distinct* route,
  fail-open everywhere (a dead model CLI must not corrupt records).

## Key Technical Decisions

- **KTD1 — Reroute is an event, not a mutation.** A `rule_rerouted` event
  (`{signature, text, old_text, routes, attempt, source:"auto"}`) is the leak
  re-anchor. The event log stays canonical; the incident .md's Auto Evaluation
  is rewritten to carry the new route (same mechanism as `reflect_incident`).
  Add `rule_rerouted` to `incidents._LIFECYCLE_TYPES` so it doesn't count as
  session activity for exposure.
- **KTD2 — Anchor = max(first activation ts, latest reroute ts).** `signal_stats`
  and `incidents.leaks()` count leaks strictly after the anchor, excluding the
  anchor's own session (mirrors the existing activation-session exclusion —
  a session already in flight when the route deployed never saw it). Exposure is
  likewise measured post-anchor. `classify_resolution` needs no change: freshly
  rerouted → `watching` (untested), held + exposed → `resolved`.
- **KTD3 — Route generation asks for K ranked alternatives.** New prompt listing
  the failed rule text(s) and leak count, demanding JSON
  `{"routes": [{prevention_rule, rationale, confidence}]}` (K=3). Best route =
  highest confidence that is *distinct* from every previously failed rule text
  (normalized compare). Deterministic fallback (`fallback_verdict`) is eligible
  only when its rule is distinct from the failed one. No distinct route →
  no reroute (never fabricate).
- **KTD4 — Attempt cap.** `MAX_REROUTES = 3` per signature. Exhausted →
  frontmatter `flag: reroute_exhausted` (rendered as a pill; human takes over).
- **KTD5 — One shared batch.** `reflect_batch` target selection widens to red
  leaking auto rules (rerouted via the new module) alongside the existing
  no-rule reflect targets, under the same limit/cooldown/marker — the
  session-end hook needs no change. `enforce.auto_escalate` runs at the end of
  a successful pass (hard-force rung for matcher families).
- **KTD6 — Injection marks reroutes as mandatory.** `incidents.inject` prefixes
  rerouted rules with `(rerouted ×N — previous rule failed; follow this route
  exactly)` so the briefing "forces" the chosen route at the nudge rung.

## Implementation Units

### U1. Leak re-anchoring on `rule_rerouted` (incidents.py)

**Goal:** Green becomes reachable again after a reroute.
**Requirements:** R3.
**Dependencies:** none.
**Files:** `src/mindhive/incidents.py`, `tests/test_incidents.py`.
**Approach:** Add `_latest_reroute(project, signature, evs=None) -> (ts, session_id)`;
use it in `signal_stats` (anchor + anchor-session exclusion + exposure + new
`reroutes` / `last_reroute` fields) and in `leaks()` (used by `_status_and_flag`,
so auto-demotion recomputes to active post-reroute). Add `rule_rerouted` to
`_LIFECYCLE_TYPES`. Extend `inject()` with the KTD6 reroute prefix.
**Test scenarios:**
- Failures before a reroute don't count as leaks after it; a post-reroute
  failure from another session does.
- Exposure requires post-reroute activity from a session other than the
  rerouting one; freshly rerouted → `classify_resolution` == `watching`; after
  clean exposure → `resolved`.
- An auto rule with `leaks >= LEAK_LIMIT` (status demoted) returns to `active`
  after a reroute and reappears in `inject()` with the reroute prefix.
- `rule_rerouted` events do not create exposure by themselves.

### U2. Resolution module (resolve.py)

**Goal:** Filter red, generate + rank routes, deploy the best.
**Requirements:** R1, R2, R4, R6.
**Dependencies:** U1.
**Files:** `src/mindhive/resolve.py` (new), `tests/test_resolve.py` (new).
**Approach:** Pure helpers `build_route_prompt`, `parse_routes`, `choose_route`
(distinct-from-failed-rules + confidence ranking) mirroring reflect.py's
testable-core style. `red_summaries(project)` filters `unresolved` lessons.
`resolve_incident(signature, ...)`: no-rule → delegate to
`reflect.reflect_incident`; human-source or dismissed → skip; auto+leaking →
route generation (model runner, `fallback_verdict` as distinct-only fallback),
rewrite Auto Evaluation, append `rule_rerouted` with the full ranked routes
list, re-project. Failed-rule history = current active rule + texts from prior
`rule_rerouted.old_text` payloads. Attempt cap per KTD4 with
`flag: reroute_exhausted` surfaced through `_status_and_flag`.
`resolve_batch(project, limit, runner)` for the manual/endpoint path.
**Test scenarios:**
- `parse_routes` tolerates fences/prose; garbage → `[]`.
- `choose_route` rejects routes equal (normalized) to any failed rule; picks
  highest confidence among distinct; empty when none distinct.
- `resolve_incident` on a leaking auto rule writes the new rule, appends
  `rule_rerouted` (attempt increments), record stays `active`.
- Human-sourced rule → skipped, file untouched.
- Dead runner + curated family with distinct fallback → reroutes; identical
  fallback → no reroute, no event, no attempt burned.
- 4th attempt refused; flag `reroute_exhausted` set in frontmatter.
- `resolve_batch` touches only red lessons and respects the limit.

### U3. Auto-batch integration (reflect.py)

**Goal:** Red rules resolve automatically at session end (R5-auto).
**Requirements:** R4, R5, R6.
**Dependencies:** U2.
**Files:** `src/mindhive/reflect.py`, `tests/test_reflect.py`.
**Approach:** `reflect_batch` builds a combined target list: existing
no-rule reflect targets first, then red leaking-auto reroute targets, bounded
by the same `limit`; routes each through the right entry point; marker/cooldown
semantics unchanged (zero-work rollback still applies). After ≥1 success, call
`enforce.auto_escalate` (best-effort) alongside the existing `auto_promote`.
**Test scenarios:**
- A batch with one unreflected + one leaking incident (limit 2) reflects the
  first and reroutes the second with a stub runner.
- Reroute-only success still stamps `last_success` and refreshes projections.
- Cooldown and LIVE_LOCK behavior unchanged (existing tests keep passing).

### U4. CLI: `hive rules resolve` (__main__.py)

**Goal:** Manual, scriptable resolution pass (R5-CLI).
**Requirements:** R1, R5.
**Dependencies:** U2.
**Files:** `src/mindhive/__main__.py`, `tests/test_rules_cli.py`.
**Approach:** `rules resolve [--project] [--limit N] [--list]` — `--list` prints
the red set (JSON) without spending tokens; default runs `resolve_batch` and
prints its JSON result. Mirrors `cmd_rules_reflect` conventions.
**Test scenarios:**
- `--list` prints only unresolved signatures.
- Run with stubbed runner reroutes and reports counts.

### U5. Dashboard: Resolve endpoint + Improvements UI (dashboard.py, dashboard_page.py)

**Goal:** One-click "Resolve red" and per-card reroute visibility (R5-UI).
**Requirements:** R1, R2, R5.
**Dependencies:** U2.
**Files:** `src/mindhive/dashboard.py`, `src/mindhive/dashboard_page.py`,
`tests/test_dashboard.py`.
**Approach:** POST `/api/rules/resolve` under `_REFLECT_LOCK`, clamped limit,
runs `resolve_batch` for the scoped project; returns
`{ok, rerouted, reflected, failed, pending}` (`pending` = red remaining).
Improvements view: a "Resolve red (N)" button beside Reflect with the Heavy
confirm, banner result; lesson cards show a `rerouted ×N` pill and
`route deployed <ts>` snip when `signal_stats` reports reroutes;
`reroute_exhausted` flag renders as a pill. `_get_improvements` already merges
signal_stats — the new fields flow through.
**Test scenarios:**
- Endpoint reroutes a seeded leaking incident (stub runner) and reports counts;
  concurrent call returns "already running".
- Improvements payload carries `reroutes`/`last_reroute` for a rerouted lesson.

## Scope Boundaries

- **In:** auto rules only; nudge-rung forcing via injection wording; hard-force
  only through the existing matcher-based `auto_escalate`.
- **Out (non-goals):** generating new *enforcement matchers* automatically
  (arbitrary code as PreToolUse deny predicates — deliberately human-authored);
  rerouting human-sourced rules; changing the recurrence threshold or decay.
- **Deferred to follow-up:** model-generated matcher *suggestions* for humans
  to review; surfacing alternate (non-chosen) routes as clickable options in
  the Improvements card.

## Risks

- **Token spend loops** — mitigated by the shared cooldown, batch limit, attempt
  cap, and distinct-route requirement (a model that keeps proposing the same
  text burns no attempts).
- **A worse route replacing a better rule** — the old text is preserved in the
  event payload (auditable) and the human override channel (Human Evaluation
  section) always wins via `derive_active`.
- **Windows file races** — all incident writes stay inside the existing
  `_write_guard`.

## Verification

Full suite `python -m pytest` green; manual: seed a leaking incident, run
`hive rules resolve`, confirm the card turns `watching` with the new route,
then after a clean session turns `resolved` and promotes to SOUL.md.
