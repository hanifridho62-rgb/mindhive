# src/mindhive/events.py
import contextlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, asdict
from . import paths
# Bound by identity under this module's name: `append_event` calls the private
# spelling, so a test (or the watcher) can swap it here without reaching into
# `embedqueue`. That module imports `paths` only -- no cycle.
from .embedqueue import count_newlines as _count_newlines
from .embedqueue import emit_pending_embed_marker as _emit_pending_embed_marker


# --- the segment path ------------------------------------------------------------
#
# A `session_id` is DATA in the row and a FILENAME on disk, and only the second
# use is dangerous. `append_event` used to build the segment path by raw
# concatenation -- `seg = d / f"{session_id}.jsonl"` -- with the id arriving
# either from `--session` on the CLI or from a hook payload, neither checked. So
# `--session ../../x` wrote the ledger outside the namespace, and an id carrying
# a character the Windows kernel refuses (`:` `*` `?` `|`) raised OSError from
# the middle of the append.
#
# The rule is the one `hooks/lib.sh` and `hooks/lib.ps1` already enforce at the
# shell boundary, restated here because the shells are not the only writer: the
# CLI, the orchestrators and `events.append_event`'s own callers all reach this
# directly. Three statements of one rule, and they must agree.
#
# Verified before it was written: all 2418 segments in the live store match this
# pattern exactly, so nothing on disk moves.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# The ANCHORED twin (PDF §8.1), for the validators, which state "the whole id"
# in the pattern itself. Beside the shared rule, not instead of it: the hooks
# copy the unanchored spelling. Always applied with `fullmatch` anyway --
# Python's `$` matches before a trailing newline, the hole `taskview` once had.
SESSION_ID_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# The segment-name namespace `segment_name` reserves for itself. A conforming
# id that starts with it, in any case, is quarantined rather than taken at face
# value, so the only writer of an `unsafe-*` segment is the quarantine path --
# which is what makes `unsafe-*` mean "something was refused here" to an
# operator triaging the directory, instead of "something was refused here, or
# somebody said so". Re-exported: `paths` owns it, for `paths.namespace_name`.
QUARANTINE_PREFIX = paths.QUARANTINE_PREFIX

# MEB SEC-01, verbatim: the Windows device stems, upper-case, checked by
# `validate_session_id` BEFORE it asks the sink. `paths.WINDOWS_DEVICE_NAMES`
# (lower-case, `com0`/`lpt0` included) stays the sink's own list, and the
# sink is still asked afterwards -- so a stem this set lacks is refused all
# the same, under the fourth message instead of the third.
WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def segment_name(session_id, os_name=None) -> str:
    """The file name for *session_id*'s segment -- always a single, safe path
    component, never a traversal and never a character the OS refuses.

    A conforming id is its own filename, which is every id the system has ever
    written. Anything else is QUARANTINED to `unsafe-<sha256[:16]>.jsonl`, and
    the three properties of that choice are all deliberate:

    * **Not `"unknown"`.** Coercing every hostile id to one name merges distinct
      writers into one segment, which is a second-order corruption of the thing
      being protected.
    * **Not a raise.** This is on the hook path -- `post_tool_use` fires on every
      tool call -- and a hook that can throw is a hook that breaks the session.
      The loud rejection belongs at the surface a human typed
      (`cliparse.session_id_arg`), not here.
    * **Not a sanitized rewrite** (stripping the bad characters). Two different
      ids must not be able to collide into one segment by both reducing to the
      same safe string; a hash cannot collide by accident.

    That last property needed a RESERVATION to be true of anything but
    accidents. `unsafe-<16 hex>` satisfies `SESSION_ID_RE` on its own -- it
    starts with a letter and every other character is in the class -- so before
    the prefix was reserved, a conforming id spelled that way took the
    conforming branch and landed in the quarantine slot of whichever hostile id
    hashes to it. Chosen, not collided: the exact merge the bullet above
    forbids, reachable by typing it. `paths.is_reserved_component` holds the
    reservation (case-insensitive, because NTFS is) and, on Windows, the names
    the OS stores as something else: `NUL.jsonl` is a device on some builds.

    **The one honest limit.** Two conforming ids that differ only in case share
    a segment on a case-insensitive filesystem (`Probe-1` and `probe-1` are one
    file on NTFS), and nothing stops a writer choosing that. Folding case would
    close it and would also rename every segment on disk, so the trade is
    refused and stated instead. The cost is bounded: every row keeps its raw
    `session_id` and `_append_lock` serialises the writes, so a shared segment
    mixes two writers' rows without tearing or misattributing either. The
    guarantees are no traversal, no component the OS aliases, and no conforming
    id in a quarantine segment under any case spelling -- not "one file per
    distinct string on every filesystem".

    `Event.session_id` keeps the RAW value. The id is inert as a JSON value --
    `canonical_json` escapes it -- and it is the only record of who wrote the
    row, so sanitizing the DATA would close the hole by deleting the evidence.
    Sanitize the path; keep the fact."""
    raw = paths.text_of(session_id)
    if raw is None:
        # `str`, `repr` and the type name all raised: the one input with nothing
        # left to tell it apart, and so the one place two values do merge.
        return QUARANTINE_PREFIX + "unstringifiable.jsonl"
    name = raw + ".jsonl"
    if SESSION_ID_RE.fullmatch(raw) and not paths.is_reserved_component(name, os_name):
        return name
    return paths.quarantine_name(raw) + ".jsonl"


