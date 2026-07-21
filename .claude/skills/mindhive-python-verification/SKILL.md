---
name: mindhive-python-verification
description: >-
  Pre-done verification loop for MindHive code changes: environment sanity,
  the full pytest suite, pack quality gates, ledger chain integrity, diff
  review, the mandatory change-log entry, and CI. Run before calling any
  src/ or tests/ change finished, before commits, and before PRs.
origin: mindhive
pack: mindhive-python
role: verification
kind: verification
---

# MindHive Python Verification Loop

The phase-gated pipeline between "the code looks right" and "the change is
done." Every phase produces real output you read; nothing passes on a proxy.
The ordering is deliberate: cheap environment checks first, the expensive
suite second, gates over a green suite only, bookkeeping last.

## When to Activate

- Before declaring any change under `src/mindhive/` or `tests/` complete
- Before every commit on a feature branch in this repo
- Before opening a PR, and again after pushing fixes to one
- After merging or rebasing, to confirm the combined state still holds
- When a session inherits a branch mid-work and needs to trust its state
- After an interrupted session, before resuming work on partially-landed code
- Before recording any experiment result whose claim rests on suite state
- When two branches both claim green and only one can be right about a contract

## Phase 1: Environment sanity

Confirm which interpreter and which checkout you are actually exercising —
the two classic wrong-machine failures in this repo.

```bash
# Git Bash. Bare `python` on this machine is a broken shim — use `py` or the
# full interpreter path. Prove the import resolves to THIS worktree:
PYTHONPATH="$PWD/src" py -c "import mindhive, pathlib; \
  print(pathlib.Path(mindhive.__file__).resolve())"
```

The printed path must start with this worktree's root. If it shows the main
checkout instead, every result after this point would be a lie about the
wrong code — stop and fix the environment before continuing. This is not a
theoretical failure: the editable install *always* wins for subprocesses
that were not given an explicit `PYTHONPATH`, and the recorded symptom is a
brand-new module raising ImportError from a subprocess while existing
imports work fine.

```bash
git rev-parse --abbrev-ref HEAD    # the branch you think you are on
git status --short                 # nothing unexpected staged or dirty
git stash list                     # no forgotten stash carrying your change
```

Interpretation: an unexpectedly dirty tree before you start means someone
(possibly an earlier interrupted session) left work behind — investigate
before mixing it into your change-set. A populated stash list on a branch
you did not stash on is the same signal.

## Phase 2: The full test suite

The suite is the contract archive — 1,600+ tests including the isolated-store
harness and fixture builders the sibling tdd file documents. Run all of it;
a targeted file proves only that file.

```bash
# During development, iterate on the touched file first (fast feedback):
PYTHONPATH="$PWD/src" py -m pytest tests/test_packs.py -x -q

# The gate itself is ALWAYS the full run:
PYTHONPATH="$PWD/src" py -m pytest -q
```

Read the tail, not just the exit code:

```text
1671 passed, 1 skipped in 113.43s
```

- The passed count must not have silently DROPPED relative to the last known
  total — a lower number with a green verdict means collection lost a file
  (an import error inside a test module can eat its tests without failing
  the run in some configurations).
- Every `skipped` must be one you can NAME. This repo's legitimate standing
  skip is the pack real-flow test before content lands; an unexplained skip
  is a finding.
- `xfail`/`xpass` entries are findings by default — this suite does not use
  expected-failure markers, so their appearance means something imported a
  marker it should not have.

If anything failed, stop here — fix and rerun; phases 3+ over a red suite
verify nothing. When a failure looks unrelated to your diff, prove it before
touching anything: check out master in a scratch worktree, run that exact
test there, and only call it flake with a green-on-master run in hand.

## Phase 3: Pack quality gates

If the change touched pack content, the validator module, or the manifest,
run the deterministic gates and read their findings:

```bash
PYTHONPATH="$PWD/src" py -m mindhive packs check
```

