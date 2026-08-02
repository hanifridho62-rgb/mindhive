---
title: "fix: close the doc-vs-code drift in style.py and the test that should have caught it"
date: 2026-08-02
type: fix
depth: standard
origin: docs/plans/2026-08-02-001-fix-caveman-style-continuity-plan.md (review of its U1–U3)
---

# fix: close the doc-vs-code drift in style.py and the test that should have caught it

**Target branch:** `claude/caveman-repo-hiveminds-9236f1` (PR #87), worktree
`.claude/worktrees/os-architecture-recovery-d5ae51`. All paths below are
repo-relative.

## Summary

Review of the continuity work (origin plan U1–U3) found five issues. Three are
doc-vs-code contradictions inside `src/mindhive/style.py` — the one file whose
entire value is that its prose explains a subtle mechanism correctly. U1 made
slash-command turns get the style; U2 made teaching mode sticky. Two docstrings
in that same file still described the *pre-fix* behaviour, and `wants_teaching`'s
docstring literally described the bug U1 had just patched.

The fourth is a hot-path ordering and bounding miss in `resolve_mode` /
`is_continuation`. The fifth is a rare cross-session write race on the mode
sidecar, deferred here as a design change.

Planning grep surfaced a **sixth instance the review missed**: `commands/caveman.md:27`
still says teaching gets prose *"for that turn, then caveman resumes"*. It is the
same drift, in a file the anti-drift test also does not read. That widens finding
#3 from "the test skips `style.py`" to "the test reads rendered strings and docs
but not the sources and the command file", which is the actual class of hole.

| # | Sev | Where | Status in this plan |
|---|-----|-------|---------------------|
| 1 | P2 | `src/mindhive/style.py` module docstring | Fixed (already in working tree) — U2 |
| 2 | P2 | `src/mindhive/style.py` `wants_teaching` docstring | Fixed (already in working tree) — U2 |
| 3 | P2 | `tests/test_caveman_skill.py` drift test | Fixed and widened — U1 |
| 6 | P2 | `commands/caveman.md:27` (new, found while planning) | Fixed — U1 |
| 4 | P3 | `resolve_mode` / `is_continuation` hot path | Fixed — U3 |
| 5 | P3 | sidecar read-modify-write race | Documented only — U4 |

## Problem Frame

`test_the_per_turn_wording_is_gone_everywhere` was added by origin-plan U3
specifically to stop the phrase "this turn only" / "for that turn" surviving the
switch to sticky teaching mode. It reads four sources: `skills/caveman/SKILL.md`,
the operating spec, `directives.directives()`, and `style.TEACH_BLOCK`.

It does not read the Python source that implements the behaviour, and it does not
read `commands/caveman.md`. Both omissions let live instances through. A test
written to catch exactly one class of drift missed three instances of it.

The root cause is that the test's `sources` dict enumerates *rendered output and
prose docs*. The stale text that actually misleads the next editor lives in
**module docstrings** and in the **command file** — neither of which is rendered
into any string the test already inspects.

Finding #4 is unrelated to drift. In `resolve_mode`, the continuation scan runs
before the dict lookup that decides whether it can matter, and `is_continuation`
carries no `PROMPT_SCAN_CAP` bound while its two siblings
(`wants_teaching`, `promptgate.is_code_work`) both do.

## Requirements

- **R1** — No source, doc, or command file in the caveman surface claims teaching
  mode is per-turn.
- **R2** — The anti-drift test reads the module sources and the command file, not
  only rendered strings and prose docs, so this class of drift fails loudly.
- **R3** — The new assertion is discriminating: it must fail against the pre-fix
  text, not pass vacuously.
- **R4** — `is_continuation` is bounded by `PROMPT_SCAN_CAP` and does not run when
  its result cannot change the outcome.
- **R5** — Behaviour is unchanged for every case the existing `tests/test_style.py`
  suite pins. The style pass stays fail-open and stays off the ledger.
- **R6** — The sidecar race is recorded where the next editor will read it, with
  the reason it is not fixed here.

## Key Technical Decisions

**The drift test grows sources, not assertions.** The two assertions
(`"this turn only"`, `"for that turn"`) are right; only the input set was too
small. Adding `src/mindhive/style.py` source and `commands/caveman.md` to the
existing `sources` dict fixes the hole without inventing a second test. The guard
is deliberately literal — a future negated mention ("not just for that turn")
would trip it, and tripping is the correct outcome for a phrase this load-bearing.

**`is_continuation` bails early rather than truncating.** `wants_teaching`
truncates (`[:PROMPT_SCAN_CAP]`) because a teach verb can legitimately appear
early in a long prompt. A continuation is bounded at `CONTINUATION_MAX_WORDS = 6`
by definition, so a prompt over the cap can never be one. The early-bail idiom
from `promptgate.is_code_work` (`if len(t) > PROMPT_SCAN_CAP: return False`) is
both cheaper and the house pattern.

Accepted behaviour change: a ≤6-word prompt exceeding 2000 characters (one
pathological multi-kilobyte token after a marker) now answers False instead of
True. That resolves to caveman, which is the module's own stated safe failure —
"Forgetting the mode is always a safe failure".

**The reorder is free.** `modes` is already read one line above the call site, so
gating on `modes.get(key) == MODE_TEACH` first costs nothing and skips the scan on
every prompt outside a live lesson — the overwhelmingly common case.

**#5 is documented, not fixed.** Making the sidecar read-modify-write atomic needs
a lock or a compare-and-swap retry around `_read_modes`/`_write_modes`. That is a
design change beyond this review's scope, and the failure is rare and self-healing
(a lost entry drops one session to caveman, which the next teach verb restores).

## Scope Boundaries

**In scope:** findings #1–#4 and #6; a docstring record of #5.

### Deferred to Follow-Up Work

- **Sidecar write atomicity (#5).** Two sessions writing near-simultaneously can
  drop each other's entry via read-modify-write on a whole-file JSON sidecar.
  `paths.write_text_atomic` makes each individual write atomic, but the
  read → mutate → write sequence is not. Fixing it needs per-file locking or a
  retry-on-mismatch loop.

**Not in scope:** re-litigating the sticky-mode or slash-command designs; any
change to `promptgate`.

## Implementation Units

### U1. Widen the anti-drift test, and fix what it exposes

**Goal:** `test_the_per_turn_wording_is_gone_everywhere` covers the sources that
actually mislead editors; `commands/caveman.md` stops claiming per-turn teaching.

**Requirements:** R1, R2, R3

**Dependencies:** none

**Files:**
- `tests/test_caveman_skill.py` (modify — extend the `sources` dict)
- `commands/caveman.md` (modify — line 27 wording)

**Approach:** Add two entries to the existing `sources` dict: the raw text of
`src/mindhive/style.py` and of `commands/caveman.md`. Keep the read-and-normalize
shape already used. Update the test's leading comment so it states the widened
contract — sources and command file included, not just prose and rendered blocks.

Rewrite `commands/caveman.md:27` so it matches sticky behaviour: the lesson holds
across follow-ups and ends on the next substantive question. Reuse the vocabulary
already pinned by `test_addendum_documents_the_sticky_lesson` so the two files
agree.

**Execution note:** Prove the new assertion is discriminating before relying on
it — run it against `git show HEAD:src/mindhive/style.py`, which still contains
"for that turn only" at line 26. A pass there would mean the assertion is vacuous.

**Patterns to follow:** the existing `sources` dict and `_flat` normalization
helper in the same file; `test_addendum_documents_the_sticky_lesson` for the
sticky-mode vocabulary.

**Test scenarios:**
- Covers R2. With `src/mindhive/style.py` and `commands/caveman.md` in `sources`,
  the test passes against the current working tree.
- Covers R3. The same assertion applied to the pre-fix `style.py` blob (from
  `git show HEAD:`) fails on "for that turn only" — verified once at
  implementation time, not committed as a test.
- Covers R1. `commands/caveman.md` no longer matches either banned phrase, and
  still satisfies the existing `test_command_says_the_style_is_already_on` and
  `test_style_compresses_without_switching_language` assertions.

**Verification:** `tests/test_caveman_skill.py` passes in full; the pre-fix blob
check fails as expected.

### U2. Land the docstring corrections already in the working tree

**Goal:** the two `style.py` docstrings describe post-fix behaviour.

**Requirements:** R1

**Dependencies:** U1 (the test that pins them lands first)

**Files:**
- `src/mindhive/style.py` (modify — module docstring, `wants_teaching` docstring)

**Approach:** The corrections are already present as uncommitted working-tree
changes. Review both for accuracy rather than rewriting: the module docstring now
says the carve-out is sticky and names `resolve_mode` as what holds it open;
`wants_teaching` now says a slash command is not injected text and points at
`_is_injected`. Confirm each claim matches the code as written, then commit.

**Test scenarios:**
- Covers R1. The widened U1 test passes against the corrected source (this is the
  green half of U1's red/green pair).
- Covers R5. `tests/test_style.py` is untouched and stays green — the change is
  prose only.

**Verification:** No behavioural diff; both test files green.

### U3. Bound and short-circuit the continuation scan

**Goal:** `is_continuation` cannot sweep an oversized prompt, and does not run when
the session is not mid-lesson.

**Requirements:** R4, R5

**Dependencies:** none

**Files:**
- `src/mindhive/style.py` (modify — `is_continuation`, `resolve_mode`)
- `tests/test_style.py` (modify — add scenarios below)

**Approach:** In `is_continuation`, add the `promptgate.is_code_work` early-bail
before normalization. In `resolve_mode`, swap the conjunction so the already-read
dict lookup gates the scan. Extend the docstrings to say why each is ordered that
way — the bound is the same critical-path argument `wants_teaching` already makes.

**Patterns to follow:** `promptgate.is_code_work` for the bail; the existing
`test_long_prompt_is_scanned_but_bounded` in `tests/test_style.py` for the bound
test's shape.

**Test scenarios:**
- Covers R4. Inside a live lesson, a prompt longer than `PROMPT_SCAN_CAP` that
  leads with a continuation marker resolves to `MODE_CAVEMAN` — the bail fires
  rather than the marker extending the lesson.
- Covers R4. With `style.is_continuation` monkeypatched to raise, a cold session
  (`resolve_mode("lanjutkan", "cold")`) returns `MODE_CAVEMAN` without raising —
  proving the dict lookup gates the scan. This assertion fails before the reorder.
- Covers R5. Every existing continuity scenario stays green: Indonesian and
  English follow-ups keep a lesson alive, a substantive question ends it, the mode
  does not latch, sessions do not share a mode.

**Verification:** `tests/test_style.py` passes in full, including the two new
scenarios; the reorder scenario fails against the pre-change `resolve_mode`.

### U4. Record the sidecar write race where the next editor reads it

**Goal:** #5 is durable knowledge in the code, not only in a review thread.

**Requirements:** R6

**Dependencies:** none

**Files:**
- `src/mindhive/style.py` (modify — `_write_modes` docstring)

**Approach:** `_write_modes` currently has no docstring, only a fail-open comment.
Give it one that names the window: `_read_modes` → mutate → `_write_modes` is not
atomic across processes even though each individual write is, so two sessions
writing near-simultaneously can drop one entry. State the blast radius (that
session falls back to caveman until the next teach verb) and what a fix needs
(locking or compare-and-swap retry), so the next editor does not have to
rediscover it.

**Test expectation:** none — documentation only, no behavioural change.

**Verification:** Full suite still green; no behavioural diff.

## Risks & Dependencies

- **The widened drift test is literal by design.** It will fail on any future
  prose containing the banned phrases, including negated mentions. Accepted: the
  phrases are load-bearing enough that a deliberate re-approval is the right cost.
- **U2 depends on U1 landing first** so the corrections arrive with the test that
  keeps them true. Ordering only; no code dependency.
- **The `is_continuation` bail changes one pathological case** (a ≤6-word prompt
  over 2000 characters) from continuation to caveman. Documented above as accepted.
- **Change-log entry required.** Per project instructions, `src/` and `tests/`
  changes need a `hive log` entry before this work is done.

## Verification Strategy

The suite runs from the worktree root; `conftest.py` puts this checkout's `src/`
at the front of `sys.path`, so tests exercise the worktree's code rather than the
editable install pointing at the main checkout.

Both discriminating checks (U1's pre-fix blob, U3's monkeypatched reorder) must be
observed failing against the pre-change state before the work is called done — a
green run alone does not distinguish a real guard from a vacuous one.
