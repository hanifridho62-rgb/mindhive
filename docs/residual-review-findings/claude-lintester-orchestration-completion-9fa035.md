# Residual Review Findings — `claude/lintester-orchestration-completion-9fa035`

Source: an eleven-lens `ce-code-review` over the WB7 terminal-board branch
(personas: correctness, testing, maintainability, project-standards,
agent-native, learnings, security, reliability, performance, adversarial).
42 findings merged. This file records the ones **not applied**, because the
branch has no open PR to carry them and the run artifacts live in a temp
directory that will be cleaned. Nothing here depends on that directory.

**Line numbers below are as reported, against the pre-split tree.**
`dashboard.py` was over the 1000-line cap after the applied fixes, so the
event vocabulary, `EventBus` and `Board` moved to
`src/mindhive/orchestrator/board.py`. Any `dashboard.py:2xx–5xx` reference
below now lives in `board.py`. The three claims marked **(re-verified)** were
checked against the committed tree on 2026-09-02; the rest are as reported and
were not re-checked after the fixes landed.

## Applied on this branch (closed, listed so the record is not ambiguous)

- P1 the header pane is dead on the real `--execute` path — `EventBus` now
  holds a bounded pre-subscription backlog and counts evictions.
- P2 an exception in the fallback log path escapes and kills the mission —
  `Dashboard._tolerate`.
- P2 `_emit` swallows bus failures with no counter — `observer_drops`.
- P3 `MissionFinished`'s authoritative totals are discarded by the fold — now
  reconciled with `max`.
- P2 `EventBus.dropped`/`unknown` have no reader in `src/` — `cli._close_board`
  reads and prints both.
- P2 `_empty_reason` says "no source survived" when one of several failed.

Eleven mutations reverted in place; all eleven went RED.

## Residual — actionable, `downstream-resolver`

### P1 `src/mindhive/mindnet/advice.py:409` — `MAX_SESSION_IDS` truncation fails OPEN

21 distinct agent session ids on one signature force `total > len(ids)`, and
the manufactured constraint is composed. Suggested: decide authorship where the
FULL list exists — derive the verdict in `incidents._render` from the complete
`rec['sessions']`, emit it as an additive frontmatter key
(`authors: agent|mixed|human`), have `summaries()` pass it through and
`agent_authored_incident` read it, falling back to the id-list logic only for
pre-existing records. Truncation then cannot change the answer.

### P1 `src/mindhive/mindnet/brief.py:551` — fence-defuser bypass

A forged banner whose gap exceeds `_BANNER_GAP` (24) survives verbatim into the
fenced block AND is counted as `dropped=0`, so a run under attack still reports
success. Suggested: add a whole-line shape rule alongside the gap rule —
`safe_text` has already collapsed the item to one line, so when a line contains
the word CONTEXT with a `_RULE_RE` run at its start and/or end, collapse those
terminal runs to a single hyphen regardless of gap length, and count the line as
defused so `dropped` rises.

### P2 `src/mindhive/mindnet/advice.py:366` — an unrenderable row still spends a slot

`resolve_sops` rendered 0 of 4 legitimate matching SOPs: a row that cannot
render still consumes a `MAX_SOPS` slot — the exact DoS that
partition-before-rank claims to close. Same shape in `resolve_constraints`
(:435) and `resolve_skills` (:312). Suggested: partition on `safe_text(...)`
being non-empty BEFORE ranking, or over-fetch until `MAX_SOPS` rows have
actually rendered.

### P2 `board.py` (`EventBus.subscribe`) — a bus bound to a previous loop never re-binds **(re-verified)**

`subscribe` binds only when `self._loop is None`, so a bus reused across two
`asyncio.run` calls keeps the first, closed loop. Publishes then fail
`call_soon_threadsafe` and are counted as `dropped` — counted, not silent, but
still lost. No caller in `src/` reuses a bus today (`cli.py:148` is the only
construction), so this is latent. The `bind` docstring was corrected on
2026-09-02 to state this, instead of claiming the mission re-binds.

### P3 `board.py` (`EventBus.emit`) — `unknown` is an unbounded tuple rebuilt per complaint **(re-verified)**

`self.unknown = self.unknown + (complaint,)` grows without bound with O(n^2)
copying, in a class that bounds everything else. Suggested: bound it the way
`_held` is bounded, and count the overflow.

### P2 `board.py` (`Board.apply`), reported at `dashboard.py:508` — a half-applied fold

One event is both counted as a failure and counted as unreadable when the
arithmetic conversion in `_finished` raises. Suggested: route that conversion
through a module `_float` helper the way every other field goes through
`_text`/`_int`, or compute the note before mutating the counters.

