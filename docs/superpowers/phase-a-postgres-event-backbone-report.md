# Phase A — Postgres Event Backbone: Implementation & Parity Report

**Status:** Code complete and verified; live `migrate-all` run **pending** (Postgres unavailable in the build environment).

This is the MIND HIVE project's record of Phase A of the MindHive→Postgres staged cutover
(see [`docs/adr/0006`](../adr/0006-postgres-canonical-event-substrate-staged-cutover.md) and
[`docs/superpowers/plans/2026-06-26-phase-a-postgres-event-backbone.md`](plans/2026-06-26-phase-a-postgres-event-backbone.md)).

Phase A makes Postgres the canonical **read/verify** path for MIND HIVE events while the file
store remains the live writer. The executable code lives in the **CC-v3 host repo**
(`Continuous-Claude-v3/opc/`), because it depends on that repo's async `event_store`,
`postgres_pool`, and `event_project`. The `mindhive` package is imported as a pure library and
was **not** modified. This report and the accompanying
[merge manifest](phase-a-merge-manifest.md) are what is brought into the MIND HIVE project.

## What shipped (in the CC-v3 host repo)

| Unit | Capability | File (cc-v3) |
|------|------------|--------------|
| U1 | `list_file_projects` — enumerate file-store projects with `events/*.jsonl` | `opc/scripts/core/event_migrate.py` |
| U2 | `migrate_project` — truncate + insert in `events.read_all()` order, preserving `ts`, idempotent | same |
| U3 | `verify_parity` — file chain head == Postgres chain head (golden gate) | same |
| U4 | `state_from_pg` — STATE.md rendered from Postgres-sourced events (asyncio bridge) | same |
| U5 | `migrate` / `migrate-all` / `parity` subcommands | `opc/scripts/core/event_cli.py` |
| U6 | `event_project.verify` ok after migrate→project (integration test) | `opc/tests/test_event_migrate.py` |
| U7 | Live migration + parity report (operational) | pending Postgres |

CC-v3 commits: `25f41aa` (migrator + parity gate), `22e0ba4` (CLI subcommands),
`18ddd99` (report), `d48411f` (review advisories).

## The parity invariant (why ordering is sacred)

File `events.read_all` orders by `(ts, seg_filename, line)`; Postgres `read_all` orders by
`(ts, session_id, id)`. Because `seg_filename == session_id + ".jsonl"`, the first two keys agree;
inserting in `read_all()` order makes the `BIGSERIAL id` rise with the line index, so the third
agrees too. **Therefore inserts must always run in `read_all()` order, serially — never sorted
differently, never parallelized within a project.** This is the load-bearing constraint behind
the golden parity gate.

## Verification performed

- **Non-DB tests (green):** enumeration (`list_file_projects` + missing-home) and the
  `migrate`/`migrate-all`/`parity` argparse surface — `5 passed` under the project test runner.
- **DB tests (`dbtest`, gated):** migrate-in-order, parity, STATE-from-PG, and post-project
  verify — **skipped** in the build environment (`asyncpg`/Postgres absent). They run when the
  `continuous-claude-postgres` container is up and `DATABASE_URL` is set.
- **DB-path logic proven without Postgres:** an in-memory store mimicking Postgres'
  `ORDER BY (ts, session_id, id)` was driven through `migrate_project` → `verify_parity` →
  `state_from_pg` against the **real** `mindhive.chain` and `mindhive.projector`. Insert order
  reproduced file order (`[session_start, first, second]`), heads matched (64-hex), STATE.md was
  byte-identical, and a **negative control** (deliberately reordering the Postgres rows) correctly
  drove `verify_parity` to `ok: false` — confirming the gate detects reordering rather than
  rubber-stamping.

## Review notes (advisory — Phase A accepts these; revisit for Phase B)

Verdict: **Ready to merge**, no correctness/security defects. Three advisory items, all inherent
to the "file store is canonical" design:

1. **`migrate_project` is not atomic** — truncate then N independent inserts; a mid-run crash
   leaves Postgres partially loaded. Safe in Phase A (re-run rebuilds; `verify_parity` reports the
   mismatch). For the Phase B write-cutover, wrap truncate + inserts in one transaction.
2. **Global `MINDHIVE_HOME` mutation** as a side effect (matches plan KTD5; how
   `mindhive.events.read_all` resolves the root). Harmless for short-lived CLI/tests.
3. **O(N) per-event inserts** — fine for a one-time migration; if batched later, preserve
   `read_all()` order so the `BIGSERIAL id` still rises with line index. Never parallelize.

## Pending: live run (requires Postgres up)

From the CC-v3 `opc/` dir, once the DB is reachable:

```bash
PYTHONPATH=. uv run python -m scripts.core.event_cli migrate-all   # expect {"ok": true, ...}
PYTHONPATH=. uv run pytest tests/test_event_migrate.py -v          # dbtest assertions execute
```

If any project reports `ok: false`, STOP and diagnose (`parity --project <name>`, `hive verify`).
Fix the root cause — never alter the hash. Record per-project `count`/`head` in the CC-v3
`opc/docs/phase-a-migration-report.md`.

## Constraints honored

No money (no embeddings, no API). No memory loss (`migrate-all` covers every project; parity gate
per project). Tamper-evidence preserved (unchanged `mindhive.chain`). V17 safety: `fabio-hft-v18`
migrates as plain rows, no embeddings; nothing touches `LIVE_LOCK`.
