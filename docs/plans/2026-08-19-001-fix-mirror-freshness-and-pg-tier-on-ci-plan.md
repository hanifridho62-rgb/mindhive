---
title: "fix: the mirror follows the ledger, and CI can see the SQL"
type: fix
date: 2026-08-19
---

# fix: the mirror follows the ledger, and CI can see the SQL

## Summary

Two gaps survive the Postgres read-mirror work (TODOS 19 and 20). Both are trust
defects, not feature gaps: the mirror answers correctly about a snapshot nobody
refreshes, and CI reports green about SQL it never executes. This plan closes
both, and closes the third defect they share — the tool envelope tells an agent
that the mirror is stale without telling it, or its operator, what to do about
it.

The mechanism is not "implement `_insert_one`". It is the opposite: delete the
single-row write seam, and give `pgmirror` an incremental per-segment `refresh`
that the already-scheduled `autosync` run calls. One write path instead of two.

## Problem Frame

The read mirror is a developer-facing product whose developer is an AI agent
asking SQL through seven MCP tools, plus one human operator on one Windows
machine. Read through that lens, the two open items are the same failure twice:
**a signal that reads as trustworthy and is not covering the thing it appears to
cover.**

**Gap 1 — CI is blind to every line of SQL.** `tests/test_pg_live.py` holds 24
tests and is marked `pg`, deselected by `addopts = "-m 'not node and not browser
and not pg'"` in `pyproject.toml:69`. They are the only tests that can answer
whether the SQL compiles, whether `mindhive_reader` can actually read, whether
37k events round-trip to the same chain head, and whether a second statement can
ride in on `query_database`. None of that runs on any CI leg. The badge is green
and says nothing about it. This is a **Credible** failure (DX characteristic 2):
the signal is reliable-looking and not predictive.

**Gap 2 — the mirror does not follow the ledger.** `pgstore._insert_one`
(`src/mindhive/pgstore.py:661`) raises `NotImplementedError`; `mirror_one`
converts that into a counted failure. The mirror is refreshed only by
`hive pg rebuild`. Every session that appends an event makes the mirror stale,
and it stays stale until a human remembers.

**Gap 3 — the staleness notice stops one sentence short.** `pgtools`
`_stale_notices` (`src/mindhive/pgtools.py:535-538`) tells the agent the mirror
is stale and to check `freshness`. It names the problem and the cause and stops.
It does not name the fix, and it does not say that the reader credential
structurally cannot perform the fix — `mindhive_reader` carries
`default_transaction_read_only = on` set on the ROLE
(`003_roles_and_views.sql:71`). An agent that reads the notice and tries to
repair the mirror itself will fail with no explanation of why. Every error is
problem + cause + fix + who can perform it; this one delivers half.

The three are one story: **the mirror is exact about a moment nobody schedules,
proven by tests nobody runs, and the agent is told so in a way it cannot act on.**

## Developer Perspective

I am the agent. My CLAUDE.md tells me the ledger is 930 segments and 20 MB and
that I must not fold it to answer a question, so I reach for the tool. I call
`get_current_state` and I get rows back with `ok: true` — real rows, a real
`freshness` block. `parity_ok` is true, so the rows I hold are exact. But
`stale` is also true, and the notice says anything newer than the last rebuild
is absent, check `freshness` before answering about now.

So I check `freshness`. It confirms what the notice already told me. Now what?
The question I was asked is "what is the active task" — a question about now,
which is exactly the class the notice says I cannot answer. I look for a repair.
`hive pg rebuild` exists in the help. I run it and get
`MINDHIVE_PG_DSN_ADMIN is not set`. I do not have that variable and nothing has
told me I never will. I have now spent two tool calls and a failed command to
learn one fact the first response could have carried: *the operator has to run
this, not you, and here is the command to hand them.*

Meanwhile the reason the mirror was stale at all is that I appended two events
this session. Nothing in the append path touched the mirror, because the seam
that was supposed to has been raising since the day it was written.

## Decisions taken without asking

The session directive in force is full-automatic: choose the best branch, ask
nothing. These four were decided by me, not by the operator. Each is reversible
and the reversal cost is named.

