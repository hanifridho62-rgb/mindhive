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

## Closed on `claude/lintester-orchestration-completion-c98a36` (2026-09-05/06)

Every address below was **re-derived with grep** before editing: this file's
line numbers predate two module splits, and `dashboard.py:2xx-5xx` now lives in
`board.py`. Each fix carries its own tests, each has a canary beside the
negative, and each was mutation-proofed in place -- the mutation reverts the
fix, the named test goes RED, the tree is restored byte-for-byte and the sha256
compared. 21 mutants, all RED (M11-M29 plus the two re-done ones).

| Finding (as recorded) | Real address | Outcome |
|---|---|---|
| P1 `advice.py:409` truncation fails OPEN | `advice.py` `agent_authored_incident` | **fixed** -- fails CLOSED now, deliberately reversed; `test_a_truncated_all_agent_id_list_fails_closed` |
| P1 `brief.py:551` fence-defuser bypass | `fence.py` `_strip_bracketed_rules` | **narrowed, not closed** -- the PAIR rule beside the gap rule (a rule run each side of CONTEXT, anywhere on the line, gaps unbroken by `.:;!?`), plus a `defused` counter through to the ledger payload. The first cut of it claimed *closed* and was wrong twice: it anchored at the string ends, so one leading character reopened the padding bypass, and it tested the two edges independently, so a trailing `...` ate honest memo text. Both were caught by an adversarial review OF THAT FIX and are pinned by tests. The residual is stated in the docstring: a forgery whose gap carries a clause-ender still passes, and that is the price of not eating prose |
| P2 `advice.py:366` an unrenderable row spends a slot | `advice.py` `resolve_sops` / `resolve_constraints` / `resolve_skills` | **fixed** -- one shared `_ranked_rendered`; the cap counts RENDERED rows |
| P2 `board.py EventBus.subscribe` never re-binds | `board.py` `subscribe` | **fixed** -- re-binds to the running loop; an explicit `bind(loop)` survives unless the loop is closed |
| P3 `board.py EventBus.emit` unbounded `unknown` | `board.py` `_note_unknown` | **fixed** -- distinct, bounded by `capacity`, overflow counted in `unknown_dropped` |
| P2 `Board.apply` half-applied fold | `board.py` `_finished` | **fixed** -- a module `_float` beside `_text`/`_int`, so the conversion cannot raise after the counters moved |
| P2 the live board never shows `Board.ignored` | `dashboard.py` `_header_panel` | **fixed** -- the same line `render_plain` draws, in the same words |
| P2 a `Note` folded as `ignored` loses its text | `board.py` `_apply` | **fixed** -- an explicit branch routes the text to the footer and counts as applied; `ignored` keeps its meaning |
| P2 `render_errors` never surfaced on the live path | `dashboard.py` `_run` | **fixed** -- reported when non-zero, after `Live` restores the terminal, with the closing `render_plain` snapshot |
| P2 `--board`/`--json` exclusive only because stdout is hardcoded | `cli.py` `_observer` | **fixed** -- `--json` moves the board to stderr instead of switching it off; `_close_board` follows it there |
| P2 board state no renderer draws | `dashboard.py` `render_plain` | **fixed by drawing, and the which is recorded**: `steal_log` and `LaneView.worktree` are drawn in the PLAIN renderer only -- that is the frame `_close_board` prints after the mission returns, and `Live` leaves nothing behind when it exits |
| P2 `DATA_MODEL.md:127` mindnet_brief doc not updated | `docs/DATA_MODEL.md` | **fixed** -- in the SAME change-set as the payload, and `test_every_brief_payload_key_is_named_in_the_data_model` now fails the build if it drifts again |

Two recorded measurements were **deliberately changed**, and both are called out
in their tests' docstrings rather than quietly edited:

- `test_the_opening_fence_is_correctly_not_treated_as_a_banner` asserted that
  `FENCE_OPEN` survives the defuser verbatim. Its reasoning about the GAP rule
  was right and its conclusion was not: a resolver row reproducing this module's
  own fence header is the forgery the whole-line rule exists to disarm. Now
  `test_the_opening_fence_in_the_data_is_disarmed_without_being_dropped`.
- `test_the_board_refuses_to_share_stdout_with_the_json_report` asserted the
  board is switched OFF under `--json`. `Dashboard` has always taken `stream=`,
  so the exclusivity was about a hardcoded stdout, not a real conflict. Now
  `test_the_board_moves_to_stderr_rather_than_losing_to_the_json_report`.

One test in this round was found VACUOUS by its own mutation and repaired: the
SOP slot fixture used an empty body, and `sop.current` drops those during the
fold, so the row never reached the resolver. It now uses `[mindnet]` -- real to
the registry, empty at render.

## Residual — actionable, `downstream-resolver` (all twelve now closed above)

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

## Security — the P0 chain, CLOSED on `claude/lintester-orchestration-completion-c98a36`

**All three are now fixed, and they were fixed together because they are ONE
exploit path, not three bugs.** Fixing any one alone leaves the path open:
attribution that fails open, a spawner that gives its children no identity to
attribute, and a gate with no authorship awareness composing MANDATORY text into
the operator's prompt every turn.

- **advice.py** — `agent_authored_incident` no longer fails open on a truncated
  id list. `ORCHESTRATION_PREFIXES` gained `lm-`, derived from the imported
  `orchestrator.lintester.MISSION_PREFIX` constant rather than a copied literal,
  and `test_the_lintester_mission_prefix_is_covered` compares the two imports so
  a rename cannot silently un-cover the family.
- **orchestrator/lintester.py** — `_run_child` now spawns with
  `env={"MINDHIVE_SESSION": run_key}` where `run_key` is
  `f"{MISSION_PREFIX}-{task.task_id}"`, so a child's rows carry the CHILD's
  identity and not the operator's. `agent.run` already accepted `env=`; nothing
  passed it.
- **sopgate.py** — `is_agent_authored` partitions agent-written rows out of the
  mandatory fence BEFORE the prompt budget is spent, and `probe()` reports
  `withheld` so the exclusion is never silent. The sidecar carries the writing
  session because `sop._project_rows` now emits `session_id`; `sop.fold` had
  always derived it and nothing wrote it out.

26 tests in `tests/test_orchestration_authorship.py`, covering all three links
plus one end-to-end case; every "it stays silent" test has its positive half and
a canary, and six mutations (M1-M6) went RED.

**What this does and does not buy.** The chain is closed for the spawner this
repo owns: a Lintester child can no longer write a SOP that reaches a sibling's
or the operator's prompt. It does NOT make a self-reported session string an
authorization boundary — a caller who sets `MINDHIVE_SESSION` by hand, or passes
`hive sop add --session <anything>`, still chooses their own stamp. That remains
a change to the ledger's trust model; see TODOS 26. What changed is that the
control is no longer INERT, which is what the three findings below said.

The findings as originally recorded: They are recorded in TODOS item 26
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

A self-reported string cannot be an authorization boundary, and that is still
true — what the fixes above close is the path through THIS repo's own spawner.
The remaining half is a change to the ledger's trust model — see TODOS 26 for
the reasoning, and for why `brief.wave_context`'s own
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
