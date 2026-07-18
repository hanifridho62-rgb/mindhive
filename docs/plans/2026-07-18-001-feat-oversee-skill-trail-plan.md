---
title: "feat: Oversee per-session skill trail (which skills, when, why)"
type: feat
date: 2026-07-18
---

# feat: Oversee per-session skill trail (which skills, when, why)

**Target repo:** `mindhive` (worktree branch `claude/oversee-skill-tracking-822fc8`; all paths repo-relative)

---

## Summary

Each interactive-session card in the dashboard's **Oversee** section gains a
**skill trail**: the skills the session invoked, when each fired, whether it
succeeded, and *why* it was used — where "why" is two deterministic ledger
facts, never a synthesized motive: (1) the `args` text handed to the Skill
tool at invocation (captured new, at both capture paths), and (2) the user
prompt the invocation was serving (the most recent `user_prompt` headline
preceding it in the session). A skills pill on the card shows the count at a
glance; expanding the card lists the trail.

## Problem Frame

The ledger already records every skill execution (`skill_used` from the live
PostToolUse hook and the transcript harvest; `invoke_result` / `tool_failed
skill:*` from the explicit invoke channel), and the Skills panel aggregates
per-skill completion telemetry — but nothing answers the *session-shaped*
question: "in this work session, which skills fired, when, and in service of
what?" Oversee is exactly the per-session live board, so the trail belongs on
its cards. Today the `skill_used` payload (`{name, plugin?, ok, text}`)
carries the *result* excerpt but not the *ask*, so the "why" must be captured
at invocation time going forward.

## Assumptions

Headless-planning inferences (autonomous session; each revisable
independently):

- **"Why" = recorded fact, not narrative.** The honest, event-sourced answer
  is the skill's invocation args plus the prompt it served. A free-text LLM
  explanation of motive would be fabrication; the standing recommendation
  reasons ("measured 13/13 completion…") describe why a skill is *suggested*,
  not why *this* invocation happened, and stay in the Skills panel.
- **Scope = interactive `chat` cards** (oversee.py). Loop-run cards keep their
  existing shape; loop skill telemetry already flows to the Skills panel.
- **Oversee stays a live board.** The trail is visible while the session is on
  the board (Oversee removes ended/stale sessions by design); durable
  per-skill history remains in the Skills panel's call history.
- **`skill_used` only feeds the trail.** Explicit `invoke_result` /
  `tool_failed skill:*` records power the Skills panel, where plugins.py
  already owns passive-vs-explicit dedup; re-implementing that pairing inside
  Oversee would double-count or drift. Dashboard-invoked runs log under the
  dashboard/manual session, not an interactive session's stream.

---

## Requirements

- **R1** A chat session card carries a `skills` list: for each invocation —
  skill name (+plugin when namespaced), event timestamp, ok/failed, and the
  why fields below.
- **R2** "Why" per entry: `why` = the Skill-tool `args` text when captured;
  `during` = headline of the most recent preceding `user_prompt` in the
  session (empty when none preceded).
- **R3** The `args` text is captured into the `skill_used` payload at BOTH
  capture paths (live hook + transcript harvest), truncated by a shared cap,
  as an optional key — absent when the tool call had no args (payloads stay
  backward/forward compatible; reads stay total).
- **R4** Using a skill counts as session activity (freshness/online), and the
  displayed trail is capped like the other card groups, with the full count
  still reported (`skills_total`).
- **R5** The dashboard renders the trail on chat cards: a count pill always,
  rows on expand — name, HH:MM time, failed marker when `ok` is false, and
  the why line(s); everything HTML-escaped.
- **R6** No new event type; no ledger migration; existing events without
  `args` still render (why falls back to `during` only).

## Key Technical Decisions

- **KTD1 — capture the ask at the source.** `hookcapture._skill_payload`
  gains optional `args` (from `tool_input.args`, capped at a new shared
  `SKILL_ARGS_MAX`); `harvest._skill_from_tool_use` mirrors it (same constant,
  same optional-key shape) so live capture and back-harvest agree — the
  existing CMD_MAX/DESC_MAX sharing idiom. The slash-command harvest path has
  no args by nature and is unchanged. Harvest's `skill_used` dedup keys on the
  skill NAME, so the added key cannot split dedup identities.
- **KTD2 — "during" is a read-time join, not a schema change.** The oversee
  fold already walks events chronologically; it tracks the latest
  `user_prompt` headline and stamps it on each subsequent skill entry. No new
  payload field needed for it.
- **KTD3 — projection shape rides the existing card dict.** `interactive_sessions`
  adds `skills` (capped, newest-last like other groups) + `skills_total`;
  `/api/oversee` passes session dicts through untouched, so no server-handler
  change. Loop cards simply lack the keys; the JS treats absent as empty.
- **KTD4 — render in the established card idiom** (`ovRow`/`ovGroup`-style
  snips, `pill` spans, `esc()` everywhere), keeping the dashboard
  zero-dependency stdlib + inline JS.

## High-Level Technical Design

```mermaid
flowchart LR
    A[Skill tool fires\n(name + args)] --> B[PostToolUse hook\nhookcapture._skill_payload]
    A -.transcript.-> C[harvest._skill_from_tool_use]
    B --> D[(skill_used event\n{name, plugin?, ok, text, args?})]
    C --> D
    E[user_prompt event] --> F[oversee.interactive_sessions fold\nlatest prompt headline = "during"]
    D --> F
    F --> G[/api/oversee\nsession dict + skills, skills_total/]
    G --> H[ovCard: skills pill\n+ expanded trail rows]
```

