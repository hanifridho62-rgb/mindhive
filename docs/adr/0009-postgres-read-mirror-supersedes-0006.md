# ADR 0009 — A Postgres read *mirror* for AI querying, superseding 0006's canonical-substrate cutover

Date: 2026-08-18
Status: Accepted — Phase A partially built (seam, schema, tools, MCP surface, CLI);
the server-touching half is blocked on PostgreSQL 17 not existing on this machine

**Supersedes:** `0006-postgres-canonical-event-substrate-staged-cutover.md`.
0006 is superseded in full, not amended: its Phase B (Postgres becomes the
*writer* and the files are retired) is withdrawn, and its Phase A is replaced by
a design that shares the goal and almost none of the mechanism.

> **Note on 0006's own status in git.** At the time of writing, both
> `docs/adr/0006-postgres-canonical-event-substrate-staged-cutover.md` and
> `docs/superpowers/plans/2026-06-26-phase-a-postgres-event-backbone.md` are
> **untracked** in the main checkout and are absent from this branch's tree
> altogether. A superseding ADR that points at a file no clone contains is a
> dangling reference, so 0006's load-bearing claims are quoted below rather than
> cited. Whether 0006 and its plan should be committed is a decision for the
> repo owner; this ADR does not take it.

## Context

MIND HIVE stores events as per-session JSONL segments under
`~/.mindhive/<project>/events/`. Answering "what happened in session X" today
means grepping projections or folding ~20 MB of JSONL. That is the problem both
ADRs are trying to solve, and it is real.

0006 proposed solving it by making Postgres **canonical**: Phase A moves the
read/`verify` path onto Postgres behind a parity gate, Phase B moves the writes
and retires the files. Three of the premises that decision rested on have since
turned out to be false, unavailable, or no longer applicable.

### 1. 0006's ordering invariant is FALSE, and the counter-example is in the live store

0006's Consequences section states that inserting in file-line order plus a
shared `ORDER BY ts, session_id, id` "reproduces the file ordering
`(ts, seg_filename, line)` exactly — the parity gate depends on it."

It does not. `events.read_all` sorts on `(ev.ts or "", seg.name, line_index)` —
the **segment filename**, which is `<session_id>.jsonl`. Ordering by the bare
`session_id` is a different order, because the separator that follows the id
differs: `-` is `0x2D` and `.` is `0x2E`, so `-` sorts *below* `.`.

The divergence is not theoretical. The live store contains both
`verify.jsonl` and `verify-e2e-001.jsonl`:

```
by session_id : ['verify', 'verify-e2e-001']
by filename   : ['verify-e2e-001.jsonl', 'verify.jsonl']
```

Two sessions, opposite orders. Since `chain.compute_chain` folds `read_all()` in
order, the two orderings produce **different chain heads** — so 0006's parity
gate, built on the invariant it names, would have failed against its own store
and the reason would have looked like data corruption rather than a sort key.
A second correction rides along: `events.read_all` sorts on `ev.ts or ""`, so a
NULL `ts` sorts **first** in Python, while Postgres puts NULLs **last** in an
ascending sort by default. A NULL `ts` is explicitly legal (the ledger checks key
presence only), so a bare `ORDER BY ts` disagrees with the file reader on a row
shape the schema deliberately admits.

### 2. 0006 assumed a CC-v3 host that this repo deliberately does not depend on

0006's decision is written around `scripts.core.event_write`,
`event_store.append_event`, `event_adapter.row_to_event` and an `asyncio.run()`
bridge — all of them in the **continuous_claude / CC-v3 repository**, not this
one. The seam it produced (`events._maybe_mirror_to_pg`,
`events._maybe_read_from_pg`) imported `scripts.core` inside a bare `except`.
That module has never been importable from this process, so the seam failed on
every call and swallowed the failure: flipping `MINDHIVE_EVENT_BACKEND` to
`dual` or `pg` did **nothing at all**, silently, for as long as the seam existed.

MIND HIVE is a standalone distribution with `dependencies = []`. Making its event
substrate depend on another repository's async layer is a coupling this project
has since decided against everywhere else.

### 3. 0006 rejected a synchronous driver for a reason that no longer applies

0006 rejected "a second synchronous driver (psycopg) for MIND HIVE's read path"
on the grounds that it "duplicates the `SELECT … ORDER BY` and row mapping in a
second place that must stay byte-identical or parity silently breaks."

