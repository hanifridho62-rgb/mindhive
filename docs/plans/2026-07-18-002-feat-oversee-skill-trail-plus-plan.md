---
title: "feat: Oversee skill trail — richer why, durable audit, UI polish"
type: feat
date: 2026-07-18
---

# feat: Oversee skill trail — richer why, durable audit, UI polish

**Target repo:** `mindhive` (worktree branch `claude/oversee-skill-tracking-822fc8`; all paths repo-relative)

---

## Summary

Three improvements to the just-merged Oversee per-session skill trail (PR #67),
all confirmed by the user:

1. **Richer "why"** — a trail row is often bare today because `why` = the Skill
   tool's `args`, which many invocations omit. Make it always meaningful: (a)
   fall back to the skill's own **intended-use one-liner** (honest "what this
   skill is for"), and (b) add a real **causal** signal — when the prompt-quality
   gate directed a skill and it was then invoked, the row says "directed by
   prompt-gate", backed by a new `skill_directed` ledger event.
2. **Durable audit trail** — the trail lives only on the live Oversee card and
   vanishes when a session ends/ages out. Add `hive skills trail` (per session /
   all sessions / JSON) plus a read API, both over one shared fold, so past
   sessions' skill usage survives the board and an agent has terminal parity.
3. **UI polish** — a compact "distinct skills ×N" summary line above the rows,
   relative time next to HH:MM, and skill names that open their Skills-panel
   detail.

## Problem Frame

The merged feature answers "which skills, when, ok/failed" per live session, but
falls short of the user's original goal — *"explain why they were used so I can
neatly track what skills came out of the session."* Two gaps: the **why** is
frequently empty (no args) and never causal (the gate that pushed a skill leaves
no trace), and the record is **ephemeral** (Oversee removes ended/stale
sessions by design, so there is nothing to review afterward). Both are fixable on
the existing append-only ledger + deterministic-projection design.

## Assumptions

- **Honesty rule (repo directive) governs "why".** `why` is recorded fact or a
  clearly-labelled skill-purpose — never a synthesized motive. The intended-use
  fallback is labelled as purpose, not as the reason this call happened.
- **A read-time manifest heuristic is not honest enough for causation.** The
  trigger manifest says a skill *would* be directed for a dimension, not that
  *this* invocation *was* — so the causal claim requires a real event, not a
  join against `rules/triggers.json`.
- **Ended-session surfacing = CLI + API, not a new dashboard view.** The live
  board keeps its lifecycle; durability is delivered through the terminal and a
  JSON endpoint (a dedicated dashboard "past sessions" view is deferred).

---

## Requirements

- **R1** When `args` is absent/empty, `why` falls back to the invoked skill's
  intended-use one-liner, labelled as the skill's purpose (not its motive).
- **R2** A `skill_directed` event is appended when the prompt-gate fires with
  trained rules, recording the directed skill name(s), plugin, measured stat,
  and session; the write is best-effort and never breaks the hook (fail-open).
- **R3** The Oversee fold stamps a trail entry "directed by prompt-gate" when a
  `skill_used` matches a preceding same-session `skill_directed` for that skill;
  otherwise it uses the R1 why. Reads stay total on old/malformed rows.
- **R4** One shared fold produces per-session skill trails for **all** sessions
  (live and ended), reused by the live projection, the CLI, and the API.
- **R5** `hive skills trail [--session <id>] [--all] [--json] [--project P]`
  prints per-session trails (name, time, ok, why) from the ledger; `--json`
  emits machine-readable output. Default scope: recent sessions.
- **R6** A read API returns the same trail data (agent-native parity with the
  dashboard and CLI).
- **R7** UI: a compact distinct-skills-with-×N summary line above the detailed
  rows; relative time ("3m ago") beside HH:MM; each skill name opens its
  Skills-panel detail. All dynamic strings HTML-escaped.
- **R8** No ledger migration; events predating this change still render. Suite
  stays green; change-log entry recorded.

## Key Technical Decisions

- **KTD1 — new `skill_directed` deterministic event, not a read-time join.**
  Registered in `events.py` `DETERMINISTIC_TYPES`; payload
  `{skills: [{name, plugin?, measured?}], dimensions: [...], session}`. Written
  from `__main__.cmd_hook` (or `promptgate`) at the `user_prompt_submit` stage
  when `promptgate.hook_context` fires — the one place that already knows the
  directed skills. Best-effort/try-except so the gate can never break a prompt
  (mirrors `promptgate.train_and_store`'s audit-append guard). Follows
  DATA_MODEL §6 extension procedure. Deterministic telemetry → not consolidated,
  not in the Memory view.
- **KTD2 — one shared skill-fold in `oversee.py`.** Factor the per-entry builder
  and a `skill_trails(evs)` → `{session_id: [entries]}` function that folds
  `skill_used` (+ `skill_directed` join, + prompt `during` join) with no
  live-board lifecycle filtering. `interactive_sessions` calls the same per-entry
  builder so live and durable views can never disagree (the KTD from PR #67:
  one rule, many surfaces).
- **KTD3 — intended-use fallback via `plugins` inventory, read once.** Build a
  `{skill_name: intended_use}` map from `plugins.live_inventory()` /
  `skill_detail`'s `description`; pass it into the fold so `why` is filled at
  projection time. Machine-level lookup (not per-project), consistent with how
  the recommend/promptgate paths read the inventory.
- **KTD4 — CLI + API reuse the fold; dashboard link reuses `showSkillDetail`.**
  No new projection logic in three places; the polish link calls the existing
  `showSkillDetail(name, plugin)`.

## High-Level Technical Design

```mermaid
flowchart TD
    subgraph capture
      G[prompt-gate fires\nuser_prompt_submit hook] -->|best-effort append| D[(skill_directed event)]
      S[Skill tool fires] --> U[(skill_used event\n+ optional args)]
    end
    subgraph fold [oversee.skill_trails — one shared fold]
      D --> F[per-session entries]
      U --> F
      P[user_prompt headlines] -->|during join| F
      I[plugins intended-use map] -->|why fallback| F
    end
    F --> L[interactive_sessions\nlive cards]
    F --> C[hive skills trail\nCLI]
    F --> A[/api/skills/trail\nJSON]
    L --> UI[dashboard: summary line,\nrelative time, detail links]
```

Why precedence in the fold (first match wins): `args` headline → "directed by
prompt-gate (measured N/M)" → intended-use one-liner → empty. `during` (the
prompt served) is always attached when present, independent of `why`.

---

## Implementation Units

### U1. `skill_directed` event + gate write

**Goal:** Record, honestly and durably, that the prompt-gate directed a skill.
**Requirements:** R2, R8.
**Dependencies:** none.
**Files:** `src/mindhive/events.py`, `src/mindhive/__main__.py` (or
`src/mindhive/promptgate.py`), `docs/DATA_MODEL.md`, `tests/test_promptgate.py`
(or the hook's existing test home), `tests/test_events.py` if type-registration
is asserted there.
**Approach:** register `skill_directed` in `DETERMINISTIC_TYPES`. In the
`user_prompt_submit` hook path, when `promptgate.hook_context` returns non-empty
context, derive the directed skills from the fired manifest rules and append one
`skill_directed` event (session from `MINDHIVE_SESSION`, same as `skill_used`).
Wrap the append in try/except — a logging/write failure must not affect the
injected context. Do not write when the gate is clear/skipped.
**Patterns to follow:** `promptgate.train_and_store`'s best-effort audit append;
`cmd_skills_record`'s `append_event` shape.
**Test scenarios:**
- Gate fires with trained rules → one `skill_directed` appended carrying the
  directed skill name(s) + session; context output unchanged.
- Gate clear/skipped → no event.
- `append_event` raises (monkeypatched) → hook still returns/print s context
  (fail-open), no exception escapes.
- Payload lists every firing dimension's skill, deduped by name.

### U2. Shared skill-fold with richer why

**Goal:** One fold builds per-session trails with the causal + fallback why.
**Requirements:** R1, R3, R4, R8.
**Dependencies:** U1.
**Files:** `src/mindhive/oversee.py`, `tests/test_oversee.py`.
**Approach:** extract the current inline `skill_used` handling into a per-entry
builder and add `skill_trails(project=None, evs=None, intended_use=None)` →
`{sid: [entries]}` with no lifecycle filtering. Track, per session, the directed
skill names seen so far (from `skill_directed`) and the latest prompt headline;
on `skill_used`, set `why` by precedence (args → directed → intended-use →
empty) and attach `during`. `interactive_sessions` reuses the per-entry builder
so live cards get the richer why too. Fold stays total (skip non-dict / nameless
payloads).
**Patterns to follow:** existing chronological fold in `interactive_sessions`;
`plugins._skill_calls` for reading `skill_used`.
**Test scenarios:**
- `args` present → why = args headline (unchanged from today).
- No args, but a preceding `skill_directed` for that skill same session → why =
  "directed by prompt-gate" (+ measured stat when present).
- No args, no direction, intended-use map has the skill → why = intended-use
  one-liner; map missing the skill → why empty.
- `skill_directed` in a *different* session does not stamp this session.
- `during` still attaches independently of which why branch fired.
- `skill_trails` includes ended/stale sessions (no lifecycle filter), unlike
  `interactive_sessions`.
- Malformed `skill_directed` / `skill_used` payloads skipped, never raise.

### U3. `hive skills trail` CLI + read API

**Goal:** Durable, agent-native access to the trail beyond the live board.
**Requirements:** R5, R6, R4.
**Dependencies:** U2.
**Files:** `src/mindhive/__main__.py`, `src/mindhive/dashboard.py`,
`tests/test_cli.py` (or the CLI's existing test home), `tests/test_dashboard.py`.
**Approach:** add `cmd_skills_trail` under the existing `skills` subparser with
`--session`, `--all`, `--json`, `--project`; render per-session trails from
`oversee.skill_trails`, newest first, default to recent sessions. Add a
`/api/skills/trail` GET handler returning the same structure (HIVE-MIND
multi-namespace merge like the other skills endpoints).
**Patterns to follow:** `cmd_skills_stats` / `cmd_skills_recommend` (flags,
`--json`); `_get_skills_stats` / `_get_skills_history` handlers and their routing
table entry.
**Test scenarios:**
- CLI default prints recent sessions' trails; `--session X` filters to one.
- `--json` emits parseable structured output matching the fold.
- Empty ledger → friendly "(no skill activity)" message, exit 0.
- API returns the same entries the CLI shows for a given project.
- `--all` (HIVE MIND) merges namespaces.

### U4. Dashboard polish

**Goal:** Faster-to-scan trail on the live cards.
**Requirements:** R7.
**Dependencies:** U2.
**Files:** `src/mindhive/dashboard_page.py`, `tests/test_dashboard.py` (served-
page hook assertion only).
**Approach:** in `ovSkills`, prepend a compact distinct-skills summary
(`name ×N` when repeated); in `ovSkillRow`, add relative time ("3m ago") next to
the HH:MM using the client clock vs the UTC `ts`; wrap the skill name so a click
calls `showSkillDetail(name, plugin)`. Keep every dynamic string through
`esc()`/`escAttr()`; grep-able hook stays `ovskill`.
**Test scenarios:** Test expectation: none — inline-JS template; covered by U2's
projection tests plus the served-page grep in U5.

### U5. Docs, verification, change-log

**Goal:** Contract documented, suite green, real flow exercised, mandatory log.
**Requirements:** R8.
**Dependencies:** U1–U4.
**Files:** `docs/DATA_MODEL.md`.
**Approach:** document `skill_directed` in the taxonomy. Run the full suite
(`PYTHONPATH=<worktree>/src`). Serve this worktree's dashboard on a side port
(never live 54461), grep the served page for `ovskill` + `showOversee`; exercise
`hive skills trail` against a synthetic ledger and confirm the causal why and the
intended-use fallback appear. `hive log update "…" --files …`.
**Test scenarios:** Test expectation: none — docs + process; the suite run,
served-page grep, and CLI output ARE the verification.

---

## Scope Boundaries

**In scope:** the three confirmed improvements, their tests, docs, change-log.

**Out of scope (true non-goals):** LLM-synthesized motive text; reworking the
Skills panel or recommend engine; per-skill trail on loop-run cards; retroactive
backfill of `args`/direction into historical events.

### Deferred to Follow-Up Work

- **Dedicated dashboard "past sessions" trail view.** The CLI + API deliver
  durability now; a first-class ended-sessions UI is a larger, separate change.
- **Directive→invocation latency / "ignored directives" analytics** (gate
  directed X but it was never invoked) — a reporting feature on top of
  `skill_directed`.

## Risks

- **Write side-effect on a currently-pure hook (U1).** Mitigated by fail-open
  try/except; no behavior change when the gate is clear.
- **Causal over-claim.** A skill directed *and* independently invoked for another
  reason would still read "directed by prompt-gate." Acceptable: the direction
  and the invocation both happened; the label claims correlation-in-session, not
  sole cause. Kept honest by wording ("directed by prompt-gate", not "because").
- **Intended-use map cost.** Built once per projection call from the local
  inventory; negligible, same source the recommend path already reads.

## Verification

Full suite green from the worktree; served side-port page carries `ovskill` +
`showOversee`/`renderOversee`; `hive skills trail` on a synthetic ledger shows a
causal "directed by prompt-gate" row and an intended-use fallback row; change-log
entry recorded.