Exit 0 with an empty error list is the bar. Warnings deserve reading — a
`below-target` depth flag or a `corpus-skipped` notice may be acceptable
today and still worth recording in the PR body. Then read the stats view:

```bash
PYTHONPATH="$PWD/src" py -m mindhive packs report
```

How to read the report, line by line:

```text
mindhive-python  (depth ratio 1.91)
  patterns        218 non-empty lines  13 block(s)  [below target]
```

- **depth ratio** near 1.0 means the pack's roles are evenly developed;
  approaching the manifest ceiling (2.5) means one role is starving — the
  ECC failure mode where a family carried a stub next to an exemplar.
- **non-empty lines / blocks** are the floors; `[below target]` marks files
  under the quartet-depth ambition. Acceptance for new content is the
  target, not the floor.
- **internal same-role overlap** percentages should sit far under the 18%
  ceiling. A pair drifting upward across commits means two packs are
  converging on shared phrasing — diverge them before the gate forces it.
- **similarity-external rows** only appear when the corpus env var is set;
  their absence in CI is expected and is why the local acceptance run
  exists (Phase 6).

## Phase 4: Ledger integrity

Any change that touches append surfaces, event types, or store readers gets a
chain check against a real namespace before "done":

```bash
py -m mindhive verify
```

The output is JSON; the healthy shape is an ok chain with nothing else to
say. The unhealthy shape names its rows:

```json
{"ok": false, "checked": 18650, "corrupt_rows": [
    {"file": "events/abc123.jsonl", "line": 42}]}
```

A non-empty `corrupt_rows` on a namespace your change wrote to means your
append path produced a torn or malformed row — that is a release blocker,
not a warning, because every future reader will silently skip those rows
forever (readers are total by design; the loud surface is THIS command).
Distinguish provenance before panicking: rows in segments dated before your
change are pre-existing damage — report them, but they are not your blocker.

## Phase 5: Diff review

Read the change as a reviewer would, before any commit:

```bash
git diff --stat                    # shape: does the size match the intent?
git diff                           # the actual change, line by line
```

Mechanical sweeps that catch this repo's recurring review findings:

```bash
git diff | grep -n "breakpoint()\|pdb.set_trace"     # debuggers left behind
git diff | grep -n "print("                          # prints outside CLI handlers
git diff | grep -n "TODO\|FIXME\|XXX"                # promises without owners
git diff -- src/ | grep -n "except Exception: pass"  # silent swallows
```

Judgment checks no grep can do: does every new function's docstring state
its failure direction (the fail-open/fail-closed table in the sibling
security file)? Do new thresholds live in config rather than code constants?
Did a "small" diff quietly change a documented exit code or JSON key that
downstream scripts switch on? Is there a test for each behavior the diff
claims — and would that test fail without the diff (the discriminating-test
bar from the sibling tdd file)?

## Phase 6: The mandatory change-log entry

This repo blocks on it: every change-set touching `src/mindhive/` or
`tests/` records a change-log event before the work is called done.

```bash
py -m mindhive log update "what changed and why, as a real sentence" \
    --files src/mindhive/<module>.py tests/test_<module>.py
```

Category is one of update / minor_change / deletion / reduction /
major_change / replacement; the explanation needs 10+ chars of actual
meaning; one entry covers one logical change-set, not one file. Skipping
this queues a blocking notice into the next session's STATE — record it
now, not under duress later. If the session also produced content worth the
external-similarity acceptance, run it here while context is fresh:

```bash
MINDHIVE_ECC_SKILLS=/c/Users/hanif/ECC/skills \
  PYTHONPATH="$PWD/src" py -m mindhive packs report
```

and read the external overlap rows against the 10%/6.5% thresholds.

## Phase 7: Commit and CI

```bash
git add <files> && git commit    # per change-set, message says why
git push && gh pr checks --watch # only when the user asked for a PR
```

Work shipped via PR is not done until CI is green — all jobs, including the
Windows and Ubuntu matrix legs. Two recorded CI lessons apply:

