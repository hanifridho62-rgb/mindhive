# src/mindhive/events.py
import contextlib
import importlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from . import paths

SEMANTIC_TYPES = {
    "decision", "experiment_result", "caveat",
    "next_step", "open_question", "state_set",
    # Mandatory code change-log: human-written, categorized explanation of a
    # change-set touching mindhive's own code (see changelog.py). Semantic so
    # it rides the LEDGER, hash chain, and Memory view like decisions do.
    "code_change",
    # Self-Reliant learn-mode report (a PRD the researcher wrote — see
    # research._record_learning). Registered here so the write is accepted:
    # before this entry, append_event rejected the type and _log's crash guard
    # swallowed the error, silently dropping every PRD from the ledger.
    "learning",
    # Managed standard operating procedure lifecycle (add/update/delete — see
    # sop.py). Semantic so the registry rides the LEDGER, hash chain, Memory
    # view, and keyword recall; the current SOP set is a deterministic fold
    # projected into STATE.md / MEMORY.md.
    "sop",
    # Roadmap registry lifecycle (set/step/interrupt/resume/complete — see
    # roadmap.py): the original ask, the plan built from it, and per-step
    # progress. Semantic so plans are recallable and chain-verified; an
    # interrupted plan projects into STATE.md as the out-of-usage marker that
    # preserves creation context for the next session.
    "roadmap",
    # MindNetwork MN-2 task graph lifecycle (declare/status/depend/reparent/
    # evidence — see mindnet/tasks.py; the phase axis is mindnet_phase's, and
    # ONLY its): the hierarchical task tree the
    # flat roadmap cannot express (no parent_id, 50-step cap, wholesale-replace
    # only). Semantic so the graph is recallable and chain-verified; the current
    # graph is a deterministic fold that REFUSES cycle-closing edges rather than
    # merely reporting them.
    "mindnet_task",
    # MindNetwork MN-1 phase spine lifecycle (see mindnet/controller.py): which
    # phase a task is in and the evidence satisfying each exit criterion. Every
    # satisfaction records verified_by (python | agent | human) so a claim
    # nobody machine-checked can never fold as if it had been.
    "mindnet_phase",
    # Participatory consensus (see consensus/): one framed input from one
    # subject, and the weighted fold over a window of them. Semantic so both
    # ride the LEDGER and the hash chain, and so `hive pg embed` indexes them —
    # a board-level number a reader can search for by meaning is the point, and
    # a deterministic type would be invisible to that.
    #
    # ONE type for every population and every instrument: `kind` inside the
    # payload is the discriminant, so a youth anxiety sample and an executive's
    # strategic input differ there and nowhere else. A second type per
    # population would put the demographic into the schema.
    "contribution",
    "consensus_record",
}
DETERMINISTIC_TYPES = {
    "file_touched", "command_run", "git_diff",
    "session_start", "session_end", "compaction",
    "plugin_catalog", "user_prompt", "assistant_response",
    "loop_run_start", "loop_iteration", "loop_run_end", "loop_run_dismissed",
    "tool_failed", "rule_revised",
    "rule_activated", "rule_promoted", "rule_demoted", "rule_expired",
    "rule_enforced", "rule_rerouted", "trigger_trained",
    "invoke_requested", "invoke_result", "skill_used",
    # Prompt-quality gate directed a skill (promptgate fired with trained rules
    # on an underspecified prompt). Payload {skills: [{name, plugin?, measured?}]}.
    # Oversee joins it to a later same-session skill_used to show the honest
    # causal "why" ("directed by prompt-gate") — see oversee.skill_trails.
    "skill_directed",
    "memory_consolidated",
    # Prompt enrichment (Oversee, see enrich.py): an enrichment act the user
    # actually used (copied/recorded) and the default-on toggle's audit
    # trail. Telemetry like trigger_trained — the settings sidecar is the
    # source of truth, these events are the ledger evidence.
    "prompt_enriched", "enrich_toggled",
    # Auto-resume delivery lifecycle (scheduled/fired/cancelled — see
    # autoresume.py): after a usage-limit interruption, the roadmap marker
    # is scheduled to re-enter Claude once the ~5h window reopens.
    # Telemetry — the roadmap events stay the semantic record; these are the
    # delivery audit trail (when it was due, whether it fired, what came
    # back).
    "autoresume",
    # MindNetwork MN-5 cat-status feed: one event per faculty waking or going
    # idle (skill-plugin, sop, improvements, history, task-graph, compatibility,
    # pr-green). Deterministic, NOT semantic — high-volume UI telemetry.
    #
    # What "deterministic" buys, precisely: membership here and NOT in
    # SEMANTIC_TYPES / MEMORY_VIEW_TYPES keeps the feed off the SEMANTIC layer —
    # no LEDGER markdown file per event (projector.LEDGER_TYPES), no Memory
    # view row, no keyword-recall corpus entry, no consolidation pass.
    #
    # What it does NOT buy: staying off the hash chain. The chain hashes rows
    # with NO type filter: `chain._scan` reads every parseable row on a complete
    # (newline-terminated) line and `chain._fold` hashes them for a v2/v3
    # checkpoint, and the v1 shim folds every parseable row of
    # `chain._legacy_order`, an unterminated final line included, through
    # `chain.compute_chain`. So every complete activity row is hashed at
    # projection time (`chain.write_chain`) and re-hashed in full by every
    # `hive verify`. The chain is over the whole ledger, not over the semantic
    # subset.
    # The volume consequence is MN-5's to carry: the cat board is the only
    # consumer, `telemetry.busy` already emits at most one active/idle PAIR per
    # transition rather than per frame, and if MN-5's rendering ever tempts a
    # per-tick emitter the cost lands on `hive verify`'s runtime for the life of
    # the store. Bound the emission rate there; do not assume this registration
    # exempts anything.
    #
    # Deliberately ledger-derived rather than in-memory: R16's "a missing cat
    # means a real bug" collapses if the feed can vanish for unrelated reasons
    # (MindSwarm's mission state is a plain instance attribute that dies with
    # the server).
    "mindnet_activity",
    # One row per task briefed for an orchestrated agent. Payload
    # {task_id, chars, skills, sops, constraints, symbols, gated, empty_reason,
    #  failures, dropped, authored}.
    #
    # This exists because the feature it measures can fail INVISIBLY. `advice`
    # answers EMPTY for a task whose words overlap nothing, and the symbol slice
    # is inert unless `MINDHIVE_SYMBOLS` is set — which it is not by default — so
    # the briefing composer can ship, hand every agent a prompt with no
    # task-specific context in it at all, and have every run still report
    # success. The counts are the only thing that can contradict that, and
    # `gated` is what keeps "switched off" from being reported as "matched
    # nothing", which are different problems with different fixes.
    #
    # The last three are ADDITIVE, appended without moving any key above them,
    # because this is an append-only hash-chained ledger and a key whose meaning
    # changes is recorded wrong permanently for every row already written:
    #
    #   `failures`  which sources RAISED while the wave context resolved
    #               (events / plugins / sidecar / symbols). `gated` says
    #               "switched off"; this says "broken". Four `except` branches
    #               used to swallow silently, so a context in which everything
    #               failed produced the identical row to a healthy one that
    #               matched nothing.
    #   `dropped`   how many non-empty lines the defuser emptied — the ONLY
    #               observable signal that an injection attempt reached the
    #               composer. The three counts above are the resolver's ROW
    #               counts, so five hostile lines used to record `skills=3
    #               chars=0` and nothing else.
    #   `authored`  how many rows were withheld because an orchestration session
    #               wrote them. A spawned child holds `hive` on PATH and its own
    #               `MINDHIVE_SESSION`, so an injected wave-1 agent could write
    #               one SOP row that briefed every later agent. Withholding it
    #               without recording the count would be the same silence one
    #               step further on.
    #
    # Deterministic, for the same reason as `mindnet_activity`: this is
    # machine-generated telemetry carrying no human intent, and semantic
    # membership would put a LEDGER markdown file, a Memory-view row and a
    # recall-corpus entry on disk per briefed task — one per agent per wave.
    #
    # The brief's TEXT is not in the payload. It goes to `runlog` under the
    # task's key plus `agent.BRIEF_KEY_SUFFIX`, because a full prompt block in
    # every row would put kilobytes per task on the hash chain, and `hive verify`
    # re-hashes the whole ledger.
    "mindnet_brief",
    # One row per PreToolUse refusal — the evidence a denial ever happened.
    # Payload {signature, tool_name, decision, reason}; `signature` is ``""``
    # for the learned-matcher tier, which has no signature to report, and the
    # reason is stored WITHOUT `enforce.ENFORCED_MARK` and truncated.
    #
    # Deterministic, and the reasoning is the same as `mindnet_activity`'s: a
    # refusal is telemetry, not a curated memo. Semantic membership would put a
    # LEDGER markdown file, a Memory-view row and a recall-corpus entry on disk
    # per denial, which is how a rule an operator hits fifty times in an
    # afternoon would drown their actual memory.
    #
    # Appended on DENY ONLY. `PreToolUse` fires on every tool call, so an append
    # on the allow path would put a file write on the hottest path in the
    # system; a denial is rare by construction and the tool call is being
    # blocked anyway. `tests/test_enforce.py` keeps an allow-path canary on
    # this, because every other test in that section would pass just as well
    # against an enforcer that appended every time.
    "policy_denied",
    # One row per mechanical test run: the argv, the REAL exit code, the parsed
    # counts, and `verified_by: "python"` (see verify.py). This is the evidence
    # a "the suite is green at N" claim gets checked against — before it existed
    # such a claim was unfalsifiable, which is how a fabricated baseline
    # survived several sessions unchallenged.
    #
    # Deterministic, for the same reason as `mindnet_activity` and
    # `policy_denied`: a run record is machine-generated telemetry carrying no
    # human intent, and semantic membership would put a LEDGER markdown file, a
    # Memory-view row and a recall-corpus entry on disk per test run.
    #
    # The payload contract below is the honesty half: the arbitrary-append
    # surfaces REFUSE a `verified_by` stamp, so this type cannot be forged by
    # hand. Only `verify.run_and_record`, which actually spawned the process,
    # may stamp it.
    "verification",
    # One row per replenishment decision: a candidate that landed as work, one
    # that was refused (with the reason), a cap that truncated the round, or a
    # round that found nothing (see replenish.py). This is the injector's ONLY
    # persistent state — spent fingerprints, the round counter and the
    # consecutive dry-round count are folded back out of these rows, so there is
    # no sidecar that can drift from the ledger.
    #
    # Deterministic, for the same reason as `verification` and `policy_denied`:
    # it is machine-generated audit telemetry carrying no human intent, and a
    # round can emit several rows. Semantic membership would put a LEDGER
    # markdown file, a Memory-view row and a recall-corpus entry on disk per
    # decision — including per REFUSAL, which is the high-volume case.
    #
    # The work itself is not here. A landed candidate becomes a `roadmap` step,
    # which IS semantic and rides the LEDGER, the chain and recall; this row is
    # the audit trail explaining why that step exists. Registry/audit content:
    # consolidation never absorbs it.
    "work_injected",
    # The dashboard's long operations, one row per lifecycle edge (see jobs.py).
    # A click that starts a pytest run or an embedding pass returns 202 and a job
    # id; these rows are what makes the answer to "what happened to it?" survive
    # the process that ran it.
    #
    # THREE types rather than one `job` with an action field, unlike `roadmap` or
    # `autoresume`: the boot seal's whole job is to pair a start against a close,
    # and a reader grepping the ledger for what never finished should be able to
    # ask for exactly that. The payload contract is shared across all three
    # (jobs.validate_payload) precisely because the fold pairs them by `job_id`.
    #
    # `job_abandoned` is written ONLY by `jobs.seal_orphans` at boot — nothing
    # records its own death. It rests on a single-writer assumption that is
    # written down in jobs.py's module docstring; read it before making the seal
    # broader than one namespace.
    #
    # Deterministic, for the same reason as `verification` and `work_injected`:
    # machine-generated audit telemetry carrying no human intent, and semantic
    # membership would put a LEDGER markdown file, a Memory-view row and a
    # recall-corpus entry on disk per button press.
    "job_started", "job_finished", "job_abandoned",
    # What the six UserPromptSubmit injectors actually delivered this turn (see
    # injectors.py). One row per CHANGED turn, never one per prompt: at 200
    # prompts a day, one-row-per-turn is 73,000 rows a year against a
    # 41,470-event store — 1.76x the whole ledger — and the hash chain hashes
    # every complete row with no type filter (`chain._scan` then `chain._fold`),
    # so every one of them is re-hashed by every `hive verify`, permanently. Content dedup does not help; the cost
    # is driven by rows, not bytes. A run of identical turns writes nothing and
    # the row that closes it carries `unchanged_turns`, so the history stays
    # exactly reconstructible.
    #
    # Deterministic for the same reason as `job_started` and `work_injected`:
    # machine telemetry carrying no human intent. Semantic membership would put
    # a LEDGER markdown file, a Memory-view row and a recall-corpus entry on
    # disk for every prompt anyone ever types.
    "context_injected",
}
VALID_TYPES = SEMANTIC_TYPES | DETERMINISTIC_TYPES

