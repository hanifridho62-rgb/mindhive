# src/mindhive/mindnet/__init__.py
"""MindNetwork — the governing brain over every phase of building code here.

MindNetwork is **not a new methodology**. `docs/adr/0005-superpowers-as-default-
methodology-spine.md` already mandates the pipeline; it ships as injected
directive prose (`directives.py:72-101`), and prose drifts. MindNetwork converts
that already-decided methodology into a deterministic, ledger-backed, observable
state machine — reusing the spine, replacing its delivery mechanism.

Slices (see `docs/superpowers/specs/2026-07-25-mindnetwork-architecture-
decomposition.md`), built in this order:

- **MN-0** parallel output durability — shipped (`runlog.py`, PR #79)
- **MN-1** the phase spine (`phases.py`, `controller.py`, `inject.py`)
- **MN-5** the cat status board (rendered in `swarm_page.py`)
- **MN-3** method memory keyed on task type
- **MN-2** the hierarchical task graph (`tasks.py`)
- **MN-4** teeth: compatibility gate, PreToolUse denies, PR-green

Standing constraints every module here inherits: stdlib-only; folds are pure,
deterministic, and **total** (skip malformed rows, never raise — the ledger
holds history written by every past version); nothing may break a prompt, a
hook, or the dashboard (fail-open); the append-only ledger is the source of
truth and every sidecar is a rebuildable projection.
"""