| # | Decision | Alternative rejected | Reversal cost |
|---|---|---|---|
| D1 | Delete `mirror_one` / `_insert_one`; refresh incrementally instead | Implement single-row insert with a third writer credential | Low before implementation, medium after (8 seam tests get rewritten) |
| D2 | Refresh granularity is one **segment**, not one row | Row-level upsert keyed on `(project, segment, seg_line)` | Low — same SQL shape, narrower `WHERE` |
| D3 | Trigger is `autosync.run_all`, not a hook on append | A post-append hook, or a systemd/Task Scheduler entry of its own | Low — one call site |
| D4 | Review mode is **DX POLISH** (harden the existing surface, add no scope) | DX EXPANSION (e.g. a mirror daemon, a web freshness panel) | N/A — scope choice only |

**D1 is the load-bearing one.** Three reasons, all from the code:

1. **Credential.** `pgstore` holds the reader DSN, and the read-only property is
   set on the ROLE, so an INSERT from there is refused by the server, not merely
   discouraged by the architecture. Adding a writer DSN puts a write credential
   in the module built specifically not to have one;
   `tests/test_pg_repo_gates.py` enforces that split structurally.
2. **Two write paths drift.** `pgmirror.scan_segments`
   (`src/mindhive/pgmirror.py:244`) numbers `seg_line` with `enumerate` over
   every physical line, blank and torn rows included, and its own docstring
   explains that a loader numbering only surviving rows "would order identically
   today and diverge the first time a segment gained a blank line". A second
   writer computing its own line number is that divergence waiting for a blank
   line.
3. **Per-segment reload cannot break order.** Global order is not a stored
   property. `pgstore.READ_ALL_SQL` re-establishes it at read time with
   `ORDER BY COALESCE(ts,''), segment, seg_line`, and `global_seq` is identity
   only — stated in the `_COPY_SQL` comment at `pgmirror.py:306`. Reloading one
   segment therefore cannot reorder anything.

---

## Requirements

### CI coverage

R1. The `pg` tier runs on every pull request and every push to `master`, against
a real PostgreSQL 17.

R2. The job needs no repository secret and adds no credential literal to the
repo. `tests/test_pg_repo_gates.py::test_no_credential_literal_anywhere_in_src_or_tests`
stays green with no allowlist entry.

R3. The job is proven non-vacuous before it is trusted: a deliberate mutation to
the schema must turn it red.

R4. The existing five jobs are unchanged. No service container is added to the
OS/Python matrix.

### Mirror freshness

R5. After a session appends events, a scheduled run brings the mirror current
without a human typing anything.

R6. Bringing it current reads only the segments that changed. A normal session
touches one segment; the cost is one file, not 37k events.

R7. There is exactly one loader. `rebuild` is a special case of `refresh`, not a
sibling of it, and both go through `scan_segments`.

R8. Incrementality is proven, not asserted. A test distinguishes a real
incremental refresh from a full rebuild wearing its name.

R9. The append path touches Postgres in no way at all. `import mindhive.events`
and a plain `hive append` import no driver under any backend value.

### The agent can act on what it is told

R10. The staleness notice names the repair command, and names that the reader
credential cannot run it, so the agent hands it to the operator instead of
looping.

R11. `hive pg refresh` exists, is cheap, and is documented in `README.md` and
`docs/session-legend.md`. `tests/test_docs_parity.py` enforces the first.

### Bookkeeping

R12. TODOS 19 and 20 are closed by this work and removed. TODOS 18 (`pgtools.py`
past the line cap) is untouched and stays.

R13. A `hive log` entry records the change before the work is called done.

---

## Design

### Part A — the `pg` tier on CI

A sixth job in `.github/workflows/ci.yml`, ubuntu-only. Service containers are a
Linux-runner feature; `windows-latest` cannot host one, so this deliberately does
not join the matrix.

```yaml
  pg-tests:
    name: pg tier (Postgres 17)
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: mindhive
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 10s
          --health-timeout 5s --health-retries 5
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e .[dev,pg]
      - name: Apply migrations
        env:
          MINDHIVE_PG_DSN_ADMIN: postgresql://postgres:postgres@localhost:5432/mindhive
        run: python -m mindhive pg migrate
      - name: Give the reader role a password
        env:
          PGPASSWORD: postgres
        run: |
          PW=$(openssl rand -hex 16)
          echo "::add-mask::$PW"
          psql -h localhost -U postgres -d mindhive \
            -c "ALTER ROLE mindhive_reader PASSWORD '$PW'"
          echo "MINDHIVE_PG_DSN_RO=postgresql://mindhive_reader:$PW@localhost:5432/mindhive" \
            >> "$GITHUB_ENV"
      - name: Run pg tier
        env:
          MINDHIVE_PG_DSN_ADMIN: postgresql://postgres:postgres@localhost:5432/mindhive
        run: python -m pytest -m pg -q
```