# Types surfaced in the dashboard "Memory" view: the curated semantic memos PLUS
# the raw conversation log (every prompt and every response). user_prompt and
# assistant_response are intentionally NOT semantic — they stay out of the
# LEDGER, the hash-chained intent layer, and the knowledge graph — but are shown
# in the Memory view so the full exchange is visible in the hive.
MEMORY_VIEW_TYPES = SEMANTIC_TYPES | {"user_prompt", "assistant_response"}

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


# --- provenance: which outputs came from a Self-Reliant run ------------------
# Self-Reliant stamps everything it writes twice: every event is tagged
# SELF_RELIANT_TAG at append time, and every run logs into its own
# "research-<run_id>" session stream. origin_of() is the ONE rule that turns
# either mark into the display label, so the Memory / Sessions / Improvements /
# Oversee sections can never disagree about what a run produced.
SELF_RELIANT_TAG = "self-reliant"
SELF_RELIANT_SESSION_PREFIX = "research-"


def origin_of(session_id, tags=None):
    """Provenance label for an event or session: ``"self-reliant"`` when it was
    produced by a Self-Reliant run, else ``None``. Computed at read time from
    the append-time marks — the append-only ledger is never migrated."""
    if tags and SELF_RELIANT_TAG in tags:
        return SELF_RELIANT_TAG
    if isinstance(session_id, str) and \
            session_id.startswith(SELF_RELIANT_SESSION_PREFIX):
        return SELF_RELIANT_TAG
    return None