- A test that passes locally can be VACUOUS on the other OS: the dead-path
  rule was inert on Linux until the probe list included `/tmp`, so its
  negative assertions passed while proving nothing. Arm negatives with a
  canary (the tdd file shows the pattern) so platform blindness cannot hide
  again.
- A red leg on a file your diff never touched may be flake — prove it by
  running that exact test on master before blaming or "fixing" your change;
  the recorded case was a Windows-only IndexError that reproduced on the
  identical commit without the diff.

Retargeting a PR's base does NOT re-trigger CI (the workflow fires on
opened/synchronize/reopened, not edited) — close and reopen, or push an
empty commit, when a retarget needs fresh checks.

## Failure triage

When Phase 2 goes red, classify before fixing — the three failure shapes
have different first moves:

```text
Collection error (module-level ImportError/SyntaxError)
  -> nothing in that file ran. If the import is the module under test,
     check Phase 1 first: is the subprocess importing the other checkout?
     A brand-new module "not existing" is the environment lie, not a code
     bug, more often than not in this repo.

Assertion failure in a test you wrote this session
  -> suspect the test as hard as the code. The recorded median bug was a
     TEST defect (an odd-length list where two formulas agree); the fix
     was making the test discriminating, not touching the code.

Assertion failure in a pre-existing test
  -> your diff changed a contract. Either the contract change is intended
     (then the test updates WITH a changelog note naming the new
     contract) or it is collateral (then the diff is wrong, not the
     test). Never "fix" a pre-existing test to green without deciding
     which of the two happened.

Failure only on one OS leg in CI
  -> platform-conditional behavior. Reproduce with the that-OS semantics
     in mind (path separators, /tmp roots, file-locking) and arm the fix
     with a canary so the passing OS cannot go vacuous again.
```

Two pytest tools earn their place in triage: `--lf` reruns only last
failures (fast bisection of a multi-failure run), and `-x` stops at the
first failure when the cascade is noise. Neither replaces the final full
`-q` run — the gate is always the whole suite.

## Stop conditions

- Phase 1 import resolves to the wrong checkout → stop; nothing downstream
  is evidence about your change.
- Phase 2 has any failure, an unexplained skip, or a dropped total → stop;
  do not run gates over a red or shrunken suite.
- Phase 4 shows corrupt rows from your append path → stop; release blocker.
- Phase 5 finds a changed exit code or JSON key nothing tested → stop and
  add the test first; contracts do not change by accident.
- Any phase's real output contradicts what you were about to report → the
  output wins; investigate before writing a single "done".

## Output Template

```
MINDHIVE CHANGE VERIFICATION
============================
Phase 1  environment      [PASS/FAIL]  import -> <path>, branch <name>, tree <clean/dirty>
Phase 2  full pytest      [PASS/FAIL]  <N> passed, <M> skipped (named: <which>)
Phase 3  pack gates       [PASS/FAIL/N-A]  check exit <code>, errors <n>, warns <n> (read)
Phase 4  ledger chain     [PASS/FAIL/N-A]  ok=<bool>, corrupt_rows=<n> (<provenance>)
Phase 5  diff review      [PASS/FAIL]  <files> files, sweeps clean: <y/n>, contracts stable: <y/n>
Phase 6  change-log       [DONE]       <category>: "<explanation>"
Phase 7  commit + CI      [GREEN/RED/LOCAL-ONLY]  <sha> / <jobs>

Overall: [READY / NOT READY]

Notes:
- <warnings read and accepted, flaky legs proven on master, anything deferred>
```

## Worked example: the validator landing

The pack-validator change-set itself ran this loop, and its transcript is
the reference for what each phase's real output looks like:

