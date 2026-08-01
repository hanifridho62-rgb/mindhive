# src/mindhive/events.py
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
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def append_event(event_type, payload, session_id=None, tags=None, ts=None, project=None) -> "Event":
    if event_type not in VALID_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    session_id = session_id or os.environ.get("MINDHIVE_SESSION") or "manual"
    ev = Event(ts=ts or now_iso(), session_id=session_id, type=event_type,
               payload=payload, tags=list(tags or []))
    d = paths.events_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    seg = d / f"{session_id}.jsonl"
    with open(seg, "a", encoding="utf-8") as f:
        f.write(canonical_json(to_dict(ev)) + "\n")
    _maybe_mirror_to_pg(ev, project)
    return ev


def _maybe_mirror_to_pg(ev, project=None) -> None:
    """Phase B dual-write seam. Default backend 'file' is a no-op so behavior is
    unchanged. When MINDHIVE_EVENT_BACKEND is 'dual' or 'pg', mirror the event
    into Postgres via the CC-v3 host bridge (event_write.append_pg), which never
    raises — the file segment written just above is the source of truth, so a
    Postgres failure degrades to file-only. The host import is deferred so
    MindHive stays standalone (no asyncpg) in 'file' mode."""
    backend = os.environ.get("MINDHIVE_EVENT_BACKEND", "file").lower()
    if backend not in ("dual", "pg"):
        return
    try:
        from scripts.core import event_write
    except Exception:
        return  # CC-v3 host bridge not importable -> stay file-only
    event_write.append_pg(
        project or paths.detect_project(),
        ev.type, ev.payload, ev.session_id, ev.tags, ts=ev.ts,
    )


def _maybe_read_from_pg(project=None):
    """Phase B read seam (U8). Returns events from Postgres ONLY when the backend
    is 'pg' (post-retirement); for 'file' and 'dual' the file store is still the
    canonical read, so this returns None and the caller reads files. Falls back
    to file (returns None) on any host/DB failure. Host import is deferred."""
    if os.environ.get("MINDHIVE_EVENT_BACKEND", "file").lower() != "pg":
        return None
    try:
        from scripts.core import event_write

        return event_write.read_pg(project or paths.detect_project())
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


def read_all(project=None) -> list["Event"]:
    pg = _maybe_read_from_pg(project)
    if pg is not None:
        return pg
    d = paths.events_dir(project)
    rows = []
    if not d.exists():
        return []
    for seg in sorted(d.glob("*.jsonl")):
        for i, line in enumerate(seg.read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            ev = _row_to_event(line)
            if ev is None:
                continue          # skip a malformed/torn row (reads stay total)
            rows.append((ev.ts or "", seg.name, i, ev))
    rows.sort(key=lambda t: (t[0], t[1], t[2]))
    return [r[3] for r in rows]


def corrupt_rows(project=None) -> list[dict]:
    """Every malformed ledger row across all segments: unparseable JSON, a
    non-object JSON value, or an object missing a required Event field.

    read_all() skips exactly these rows so folds and projections stay total;
    this is the strict companion that REPORTS them, so `hive verify` and the
    dashboard can show corruption instead of it passing silently. Returns [] for
    a clean store. Each row: ``{segment, line, reason, preview}``."""
    d = paths.events_dir(project)
    out: list[dict] = []
    if not d.exists():
        return out
    for seg in sorted(d.glob("*.jsonl")):
        for i, line in enumerate(seg.read_text(encoding="utf-8").splitlines()):
            s = line.strip()
            if not s:
                continue
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