@dataclass
class Event:
    ts: str
    session_id: str
    type: str
    payload: dict
    tags: list = field(default_factory=list)


def to_dict(ev: "Event") -> dict:
    return asdict(ev)


def canonical_json(obj) -> str:
    """The ledger's one serialiser, and the bytes the hash chain covers.

    `allow_nan=False` because the default emits BARE `NaN` / `Infinity`
    tokens, which are not JSON. Measured: a contribution carrying
    `float('nan')` wrote `{"value":NaN}` straight into an append-only,
    hash-chained segment. Python's own reader accepts those tokens, which is
    exactly what kept it invisible -- nothing here would ever have
    complained -- while the Postgres mirror's JSONB and every strict parser
    downstream reject the row, and the chain makes it permanent.

    Refusing at the writer is the only place this can be caught for every
    event type at once, so it is refused here and not in one contract."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


# The append lock. `msvcrt` on Windows, `fcntl` everywhere else; both give a
# real OS byte-range lock, so the kernel releases it if the holder dies.
try:                                                    # pragma: no cover
    import msvcrt
    _flock = None
except ImportError:                                     # pragma: no cover
    msvcrt = None
    import fcntl as _flock

# How long an append waits for its segment before writing anyway. The critical
# section is one small write, so reaching this bound means something is wedged
# rather than merely busy.
_LOCK_WAIT_S = 10.0


@contextlib.contextmanager
def _append_lock(project):
    """Serialise ledger appends for one namespace ACROSS PROCESSES AND THREADS.

    `open(seg, "a")` is NOT an atomic append on Windows: the CRT seeks to end and
    then writes, two syscalls, so two processes can take the same offset and the
    second silently overwrites the first. Measured here with six processes
    appending sixty rows each into one segment: 72 of 360 rows GONE, five runs
    out of five; twelve-way lost 606 of 2400. The same race tears rows, leaving
    the exact fragment `_row_to_event` above was hardened to tolerate -- reads
    were made total long before the write was made atomic, and the seven corrupt
    rows in the live ledger between 2026-07-19 and 2026-08-12 are its
    fingerprints.

    `os.write` on an O_APPEND descriptor was measured as the cheaper fix and is
    NOT one (328 lost against 322 for the plain write): Windows has no
    atomic-append syscall to reach for, so the serialisation has to be explicit.

    An OS byte-range lock, rather than the O_CREAT|O_EXCL sidecar pattern
    `tasktool` uses: same zero-loss result, three times faster under twelve-way
    contention (0.89s against 2.68s), and no stale lock file can wedge later
    appends, because the kernel drops the lock when the holding process exits.

    ONE lock for the whole namespace, held in the project directory rather than
    a companion beside each segment. Per-segment would allow more parallelism
    that nothing here needs -- the real contention is several tool calls inside
    ONE session, which share a segment anyway -- and it would leave a `.lock`
    next to each of the live store's ~930 segments. `events/` holds segments and
    nothing else; that is worth more than the parallelism.

    Threads are covered as well as processes: Windows locks are per HANDLE, and
    each caller opens its own, so two threads of the dashboard's
    ThreadingHTTPServer contend exactly as two processes do. Measured: twelve
    threads x 100 appends, 1200 of 1200 intact, worst single append 0.317s while
    ten spinners fought them for the CPU.

    Fails OPEN in both directions -- if the lock cannot be created, and if the
    deadline passes. A process that writes unserialised has a bad day; a process
    that refuses to record the caller's event has lost it forever.

    Yields the lock file's descriptor only while the lock is HELD, and None in
    both fail-open cases. `_next_seq` keeps the append sequence's high-water
    mark in this same file, past the byte the lock covers, so stamping a row
    costs no second open -- and a caller that does not hold the lock must not
    touch that mark. Handed the descriptor after its deadline, such a caller
    read the mark, lost the CPU while locked appends raised it, and wrote its
    stale value back: the mark went DOWN, and the next locked append repeated a
    stamp an earlier one already carried.
    """
    fd, held = None, False
    try:
        lock = paths.project_dir(project) / "append.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o666)
    except OSError:
        yield None
        return
    try:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                if msvcrt is not None:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    _flock.flock(fd, _flock.LOCK_EX | _flock.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.001)
        yield fd if held else None
    finally:
        if held:
            try:
                if msvcrt is not None:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    _flock.flock(fd, _flock.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


# --- the append sequence -------------------------------------------------------
# `ts` is second-resolution AND caller-supplied, and a segment is named after its
# session, so nothing on a row used to say which of two same-second rows in two
# segments was written first. Readers broke that tie on the segment FILENAME:
# an `update` from session `aaa-…` replayed before the `add` from `zzz-…` it
# edits, and `sop.fold` skipped it as a partial update on an unknown id -- the
# edit lost, with no error anywhere. Every order-sensitive fold had the defect.
#
# So every row written from here on carries `seq`: a per-namespace stamp taken
# INSIDE `_append_lock`, which serialises appends across processes and threads,
# so wherever the high-water mark ruled the stamp -- the append held the lock
# and read a mark holding the last stamp issued -- `seq` order is the order rows
# actually reached the disk (`_next_seq` names the exceptions). Rows written before
# it existed carry nothing that records cross-segment order -- not the chain
# checkpoint (per-segment line counts), not `ts`, not the mirror's `global_seq`
# (load identity) -- so their order is NOT reconstructed, and cannot be: they
# keep the `(ts, segment, line)` order they always had. See `_order_key`.
#
# `seq` rides the ROW, never `Event`. `to_dict` therefore keeps its five keys for
# every consumer, and a legacy row hashes identically under every checkpoint
# format (`chain._scan`).

# Where the high-water mark lives in `append.lock`: just past the single byte
# `msvcrt.locking` covers, fixed-width so a shorter number never leaves a stale
# digit behind.
_SEQ_OFFSET = 1
_SEQ_WIDTH = 20
# The mirror stores `seq` as BIGINT; anything outside it is not a stamp.
_SEQ_LIMIT = 2 ** 63
# What a row with no stamp to order by sorts as. Below every real stamp. A row is
# ordered by the highest stamp at or above it in its own segment
# (`_segment_events`), so only a row with no valid `seq` AND no stamped row above
# it sorts as this -- every row written before stamps existed, and a pre-`seq`
# writer's rows in a segment no stamping process has touched: FIRST within its
# second, WHENEVER it was written. That is the true append order only if no
# pre-`seq` process appended after the first stamped row existed: a long-lived
# writer that imported the old `append_event` (the dashboard, a supervisor, the
# frozen exe) keeps writing unstamped rows, and one landing in such a segment, in
# a second that already holds a stamped row elsewhere, replays BEFORE it --
# nothing on the row can say otherwise. Restart such writers after upgrading.
# `pgstore.READ_ALL_SQL` coalesces to exactly this value.
LEGACY_SEQ = -1


def _now_us() -> int:
    """Wall clock in microseconds -- the seed of a stamp, and the seam tests
    patch to step the clock. Microseconds keep a stamp below 2**53, so it
    survives a JavaScript reader intact until the year 2255."""
    return time.time_ns() // 1000


def _next_seq(fd) -> int:
    """The next append stamp: ``max(clock, high-water mark + 1)``.

    Call it only while holding `_append_lock` on *fd*. The clock alone is not
    enough. It is ADJUSTABLE, and a step backwards inside one second would order
    that second's rows by the step. It can also be COARSE: this venv's is
    GetSystemTimePreciseAsFileTime at 100ns, which `time.time()` reads from
    Python 3.13 on, but CI also runs 3.11 and 3.12, which read
    GetSystemTimeAsFileTime -- measured here ticking about once a millisecond --
    so appends inside one tick read one value and the mark carries them ahead of
    it. The high-water mark keeps locked appends' stamps strictly increasing
    whatever the clock does, for as long as it holds the last stamp issued; if
    `append.lock` is deleted, the next stamp seeds from the clock (see the
    exceptions below).

    Fails OPEN, like the lock: an unreadable or garbled mark falls back to the
    clock, and an unwritable one is not written. A stamp is never a reason to
    refuse an append. Both of the lock's fail-open paths hand this None, so an
    append that does not hold the lock stamps from the bare clock and never
    reads or writes the mark.

    A stamp can repeat, or fall below one issued before it, only where the mark
    did not rule it: an append that did not hold the lock, or one that, after
    the clock stepped back, found the mark lost -- `append.lock` deleted,
    garbled or unreadable -- or stale, because an earlier append's write of it
    failed. `_segment_events` and `_order_key` still order such a row
    deterministically."""
    now = _now_us()
    if fd is None:
        return now
    last = 0
    try:
        os.lseek(fd, _SEQ_OFFSET, os.SEEK_SET)
        raw = os.read(fd, _SEQ_WIDTH)
        if len(raw) == _SEQ_WIDTH and raw.isdigit():
            last = int(raw)
    except OSError:
        pass
    seq = now if last + 1 >= _SEQ_LIMIT else max(now, last + 1)
    try:
        os.lseek(fd, _SEQ_OFFSET, os.SEEK_SET)
        os.write(fd, b"%020d" % seq)
    except OSError:
        pass
    return seq


def append_event(event_type, payload, session_id=None, tags=None, ts=None, project=None) -> "Event":
    if event_type not in VALID_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    session_id = session_id or os.environ.get("MINDHIVE_SESSION") or "manual"
    ev = Event(ts=ts or now_iso(), session_id=session_id, type=event_type,
               payload=payload, tags=list(tags or []))
    d = paths.events_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    seg = d / f"{session_id}.jsonl"
    with _append_lock(project) as fd:
        row = to_dict(ev)
        row["seq"] = _next_seq(fd)
        with open(seg, "a", encoding="utf-8") as f:
            f.write(canonical_json(row) + "\n")
    return ev


# THE APPEND PATH TOUCHES POSTGRES IN NO WAY AT ALL, under every value of
# `MINDHIVE_EVENT_BACKEND`. `append_event` writes one line to one file and
# returns.
#
# A `_maybe_mirror_to_pg` seam used to sit on that last line, calling
# `pgstore.mirror_one`. It is gone, and its absence is the design rather than an
# omission:
#
# * `pgstore` holds the READER credential and `mindhive_reader` is read-only at
#   the ROLE level, so a single-row INSERT from there was refused by the server.
#   The seam had been raising on every call since the day it was written.
# * A row-level writer would have had to compute `seg_line` itself, and
#   `pgmirror.scan_segments` numbers it by PHYSICAL line — blank and torn rows
#   included. The two would have agreed until the first blank line.
#
# The mirror now follows the ledger through `pgmirror.refresh_changed`, which
# reloads only the segments whose bytes moved and is called by the scheduled
# `autosync` run and by `hive pg refresh`. `MINDHIVE_EVENT_BACKEND` keeps its
# meaning for READS — see `_maybe_read_from_pg` directly below, which is now the
# only seam in this module.


def _maybe_read_from_pg(project=None):
    """Read one namespace from the Postgres mirror, or None to read the files.

    Returns rows ONLY under backend 'pg'. Under 'file' and 'dual' the file store
    is still the canonical read, so this returns None before importing anything
    and the caller falls through to the segments.

    `read_all_or_none` is the TOTAL reader: None on any failure, never an empty
    list, because an empty list is indistinguishable from a namespace with no
    history. `read_all_strict` — the one with no fallback — is for the parity
    gate and is deliberately not reachable from here."""
    if os.environ.get("MINDHIVE_EVENT_BACKEND", "file").lower() != "pg":
        return None
    try:
        from . import pgstore
        return pgstore.read_all_or_none(project)
    except Exception:
        return None  # degrade to file read


# Fields an Event row cannot be reconstructed without. A row missing any of
# these is malformed (a torn write, or a pre-schema line) and is skipped by the
# total read below — corrupt_rows() reports it instead.
_REQUIRED_FIELDS = ("ts", "session_id", "type", "payload")


def _row_to_event(line: str):
    """Parse one ledger line into an ``Event``, or return ``None`` when the row
    is malformed — unparseable JSON, a non-object value, or an object missing a
    required Event field.

    Reads stay TOTAL (ledger-schema SOP): the append-only log carries history
    written by every past version of the code, and a torn or interleaved write
    from a crashed / concurrent session must never crash a fold or projection.
    A single corrupt line used to raise here, which `main()` and all 14 dashboard
    endpoints turned into a total outage — every `hive` command and every UI
    section failed on one bad `command_run` fragment. Corruption is not swallowed
    silently: it is surfaced, loudly but non-fatally, by corrupt_rows() and
    `hive verify` (any dropped row also changes the hash-chain head, so verify()
    still reports a mismatch — tamper-evidence lives there, not in crashing
    readers)."""
    return _row_parts(line)[0]


def _valid_seq(value):
    """*value* as an append stamp, or None. Only a real ``int`` in BIGINT range
    counts: a bool is an int subclass (`True` would read as stamp 1), a float
    compares against ints and would silently reorder, and a str would make the
    sort itself raise. Absent and malformed are the same answer -- a LEGACY row
    -- because reads stay total."""
    if type(value) is int and 0 <= value < _SEQ_LIMIT:
        return value
    return None


def _row_parts(line: str):
    """One ledger line as ``(Event, seq)``, or ``(None, None)`` when the row is
    malformed (see `_row_to_event`). `seq` is None for a row written before the
    append sequence existed, and for one whose stamp is not a valid one.

    One `json.loads` serves both halves, which is why this exists rather than a
    second parse beside `_row_to_event`: every total reader and `chain._scan`
    already pay for exactly one."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(obj, dict) or any(k not in obj for k in _REQUIRED_FIELDS):
        return None, None
    known = {k: obj[k] for k in Event.__dataclass_fields__ if k in obj}
    return Event(**known), _valid_seq(obj.get("seq"))


