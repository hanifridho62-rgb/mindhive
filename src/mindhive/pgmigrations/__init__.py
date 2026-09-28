# SPDX-License-Identifier: Apache-2.0
# src/mindhive/pgmigrations/__init__.py
"""The mirror's DDL, as package data.

The .sql files beside this module are the schema of the Postgres read mirror.
They are applied in LEXICOGRAPHIC filename order by `pgmirror.apply_migrations`
and read through `importlib.resources`, never through a path computed from
`__file__` — the difference matters for the frozen build, where `__file__` is
inside a bundle.

Two rules the files themselves repeat in their headers:

* **Every statement is re-runnable.** `IF NOT EXISTS` throughout, `CREATE OR
  REPLACE` for views, a `DO` block for roles (`CREATE ROLE` has no `IF NOT
  EXISTS` in any Postgres version). Applying twice must leave the same catalog.
* **An applied file is immutable.** `apply_migrations` records each filename
  with the sha256 of its bytes and REFUSES to continue when a recorded file's
  content has changed — silently re-running it and silently skipping it are
  both worse than stopping. A schema change is a NEW file, always.
"""