### P2 `dashboard.py:723` — the live board never shows `Board.ignored`

Only `render_plain` does, contradicting the class's own docstring. Suggested:
append a styled warning to `_header_panel` when `counts['ignored']` is
non-zero, mirroring `render_plain`'s wording, and add the count to the
feature-hook test so the two renderers cannot diverge again.

### P2 `dashboard.py:556` — a `Note` is folded as `ignored`, losing its text

`Note` is the event an unrecognised kind becomes, so the one event carrying
drift information is the one whose text is dropped. Suggested: give it an
explicit branch that routes the text to the footer both renderers already draw,
and let it count as applied; keep `ignored` for genuinely unreadable input.

### P2 `dashboard.py:892` — `render_errors` is never surfaced on the live path

A board frozen by repeated repaint failure exits saying nothing. Suggested:
replace the bare return in `_run` with a report naming `render_errors` and
`last_render_error`, then write the closing `render_plain` snapshot the
fallback path already writes, after `Live` has restored the terminal.

### P2 `cli.py:101` — `--board` and `--json` are exclusive only because stdout is hardcoded

`Dashboard` already takes `stream=` and `console=`. Suggested: when `--json` is
set, attach the board to stderr instead of refusing. Keep the dry-run refusal
as-is — that one is about there being nothing to watch, not about who owns
stdout.

### P2 `dashboard.py:483` — board state no renderer draws

`steal_log` and `LaneView.worktree` are folded, bounded, and displayed by
nothing. Either draw them or delete them, and say which.

### P2 `docs/DATA_MODEL.md:127` — the `mindnet_brief` payload doc is not updated

Three new additive keys are undocumented.

## Residual — security, owner `human`, already recorded in TODOS 26

These three are P0 and are **not** fixed. They are recorded in TODOS item 26
("NARROWED, NOT CLOSED — corrected 2026-09-02 after review") with the verbatim
probe output that demonstrates the first one. They are listed again here only
so this file is complete:

- `src/mindhive/mindnet/advice.py:154` — the authorship signal the TODOS 26 fix
  rests on is chosen by the attacker: `hive sop add --session <anything>` (or a
  re-exported `MINDHIVE_SESSION`) de-attributes the row, and unknown authorship
  is treated as human. Demonstrated: the same agent stamping itself honestly is
  withheld; adding one flag composes the injected line.
- `src/mindhive/orchestrator/lintester.py:576` — the control is inert for this
  repo's own spawner: `_run_child` spawns agents with no `MINDHIVE_SESSION`
  overlay, so their rows carry the operator's session.
- `src/mindhive/sopgate.py:347` — agent-written mandatory SOPs still reach the
  OPERATOR's prompt every turn; withholding is applied in `advice.resolve_sops`
  only, not to the mandatory-SOP injection.

A self-reported string cannot be an authorization boundary. Closing these is a
change to the ledger's trust model, which was judged out of scope for a UI
branch — see TODOS 26 for the reasoning, and for why `brief.wave_context`'s own
docstring rules out the one structurally sound alternative.

## Residual — advisory, not actioned

- P1 `dashboard.py:962` — `print()`/rich writes run synchronously on the
  mission's event loop, so a slow or frozen terminal can stall lane dispatch.
  Suggested: offload the write in `_say` via `run_in_executor`, or bound it.
- P2 `tests/test_orchestrator_dashboard.py:704` — a fixed `asyncio.sleep(0.4)`
  races a 0.25s repaint tick; this repo's ledger has repeatedly traced CI
  intermittency to exactly this shape.
- P2 `tests/test_orchestrator_dashboard.py:770` — the decoupling scanner's regex
  misses `importlib.import_module` / `__import__` forms.
- P2 the observer/bus counters (`dropped`, `unknown`, `render_errors`,
  `observer_drops`) reach `_close_board` but not the mission report.
- P2 `advice.py:354` — SOP authorship is the LAST editor's session, so a later
  human edit launders an agent-authored body back into every brief.
- P2 `brief.py:673` — the new `dropped` counter fires on this project's own
  documentation and stays silent on a real injection.
- P2 naming drift: the "rows withheld for orchestration authorship" count is
  named differently at every layer.
- P2 three near-identical `_emit` bus wrappers have already drifted; two of the
  three are never exercised with a raising bus.
- P2 `docs/runbook-empty-brief.md:83` does not mention the new
  `failed=`/`dropped=`/`withheld=` signals it now prints.
- P3 `queue.py:465`, `tests/test_mindnet_advice.py:661`, and the absence of any
  machine-readable sink for the ten event kinds.