def _order_key(ts, seq, segment, line_index):
    """THE ledger read order: ``(ts, seq, segment, line_index)``.

    `ts` first, because it is the event's own time and a backdated row
    (`incidents.audit`) is meant to sort by it. `seq` breaks ties inside one
    second in true append order -- the defect this key exists to fix.

    *seq* is the ORDERING stamp `_segment_events` yields, not necessarily the
    row's own: its own stamp raised to the highest one above it in its segment.
    None -- no stamp on the row and none above it -- sorts as `LEGACY_SEQ`:
    FIRST within its second, whenever it was written. A second holding only such
    rows therefore orders exactly as `(ts, segment, line_index)` always did. A
    second holding both puts them first, which is the true append order only if
    no pre-`seq` process appended after the first stamped row existed -- nothing
    on such a row records when it was written, so long-lived writers such as the
    dashboard must be restarted after upgrading. The carry covers the commonest
    shape of that mistake, an unrestarted writer appending to a segment a
    stamping process also writes; it cannot cover a segment only the old writer
    uses.

    This is NOT the order a v1 checkpoint was minted in. Every v1 writer sorted
    seq-blind, over stamped rows too, so `chain._verify_v1` folds
    ``_order_key(ts, None, segment, line_index)`` -- the legacy order -- and
    not this key; folding this one against such a head would report untouched
    history as tampered.

    `segment, line_index` stay last. They are unique per row, so the key is
    total, and they settle every tie a stamp leaves: rows carrying one stamp
    down a segment (line order, which is append order there), and a stamp
    repeated where the high-water mark did not rule it (`_next_seq`).
    `read_all`, `read_types`
    and `pgmirror.scan_segments` all sort `_segment_events` on this function;
    `pgstore.READ_ALL_SQL` spells the same key in SQL."""
    return (ts or "", LEGACY_SEQ if seq is None else seq, segment, line_index)