Three constraints the shape encodes:

* **Step order is load-bearing.** The password step must follow `pg migrate`.
  `mindhive_reader` does not exist until migration 003 runs, and 003 creates it
  `LOGIN` with no password on purpose — the comment at
  `003_roles_and_views.sql:26` says the operator sets it out of band "so no
  secret is ever" written down. CI is one such operator.
* **The password is generated, never stored.** `openssl rand -hex 16` plus
  `::add-mask::` keeps it out of the log, and nothing enters the repo, so R2
  holds with no allowlist edit.
* **`-m pg` beats `addopts`.** `-m` is last-one-wins; the `node-tests` job
  already relies on the same fact and its comment records it.

### Part B — `pgmirror.refresh`, and the deletion that pays for it

**New signature, replacing `rebuild` as the primitive:**

```python
def refresh(project=None, segments=None) -> dict:
    """Reload the named segments of one namespace. None means every segment.

    DELETE WHERE project = %s AND segment = ANY(%s), then COPY those rows, in
    ONE transaction. rebuild() is refresh(project) — the same function, not a
    sibling, so the two cannot number seg_line differently."""
```

`rebuild(project)` stays as a thin alias so `hive pg rebuild` and the existing
`tests/test_pgmirror.py` keep working.

**Change detection.** `mirror_state.ledger_fingerprint` currently stores
`{"segments": <count>, "bytes": <sum>}`. It becomes
`{"segments": {"<name>.jsonl": <bytes>, ...}, "bytes": <sum>}`. Comparing the
stored map against a fresh `stat()` of the directory names the changed segments
without opening a single file. A stored fingerprint whose `segments` is an
integer is the legacy shape and means *unknown* — which forces a full refresh,
never a skip. Failing open here is the whole safety property: an unrecognised
fingerprint must cost time, not correctness.

This is a JSONB column, so no SQL migration is needed. `pgstore._ledger_fingerprint`
(`src/mindhive/pgstore.py:595`) is the deliberate duplicate of
`pgmirror._fingerprint` and changes in the same commit; the test that pins the
two definitions to each other is what keeps them honest.

**Deletions.** `pgstore.mirror_one`, `pgstore._insert_one`,
`events._maybe_mirror_to_pg`, and its call site in `events.append_event`
(`src/mindhive/events.py:317`). After this, `append_event` writes one line to one
file and returns. The `MINDHIVE_EVENT_BACKEND` variable keeps its meaning for
reads: `_maybe_read_from_pg` is untouched.

**Trigger.** `autosync.run_all` (`src/mindhive/autosync.py:44-58`) already loops
per project on a scheduled task. Add, inside the existing `try`, after
`projector.project` / `sync.sync`:

```python
        # Best-effort: no admin DSN, no refresh, no noise. A mirror that did not
        # update is stale, and `freshness` already says so — it is never a reason
        # to fail a sync that otherwise succeeded.
```

The admin-DSN check comes first, so a machine without Postgres pays one
`os.environ.get`.

### Part C — the notice carries the remedy

`pgtools._stale_notices` gains the fix and the actor:

> the mirror is stale (`<reason>`); rows are still real but anything newer than
> the last rebuild is absent. To repair: the operator runs `hive pg refresh`
> (only changed segments are reloaded). You cannot run it yourself — it needs
> `MINDHIVE_PG_DSN_ADMIN`, and the reader role this tool connects with is
> read-only at the role level.

New CLI verb `hive pg refresh [--project P]`, alongside `rebuild`. `rebuild`
keeps its meaning (reload everything) and its place; `refresh` is the cheap one
and is what the notice names.

## Non-goals

* Real-time mirroring. Freshness becomes bounded by the autosync cadence, not
  zero. That is a deliberate trade for having one write path, and `freshness.stale`
  reports it honestly either way.
* Splitting `pgtools.py` (TODOS 18). Unchanged, still over the cap, still on the
  allowlist.
* TLS, real auth, or backups on the local scratch cluster. Out of scope and
  named in the risks.
* Any change to the five existing CI jobs.

## Test plan

Every test below must be RED before its change and GREEN after. Where a test
could pass against a wrong implementation, the mutant that must break it is
named — a regression test whose fixture does not reproduce the defect proves
nothing and reads exactly like one that does.