def _quote(value) -> str:
    """MEB's `'{session_id}'` rendering of a refused value, with every
    control and non-ASCII character escaped: a raw newline in the value
    would break the message and the `re.search` the tests run over it.
    `paths.text_of`, not `str`, so a value whose `__str__` raises is still
    named rather than crashing the refusal."""
    text = paths.text_of(value)
    if text is None:
        text = "unstringifiable:" + type(value).__name__
    return "'" + text.encode("unicode_escape").decode("ascii") + "'"


def validate_session_id(session_id) -> str:
    """*session_id* VERBATIM, or ``ValueError`` -- the loud half of Bug 2.

    `segment_name` is the quiet half: it quarantines, because it sits on the
    hook path where a raise breaks the session. This is for the surfaces that
    can afford to refuse -- `cliparse.session_id_arg` and
    `append_event(strict_session=True)` -- and it accepts exactly what the sink
    keeps at its STRICTEST platform: the anchored whitelist AND
    `segment_name(x, os_name="nt") == x + ".jsonl"`. Asked at `nt` on every
    runner because a store moves between machines: a `CON` accepted on Linux
    is a device the day the store is opened on Windows, and the reserved
    `unsafe-` prefix is refused everywhere already. `fullmatch`, never `match`.

    No `.strip()`, no `"unknown"` for `None`: a validator that hands back a
    value the caller did not pass is a second sanitiser competing with the
    sink. `append_event` resolves `None` to the environment BEFORE calling.
    Pure -- it touches no filesystem, which is what lets a strict append
    refuse before the events directory or the lock exists. The value is
    rendered by `_quote` -- MEB's `'{session_id}'` form with every control
    character escaped -- so a raw newline cannot break the message (or a
    `re.search` over it).

    Four refusals, in MEB SEC-01's order and wording: the type, the
    whitelist, the Windows device stem (`WINDOWS_RESERVED_NAMES`), then the
    sink's own verdict at `nt` -- which is what catches the reserved
    `unsafe-` prefix in any case and the `com0`/`lpt0` stems the MEB set
    lacks. Each message names exactly one rule, so the operator reads the one they
    broke."""
    if not isinstance(session_id, str):
        raise ValueError(
            f"Format session_id {_quote(session_id)} tidak valid: tipe data harus string.")
    if not SESSION_ID_REGEX.fullmatch(session_id):
        raise ValueError(
            f"Format session_id {_quote(session_id)} tidak valid: karakter tidak diizinkan.")
    if session_id.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError(
            f"Format session_id {_quote(session_id)} tidak valid: "
            "nama perangkat khusus Windows ditolak.")
    if segment_name(session_id, os_name="nt") != session_id + ".jsonl":
        raise ValueError(
            f"Format session_id {_quote(session_id)} tidak valid: "
            "inkonsistensi nama segmen fisik.")
    return session_id


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
    # Work sessions (blueprint P1b, BAB 5): a `work_session` row (owner
    # `worksessions.py`) is one step on the ten-stage axis or a status move;
    # `initiative_declared` (owner `initiatives.py`) records an autonomous
    # initiative that is not one step (BAB 5.3.2).
    "work_session",
    "initiative_declared",
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
    # What it does NOT buy: staying off the hash chain. `chain.compute_chain`
    # folds `events.read_all()` with NO type filter, so every activity event is
    # hashed at append-projection time and re-hashed in full by every `hive
    # verify`. The chain is over the whole ledger, not over the semantic subset.
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
    # 41,470-event store — 1.76x the whole ledger — and `chain.compute_chain`
    # folds `read_all()` with no type filter, so every one of them is re-hashed
    # by every `hive verify`, permanently. Content dedup does not help; the cost
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

