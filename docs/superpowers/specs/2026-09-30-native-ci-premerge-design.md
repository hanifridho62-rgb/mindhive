# Native pre-merge CI: `hive ci premerge`, `watch`, `conflicts`, `legs`

Date: 2026-09-30. Branch: `claude/native-ci-premerge`. Status: design, owner-requested.

## Why

GitHub Actions has not started a single job on `hanifridho62-rgb/mindhive` for
weeks: every job carries GitHub's annotation "The job was not started because
recent account payments have failed or your spending limit needs to be
increased". The ledger records it repeatedly (caveats from 2026-08-22 to
2026-09-29). The repository is private on the free plan, so branch protection
and rulesets answer 403: GitHub itself never blocks a merge. The block is the
project's own rule, "a PR is not done until CI is green", and the only CI that
could turn green is the one that cannot start.

The owner asked for a pre-merge check that is **self-contained** (no paid
runner), **equivalent to the GitHub CI** (`.github/workflows/ci.yml`), runs
**automatically before merge**, and lets PRs merge **without obstacles or
conflicts**.

`hive ci run` (blueprint P1g) already exists, but it is not that check. It runs
one interpreter (the operator's venv, with every optional extra installed), on
one OS, against the branch rather than the merge result, and reports nothing to
GitHub. Its `--merge` squashes and deletes the branch, which is not this
repository's convention (merge commits, branch kept).

## What ships

Four verbs on the existing `hive ci` group:

| verb | does |
|---|---|
| `hive ci premerge <PR>` / `--branch B` | merge preview, then every ci.yml job as a local **leg**, then one verdict: recorded in the ledger, cached in a sidecar, posted to the PR head as GitHub **commit statuses** |
| `hive ci premerge <PR> --merge` | the same, then a **guarded merge** (`gh pr merge --merge --match-head-commit`) only when green, and only when the tree that lands is the tree that was tested |
| `hive ci watch [--once]` | poll open PRs; run `premerge` for every PR whose head has no verdict yet; re-check conflicts when only the base moved. Never merges |
| `hive ci conflicts` | every open PR against the base and against each other, through `git merge-tree`: who conflicts with whom, on which files |
| `hive ci legs` | the leg catalog and what this machine can run of it, with the reason for every leg it cannot |

Commit statuses are the free REST API (`POST /repos/{o}/{r}/statuses/{sha}`),
not Actions, so they work whatever the account's billing says.

Non-goals:

- A self-hosted Actions runner. It still rides the account's Actions state, and
  it is standing configuration on the machine.
- Auto-merging from `watch`. Merging stays an explicit operator act ("merge only
  when I say so").
- Resolving conflicts. They are detected and named, never resolved by the tool.
- Registering a Windows scheduled task. That is persistent configuration; the
  one-liner is documented for the owner to run.

## The leg catalog (parity with ci.yml)

A **leg** is one ci.yml job, or one matrix cell of the `test` job. `ci/legs.py`
holds the catalog as data. `tests/test_ci_legs.py` parses ci.yml and fails when
a job or a matrix cell has no leg, or a leg names a job ci.yml does not have.

| leg | ci.yml job | host here | how |
|---|---|---|---|
| `windows-py3.11`, `windows-py3.12`, `windows-py3.14` | `test (windows-latest, pyX)` | Windows | fresh venv from the interpreter `py -0p` lists; `pip install -e .[dev]`; `python -m pytest -q` |
| `ubuntu-py3.11`, `ubuntu-py3.12`, `ubuntu-py3.14` | `test (ubuntu-latest, pyX)` | WSL | the same inside WSL, with `/mnt/*` removed from `PATH` so no Windows binary is reachable |
| `node` | `node tier-1 (paint guard)` | Windows | fresh 3.12 venv, `.[dev]`; `python -m pytest -m node -q`; needs `node` on `PATH` |
| `browser` | `browser tier (T12)` | Windows | the interpreter running `hive`, which must import `playwright`; `python -m pytest -m browser -q` with `PYTHONPATH=src`; no venv, no download |
| `pg` | `pg tier (Postgres 17)` | Windows | only when `MINDHIVE_CI_PG_DSN` names a **throwaway** database: fresh venv `.[dev,pg]`, `python -m mindhive pg migrate`, `python -m pytest -m pg -q` |

Measured on this machine (2026-09-30): `py -0p` lists 3.14, 3.12 and uv's
3.11.16; WSL Ubuntu has `/usr/bin/python3.12` and uv's 3.11.16 and 3.14.7 under
`~/.local/bin`, which only a login shell puts on `PATH`; `node` v24 is on the
Windows `PATH` and absent from WSL; the operator venv imports Playwright 1.62.0
with its Chromium installed.

Deviations, each named in the leg's `note` and in `hive ci legs`:

- `node` and `browser` run on the Windows host, not Ubuntu. ci.yml's own
  comments say why that is equivalent: node cases "have no OS or Python
  dimension", and browser cases are "about a rendering engine, and the engine
  is the same one on every leg".
- `browser` reuses the operator's interpreter rather than a clean venv, so it
  never downloads a Chromium on its own. It is the one leg that is not a clean
  room, and the one whose subject is not Python dependencies.
- `pg` never runs against `MINDHIVE_PG_DSN_ADMIN`. That DSN is the operator's
  real mirror, and `pg migrate` from an unmerged branch cannot be undone. It
  never sets a role password either (ci.yml's `ALTER ROLE` step): that is
  server administration, not a test.

A leg the machine cannot run is **unavailable**, with a reason: "no Python 3.11
found by `py -0p`", "WSL is not installed", "`node` is not on PATH", "no
`MINDHIVE_CI_PG_DSN`". Unavailable is never success.

## How one leg runs

Every leg gets a **fresh single-commit repository**, not a linked worktree.

1. `git -c core.autocrlf=<true on Windows, false on WSL> archive --format=tar <commit>`.
   Measured: archive honours autocrlf (`pyproject.toml` comes out CRLF with
   `true` and LF with `false`; `*.sh` stays LF through `.gitattributes`), which
   is what `actions/checkout` produces on each runner image.
2. Extract it (Windows: stdlib `tarfile` with the `data` filter; WSL: `tar -x`
   fed on stdin), then `git init`, `add -A` and `commit` under a fixed
   `mindhive-ci` identity. CI's checkout is a depth-1 repository, and so is
   this one. A test that runs `git` against the repository root can only ever
   touch this throwaway repository, never the operator's refs or stash.
3. Create a fresh venv and run `pip install -e .[extras]`, ci.yml's Install step.
4. Run ci.yml's test command verbatim: `python -m pytest -q`, or
   `python -m pytest -m <tier> -q`.
5. Remove the leg's directory with the runner's read-only-safe removal. The
   output survives as a `runlog` artifact.

The child environment is built, not inherited:

- Start from the operator's environment with the SOP secret and token scrubbed
  (`advice.scrub_agent_env(withhold_secret=True)`, the rule `hive ci run`
  already follows).
- Drop `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, `PYTEST_ADDOPTS`,
  `PYTEST_PLUGINS` and every `MINDHIVE_*` variable. CI has none of them, and an
  operator's `PYTEST_ADDOPTS` would change what runs.
- Point `MINDHIVE_HOME` at a directory inside the leg, so a test that escaped
  `tests/conftest.py`'s isolation still cannot reach the real store.
- Put the venv's scripts directory first on `PATH`, as `setup-python` does.

Classification: pytest exit 0 is `success` and exit 1 is `failure`. Any other
exit (2 interrupted, 3 internal, 4 usage, 5 nothing collected, 124 killed at the
cap) or a failed setup step is `error`. A setup failure names the step that
failed and is never reported as a test failure.

Legs run in parallel on a bounded pool, `--jobs` (default 3). The machine has 12
logical CPUs and 7.7 GB of RAM, and two concurrent Windows suites were measured
green side by side on 2026-09-30. One leg is capped at 5400 s.

## Merge preview: test the tree that would land

1. `git fetch origin <base>` and
   `git fetch origin +refs/pull/<N>/head:refs/remotes/origin/pr/<N>`.
2. `git merge-tree --write-tree --name-only <base_sha> <head_sha>`. Exit 1 is a
   conflict: the verdict is red, the conflicting paths are named, and no leg
   runs, because there is no tree to test.
3. When the base is already an ancestor of the head, the head is the tree that
   lands and is tested as it is. Otherwise `git commit-tree <tree> -p <base>
   -p <head>` builds an unreferenced merge-preview commit under the
   `mindhive-ci` identity, and every leg tests that commit.

The verdict records `base_sha`, `head_sha`, `tested_sha` and `tree`.

## The verdict

- **green**: the preview is clean, at least one default-tier leg ran, and every
  leg that ran is `success`.
- **red**: the preview conflicts, or a leg that ran is `failure` or `error`.
- **error**: nothing could run at all (no leg available, the preview itself
  failed, the PR could not be read).

Exit codes: 0 green, 1 red, 2 error or usage. Unavailable legs never turn a
verdict green or red. They are listed by name in the report and in the overall
status description.

## GitHub commit statuses

Statuses go on the PR **head** commit, because that is where the PR's checks
view reads them.

- Contexts: `mindhive-ci` for the verdict and `mindhive-ci/<leg>` for each leg
  that runs. They never imitate an Actions job name.
- `pending` when the run starts, then the final state: `success`, `failure` or
  `error`.
- Descriptions are cut to GitHub's 140 characters and name what did not run.
- No status for an unavailable leg. `success` would be a lie, and `error` would
  paint the PR red for something that is not a test result.
- `--no-status` skips posting. Branch mode never posts.

## The guarded merge

`hive ci premerge <N> --merge` merges only when all of these hold, re-read
immediately before the call:

- the verdict is green;
- the PR is OPEN and not a draft;
- the head is still the tested head (`--match-head-commit` makes GitHub itself
  refuse otherwise, atomically);
- the base has not moved, **or** `git merge-tree` over the new base produces the
  identical tree that was tested.

When the new base produces a different tree, the run starts again on that tree,
at most 3 rounds. `--allow-base-drift` instead accepts a clean merge-tree over a
verdict for an older base, which is GitHub's own default semantics.

The merge is `gh pr merge <N> --merge --match-head-commit <sha>`: a merge
commit, never squash or rebase, never `--delete-branch`, `--admin` or `--auto`.
Success is judged from `gh pr view --json state,mergeCommit`, never from `gh`'s
exit code. Afterwards the landed merge commit's tree is compared with the tested
tree and the answer is recorded as `landed_tree_matches`.

## Automatic

`hive ci watch` is the trigger. Every `--interval` seconds (default 300) it
lists open PRs and runs `premerge` for each one whose head has no recorded
verdict. When only the base moved, it re-runs the merge preview alone and turns
the status red if a conflict appeared. Drafts are included by default, because
a draft is exactly where "wait for CI" happens (`--skip-drafts` to leave them
out). It handles one PR at a time. `--once` does a single pass, which is what a
scheduled task would call:

```
schtasks /Create /SC MINUTE /MO 10 /TN "MindHive CI watch" /TR "<venv>\Scripts\hive.exe ci watch --once --root C:\Users\hanif\mindhive"
```

Registering that task is the owner's call, never this tool's.

## Records

- One `verification` event per run, with `scope: "ci"`, `gate: "premerge"`,
  argv `["hive", "ci", "premerge", ...]`, the overall exit code, counts summed
  over the legs, and `legs`, `base_sha`, `head_sha`, `tested_sha`, `tree`,
  `conflicts` and `merge_state`. `verify.validate_stored` owns the shape and the
  stamp. `hive ci status` reads it like any other CI record.
- One `runlog` artifact per leg, holding its full output.
- The sidecar `state/ci_premerge.json` holds the newest verdict per PR: head,
  base, tree, verdict, time. `watch` and the merge guard consult it. The ledger
  stays the audit trail; a lost or unreadable sidecar only means work is redone.

## Modules

| module | owns |
|---|---|
| `ci/legs.py` | the `Leg` catalog, interpreter discovery (`py -0p`, WSL login shell), availability probes |
| `ci/preview.py` | the `git merge-tree` merge preview and the pairwise conflict matrix |
| `ci/legrun.py` | one leg end to end: archive, fresh repository, venv, install, pytest, classification, cleanup |
| `ci/github.py` | every `gh` call: PR info, open PRs, repository slug, commit statuses, merge, merged state |
| `ci/premerge.py` | the run: preview, legs in parallel, verdict, statuses, record, sidecar, guarded merge |
| `ci/watch.py` | the poll loop |
| `ci/premerge_cli.py` | the four verbs, registered onto the existing `ci` group from `cli.register` |

Every subprocess goes through one injectable runner so the tests drive the whole
run with a fake, the pattern `tests/test_ci_runner.py` already uses. Every
module stays under the 1000-line cap. `cli.py`'s top-level imports stay exactly
what `tests/test_ci_cli.py` pins, so the new module is imported inside
`register`.

## Testing

- Unit tests with a fake runner for every module.
- Real-git tests on a small repository in `tmp_path`: a clean preview, a
  conflicting preview, an already-up-to-date head, and the single-commit leg
  repository with its line endings.
- One real Windows leg end to end on a toy checkout whose suite is two tests.
- A ci.yml parity test.
- A canary for every negative assertion. "No status posted for an unavailable
  leg" is only meaningful next to a proof that the run reached its statuses.

Dogfood before calling it done: `hive ci legs` on this machine, then
`hive ci premerge` on a real open PR with every available leg.

## Risks

- **Time.** A full run takes about the slowest leg times the number of pool
  rounds: roughly 25 min per Windows suite and 15 per Linux one. `--legs`
  narrows a run, and `watch` spends the machine's idle time instead of the
  operator's.
- **Network.** `pip install` needs PyPI, as CI's does. Offline, the install step
  fails and the leg is `error`, said plainly.
- **WSL memory.** The VM has about 3 GB. `--jobs` lowers the pressure.
- **Status spoofing.** Anyone with write access can post a `mindhive-ci`
  status. The ledger event is the evidence; the status is the signal on the PR.