### `pg` tier (new, `tests/test_pg_live.py`)

| # | Test | Mutant that must break it |
|---|---|---|
| P1 | append one event to one segment, `refresh`, parity is ok | — |
| P2 | `global_seq` of rows in *untouched* segments is unchanged across a refresh | make `refresh` always pass `segments=None` (a rebuild wearing the name) |
| P3 | a segment whose earlier lines were rewritten converges (per-segment DELETE precedes its COPY) | drop the per-segment `DELETE`, keep the `COPY` — rows duplicate |
| P4 | a legacy integer fingerprint forces a full refresh | treat an unrecognised fingerprint as "nothing changed" |
| P5 | `refresh` on a namespace never built equals `rebuild` | — |
| P6 | refreshing one namespace leaves another namespace's rows alone | widen the `DELETE` to drop the `project` predicate |

### Default tier

| # | Test | Mutant that must break it |
|---|---|---|
| D1 | `_stale_notices` names `hive pg refresh` **and** says the reader cannot run it | delete either half of the sentence |
| D2 | `hive pg refresh` appears in `hive pg --help` and in `README.md` (`tests/test_docs_parity.py`) | — |
| D3 | `import mindhive.events` and one `hive append` import no `psycopg` under every backend value | — |
| D4 | nothing under `src/` writes through the reader credential (`test_pg_repo_gates.py`) | — |

### Stale tests this work invalidates

These assert the seam being deleted and must be rewritten or removed in the same
change, not left to rot:

* `tests/test_events_pg_seam.py::test_the_mirror_seam_calls_pgstore_under_dual`
* `…::test_the_mirror_seam_calls_pgstore_under_pg`
* `…::test_a_failing_mirror_cannot_break_an_append`
* `…::test_a_mirror_failure_is_counted_rather_than_swallowed`
* `…::test_the_backend_value_is_matched_case_insensitively` (mirror half only; the read half stays)
* `…::test_the_guards_are_the_first_statement_of_each_seam` (drops to one seam)

`test_the_file_backend_never_touches_pgstore_at_all` and
`test_an_append_under_the_file_backend_imports_no_driver` get *stronger* after
the deletion — they become true under every backend, not just `file`, and should
be rewritten to say so.

### Non-vacuity of the CI job itself (R3)

Before trusting the new job: on a scratch branch, delete one `GRANT` from
`003_roles_and_views.sql` and push. The job must go red on
`test_the_reader_cannot_reach_the_base_table_or_write_to_it`. If it stays green,
the job is not running the tests and nothing else it reports means anything.

## Sequencing

1. **Part A alone, as its own PR.** It touches one file, adds no source code,
   and is the change that makes Part B's tests mean something on CI. Prove R3
   on it before merging.
2. **Part B + Part C as a second PR.** Loader, deletions, autosync wiring, the
   notice, the CLI verb, docs, and the stale-test rewrite together — splitting
   them leaves the tree with a deleted seam and tests still asserting it.
3. `hive log major_change "…"` before either is called done (R13).
4. TODOS 19 and 20 removed in step 2 (R12).

Part A does not depend on Part B. Part B's `pg`-tier tests are unverified on CI
until Part A lands, which is the entire argument for that order.

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| A segment is rewritten in place with the same byte length; the fingerprint misses it | Low — the ledger is append-only and `hive verify` would already flag it | Parity still catches it on the next run; a fingerprint is a change *hint*, never the correctness argument |
| Postgres 17 image on CI drifts from the local 17.11 | Low | Pin `postgres:17`, not `postgres:latest`; migrations are version-checked by their sha256 |
| `autosync` gains a failure mode that quietly stops syncing | Low | The refresh sits inside the existing per-project `try`, which already records `{"project", "error"}` and continues |
| The scratch cluster (port 5433, trust auth, no TLS, no backup) gets mistaken for a deployment | Medium | Out of scope here; it is a proving cluster, and the CI job is ephemeral by construction |

## DX scorecard

Scores are for the mirror's tool surface as experienced by the agent, before and
after this plan. Evidence for each is named above; nothing here is a vibe.

