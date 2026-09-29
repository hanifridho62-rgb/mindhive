# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/__init__.py
"""The `hive ci` group (blueprint P1f/P1g; MEB CI-01, CI-02).

Imports NOTHING at package load, the same rule `core/__init__.py` keeps: every
`hive --help` pays for a package `__init__` that reaches for the orchestrator,
and nothing here is on a hot path that would earn it. `cli.py` self-registers
the group's parser and imports its own dependencies inside the handlers.

P1f ships `breaker`, the operator's only exit from a QUARANTINED integration
circuit. `runner.py` (the native CI runner) lands on the same group in P1g.
"""

__all__: tuple = ()