def _segment_parts(seg):
    """One segment split into physical lines. **The single decision about where
    a ledger row ends, and the reason it is a function of its own.**

    Two readers need two different SHAPES over the same split. `_segment_lines`
    below wants `(index, text)` with blanks skipped; `chain._scan` wants the raw
    list, because it also needs `len(parts) - 1` -- the count of COMPLETE lines,
    which is what a segment may be sealed at. A different return shape is not a
    different rule, so the rule lives here and both build on it.

    It is a function rather than a convention because the two ARE allowed to
    drift silently. Nothing compares them at runtime: the chain records a count,
    the mirror records an index, and each is self-consistent. A forward change
    applied to one and not the other -- stripping a BOM, say -- leaves the whole
    suite green while the readers surface a row the chain never sealed, and that
    row can then be rewritten with `hive verify` still reporting `ok: true`.
    That is the same tamper-evidence hole `splitlines()` opened, re-reachable by
    an ordinary edit. `tests/test_architecture_drift.py` pins the rule; this
    function is what makes the rule cheap to keep."""
    return seg.read_text(encoding="utf-8", errors="replace").split("\n")


def _segment_lines(seg):
    """One segment's rows as ``(line_index, stripped_text)``, blank lines skipped.

    **The split is on \\n and nothing else, and that is the whole reason this
    function exists.** `str.splitlines()` -- what every reader here used until
    this landed -- also breaks on U+0085, U+2028, U+2029 and five C0 controls.
    The ledger is NEWLINE-delimited JSON, so to this format those are ordinary
    characters INSIDE a row. `canonical_json` writes with `ensure_ascii=False`,
    and measured against its exact arguments, exactly three of the eight reach a
    segment raw: U+0085, U+2028 and U+2029. The other five are below U+0020,
    which JSON forbids unescaped, so `json.dumps` escapes them and they can
    never arrive as separators. All three that do arrive are legal inside a JSON
    string and `json.loads` accepts them, so the row on disk is INTACT and only
    the reader destroyed it.

    What that cost, reproduced on three events with one U+2028 in the middle
    one: `read_all` returned TWO -- the event vanished from every projection,
    the briefing and the mirror -- `corrupt_rows` invented two torn rows for the
    one intact event, and every later row's line index shifted by one, including
    `seg_line`, which the Postgres mirror stores as part of a UNIQUE natural key.

    `line_index` counts EVERY physical line, blanks and malformed rows included,
    because `pgmirror.scan_segments` persists it as `seg_line` and
    `corrupt_rows` reports it to a human. It is zero-based; `corrupt_rows`
    documents why.

    Every ledger-segment reader shares this one function so they cannot fork
    again. `chain._scan` is the one exception and splits identically, because it
    also needs each segment's COMPLETE-line count -- a different return shape,
    not a different rule.

    Line endings need no handling here: `read_text`'s universal-newline decoding
    turns CRLF, and a lone CR, into \\n before the split -- which is load
    bearing, since `append_event` opens the segment in text mode and Windows
    therefore writes CRLF. The trailing empty string that `split` yields on a
    newline-terminated file is dropped by the blank skip, so on any segment
    carrying none of the three characters the indices are byte-identical to
    `splitlines()`'. Verified 2026-09-09 across all 74 namespaces, then 2495
    segments and 118,875 rows: the same indices and the same chain head under
    both splits. Those totals are a live store's and grow by the hour -- they
    date the check, they are not a figure to re-assert.
    This change therefore renumbers nothing already stored, and needs no
    `hive pg rebuild`.

    Cost: none worth the name. `read_all("mindhive")` -- 1283 segments and 69k
    rows in that namespace, not the whole store's 2518 at that later moment --
    warm cache, best of four interleaved runs against the same data in one
    process: 2.157s through this generator against 2.181s through the inline
    `splitlines()` it replaced. (A first, non-interleaved attempt said 2.09s
    against 3.38s; that was cold page cache on the first run, not a speedup,
    which is why the number quoted here is the interleaved one.)"""
    for i, raw in enumerate(_segment_parts(seg)):
        line = raw.strip()
        if line:
            yield i, line


