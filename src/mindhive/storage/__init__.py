# SPDX-License-Identifier: Apache-2.0
"""The storage service-provider interface (blueprint P1i, MEB STO-01/STO-02).

Four drivers behind two ABCs: `LocalEventStore` over the JSONL ledger,
`PostgresEventStore` over the mirror, `LocalVectorIndex` over chroma and
`PgVectorIndex` over pgvector-or-BYTEA. `EventReplayMigrationEngine` moves
the first to the second and verifies the hash chain on the way.

**This module imports nothing**, and that is the design rather than an
oversight. `mindhive.storage.postgres_driver` reaches `pgmirror`, which holds
the owner DSN; `mindhive.storage.local_vector` reaches chromadb and a 90 MB
encoder. A package `__init__` that imported either would put both on the
import graph of anything that so much as names the package -- including
`hive --help`, which `tests/test_cli_startup.py` measures. Import the driver
you want by its own module path.
"""

__all__: list = []