That objection assumed the async CC-v3 store would remain the *first* place. It
will not: this design has no async store, no CC-v3 dependency, and exactly one
`SELECT … ORDER BY` (`pgstore.READ_ALL_SQL`) with one row-mapping function
(`pgstore._rows_to_events`). There is no second place to drift from. Meanwhile
`asyncio.run()` on a synchronous CLI is a bridge whose one rule ("never call it
from inside a running loop") the dashboard would violate the moment it read from
Postgres — a constraint that has to be remembered forever, in exchange for
reusing code from a repository this one does not ship with.

## Decision

**Postgres is a read MIRROR, never the canonical store, and never the writer.**

- The **file ledger stays canonical and stays the sole writer.**
  `MINDHIVE_EVENT_BACKEND` stays `file`. Nothing in this change flips it, and a
  repo test asserts that nothing in `src/` writes it.
- The mirror exists for exactly one purpose: **giving an AI a query surface**,
  through four MCP tools (`query_database`, `get_recent_events`,
  `search_events_by_payload`, `get_session_summary`) and a `hive pg` CLI group
  that dispatches through the same registry.
- The two dead seams are **repaired against `pgstore`**, in this package. The
  `MINDHIVE_EVENT_BACKEND` guards stay byte-identical and return before any
  import, so the default `file` path costs one `os.environ.get` and one
  comparison and `import mindhive.events` never pays for `psycopg`.
- The ordering contract is `(COALESCE(ts,''), segment, seg_line)` — the
  **segment filename**, never `session_id`, and `COALESCE` so NULL timestamps
  land where Python puts them. This is the correction of 0006's invariant, and
  it is written into the SQL, the tool descriptions, and two tests.

  > Amended by migration 007 (`007_append_seq.sql`): the contract is now
  > `(COALESCE(ts,''), COALESCE(append_seq,-1), segment, seg_line)`. The
  > argument above about chain heads still holds for the parity gate:
  > `pgmirror.parity` folds `chain.compute_chain` over both sides in that order,
  > which is why 007 changed `pgstore.READ_ALL_SQL` together with the file
  > readers. (Separately, and before 007, the `chain.json` checkpoint moved to
  > an append-order fold, `chain._scan`, in c41b469.) See DATA_MODEL.md, "The
  > ordering contract, and the invariant it corrects".
- A **synchronous** driver (`psycopg` 3), behind an optional extra. No async, no
  bridge, no loop-safety rule to remember.
- **Phase B is withdrawn.** There is no plan to make Postgres the writer. If one
  is ever wanted it is a new decision with a new ADR, and it would have to start
  by re-measuring `/api/overview`: `dashboard_memo._ledger_fingerprint` returns
  `None` for any non-`file` backend, so flipping the backend disables the
  overview memo — a measured **726 ms → 6.6 ms** regression on every poll of a
  dashboard that polls.

## Consequences

- **Capture never depends on a database**, which was 0006's own stated
  requirement and is now structural rather than staged: the file line is on disk
  before the mirror is consulted, `mirror_one` cannot raise, and a one-strike
  circuit breaker means 50 appends against a dead host cost one connect timeout
  rather than fifty.
- **The parity gate keeps its teeth.** `pgstore.read_all_strict` has no file
  fallback, deliberately: a strict reader that degraded to files would make the
  gate compare the file ledger with itself and pass while proving nothing.
- **Two credentials, split by module.** `pgstore`/`pgtools` see only
  `MINDHIVE_PG_DSN_RO`; `pgmirror` is the only module that reads
  `MINDHIVE_PG_DSN_ADMIN`, and no module on the AI path imports it. Tests assert
  both, and that `enforce.py`, `sopgate.py`, `rotation/hook.py` and
  `mindnet/enforcer.py` reach none of the cluster.
- **The base install stays dependency-free.** `psycopg` and `mcp` are extras
  (`.[pg]`, `.[mcp]`), guarded the way `vectors.py` guards chromadb. Everything
  answers structurally with neither installed, which is the state of this
  machine and of CI.
- **A residual this ADR does not fix:** the mirror adds no new *disclosure* —
  the file ledger already holds prompts and responses in plaintext — but it adds
  new **queryability**. Bulk-searching the ledger for a secret becomes one tool
  call instead of a grep over 920 segments. Naming it is the mitigation
  available at this layer.
- **Unverified until PostgreSQL 17 exists here:** every statement in
  `pgstore`, `pgtools` and `pgmigrations/` is written but has never been parsed
  by a server, and `pgmirror`'s server-touching entry points deliberately raise
  rather than report a success they did not achieve.

## Alternatives considered

- **Amend 0006 instead of superseding it.** Rejected: its ordering invariant,
  its host assumption and its rejection of a sync driver are the three load-
  bearing parts of the decision, and a document with all three replaced is a
  different decision wearing an old number.
- **Keep Phase B as a stated future.** Rejected: a documented intention to
  retire the file store makes every reader treat the mirror as temporary, and it
  would have to be paid for in a measured dashboard regression that nobody has
  agreed to.
- **Order the mirror by `session_id` and change `events.read_all` to match.**
  Rejected: `read_all`'s order is what the hash chain has always been computed
  over. Changing it retires every chain head ever recorded, to make a mirror's
  SQL slightly shorter.