def _segment_events(seg):
    """One segment's parseable rows as ``(line_index, Event, order_seq)``, in
    line order, malformed and torn rows skipped (reads stay total). The row
    stream every ORDERED reader sorts: `read_all`, `read_types` and
    `pgmirror.scan_segments`.

    `order_seq` is the row's own stamp RAISED to the highest stamp above it in
    this segment, or None while no row so far has carried one. Line order inside
    a segment IS append order -- appends to it are serialised by `_append_lock`
    and land at its end -- so a row was written no earlier than anything above
    it, and the highest stamp above it is a lower bound on when it was written.
    Two rows it settles that their own stamps get wrong:

    * an UNSTAMPED row below a stamped one. A long-lived process still on
      pre-`seq` code (the dashboard nobody restarted) appends to `manual.jsonl`
      beside a `hive` command that stamps; sorted as `LEGACY_SEQ`, its `update`
      replayed before the `add` above it -- the lost edit again, inside ONE file,
      which even the filename tie-break never did.
    * a stamp BELOW one above it, which only an append the mark did not rule can
      write: one that did not hold the lock, or one that found the mark lost
      or stale while the clock read no later than a stamp already issued
      (`_next_seq`).

    A malformed or torn row between two rows does not reset the carry: it is
    skipped, and the rows below it were still appended after the rows above.

    Across segments the carry only ever moves a row LATER, and only to a stamp
    issued before the row was written. A row a locked append wrote after it
    carries a higher stamp, so it still sorts after it -- unless, at that
    append, the mark it read was below the carried stamp and the clock was not
    above it: the stamp it issued is then no higher, and an equal one ties and
    breaks on segment name. The mark is below it only where it never held that
    stamp or no longer does: a stamp taken from the bare clock ahead of the
    mark, one whose own write of the mark failed, or a mark lost since
    (`append.lock` deleted, garbled or unreadable); the clock is not above it
    after a step back, or inside one tick of a coarse clock (`_next_seq`).
    Rows above a segment's first stamp keep None, so a ledger
    written entirely before stamps orders exactly as it always did.

    The carried value is an ORDERING decision, not part of the row:
    `chain._scan` folds each row's OWN stamp into v3 material, and
    `chain._legacy_order` ignores stamps altogether, because every v1 writer
    did."""
    carried = None
    for i, line in _segment_lines(seg):
        ev, seq = _row_parts(line)
        if ev is None:
            continue
        if seq is not None and (carried is None or seq > carried):
            carried = seq
        yield i, ev, carried


