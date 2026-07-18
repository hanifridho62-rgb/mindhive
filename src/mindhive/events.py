# src/mindhive/events.py
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
    "memory_consolidated",
    # Prompt enrichment (Oversee, see enrich.py): an enrichment act the user
    # actually used (copied/recorded) and the default-on toggle's audit
    # trail. Telemetry like trigger_trained — the settings sidecar is the
    # source of truth, these events are the ledger evidence.
    "prompt_enriched", "enrich_toggled",
}
VALID_TYPES = SEMANTIC_TYPES | DETERMINISTIC_TYPES

# Types surfaced in the dashboard "Memory" view: the curated semantic memos PLUS
# the raw conversation log (every prompt and every response). user_prompt and
# assistant_response are intentionally NOT semantic — they stay out of the
# LEDGER, the hash-chained intent layer, and the knowledge graph — but are shown
# in the Memory view so the full exchange is visible in the hive.
MEMORY_VIEW_TYPES = SEMANTIC_TYPES | {"user_prompt", "assistant_response"}

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
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise json.JSONDecodeError(
                    f"{seg.name}:{i}: {e.msg}", e.doc, e.pos
                ) from e
            # A structurally valid JSON line that is missing a required Event
            # field would otherwise raise a raw TypeError from Event(**known),
            # bypassing the framed corrupt-log handling. Fail loud, framed.
            missing = [k for k in ("ts", "session_id", "type", "payload")
                       if k not in obj]
            if missing:
                raise json.JSONDecodeError(
                    f"{seg.name}:{i}: event missing required field(s): "
                    f"{', '.join(missing)}", line, 0)
            known = {k: obj[k] for k in Event.__dataclass_fields__ if k in obj}
            rows.append((obj.get("ts", ""), seg.name, i, Event(**known)))
    rows.sort(key=lambda t: (t[0], t[1], t[2]))
    return [r[3] for r in rows]
