# The five golden rules

Blueprint spec §9.3 sets five operating rules for every Claude Code agent that
works in this repository. This document gives each rule, why it exists, and what
in this codebase enforces it. It is just as plain about where the code only
*supports* a rule and nothing enforces it: a rule that reads as enforced when it
is not is the failure rule 1 exists to prevent.

Loading these rules as an SOP is **optional** — see the last section. What
enforcement exists lives in the code below and holds whether or not the SOP is
loaded.

## Rule 1 — Absolute honesty

**The rule.** Never report a task as done, passing or fixed without real
execution evidence from a test run. A success claim resting on reasoning alone,
with no verified terminal output, is a critical violation. When a tool errors or
a test fails, report it plainly, with the original traceback.

**Why.** An agent's "done" is otherwise indistinguishable from a real one, and
this project's record holds several confident handoffs whose checkable claims
were false.

**How it is enforced.**

- `src/mindhive/verify.py` — `run_and_record` (line 330) runs the real test
  subprocess and appends a `verification` event carrying the exit code, the
  counts and `verified_by: "python"`. It is the only writer allowed to stamp
  that event. The arbitrary-append surfaces refuse a `verified_by` stamp
  (`contracts.py`; the `verification` entry in `events.py`'s type registry), so
  a pass cannot be recorded without a run. `hive selftest` (`selftest.py`) goes
  through it, and so does `hive ci run`, which records its verdict with
  `scope: "ci"`.
- `src/mindhive/orchestrator/gate.py` — `SuiteGate` (line 390) judges every
  `hive lintester` unit through `run_and_record`, and it is **fail-closed**
  (blueprint P2). A unit whose changed module no test names is refused, and a
  new module is refused with the exact test file to write.
  `--allow-untested` passes a change no test could name, and the mission report
  counts those units.
- `src/mindhive/directives.py` — §0 "Absolute honesty" opens the operating
  directives that `hooks/session_start.ps1` / `hooks/session_start.sh` print
  (`hive directives`) at every session start.
- Tests: `tests/test_verify.py`, `tests/test_orchestrator_gate.py` (for example
  `test_a_new_module_with_no_test_fails_closed_and_names_the_path`,
  `test_the_override_does_not_excuse_a_new_untested_module`).

**What is not enforced.** Nothing can stop an agent from *writing* "done" in
prose. What the code guarantees is that the claim is checkable: either a
`verification` event exists for the run, or it does not.

## Rule 2 — Zero-assumption grounding

**The rule.** Before starting work or answering a question about project state,
read the record: `hive state` (STATE.md), or search past memory with
`hive recall "<query>" --semantic`. Never reconstruct history, paths or the
reasons behind past decisions from conversation memory. When the ledger does not
have it, say so.

**Why.** A model answers from its own weights unless something makes it read the
record first.

**How it is enforced.**

- `src/mindhive/premise.py` — the `premise` pass (pass 4 of the injection
  pipeline, `premise.extend_hook_context`) runs on **every** prompt. It checks
  the prompt's checkable claims against the record — asserted file paths,
  roadmap ids and test counts — and says which claims it could not check.
  `hive premise check [--file F]` runs the same check on a handoff document by
  hand.
- The record is loaded before the agent is asked anything. The session-start
  hooks print the directives, and `hive sync` writes the `MEMORY.md` digest the
  session auto-loads.
- Tests: `tests/test_premise.py`, `tests/test_premise_hook.py`,
  `tests/test_cli_premise.py`.

**What is not enforced.** Nothing forces an agent to run `hive state` or
`hive recall` before it answers. The premise pass catches contradicted claims in
the *prompt*, not unsupported claims in the *reply*.

## Rule 3 — Work-session and initiative tags

**The rule.** Tie every code change to a work-session tag naming the technical
method, `[WS: execution | <technical action> | <timestamp>]`. Work the agent
takes on beyond the user's specification — a security function, a new test, an
unrequested refactor — carries an initiative tag,
`[INITIATIVE: <technical reason> | Ref: <incident>]`.

**Why.** An auditor must be able to tell which changes the user asked for from
which ones an agent decided on, and how each was done.

**How it is supported.**

- `src/mindhive/worksessions.py` — `make_work_session_tag` (line 130) builds
  `[WS: <phase> | <action> | <UTC timestamp>]`, parsed back by `_WS_TAG`
  (line 96). `make_initiative_tag` (line 141) builds
  `[INITIATIVE: <reason> | Ref: <ref>]` (`_INITIATIVE_TAG`, line 97).
  `src/mindhive/initiatives.py` folds the declarations.
- The command:

      hive worksession record --phase execution --action <catalog key or text> [--initiative --reason "…" --ref "…"]

  It records one step with its four tags (`[WS: …]`, `[USER_SPEC: …]` or
  `[INITIATIVE: …]`, `[USAGE: …]`). `--reason` / `--ref` without
  `--initiative` is refused. `hive worksession list [--initiatives]` reads them
  back, and the dashboard's Oversee cards show them.
- Tests: `tests/test_worksessions.py`.

**What is not enforced.** The spec says tagging is mandatory. The code provides
the tags and does not enforce them: no hook records a step per edit, and nothing
refuses an untagged change. The tags exist when the agent or the operator runs
`hive worksession record`.

## Rule 4 — Context budget and smart rotation

**The rule.** Before a large task, run `hive context --check`. When occupancy
passes the smart-zone boundary of 50% (the spec says 100,000 tokens), save state
and recommend rotating the session (`ROTATION_PENDING`) before reasoning
degrades.

**Why.** Past about half the window, a session's reasoning degrades faster than
its remaining room suggests.

**How it is enforced — two mechanisms, not the one the spec names.**

- `hive context --check` (`src/mindhive/context.py`,
  `src/mindhive/commands/context_ops.py`) measures the **auto-loaded surface**:
  everything that loads into every session, ranked by estimated token cost. It
  checks that surface against the namespace's token budget and the `MEMORY.md`
  loader cuts. It exits 1 when over budget, or when `MEMORY.md` would be cut
  with no rollover notice ahead of the cut. It does **not** measure a live
  conversation's occupancy, and no hook runs it.
- Live occupancy is measured by the Stop hook. `hooks/stop.ps1` / `hooks/stop.sh`
  call `src/mindhive/rotation/hook.py` after every turn, which reads the
  transcript. The threshold is `min(cap, ratio × window)`: cap
  `MINDHIVE_ROTATION_CAP` (default 500,000), ratio `MINDHIVE_ROTATION_RATIO`
  (default 0.5), and the model's window (200,000 when unknown). That is 100,000
  tokens on a 200k window, the spec's figure, but 500,000 on a 1M window, where
  the cap binds.
- Past the threshold, the hook writes the `ROTATION_PENDING` artifact
  (`src/mindhive/rotation/artifact.py`) and prints advice. `hive session run`
  then starts a successor seeded from the ledger. The hook is **inert unless
  `MINDHIVE_ROTATION` is set**, which only `hive session run` does for its
  children: opt-in by design, and fail-open (the project's `CLAUDE.md`).
- Tests: `tests/test_context.py`, `tests/test_rotation_hook.py`,
  `tests/test_rotation_artifact.py`.

**What differs from the spec.**

- The spec's 100,000 is a fixed number. The code's boundary is half of the
  model's window, capped at 500,000.
- The spec has the agent verify occupancy with `hive context --check`. That
  command measures what auto-loads, not how full the conversation is.
- Rotation happens only in sessions `hive session run` supervises.

## Rule 5 — Mandatory code change-log

**The rule.** Record every change to `src/mindhive/` or `tests/`, however small,
in the change log before calling the task done:

    hive log update "Validate session_id with a regex in events.py to close a path-traversal hole"

The valid categories are `update`, `minor_change`, `deletion`, `reduction`,
`major_change` and `replacement`. The explanation is a real sentence of at least
10 characters.

**Why.** The ledger is the project's memory; a change nobody logged is a change
the next session cannot find.

**How it is enforced.**

- `src/mindhive/changelog.py` — `CATEGORIES` holds the six categories, and
  `MIN_EXPLANATION_CHARS = 10`. The affected files are auto-detected: the
  pending marker, then the session's touched files, then `git diff`.
  `--files` overrides them. Entries are `code_change` events and appear in the
  dashboard's Log view.
- `hooks/session_end.ps1` / `hooks/session_end.sh` run
  `hive log queue --session <id>`. It writes a pending-changelog marker for any
  source file the session touched without a log entry, and STATE.md and the
  briefing then carry an "Unlogged code changes" notice until the entry is
  written. `hive log pending` prints the marker.
- The project's `CLAUDE.md` states the rule.
- Tests: `tests/test_changelog.py`.

**What is not enforced.** The queue runs after the fact, at session end. Nothing
refuses the "done" claim itself; the notice is what keeps the gap visible.

## Loading the rules as an SOP (optional)

None of the enforcement above depends on this step. Loading the rules as an SOP
makes the text itself show up where SOPs are shown: STATE.md's SOP section,
`hive sop preflight`, `hive sop list` and `hive sop show`. It is an ordinary,
**non-mandatory** SOP unless you choose otherwise:

    hive sop add five-golden-rules --title "Five golden rules (spec 9.3)" --file docs/SOP_FIVE_GOLDEN_RULES.md --tags sop,golden-rules

Without a tier flag, `hive sop add` records an ordinary, scope-matched SOP.
`--no-mandatory` says the same thing explicitly. `--mandatory` would re-assert
the body on every prompt, and a body this long would exceed the per-prompt
budget and arrive as a `hive sop show five-golden-rules` pointer instead, so it
is not recommended here.

- Check it: `hive sop list`, `hive sop show five-golden-rules`.
- Refresh it after this file changes:
  `hive sop update five-golden-rules --file docs/SOP_FIVE_GOLDEN_RULES.md`.
- Remove it: `hive sop delete five-golden-rules --reason "…"`.

**Who may run it.** Every `hive sop add|update|delete` — not only a mandatory
one — passes the authorship gate in `src/mindhive/advice.py`
(`is_authorized_sop_author`) before anything is read or written.

- With `MINDHIVE_ORCHESTRATION_SECRET` set on the host, only a process whose
  `MINDHIVE_PROCESS_TOKEN` equals it may write. The token is withheld from every
  agent this package spawns.
- With no secret configured, the session rule decides: an orchestration session
  id (`mn9-`, `lm-`, …) is refused.
- A refusal exits 2 with `penulisan SOP (<action>) ditolak: …` on stderr and
  appends nothing.
- A row that an agent did manage to write (`sop.is_agent_authored`) is marked
  `[agent-written]` by `hive sop list` and withheld from the auto-loaded
  context. There it is admitted only as an `[ADVISORY TIP - UNVERIFIED AGENT]`
  count line.

So this is the operator's command, run from the operator's own shell. The rules
it loads are the operator's to adopt, not an agent's to impose on itself.

Tests: `tests/test_sop_authorship.py`, `tests/test_advice_authorship.py`,
`tests/test_sopgate.py`, `tests/test_sop_mandatory.py`.