| Dimension | Before | After | What moved |
|---|---|---|---|
| Credible (signal predicts reality) | 3/10 | 8/10 | CI executes the SQL; a green badge starts covering the mirror |
| Fight uncertainty (problem + cause + fix + actor) | 5/10 | 9/10 | The stale notice names the command and who can run it |
| Speed (time from ledger change to a correct answer) | 2/10 | 7/10 | Bounded by autosync cadence instead of by a human remembering |
| Usable (cost of the repair) | 4/10 | 8/10 | One changed segment instead of a 37k-event reload |
| Pit of Success (the right thing is the easy thing) | 4/10 | 8/10 | One loader; the wrong-but-consistent second write path no longer exists to be written |

Not moved by this plan, and honestly so: **Accessible** stays low — the whole
surface still needs a local PostgreSQL the operator installed by hand, and
nothing here changes that.

## As built — where the implementation departed from this plan

Written after the work, not before it. Every item is a place the plan was wrong
or incomplete; recording them here is what keeps the plan usable as a document of
record rather than a description of an intention.

**1. `refresh_changed`'s verification fold is NOT incremental (R6).** The plan
asked for an incremental refresh; the *write* is incremental and the *proof* is
not. `parity` runs at the end and reads both sides in full, because it is what
re-stamps `mirror_state.ledger_fingerprint` — skip it and the same segments are
recomputed as changed on every run, so the mirror reports itself stale forever,
which is the exact bug this plan exists to fix. It is also what keeps `parity_ok`
from describing rows that no longer exist.

**2. `hive pg rebuild` now verifies too — not in the plan at all.** Found by
running P2: `pgmirror.rebuild` writes no `mirror_state` row, so a bare rebuild
left `mirror_health` answering *"the mirror has never been built for this
namespace; run `hive pg rebuild`"* immediately after a successful rebuild. Both
false and a loop. Fixed at the operator surface (`cmd_rebuild` loads then runs
parity, and `_admin` gates the exit code on the `verified` verdict) rather than in
the library, because `pgmirror.rebuild` is composed by `refresh` and called by
tests that want the load alone.

**3. P3's fixture had to change the segment's byte count.** The first version
rewrote `"old"` → `"new"` — same length, so the size fingerprint saw nothing and
`refresh` correctly did nothing. That is a real limit of the design, not a bug in
it, and it is now stated outright by
`test_a_same_length_in_place_edit_is_invisible_to_the_fingerprint`, which also
shows `parity` still catches the edit. The fingerprint is a staleness heuristic
cheap enough to take on every tool call; `parity` is the integrity check.

**4. P5 needed the namespace forcibly forgotten first.** Namespaces here are
deterministic per test, so a second run of the file inherits the first run's
`mirror_state` and the "cold start" is not cold. Without `_forget`, P5 passed on
run one and failed on run two — the worst shape a test can have. The pg tier was
run twice end to end to prove it is now repeatable.

**5. The D4 gate is a write-SQL scan, not a `mirror_one` absence check.** A
deleted function is not a guarantee; the next person wanting a write seam will put
it in the module that already has a connection. The gate asserts the *shape*
instead — every write statement in `src/` lives in the one module holding the
owner DSN — and is anchored on `mindhive.<table>` so prose describing the rule is
not itself an offence.

**6. R3 is HALF proven.** Two mutants to `003_roles_and_views.sql`, each applied
to a throwaway database, each turn the tier red on the test that names the
property: deleting the reader's `GRANT SELECT` on the read-only view, and handing
the reader the base table the split exists to hide. So the assertions bite. What
is **not** proven is that the workflow YAML runs them — that needs a push and a
real run, and nothing was pushed. Until then the `pg-tests` job is unexercised.

### Verification actually performed

| Tier | Result |
|---|---|
| default (`pytest -q`) | 5373 passed, 3 skipped, 121 deselected |
| node (`-m node`) | 75 passed |
| browser (`-m browser`) | 14 passed |
| pg (`-m pg`, live PostgreSQL 17.11 on 127.0.0.1:5433) | 32 passed, twice |
| mutants | 8 of 8 caught (P2, P3, P4, P6, D1 ×2, D3, D4) |
| R3 mutants | 2 of 2 caught, on throwaway databases |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | codex not installed on this machine |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | NOT CLEARED | required before implementation |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | no UI surface in this plan |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | issues_open | mode DX POLISH; 3 gaps found (CI blindness, unscheduled freshness, notice without a remedy); scorecard 3.6/10 → 8.0/10 |

**VERDICT:** DX CLEARED — eng review required before implementation.

NO UNRESOLVED DECISIONS
