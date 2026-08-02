---
title: "fix: caveman style continuity — slash-command turns and multi-turn lessons"
date: 2026-08-02
type: fix
depth: standard
origin: none (continuation of PR #87)
---

# fix: caveman style continuity — slash-command turns and multi-turn lessons

## Summary

PR #87 made caveman the standing reply style: `directives.py` §9 establishes it at
SessionStart, and `style.extend_hook_context` re-asserts it on every human-typed
prompt. Two turn shapes fall outside "every" — and both were confirmed by running
the real hook, not inferred:

| Turn | Expected | Actual |
|------|----------|--------|
| `/lfg lanjutkan` | caveman | **no style block at all** |
| `/circle` | caveman | **no style block at all** |
| `ajarkan saya cara kerja event store` | teaching | teaching ✓ |
| → follow-up `lanjutkan` | teaching (lesson continues) | **caveman** |
| → follow-up `terus, bagian projector` | teaching | **caveman** |

The second row group is the damaging one. The user's requirement is a *thorough*
lesson — "menjelaskan dan mengajarkan kepada saya dengan **menyeluruh**" — and a
thorough lesson is never one turn. Today the lesson reverts to terse fragments on
the first follow-up, which is precisely the moment the explanation is being
extended.

## Problem Frame

Both gaps trace to one design property: `style.block_for` is **pure and
memoryless**, deciding from the current prompt text alone.

- **Slash commands.** `block_for` delegates to `promptgate.is_machine_prompt`,
  which answers True for any prompt starting with `/`. That is correct for the
  *prompt-quality gate* — there is no point grading the phrasing of `/circle`. It
  is wrong for *style*: the user typed that slash command and is owed a reply in
  the standing style. Borrowing the gate's predicate imported the gate's intent
  along with its mechanics.
- **Multi-turn lessons.** With no memory of the previous turn, a continuation
  prompt (`lanjutkan`, `terus`, `continue`, `go on`) carries no teach verb, so the
  matcher correctly reports "not a teaching request" and the mode collapses.

## Requirements

- **R1** — A slash-command turn receives the standing style; genuinely
  machine-injected text (`<…>` tags, injected skill bodies) still receives none.
- **R2** — Teaching mode survives follow-up turns that continue the same lesson,
  in English and Indonesian.
- **R3** — Teaching mode ends on its own when the user moves on; it must not
  latch for the rest of the session.
- **R4** — The pass stays fail-open and additive. Any error leaves the prompt
  untouched, exactly as today.
- **R5** — No ledger reads or writes on the prompt critical path. This runs on
  every turn.

## Key Technical Decisions

**KTD1 — Style gets its own machine-text predicate; `is_machine_prompt` is left
alone.** Adding a `for_style=` flag to the gate's predicate would couple two
subsystems that happen to agree on three of four cases. `style.py` defines its own
narrow `_is_injected` (angle-bracket tags, `Base directory for this skill:`
envelopes, empty text) and deliberately does **not** treat a leading `/` as
machine. The gate keeps its predicate and its tests unchanged.

**KTD2 — Sticky teaching mode via a per-session sidecar, not the ledger.** Mode is
derived, ephemeral, per-session state. The ledger is append-only and hash-chained;
writing a row per prompt would bloat the chain forever with unprunable noise. This
follows the precedent already set by `plugins._catalog_path` and
`mindnet/sidecar.py`: overwrite-in-place JSON under `paths.project_dir()`,
written with `paths.write_text_atomic`.

**KTD3 — Three-way transition, so the mode expires by itself.** Per turn:

| Prompt shape | Transition |
|---|---|
| teach/explain verb | → `teach` (sticky) |
| continuation-shaped and mode is already `teach` | stay `teach` |
| anything else | → `caveman`, clear the entry |

R3 falls out of the third row: any substantive new question ends the lesson. No
timeout heuristic, no explicit exit command to remember.

**KTD4 — Continuation detection is shape-based and short.** A continuation prompt
is *short* (few content words) and made of continuation markers — `lanjutkan`,
`lanjut`, `terus`, `teruskan`, `berikutnya`, `selanjutnya`, `continue`, `go on`,
`next`, `and then`, `more`. The length bound is what stops a substantive prompt
that merely contains "next" from extending a stale lesson.

## Assumptions

Recorded as assumptions rather than decisions — pipeline mode had no synchronous
user to confirm them with.

- **A1** — Sticky teaching is what "menyeluruh" implies. If the user actually
  wants per-turn teaching, KTD3's first row alone would suffice and the sidecar
  could be dropped.
- **A2** — Session scope is the right key. Sidecar entries are keyed by
  `session_id` from the hook payload; a missing id degrades to non-sticky
  behaviour rather than sharing one global mode across sessions.
- **A3** — Slash commands are user turns for style purposes. The competing read —
  that a slash command's output is skill-shaped and should not be compressed — is
  not what "setiap saat" asks for.

## Scope Boundaries

**In scope:** `src/mindhive/style.py`, its tests, and the skill/spec/directives
prose that documents the behaviour.

**Out of scope (true non-goals):**
- `promptgate.is_machine_prompt` and the prompt-quality gate's own semantics.
- Whether the *model* honours the injected block. Advisory context is advisory.

### Deferred to Follow-Up Work
- Sidecar pruning for long-lived stores. Entries are tiny and overwrite in place;
  revisit only if the file is observed growing.
- Persisting the `/caveman <level>` intensity choice. Independent of this fix.

---

## Implementation Units

### U1. Style stops borrowing the gate's machine-text predicate

**Goal:** A slash-command turn gets the standing style; true machine injections
still get nothing.

**Requirements:** R1, R4

**Dependencies:** none

**Files:**
- `src/mindhive/style.py` (modify)
- `tests/test_style.py` (modify)

**Approach:** Add a module-local `_is_injected(text)` to `style.py` covering empty
or whitespace-only text, a leading `<`, and the `Base directory for this skill:`
prefix. Point `block_for` and `wants_teaching` at it instead of
`promptgate.is_machine_prompt`. Leave the `PROMPT_SCAN_CAP` import in place. Carry
a comment stating why the two predicates are deliberately near-duplicates —
without it, a later reader will "helpfully" re-unify them and silently reintroduce
this gap.

**Patterns to follow:** `promptgate.is_machine_prompt` for shape; keep the same
cheap, pure, no-disk contract.

**Test scenarios:**
- `/caveman ultra`, `/circle`, `/lfg lanjutkan` → `CAVEMAN_BLOCK` (regression
  cases; each returns `""` today).
- `/explain the parser` → `TEACH_BLOCK` — a slash command can itself be a
  teaching request.
- `<task-notification>done</task-notification>`, `<system-reminder>x</…>` → `""`.
- `Base directory for this skill: /x/y` → `""`.
- `""` and `"   "` → `""`.
- `promptgate.is_machine_prompt("/circle")` still returns True — the gate's
  predicate is untouched.

**Verification:** Running the real hook with `{"prompt":"/circle"}` emits a
context block containing the caveman directive; with a `<task-notification>`
payload it stays silent.

---

### U2. Teaching mode persists across a multi-turn lesson

**Goal:** A lesson that spans several turns stays in prose; a new question ends it.

**Requirements:** R2, R3, R4, R5

**Dependencies:** U1

**Files:**
- `src/mindhive/style.py` (modify)
- `tests/test_style.py` (modify)

**Approach:** Add `_CONTINUATION_RES` (EN + ID markers) and an `is_continuation`
helper gated on a short content-word count, mirroring how
`promptgate._content_words` measures substance. Add a small sidecar module-level
pair — `_mode_path(project)` → `paths.project_dir(project) / "style_mode.json"`,
plus read/write helpers using `paths.write_text_atomic`. Introduce
`resolve_mode(text, session_id, project=None) -> str` implementing KTD3's
three-way transition, and have `extend_hook_context` pass
`payload.get("session_id")` through to it. A missing or unreadable sidecar, or a
missing `session_id`, degrades to today's memoryless behaviour — never an error.

**Execution note:** Write the sticky-transition tests first. The bug being fixed
is a *sequence* bug, and a single-prompt test cannot express it; the failing test
must drive three prompts through one session id before any sidecar code exists.

**Technical design** *(directional, not implementation specification)*:

```
resolve_mode(text, sid):
    if teach_verb(text):        store[sid] = teach   ; return teach
    if continuation(text) and store[sid] == teach:   return teach
    clear store[sid]                                 ; return caveman
```

**Patterns to follow:** `plugins._catalog_path` / `_write_catalog` for the
overwrite-in-place sidecar; `mindnet/sidecar.py` for fail-open decode.

**Test scenarios:**
- Sequence, one session: `ajarkan saya X` → teach; `lanjutkan` → teach;
  `terus, bagian projector` → teach. (Fails today at turn 2.)
- Same in English: `teach me X` → teach; `continue` → teach; `go on` → teach.
- Lesson ends: `ajarkan saya X` → teach; then `perbaiki bug di dashboard` →
  caveman, and the sidecar entry is gone.
- No latching: after the lesson ends, a bare `lanjutkan` → caveman.
- Session isolation: session `a` in teach mode does not put session `b` in teach.
- Cold start: `lanjutkan` as the very first prompt of a session → caveman.
- Length bound: a long substantive prompt containing "next" → caveman, not a
  continuation.
- Missing `session_id` → behaves exactly as the memoryless implementation does.
- Unreadable/corrupt sidecar → caveman, no raise.
- No ledger read: `resolve_mode` performs no `events.read_all` (R5).

**Verification:** Driving the real hook three times with the same `session_id`
(`ajarkan…`, `lanjutkan`, `terus…`) yields the teaching block all three times; a
fourth substantive prompt returns the caveman block.

---

### U3. Documentation catches up with the behaviour

**Goal:** The skill, directives, and spec describe sticky teaching and
slash-command coverage, so the next reader is not misled by PR #87's per-turn
wording.

**Requirements:** R2, R3

**Dependencies:** U1, U2

**Files:**
- `skills/caveman/SKILL.md` (modify — MindHive addendum only, upstream body
  untouched)
- `src/mindhive/directives.py` (modify — §9)
- `docs/superpowers/specs/2026-06-23-mindhive-assistant-operating-spec.md` (modify — §9)
- `tests/test_caveman_skill.py` (modify)

**Approach:** PR #87 says teaching mode is "THIS turn only" and directives §9 says
it "resumes on the next turn that is not a teaching request". Both become false
with U2. Replace with the sticky rule and its exit condition. Add the
slash-command clarification. Keep every edit below the `## MindHive addendum`
heading so the upstream-verbatim split holds.

**Test scenarios:**
- Skill addendum states teaching persists across follow-up turns and names the
  exit condition.
- Directives §9 no longer contains the "next turn" per-turn wording.
- Existing provenance and upstream-split tests still pass.

**Verification:** `pytest tests/test_caveman_skill.py` green; the phrase "THIS
turn only" no longer appears in any of the three documents.

---

## Risks

| Risk | Mitigation |
|---|---|
| Sticky mode latches and the user is stuck getting essays | KTD3 clears on any non-continuation prompt; explicit "no latching" test |
| Continuation matcher over-fires on substantive prompts containing "next"/"more" | Length bound on content words; explicit long-prompt test |
| Sidecar write on every turn adds I/O to the critical path | Single small atomic JSON write, same cost class as the existing catalog sidecar; no ledger access (R5) |
| A future reader re-unifies the two machine-text predicates | Comment in `style.py` stating the reason; regression test asserting `/circle` gets a style block |

## Verification Strategy

1. `pytest -q` fully green (3377 today; U1–U3 add cases).
2. Drive the **real hook** for both fixed shapes — slash command, and a
   three-turn lesson sharing one `session_id` — against this worktree's `src`,
   not the editable install, which resolves to the main checkout.
3. Confirm the untouched behaviours from PR #87 still hold: `review ini secara
   menyeluruh` stays caveman, `<task-notification>` stays silent.