```text
Phase 1  import -> <worktree>/src/mindhive/__init__.py       PASS
Phase 2  1671 passed, 1 skipped (pack real-flow, named)      PASS
Phase 3  check exit 1 — 8 missing-skill errors               HONEST-RED
         (content not yet authored; the gate telling the truth)
Phase 4  N-A (no append-surface change)
Phase 5  diff: 3 files, +1131; sweeps clean                  PASS
Phase 6  major_change logged, 3 files                        DONE
Phase 7  committed f14f4a5; local-only until PR              LOCAL-ONLY
```

Note the honest-red: a phase that correctly reports a known-incomplete state
is a PASS for the *loop* — the failure mode is reporting green, not being
red for a reason you can name and schedule.

## Evidence discipline

The Output Template is only as good as what gets pasted into it. Three
rules keep reports evidentiary rather than narrative:

```text
Numbers verbatim, never paraphrased. "1671 passed, 1 skipped in 113.43s"
  goes in as printed; "the suite passed" goes in nowhere. A paraphrase
  cannot be re-checked and hides a dropped total.

Name the honest-red. A phase that correctly reports incomplete work
  ("check exit 1 — 8 missing skills, content unauthored") is recorded AS
  the phase's result with its reason, never rounded up to green and never
  omitted. The loop's credibility rests on reds being reportable.

Exit codes travel with their commands. "$LASTEXITCODE was 0" means
  nothing without the exact command line above it; the pair is the
  evidence unit, in reports exactly as in scripts.
```

## Cadence: which phases, at which moments

The full seven-phase loop is the pre-done gate; running all of it after
every edit would grind development to a halt. The practiced cadence:

```text
While iterating          Phase 2 targeted form only (touched file, -x),
                         with the full run deferred — plus Phase 1 once
                         at session start (the environment does not drift
                         mid-session; the checkout might after a rebase).

Before each commit       Phases 2 (full) + 5 + 6. The commit is the unit
                         of history; it gets the suite, the diff read,
                         and the change-log entry every time.

Before PR / handoff      The complete loop, 1 through 7, plus the
                         external-similarity acceptance if content moved.
                         This is the run whose Output Template actually
                         gets written down.

After merge or rebase    Phases 1 + 2 again on the resulting tree — the
                         combination is a state nobody has tested yet,
                         whatever each side's status was.
```

Two cadence rules with recorded incidents behind them: never batch multiple
change-sets into one Phase-6 entry at session end (the entry loses its
files and its why — record at the moment of completion), and never let
"the suite passed an hour ago" stand in for a pre-commit run when ANY file
changed since — the hour-old green proved a tree that no longer exists.

## Quick Reference

| Mechanic | Use |
|---|---|
| `py -c "import mindhive; print(__file__)"` | prove which checkout the process imports |
| `py -m pytest -q` (full suite) | the contract archive; read counts, name every skip |
| dropped-total check | a green run with fewer tests means collection lost a file |
| `py -m mindhive packs check` / `report` | deterministic content gates; ratio, floors, overlap trends |
| `py -m mindhive verify` | hash chain + corrupt_rows; your-path rows = blocker, dated rows = report |
| diff sweeps (`breakpoint\|print\|TODO\|except-pass`) | the recurring review findings, mechanically |
| contract-stability check | exit codes and JSON keys change only with tests |
| `py -m mindhive log <category> "..."` | the mandatory change-log event, before "done" |
| `MINDHIVE_ECC_SKILLS=... packs report` | the local external-similarity acceptance run |
| `gh pr checks --watch` + prove-flake-on-master | PR done = every leg green, blame proven |
| close/reopen after base retarget | retargeting alone never re-triggers CI |
| `--lf` / `-x` for triage only | development tools; the gate is always the full quiet run |
| verbatim tails in reports | paraphrased counts hide dropped totals and unnamed skips |
| honest-red vocabulary | a truthful failing phase outranks a rounded-up green |
| per-change-set log entries | batching at session end loses the files and the why |
| cadence over completeness | targeted while iterating, full loop only at the gates that matter |
| stale-green rule | an hour-old suite result proves a tree that no longer exists |
| triage-before-fix | collection vs new-test vs pre-existing vs one-OS — four different first moves |