# Work-session rows (blueprint P1b): semantic, but bookkeeping ABOUT a session,
# never activity OF one -- no liveness surface (the Oversee board, the Sessions
# view) lets them open or re-date a session, since `hive worksession migrate`
# writes them by the hundred under old plans' owners.
BOOKKEEPING_TYPES = frozenset({"work_session", "initiative_declared"})

# Types surfaced in the dashboard "Memory" view: the curated semantic memos PLUS
# the raw conversation log (every prompt and every response). user_prompt and
# assistant_response are intentionally NOT semantic — they stay out of the
# LEDGER, the hash-chained intent layer, and the knowledge graph — but are shown
# in the Memory view so the full exchange is visible in the hive.
MEMORY_VIEW_TYPES = SEMANTIC_TYPES | {"user_prompt", "assistant_response"}

# --- registry payload contracts ----------------------------------------------
# Carved out to `contracts.py` (blueprint P0b) and re-exported BY IDENTITY:
# `events._PAYLOAD_CONTRACTS is contracts._PAYLOAD_CONTRACTS`, so the two
# append surfaces, the owning modules and the tests keep this spelling.
from .contracts import _PAYLOAD_CONTRACTS, validate_typed_payload  # noqa: E402,F401


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


# RFC 8785 (JCS) alignment, MEB SEC-02. The three properties `canonical_json`
# always had -- sorted keys, no whitespace, `allow_nan=False` -- are JCS's;
# the two it lacked are added as one copy-on-write pass over the payload:
# `-0.0` becomes `0.0` (JCS has no negative zero, and neither has the
# mirror's `numeric`, which is why `-0.0` was the one float the mirror could
# never hold verbatim), and nesting is capped so a hostile payload cannot
# drive `json.dumps` into a RecursionError from inside the append lock.
# BYTE-IDENTICAL for every payload without a `-0.0` and within the cap --
# `tests/test_events_canonical.py` proves it over a corpus -- so no stored
# chain head moves. Key order stays code-point order, which is JCS's UTF-16
# order for the BMP; the astral-plane divergence is recorded here, not
# fixed, because fixing it would move every head.
MAX_NESTING_DEPTH = 100


def _normalise(obj, depth=0):
    """*obj* with every `-0.0` replaced by `0.0`, or *obj* ITSELF when nothing
    changed: containers are copied only on the path to a change, so the
    ordinary payload costs one walk and no allocation, and the caller's
    object is never mutated. *depth* counts the containers entered; the
    (`MAX_NESTING_DEPTH` + 1)-th raises `ValueError`."""
    # Scalar-first: `str`, `None` and `int` (bool included) are most of the
    # leaves and never change, so they leave before the float and container
    # tests. Measured on the live 78,273-row `mindhive` ledger this ordering
    # cut the walk from 0.200 s to 0.132 s.
    if isinstance(obj, str) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return 0.0 if obj == 0.0 and math.copysign(1.0, obj) < 0 else obj
    if isinstance(obj, dict):
        if depth >= MAX_NESTING_DEPTH:
            raise ValueError(
                f"kedalaman bersarang payload melebihi MAX_NESTING_DEPTH={MAX_NESTING_DEPTH}")
        out = None
        for key, value in obj.items():
            new = _normalise(value, depth + 1)
            if new is not value:
                if out is None:
                    out = dict(obj)
                out[key] = new
        return obj if out is None else out
    if isinstance(obj, (list, tuple)):
        if depth >= MAX_NESTING_DEPTH:
            raise ValueError(
                f"kedalaman bersarang payload melebihi MAX_NESTING_DEPTH={MAX_NESTING_DEPTH}")
        out = None
        for i, value in enumerate(obj):
            new = _normalise(value, depth + 1)
            if new is not value:
                if out is None:
                    out = list(obj)
                out[i] = new
        return obj if out is None else out
    return obj


def canonical_json(obj) -> str:
    """The ledger's one serialiser, and the bytes the hash chain covers.

    RFC 8785-aligned (MEB SEC-02): sorted keys, no whitespace, no NaN, and --
    through `_normalise` -- no `-0.0` and no nesting past `MAX_NESTING_DEPTH`.

    `allow_nan=False` because the default emits BARE `NaN` / `Infinity`
    tokens, which are not JSON. Measured: a contribution carrying
    `float('nan')` wrote `{"value":NaN}` straight into an append-only,
    hash-chained segment. Python's own reader accepts those tokens, which is
    exactly what kept it invisible -- nothing here would ever have
    complained -- while the Postgres mirror's JSONB and every strict parser
    downstream reject the row, and the chain makes it permanent.

    Refusing at the writer is the only place this can be caught for every
    event type at once, so it is refused here and not in one contract."""
    return json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


