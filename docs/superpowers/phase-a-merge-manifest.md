# Phase A — Merge Manifest (cc-v3 → MIND HIVE)

Tracks which Phase A artifacts were merged into the MIND HIVE project (this repo,
`hanifridho62-rgb/Lintester-project`) and which remain in the CC-v3 host repo
(`Continuous-Claude-v3`), with the reason for each.

**Why most code is NOT merged:** the Phase A executable code imports CC-v3 host modules
(`scripts.core.db.event_store`, `scripts.core.db.postgres_pool`, `scripts.core.event_project`,
`scripts.core.event_adapter`). It cannot compile inside the pure `mindhive` package — and per
[ADR 0006](../adr/0006-postgres-canonical-event-substrate-staged-cutover.md) it is *meant* to live
in the host. The `mindhive` package is imported as a library and was deliberately left untouched.

**CC-v3 source commits** (branch `docs/mindhive-postgres-cutover-roadmap`): `25f41aa`, `22e0ba4`,
`18ddd99`, `d48411f`.

## Status legend

- **MERGED** — added to this MIND HIVE repo by this change.
- **ALREADY PRESENT** — already exists in this repo (user-authored), no action needed.
- **NOT MERGED (cc-v3)** — stays in the CC-v3 host repo by design; tracked here for traceability.

## Manifest

| Artifact | Type | Status | Location |
|----------|------|--------|----------|
| Phase A implementation & parity report | doc | **MERGED** | `docs/superpowers/phase-a-postgres-event-backbone-report.md` |
| This merge manifest | doc | **MERGED** | `docs/superpowers/phase-a-merge-manifest.md` |
| Phase A implementation plan | doc | **ALREADY PRESENT** | `docs/superpowers/plans/2026-06-26-phase-a-postgres-event-backbone.md` |
| ADR 0006 (Postgres canonical substrate, staged cutover) | doc | **ALREADY PRESENT** | `docs/adr/0006-postgres-canonical-event-substrate-staged-cutover.md` |
| `event_migrate.py` (`list_file_projects`, `migrate_project`, `verify_parity`, `state_from_pg`) | code | **NOT MERGED (cc-v3)** | `Continuous-Claude-v3/opc/scripts/core/event_migrate.py` |
| `event_cli.py` migrate / migrate-all / parity subcommands | code | **NOT MERGED (cc-v3)** | `Continuous-Claude-v3/opc/scripts/core/event_cli.py` |
| `test_event_migrate.py` (enumeration + dbtest parity/state/post-project) | test | **NOT MERGED (cc-v3)** | `Continuous-Claude-v3/opc/tests/test_event_migrate.py` |
| `test_event_cli.py` parser test | test | **NOT MERGED (cc-v3)** | `Continuous-Claude-v3/opc/tests/test_event_cli.py` |
| CC-v3 migration report (per-project parity table, live-run) | doc | **NOT MERGED (cc-v3)** | `Continuous-Claude-v3/opc/docs/phase-a-migration-report.md` |

## Reason detail for NOT MERGED items

| Artifact | Reason it stays in cc-v3 |
|----------|--------------------------|
| `event_migrate.py` | Imports `scripts.core.db.event_store` (cc-v3) at 3 call sites; the Postgres store and pool live in cc-v3. Architecturally the migrator belongs in the host per ADR 0006. |
| `event_cli.py` changes | Extend the cc-v3 `event_cli` argparse surface; depend on `event_project` / `event_graph_export` (cc-v3). |
| `test_event_migrate.py` / `test_event_cli.py` | Import `scripts.core.*`; gated by the cc-v3 `dbtest` marker + `pytest.importorskip("asyncpg")`. |
| CC-v3 migration report | The per-project live-run parity table is produced by running `migrate-all` against the CC-v3 Postgres instance; its home is the host repo. The MERGED report above is the MIND HIVE-side summary. |

## Status

- **U7 live run — DONE (2026-06-26).** `migrate-all` ran against the real `~/.mindhive` store via a
  userspace embedded Postgres (`pgserver`, no sudo, WSL): 11 projects, 7,399 events, every project
  `file_head == pg_head`. Per-project table is in the report. To re-run against the project's own
  `continuous-claude-postgres`, see the report's "Re-running the live migration" section.
- If a `mindhive`-native, host-independent migrator is ever wanted, it would be new work (a pure
  port that does not import `scripts.core.*`) — out of scope for Phase A.