def read_all(project=None) -> list["Event"]:
    """Every parseable event in the namespace, in `_order_key` order over
    `_segment_events`."""
    pg = _maybe_read_from_pg(project)
    if pg is not None:
        return pg
    d = paths.events_dir(project)
    rows = []
    if not d.exists():
        return []
    for seg in sorted(d.glob("*.jsonl")):
        for i, ev, seq in _segment_events(seg):
            rows.append((_order_key(ev.ts, seq, seg.name, i), ev))
    rows.sort(key=lambda t: t[0])
    return [r[1] for r in rows]


def read_types(types, project=None) -> list["Event"]:
    """Every event whose ``type`` is in *types*, in `read_all`'s exact order.

    **What this is, stated plainly, because it was nearly mislabelled.** This is a
    RETENTION and LATENCY fix, not a bounded reader. It still opens every segment
    and still parses every line: measured on the live store, 1079/1079 segments and
    27.8 MB read, 50,019 lines `json.loads`-ed, to keep 1,451 events. What changes
    is what survives the call — 1,451 `Event` objects instead of 50,003 — and the
    tuple-build and sort that dominated the cost (1.54 s -> 0.42 s, best of 3).

    It therefore does NOT discharge `CLAUDE.md`'s "no unbounded scans" guardrail,
    and no comment here should claim it does (/autoplan skeptic, 2026-08-30). That
    rule sits under "Data-access guardrails", is scoped "to answer a question", and
    closes with "Self-audit before answering": it governs the agent's own inspection
    tier, not library internals — `read_all` has 130 call sites across
    `src/mindhive/`, and none of them are rule breaches either. The genuinely
    bounded typed reader is `pgtools.search_events_by_type`, which is index-served
    and takes a limit; it needs the Postgres mirror, which is not available on every
    machine, so it cannot be what the composer depends on.

    Three seams are shared with `read_all` deliberately, and changing any of them
    here silently forks the two readers:
      * `_maybe_read_from_pg` first, so `MINDHIVE_EVENT_BACKEND=pg` does not make
        one reader answer from the mirror and the other from the file store;
      * `_segment_events`, so a torn or malformed row is skipped identically,
        reads stay total, and each row is ordered by the same carried stamp.
        The carry runs over EVERY row, before the type filter: a row of another
        type can hold the stamp a survivor below it sorts by;
      * `_order_key`, the `(ts, seq, segment name, line index)` sort key.
        Filtering BEFORE the sort cannot reorder the survivors — the key is
        unique per row, so there are no ties to break differently — which is
        what lets the order-sensitive folds downstream (`sop.fold`,
        `tasks.fold`, the skill-call pairing) see exactly what a full read would
        have shown them."""
    want = frozenset(str(t) for t in types)
    pg = _maybe_read_from_pg(project)
    if pg is not None:
        return [e for e in pg if e.type in want]
    d = paths.events_dir(project)
    rows = []
    if not d.exists():
        return []
    for seg in sorted(d.glob("*.jsonl")):
        for i, ev, seq in _segment_events(seg):
            if ev.type not in want:
                continue          # filtered AFTER the carry, never before it
            rows.append((_order_key(ev.ts, seq, seg.name, i), ev))
    rows.sort(key=lambda t: t[0])
    return [r[1] for r in rows]