# --- the hash chain's primitives, exported ------------------------------------
# Defined in `core.models` (blueprint P0b, K6) and re-exported BY IDENTITY:
# `events.EventRecord is core.models.EventRecord`. `chain.py` is untouched
# and `tests/test_events_chain_helpers.py` still pins the fold identical.
from .core.models import EventRecord, GENESIS_PREV_HASH, compute_chain_hash  # noqa: E402,F401


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
    """
    fd, held = None, False
    try:
        lock = paths.project_dir(project) / "append.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o666)
    except OSError:
        yield
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
        yield
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


def append_event(event_type, payload, session_id=None, tags=None, ts=None,
                 project=None, *, strict_session=False, fsync=False) -> "Event":
    # `fsync`: the storage SPI's durable append (MEB STO-01) -- flush and
    # `os.fsync` before the lock is released. Off by default: the hook path
    # must not pay a disk flush per event, and the row is durable enough
    # once the OS holds it for every reader this process will ever have.
    if event_type not in VALID_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    session_id = session_id or os.environ.get("MINDHIVE_SESSION") or "manual"
    # Bug 2's loud half, keyword-only and off by default: the hook path keeps
    # the quarantine below (fail-open), and a surface that can afford to refuse
    # asks by name. Validated on the RESOLVED id, before the directory and the
    # lock exist, so a refusal leaves nothing behind -- not even `events/`.
    if strict_session:
        validate_session_id(session_id)
    ev = Event(ts=ts or now_iso(), session_id=session_id, type=event_type,
               payload=payload, tags=list(tags or []))
    d = paths.events_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    # Computed BEFORE the lock is taken. `_append_lock` releases in a `finally`,
    # so a raise inside it never wedged the lock -- but a malformed id should not
    # be able to raise from inside a critical section at all, and this ordering
    # means it cannot. The strict check above sits earlier still, for the same
    # reason plus one: it must not be the thing that mints the directory.
    seg = d / segment_name(session_id)
    with _append_lock(project):
        # 1-based line of the row, counted BEFORE the write, only for rows the
        # watcher wants; an unreadable segment skips the MARKER, never the row.
        try:
            seg_line = _count_newlines(seg) + 1 if event_type in SEMANTIC_TYPES else None
        except OSError:
            seg_line = None
        with open(seg, "a", encoding="utf-8") as f:
            f.write(canonical_json(to_dict(ev)) + "\n")
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if seg_line is not None:
            # A secondary projection behind the primary record: a marker that
            # cannot be read or published must never lose or delay the row.
            try:
                _emit_pending_embed_marker(project, session_id, seg.name, seg_line)
            except Exception:                               # noqa: BLE001
                pass
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
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or any(k not in obj for k in _REQUIRED_FIELDS):
        return None
    known = {k: obj[k] for k in Event.__dataclass_fields__ if k in obj}
    return Event(**known)


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


def read_all(project=None) -> list["Event"]:
    pg = _maybe_read_from_pg(project)
    if pg is not None:
        return pg
    d = paths.events_dir(project)
    rows = []
    if not d.exists():
        return []
    for seg in sorted(d.glob("*.jsonl")):
        for i, line in _segment_lines(seg):
            ev = _row_to_event(line)
            if ev is None:
                continue          # skip a malformed/torn row (reads stay total)
            rows.append((ev.ts or "", seg.name, i, ev))
    rows.sort(key=lambda t: (t[0], t[1], t[2]))
    return [r[3] for r in rows]


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
      * `_row_to_event`, so a torn or malformed row is skipped identically and
        reads stay total;
      * the `(ts, segment name, line index)` sort key. Filtering BEFORE the sort
        cannot reorder the survivors — the key is unique per row, so there are no
        ties to break differently — which is what lets the order-sensitive folds
        downstream (`sop.fold`, `tasks.fold`, the skill-call pairing) see exactly
        what a full read would have shown them."""
    want = frozenset(str(t) for t in types)
    pg = _maybe_read_from_pg(project)
    if pg is not None:
        return [e for e in pg if e.type in want]
    d = paths.events_dir(project)
    rows = []
    if not d.exists():
        return []
    for seg in sorted(d.glob("*.jsonl")):
        for i, line in _segment_lines(seg):
            ev = _row_to_event(line)
            if ev is None:
                continue          # skip a malformed/torn row (reads stay total)
            if ev.type not in want:
                continue
            rows.append((ev.ts or "", seg.name, i, ev))
    rows.sort(key=lambda t: (t[0], t[1], t[2]))
    return [r[3] for r in rows]


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