---

## Implementation Units

### U1. Capture the invocation args in `skill_used`

**Goal:** Both capture paths record the Skill tool's `args` so future events
carry the ask.
**Requirements:** R3, R6.
**Dependencies:** none.
**Files:** `src/mindhive/hookcapture.py`, `src/mindhive/harvest.py`,
`tests/test_hookcapture.py`, `tests/test_harvest.py` (or the repo's existing
test homes for those modules).
**Approach:** add `SKILL_ARGS_MAX` beside the existing shared caps in
hookcapture; `_skill_payload` includes `"args"` only when `tool_input.args`
is a non-empty string (stripped, capped). Harvest's `_skill_from_tool_use`
does the same from the tool_use block's input. `text` stays byte-identical to
today's values.
**Test scenarios:**
- Skill PostToolUse with `args` → payload has capped `args`; `name/plugin/ok/text` unchanged.
- Skill PostToolUse without `args` (or non-string) → no `args` key.
- Args longer than the cap → truncated to exactly the cap.
- Harvest tool_use block with `args` → same optional-key shape; slash-command
  harvest payload has no `args` key.

### U2. Oversee projection: per-session skill trail

**Goal:** `interactive_sessions` returns `skills` + `skills_total` per chat
session, with `during` joined from the prompt stream.
**Requirements:** R1, R2, R4, R6.
**Dependencies:** U1 (for `args`-bearing events; must also work without).
**Files:** `src/mindhive/oversee.py`, `tests/test_oversee.py`.
**Approach:** fold tracks the current prompt headline; on `skill_used` append
`{name, plugin, ts, ok, why, during}` (`why` = headline of `args` or `""`;
`plugin` may be `""`). Add `skill_used` to `_ACTIVITY_TYPES`. At output time
cap the list to the trailing entries (own small cap constant, `_DONE_CAP`-
sized) and set `skills_total` to the uncapped count.
**Test scenarios:**
- Two skills after a prompt → both entries in order with ts, ok, `during` =
  that prompt's headline; second prompt shifts `during` for later skills.
- `args` present → `why` carries it; absent → `why` empty, `during` still set;
  skill before any prompt → `during` empty.
- Failed skill (`ok: false`) survives with `ok: False`.
- More entries than the cap → list capped to trailing entries,
  `skills_total` = full count.
- A session whose only recent event is `skill_used` stays fresh (not aged
  out; counts for online).
- Malformed payload (non-dict / missing name) is skipped, never raises.

### U3. Dashboard UI: skills pill + expanded trail

**Goal:** Oversee chat cards show the trail.
**Requirements:** R5.
**Dependencies:** U2.
**Files:** `src/mindhive/dashboard_page.py`, `tests/test_dashboard.py` (only
if it asserts on oversee card fields; expected: no server change — KTD3).
**Approach:** in `ovCard`: for chat cards with `skills_total > 0`, add a
`N skill(s)` pill; in the expanded body render one row per entry — glyph,
`HH:MM` (from the ISO ts), `plugin:name`, a `failed` marker when `!ok`, and a
dimmed why line combining `why` and `during: …` when present. All dynamic
strings through `esc()`/`escAttr()`. Include a stable feature hook (e.g. a
`ovskill` class) greppable in the served page.
**Test scenarios:** Test expectation: none — inline-JS template; behavior is
covered by U2's projection tests plus the served-page grep in U4 (existing
dashboard JS has no JS-level test harness).

### U4. Docs, verification, change-log

**Goal:** Contract documented, suite green, real flow exercised, mandatory log
recorded.
**Requirements:** R6 (docs); repo SOPs (change-audit).
**Dependencies:** U1–U3.
**Files:** `docs/DATA_MODEL.md`.
**Approach:** note the optional `args` key where `skill_used` is listed. Run
the full pytest suite (worktree rule: `PYTHONPATH=<worktree>/src` for
subprocesses). Serve THIS worktree's dashboard on a side port (never the live
54461 process) and grep the served page for the new feature hook and for the
existing `showOversee`/`renderOversee` hooks. Record
`hive log update "…"` with explicit `--files`.
**Test scenarios:** Test expectation: none — docs + process unit; the suite
run and served-page grep ARE the verification.

---

## Scope Boundaries

**In scope:** interactive chat cards' skill trail; args capture; docs; tests.

**Out of scope (true non-goals):** synthesized/LLM "reason" text; reworking
the Skills panel; per-skill trail on loop-run cards; retroactive backfill of
`args` into historical events (append-only ledger — old entries render with
`during` only).

### Deferred to Follow-Up Work

- **Directive provenance:** append a small event when promptgate injects a
  MUST USE/TRY directive, so a trail entry can say "used because the prompt
  gate directed it (measured N/M)". Needs a new event type → DATA_MODEL §6
  extension procedure; separate change-set.
- **Post-session trail:** surface an ended session's trail (e.g. in Sessions
  or Log view) once Oversee's live-board lifecycle removes the card.

## Risks

- **Payload drift between capture paths** — mitigated by sharing the cap
  constant and mirroring tests on both paths (KTD1).
- **Card bloat on skill-heavy sessions** — mitigated by the cap +
  `skills_total` (R4).
- **Old events lack `args`** — by design; `during` still gives context (R6).

## Verification

Full suite green from this worktree; served side-port page contains the new
`ovskill` hook and the pre-existing Oversee hooks; a synthetic session's
ledger events produce the expected trail via `interactive_sessions` (unit
tests); change-log entry recorded.
