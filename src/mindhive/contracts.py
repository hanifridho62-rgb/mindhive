# SPDX-License-Identifier: Apache-2.0
# src/mindhive/contracts.py
"""The registry payload contracts (DATA_MODEL.md §6.2), carved out of
`events.py` VERBATIM (blueprint P0b) so that module stays under the
1000-line cap `tests/test_file_size_cap.py` holds it to. Nothing else
changed: `events` re-exports both names, so `events._PAYLOAD_CONTRACTS`
and `events.validate_typed_payload` are the SAME objects, every caller
and every test keeps its spelling, and the owning modules named by the
table are still resolved relative to the `mindhive` package.

A leaf: `importlib` is its only import, so `import mindhive.contracts`
loads nothing else of the package (`tests/test_contracts_reexport.py`
pins it from a child interpreter). The owner modules are imported at
the one append that needs them, never here.
"""
import importlib

# --- registry payload contracts ----------------------------------------------
# DATA_MODEL.md §6.2: the store stays type-agnostic (append_event does NOT call
# this — every internal writer already builds a valid payload), and each type's
# contract lives in its owning module, enforced at the surfaces that accept an
# arbitrary type+payload from the user. There are TWO such surfaces — `hive
# append` and the dashboard's POST /api/append — so the dispatch lives here,
# beside the type registry, instead of being copied into both. Copying is how
# they drifted: the CLI enforced four contracts and the endpoint enforced none.
#
# event type -> (contract module, corrective hint carried in the rejection)
_PAYLOAD_CONTRACTS = {
    "code_change": (".changelog", 'hive log <category> "..."'),
    "sop": (".sop", "hive sop add|update|delete …"),
    "roadmap": (".roadmap",
                "hive roadmap set|step|interrupt|resume|complete …"),
    "autoresume": (".autoresume", "hive autoresume schedule|fire|cancel …"),
    # `state_set` has NO subcommand -- there is no `hive state --set`, and
    # `grep -rn state_set src/mindhive/commands/` finds nothing -- so the raw
    # append below is not one writer among several, it is the ONLY one. That is
    # also why this row was missing for so long: every other contract was
    # registered by the module that owned the surface, and this surface owned
    # no module until `state.py`. What it guards is the top of STATE.md, which
    # `sync` embeds in the auto-loaded MEMORY.md -- the highest-trust text in
    # the store. Following `mindnet_task`, the hint names only what ships.
    "state_set": (".state",
                  "hive append state_set '{\"task\": \"…\", "
                  "\"open_questions\": []}' --json — keys: task, verified, "
                  "next_step, open_questions, deferred; every one optional. "
                  "There is no subcommand; this raw append is the only writer"),
    # The hint names ONLY what ships. `mindnet_task` has five actions
    # (tasks.ACTIONS) but exactly one CLI producer — `hive mindnet declare`;
    # `status`, `depend`, `reparent` and `evidence` have no subcommand yet and
    # are reachable only through this same raw-append surface. Advertising them
    # as commands sent an operator whose append was just rejected off to run
    # four things that do not exist, which is a worse failure than the original
    # rejection. When a subcommand lands, add it here in the same change-set.
    "mindnet_task": (".mindnet.tasks",
                     "hive mindnet declare … — the status/depend/reparent/"
                     "evidence actions have no subcommand yet; append them as "
                     "raw JSON with a valid `action` field"),
    # mindnet_phase points at the APPEND-SURFACE half of a two-validator
    # contract: phases.validate_payload refuses any payload carrying
    # `verified_by`, so this surface cannot self-attest a machine verification
    # nothing performed. The writer's shape (which REQUIRES verified_by) is
    # phases.validate_stored, called by the CLI just before append_event —
    # registering that one here instead would invert the honesty rule.
    "mindnet_phase": (".mindnet.phases",
                      "hive mindnet declare|enter|satisfy|fall-back …"),
    # Same two-validator split as mindnet_phase, and for the same reason:
    # `verify.validate_payload` refuses any payload carrying `verified_by`, so
    # this surface cannot stamp a mechanical run that never happened. The
    # writer's shape (`verify.validate_stored`, which REQUIRES the stamp) is
    # applied inside `run_and_record` just before append_event. Registering
    # that one here would let a hand-written append forge the evidence the
    # premise gate reads.
    "verification": (".verify", "hive selftest — it records the run"),
    # The replenishment audit row. Registered because the round's own state is
    # folded back out of these events: a hand-appended row with a bogus
    # `outcome` or a missing `fingerprint` would silently corrupt the dedup set
    # and the dry-round counter, and the damage would only show up as work that
    # stopped being proposed.
    "work_injected": (".replenish", "hive replenish run --session <id>"),
    # All three job types share ONE contract, because this surface sees only the
    # payload and the three rows are paired by the two fields it enforces
    # (`job_id`, `kind`). The per-type requirement — a `job_finished` MUST carry
    # an outcome, or a finished job reads as running forever — is the writer's
    # half, `jobs.validate_stored`, the same two-validator split `verification`
    # and `mindnet_phase` use above. The hint names no command because there is
    # none: these rows are written by the dashboard as it runs the work, and a
    # hand-appended one is almost certainly a mistake.
    "job_started": (".jobs", "the dashboard writes these as it runs a job"),
    "job_finished": (".jobs", "the dashboard writes these as it runs a job"),
    "job_abandoned": (".jobs", "written only by the boot seal, jobs.seal_orphans"),
    # Participatory consensus. Two types, two submodules — the same split
    # `mindnet_task` / `mindnet_phase` use, because this table calls one
    # `validate_payload` per module and cannot pass the type down.
    #
    # Both are gated rather than left free-text because the round's own meaning
    # is folded back out of them. A contribution with no `subject` breaks the
    # per-subject weight normalisation for everyone else in the frame, and a
    # `consensus_record` whose inputs carry no weight or reason is exactly the
    # untraceable conclusion the PRD names as a failure condition — permanent,
    # since this store is append-only, and authoritative-looking forever after.
    "contribution": (".consensus.contributions",
                     "hive consensus contribute --frame F --kind K "
                     "--subject S --value V"),
    "consensus_record": (".consensus.records",
                         "hive consensus fold --frame F — the fold is the only "
                         "writer, so a hand-appended record is almost "
                         "certainly a mistake"),
}


def validate_typed_payload(event_type, payload) -> None:
    """Enforce *event_type*'s registry contract at an arbitrary-payload append
    surface. Raises ``ValueError`` whose message already carries the corrective
    hint, so both surfaces report the same thing. A type with no registered
    contract — every free-text memo and every telemetry type — is accepted
    unchanged, which keeps this a gate on the registry, not on the store."""
    contract = _PAYLOAD_CONTRACTS.get(event_type)
    if contract is None:
        return
    module_name, hint = contract
    # Deferred import: keeps `hive` startup cheap and the import side effects
    # of five modules scoped to the one append that actually needs them.
    mod = importlib.import_module(module_name, __package__)
    try:
        mod.validate_payload(payload)
    except ValueError as e:
        raise ValueError(f"{e} (use: {hint})") from e

__all__ = ["validate_typed_payload"]