def corrupt_rows(project=None) -> list[dict]:
    """Every malformed ledger row across all segments: unparseable JSON, a
    non-object JSON value, or an object missing a required Event field.

    read_all() skips exactly these rows so folds and projections stay total;
    this is the strict companion that REPORTS them, so `hive verify` and the
    dashboard can show corruption instead of it passing silently. Returns [] for
    a clean store. Each row: ``{segment, line, reason, preview}``.

    **The seven historical rows stay reported, permanently. Decided 2026-09-05.**
    They are orphan tails from the pre-lock append race (`_append_lock` above
    names the measurement; `tests/test_events_append_race.py` reproduces both of
    the reasons they show and proves they are ONE defect, not two). Three
    options were on the table and two were rejected:

    - **Repair them.** Impossible by contract: the ledger is append-only and
      hash-chained, and hand-editing a segment breaks chain parity and shows up
      in the Postgres mirror as divergence. A correction is a new event.
    - **Quarantine them** behind an audited `corrupt_quarantined` event so
      `verify` could stop naming them. Rejected: it converts a visible scar into
      a config, and the next real corruption would land in a report that has
      already been taught to stay quiet about this class.
    - **Keep reporting them.** Chosen. Seven rows is a fixed, non-growing set
      whose cause is fixed, so the count is a REGRESSION DETECTOR: if it ever
      moves, an unserialised writer exists again. A permanently honest scar is
      worth more than a clean-looking report.

    `line` is ZERO-BASED, so `sed -n 40p` finds the row this reports as 39. That
    is off by one from every editor and every other line-numbering tool, and it
    cost real time during an investigation, but it is not a typo to fix here:
    `pgmirror.scan_segments` numbers `seg_line` the same way, that number is a
    stored join key in the Postgres mirror, and the two agreeing matters more
    than either matching `sed`. Add one before quoting a line number to a human.
    """
    d = paths.events_dir(project)
    out: list[dict] = []
    if not d.exists():
        return out
    for seg in sorted(d.glob("*.jsonl")):
        for i, s in _segment_lines(seg):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                reason = f"unparseable json: {e.msg}"
            else:
                if not isinstance(obj, dict):
                    reason = "not a json object"
                else:
                    missing = [k for k in _REQUIRED_FIELDS if k not in obj]
                    reason = (f"missing required field(s): {', '.join(missing)}"
                              if missing else None)
            if reason:
                out.append({"segment": seg.name, "line": i,
                            "reason": reason, "preview": s[:80]})
    return out
