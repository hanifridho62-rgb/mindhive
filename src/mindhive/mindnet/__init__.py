# SPDX-License-Identifier: Apache-2.0
# src/mindhive/mindnet/__init__.py
"""MindNetwork — the governing brain over every phase of building code here.

MindNetwork is **not a new methodology**. `docs/adr/0005-superpowers-as-default-
methodology-spine.md` already mandates the pipeline; it ships as injected
directive prose (`directives.py:72-101`), and prose drifts. MindNetwork converts
that already-decided methodology into a deterministic, ledger-backed, observable
state machine — reusing the spine, replacing its delivery mechanism.

Slices (see `docs/superpowers/specs/2026-07-25-mindnetwork-architecture-
decomposition.md`). AS BUILT, which is not the decomposition's planned order —
MN-2 landed before MN-1 because the phase spine needs a task to hang a phase
on, and that reordering is recorded as amendment A5 in
`docs/superpowers/specs/2026-07-25-mn1-phase-spine-design.md`:

- **MN-0** parallel output durability — shipped (`runlog.py`, PR #79)
- **MN-2** the hierarchical task graph — shipped (`tasks.py`)
- **MN-1** the phase spine — shipped: `phases.py` (the legend and the
  two-surface evidence contract), `controller.py` (the fold and the transition
  table), `sidecar.py` (the hook-cheap projection and its freshness stamp),
  `inject.py` (unconditional phase-aware injection), `executor.py` (the
  validation runner behind a fail-closed gate), `telemetry.py` (the
  `mindnet_activity` faculty feed MN-5 will render)
- **MN-5** the cat status board (rendered in `swarm_page.py`)
- **MN-3** method memory keyed on task type
- **MN-4** teeth: compatibility gate, PreToolUse denies, pr-green

Note the spelling in that last line. `pr-green` is BOTH the terminal phase in
`tasks.PHASES` and a canonical faculty in `telemetry.FACULTIES`; invariant 10
says the one legend string is used verbatim everywhere, so no layer here
re-cases it or swaps the hyphen for an underscore. A consumer that needs to
tell the two apart must key on membership in the relevant tuple — never on how
the string looks, because for this one name the two tuples agree.

Standing constraints every module here inherits: stdlib-only; folds are pure,
deterministic, and **total** (skip malformed rows, never raise — the ledger
holds history written by every past version); nothing may break a prompt, a
hook, or the dashboard (fail-open); the append-only ledger is the source of
truth and every sidecar is a rebuildable projection.
"""
