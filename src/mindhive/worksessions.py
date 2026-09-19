# SPDX-License-Identifier: Apache-2.0
"""Work Sessions: the ``work_session`` event type and the four structured tag
variants (blueprint BAB 5 and section 8.4; MEB SES-01).

A work session is the v1.1 replacement for the *reporting* half of the flat
roadmap: instead of "edited 8 files (+120 -44)" a session says what it is doing
in engineering terms, bound to a phase of the ten-stage methodology spine and
labelled with who asked for it. Four reusable tag builders compose that label
(BAB 5.2), and every writer here goes through them, so the grammar guards and
the BAB 5.3.1 ``[:120]`` cap hold for every stored step:

* ``[WS: <phase> | <technical action> | <UTC ts>]`` -- `make_work_session_tag`
* ``[INITIATIVE: <reason> | Ref: <incident>]`` / ``[USER_SPEC: <requirement>]``
  -- `make_initiative_tag` / `make_user_spec_tag` (BAB 5.3.2)
* ``[USAGE: <used>/<budget> | <pct>%]`` -- `make_usage_tag`; the verbose BAB
  5.3.3 report lives with the measurement, in `worksession_usage`
* the phase tag itself: one of `CANONICAL_PHASES`.

**The phase axis is `WORK_STAGES` (alias `CANONICAL_PHASES`), a DIFFERENT axis
from `mindnet.tasks.PHASES`.** MindNetwork's eight phases are the MN-1 spine
with exit criteria and evidence; these ten label a work-session step. The two
are deliberately not unified, and `tests/test_worksessions.py` pins them
unequal.

A ``work_session`` row carries ``action`` ∈ `ACTIONS`: ``step_append`` holds a
whole `WorkSessionStep` (the §8.4 payload), ``step_status`` moves one step. The
EVENT's session is authoritative -- `fold` files a row under ``ev.session_id``
and skips one whose payload names another session -- and a step id is unique
within its session: §8.4's millisecond id repeats within one millisecond and
every 1000 s, so `record_work_session_step` suffixes a collision and `fold`
keeps the first row for an id. A step whose ``initiative_tag`` is
``[INITIATIVE: …]`` IS an initiative declaration; `initiatives.initiatives`
lists it beside the ``initiative_declared`` rows that module owns.

Companions: `initiatives` (the ``initiative_declared`` type and its own
contract), `worksession_migrate` (the Opsi A hibrida roadmap bridge) and
`worksession_usage` (the Oversee context bar). This module imports only
`events` at module level, so the typed-append path (`events` -> `contracts` ->
here) stays cheap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import events

# --- the phase axis (BAB 5.3.4; section 8.4 verbatim) ------------------------
CANONICAL_PHASES = (
    "brainstorm", "plan", "observation", "understand-and-converge",
    "PRD", "recommendation matrix", "kanban", "execution",
    "quality assessment", "learning",
)
# The same tuple under the plan's name for the axis; NOT `mindnet.tasks.PHASES`.
WORK_STAGES = CANONICAL_PHASES

# --- the reusable technical action catalog (BAB 5.4 and section 8.4) ----------
# Both key sets are valid: an agent sends the short key, the writer expands it.
# Descriptions are the document's own, verbatim.
ACTION_CATALOG_BAB5 = {
    "REF_AST_PARSE": "Analisis statis AST untuk ekstraksi fungsi dan kelas",
    "FIX_PATH_TRAVERSAL": "Sanitasi regex whitelist pada parameter session_id",
    "LOCK_MUTEX_ACQUIRE": "Sinkronisasi file lock multi-proses dengan exponential spin-wait",
    "VEC_BATCH_EMBED": "Penanaman vektor 384-dimensi lokal via all-MiniLM-L6-v2 CPU",
    "TDD_RED_SUITE": "Penyusunan kasus uji gagal untuk memvalidasi batas invarian",
    "TDD_GREEN_REFACTOR": "Implementasi sintaksis minimal untuk meloloskan rangkaian tes",
    "GIT_WT_ISOLATE": "Pencabangan jalur kerja terisolasi pada .worktrees/lane-N",
    "CTX_TRIM_BUDGET": "Pemotongan konteks adaptif sesuai plafon byte dan baris MEMORY.md",
}
ACTION_CATALOG_BAB8 = {
    "AST_PARSE": "Analisis statis AST untuk ekstraksi fungsi, kelas, dan siklomatik",
    "REGEX_SANITIZE": "Sanitasi regex whitelist parameter session_id untuk keamanan path",
    "LOCK_ACQUIRE": "Sinkronisasi file lock multi-proses dengan exponential spin-wait",
    "VEC_EMBED_CPU": "Penanaman vektor 384-dimensi lokal via all-MiniLM-L6-v2 CPU",
    "TDD_RED": "Penyusunan kasus uji gagal untuk memvalidasi batas invarian arsitektur",
    "TDD_GREEN": "Implementasi sintaksis minimal untuk meloloskan rangkaian tes pengujian",
    "WT_ISOLATE": "Pencabangan jalur kerja terisolasi pada direktori .worktrees/lane-N",
    "TAIL_TRIM": "Pemotongan konteks adaptif sesuai kuota byte LF-normalized MEMORY.md",
    "CI_INTERCEPT": "Intersepsi siklus hidup status penggabungan PR dan pipeline CI",
}
ACTION_CATALOG = {**ACTION_CATALOG_BAB5, **ACTION_CATALOG_BAB8}

STEP_STATUSES = ("active", "completed", "pending", "blocked")
ACTIONS = ("step_append", "step_status")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_TOKEN_BUDGET = 100000
CONTEXT_BUDGET_TOKENS = 200_000      # BAB 5.5: the Oversee bar's occupancy window
CHARS_PER_TOKEN = 4                  # BAB 5.3.3: the approximation ratio
WS_ACTION_MAX_CHARS = 120            # BAB 5.3.1: `[:120]`
TAG_TEXT_MAX_CHARS = 200             # a reason, a ref or a requirement in a tag
INITIATIVE_DEFAULT_REASON = "Penanganan temuan audit otonom"
USER_SPEC_DEFAULT = "Sesuai spesifikasi awal pengembang"
_TAG_BREAKERS = re.compile(r"[|\[\]\r\n]")
_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"   # strptime alone takes "2026-9-1T0:0:0Z"
_WS_TAG = re.compile(rf"\[WS: (?P<phase>[^|\[\]]+) \| (?P<action>[^|\[\]]+) \| (?P<ts>{_STAMP})\]")
_INITIATIVE_TAG = re.compile(r"\[INITIATIVE: (?P<reason>[^|\[\]]+?)(?: \| Ref: (?P<ref>[^|\[\]]+))?\]")
_USER_SPEC_TAG = re.compile(r"\[USER_SPEC: (?P<requirement>[^|\[\]]+)\]")
_USAGE_TAG = re.compile(r"\[USAGE: [^\[\]\r\n]+\]")
_STEP_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_INITIATIVE_KEYS = ("reason", "ref", "tag")
_NEED = f"work_session memerlukan kunci 'action' ({'|'.join(ACTIONS)})"


def expand_action(key_or_text: str) -> str:
    """The catalog text for a short key, else the text itself (BAB 5.4)."""
    return ACTION_CATALOG.get(key_or_text, key_or_text)


def _now(now_fn=None) -> datetime:
    return now_fn() if now_fn is not None else datetime.now(timezone.utc)


def _require_phase(phase) -> None:
    if phase not in CANONICAL_PHASES:
        raise ValueError(f"Phase tag tidak valid: '{phase}'. Harus salah satu dari: "
                         f"{CANONICAL_PHASES}")


# --- the four tag builders (BAB 5.2 / 5.3) and their parsers -----------------

def clean_tag_text(text, limit: int | None = None) -> str:
    """Text as a tag field carries it: the characters that would break the tag
    grammar (``|``, ``[``, ``]``, CR, LF) become spaces, whitespace runs fold to
    one space (BAB 5.3.1's ``" ".join(....split())``), then the *limit* cut."""
    s = " ".join(_TAG_BREAKERS.sub(" ", str(text or "")).split())
    return s[:limit].rstrip() if limit else s


def make_work_session_tag(phase: str, technical_action: str, ts: str = None) -> str:
    """Membangun tag Work Session standar yang hemat token (BAB 5.3.1): a
    canonical phase, the action cleaned and cut to 120 characters, the stamp."""
    _require_phase(phase)
    action = clean_tag_text(technical_action, WS_ACTION_MAX_CHARS)
    if not action:
        raise ValueError("Technical action tidak boleh kosong.")
    ts_str = ts or datetime.now(timezone.utc).strftime(TS_FORMAT)
    return f"[WS: {phase} | {action} | {ts_str}]"


def make_initiative_tag(reason: str, ref: str = None) -> str:
    """``[INITIATIVE: <reason> | Ref: <ref>]`` (BAB 5.3.2); an empty reason takes
    the §8.4 default, and without a ref the ``Ref:`` part is absent. Both fields
    are cleaned, so a reason cannot forge a ``| Ref:`` of its own."""
    reason = clean_tag_text(reason, TAG_TEXT_MAX_CHARS) or INITIATIVE_DEFAULT_REASON
    ref = clean_tag_text(ref, TAG_TEXT_MAX_CHARS)
    return f"[INITIATIVE: {reason} | Ref: {ref}]" if ref else f"[INITIATIVE: {reason}]"


def make_user_spec_tag(requirement: str) -> str:
    """``[USER_SPEC: <requirement>]`` (BAB 5.3.2); empty -> the §8.4 default."""
    return f"[USER_SPEC: {clean_tag_text(requirement, TAG_TEXT_MAX_CHARS) or USER_SPEC_DEFAULT}]"


def occupancy_pct(tokens_used: int, token_budget: int) -> float:
    """The §8.4 arithmetic: ``round(used / max(1, budget) * 100, 2)``."""
    return round((tokens_used / max(1, token_budget)) * 100, 2)


def make_usage_tag(tokens_used: int, token_budget: int, trim: str = None) -> str:
    """The compact §8.4 form ``[USAGE: <used>/<budget> | <pct>%]``, with
    `` | <trim>`` appended when a trim note is given. A budget of zero means
    "unknown", reported as 0.0% rather than as the §8.4 division by one, which
    would print tokens as percent. Negative figures are refused."""
    if tokens_used < 0 or token_budget < 0:
        raise ValueError("Usage tag: tokens_used dan token_budget tidak boleh negatif.")
    pct = occupancy_pct(tokens_used, token_budget) if token_budget > 0 else 0.0
    tag = f"[USAGE: {tokens_used}/{token_budget} | {pct}%"
    if trim:
        tag += f" | {clean_tag_text(trim, TAG_TEXT_MAX_CHARS)}"
    return tag + "]"


def parse_work_session_tag(tag) -> tuple | None:
    """``(phase, action, ts)`` when *tag* is exactly what `make_work_session_tag`
    builds (a canonical phase, a clean non-empty action within 120 characters, a
    `TS_FORMAT` stamp), else None."""
    m = _WS_TAG.fullmatch(tag) if isinstance(tag, str) else None
    if m is None or m["phase"] not in CANONICAL_PHASES:
        return None
    if m["action"] != clean_tag_text(m["action"], WS_ACTION_MAX_CHARS):
        return None
    try:
        datetime.strptime(m["ts"], TS_FORMAT)
    except ValueError:
        return None
    return m["phase"], m["action"], m["ts"]


def parse_initiative_tag(tag) -> tuple | None:
    """``(reason, ref)`` (``ref`` "" when absent) when *tag* round-trips through
    `make_initiative_tag`, else None."""
    m = _INITIATIVE_TAG.fullmatch(tag) if isinstance(tag, str) else None
    if m is None or make_initiative_tag(m["reason"], m["ref"]) != tag:
        return None
    return m["reason"], m["ref"] or ""


def parse_user_spec_tag(tag) -> str | None:
    """The requirement when *tag* round-trips through `make_user_spec_tag`."""
    m = _USER_SPEC_TAG.fullmatch(tag) if isinstance(tag, str) else None
    return m["requirement"] if m and make_user_spec_tag(m["requirement"]) == tag else None


# --- the step and the session (section 8.4) ----------------------------------

@dataclass(frozen=True)
class WorkSessionStep:
    step_id: str
    phase_tag: str
    technical_action: str
    initiative_tag: str
    usage_tag: str
    status: str  # active | completed | pending | blocked
    timestamp: str


@dataclass
class WorkSession:
    session_id: str
    project: str
    title: str
    steps: list[WorkSessionStep] = field(default_factory=list)
    active_step_index: int = 0
    status: str = "active"


def create_work_session_step(phase: str, action_key_or_text: str, is_initiative: bool,
                             initiative_reason: str = "", tokens_used: int = 0,
                             token_budget: int = 100000, project: str = None,
                             *, now_fn=None, initiative_ref: str = None) -> WorkSessionStep:
    # Membangun satu langkah Work Session yang memadukan 4 Varian Tag secara
    # presisi. The §8.4 body with the review's two corrections: each tag comes
    # from its BAB 5.3 builder (the grammar guards, the [:120] cap and the zero
    # budget rule then hold on the path every writer takes), and the keyword
    # additions `now_fn` (pins the stamp) and `initiative_ref` (the incident).
    _require_phase(phase)
    technical_action = ACTION_CATALOG.get(action_key_or_text, action_key_or_text)
    now = _now(now_fn)
    ts = now.strftime(TS_FORMAT)
    # 1. Work Session Tag
    ws_tag = make_work_session_tag(phase, technical_action, ts)
    # 2. Initiative Tag
    if is_initiative:
        init_tag = make_initiative_tag(initiative_reason, initiative_ref)
    else:
        init_tag = make_user_spec_tag(USER_SPEC_DEFAULT)
    # 3. Usage Tag
    usage_tag = make_usage_tag(tokens_used, token_budget)
    step_id = f"step-{int(now.timestamp() * 1000) % 1000000}"
    return WorkSessionStep(
        step_id=step_id,
        phase_tag=phase,
        technical_action=ws_tag,
        initiative_tag=init_tag,
        usage_tag=usage_tag,
        status="pending",
        timestamp=ts,
    )


def session_events(session_id: str, project: str | None = None) -> list:
    """One session's events in append order, reading only that session's
    segment: the bounded read a writer's collision or membership check needs,
    instead of a whole-namespace scan. Under ``MINDHIVE_EVENT_BACKEND=pg`` the
    namespace read `events.read_all` would answer from is filtered instead, so
    both backends agree."""
    pg = events._maybe_read_from_pg(project)
    if pg is not None:
        return [e for e in pg if e.session_id == session_id]
    from . import paths
    seg = paths.events_dir(project) / events.segment_name(session_id)
    if not seg.is_file():
        return []
    rows = (events._row_to_event(line) for _i, line in events._segment_lines(seg))
    return [e for e in rows if e is not None and e.session_id == session_id]


def record_work_session_step(session_id: str, step: WorkSessionStep,
                             project: str | None = None) -> WorkSessionStep:
    # Mencatat mutasi langkah Work Session ke dalam event ledger. Returns the step as
    # recorded (a held id gets -2, -3, ...), checked and appended under `worksession_lock`.
    from .worksession_lock import record_lock
    with record_lock(project):
        have = {e.payload.get("step_id") for e in session_events(session_id, project)
                if e.type == "work_session" and isinstance(e.payload, dict)
                and e.payload.get("action") == "step_append"}
        if step.step_id in have:
            n = 2
            while f"{step.step_id}-{n}" in have:
                n += 1
            step = replace(step, step_id=f"{step.step_id}-{n}")
        payload = {
            "action": "step_append",
            "session_id": session_id,
            "step_id": step.step_id,
            "phase": step.phase_tag,
            "ws_tag": step.technical_action,
            "initiative_tag": step.initiative_tag,
            "usage_tag": step.usage_tag,
            "status": step.status,
            "timestamp": step.timestamp,
        }
        events.append_event("work_session", payload, session_id=session_id,
                            tags=["work_session", step.phase_tag], project=project)
    return step


def record_step_status(session_id: str, step_id: str, status: str,
                       project: str | None = None, *, now_fn=None):
    """Append the ``step_status`` action; returns the event."""
    if status not in STEP_STATUSES:
        raise ValueError(f"work_session: kunci 'status' harus salah satu dari "
                         f"{STEP_STATUSES}; diterima: '{status}'")
    payload = {"action": "step_status", "session_id": session_id,
               "step_id": step_id, "status": status,
               "timestamp": _now(now_fn).strftime(TS_FORMAT)}
    return events.append_event("work_session", payload, session_id=session_id,
                               tags=["work_session", "step_status"], project=project)


# --- the append-surface contract (``work_session`` only) ----------------------

def _require_str(payload: dict, key: str) -> str:
    v = payload.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"work_session: kunci '{key}' wajib berupa teks non-kosong")
    return v


def validate_payload(payload) -> None:
    """The ``work_session`` contract. ``initiative_declared`` has its own module
    (`initiatives`): `contracts.validate_typed_payload` hands a module only the
    payload, and one module guessing two types from the payload's shape let each
    type accept the other's rows. Tags must be exactly what the builders write,
    with the ``ws_tag`` phase and stamp equal to the payload's own."""
    if not isinstance(payload, dict):
        raise ValueError("work_session: payload harus berupa objek JSON")
    if not payload:
        raise ValueError(f"payload kosong: {_NEED}")
    stray = sorted(k for k in payload if k in _INITIATIVE_KEYS)
    if stray:
        raise ValueError(f"work_session: kunci {stray} milik initiative_declared "
                         "(hive worksession initiative)")
    action = payload.get("action")
    if "action" not in payload:
        # `hive append <type> '<json>'` without --json wraps the argument as
        # {"text": ...}; that is the one shape worth naming outright.
        wrapped = " (tanpa --json, `hive append` mengirim teks sebagai kunci 'text')" \
            if set(payload) == {"text"} else ""
        raise ValueError(f"payload tidak dikenali, kunci diterima: "
                         f"{sorted(map(str, payload))}{wrapped}: {_NEED}")
    if action not in ACTIONS:
        raise ValueError(f"work_session: kunci 'action' harus salah satu dari {ACTIONS}; "
                         f"diterima: {action!r}")
    _require_str(payload, "session_id")
    if not _STEP_ID.fullmatch(_require_str(payload, "step_id")):
        raise ValueError("work_session: kunci 'step_id' hanya boleh huruf, angka, '.', '_', "
                         "':' dan '-' (maks 128)")
    if payload.get("status") not in STEP_STATUSES:
        raise ValueError(f"work_session: kunci 'status' harus salah satu dari "
                         f"{STEP_STATUSES}; diterima: {payload.get('status')!r}")
    ts = _require_str(payload, "timestamp")
    try:                        # zero-padded (`_STAMP`), then a real calendar time
        datetime.strptime(ts if re.fullmatch(_STAMP, ts) else "", TS_FORMAT)
    except ValueError:
        raise ValueError(f"work_session: kunci 'timestamp' harus berformat {TS_FORMAT}; "
                         f"diterima: {ts!r}") from None
    if action == "step_status":
        return
    phase = payload.get("phase")
    if phase not in CANONICAL_PHASES:
        raise ValueError(f"work_session: kunci 'phase' harus salah satu dari "
                         f"{CANONICAL_PHASES}; diterima: {phase!r}")
    parsed = parse_work_session_tag(_require_str(payload, "ws_tag"))
    if parsed is None or parsed[0] != phase or parsed[2] != ts:
        raise ValueError("work_session: kunci 'ws_tag' harus sama dengan "
                         "make_work_session_tag(phase, action, timestamp)")
    init = _require_str(payload, "initiative_tag")
    if parse_initiative_tag(init) is None and parse_user_spec_tag(init) is None:
        raise ValueError("work_session: kunci 'initiative_tag' harus dibangun oleh "
                         "make_initiative_tag atau make_user_spec_tag")
    if not _USAGE_TAG.fullmatch(_require_str(payload, "usage_tag")):
        raise ValueError("work_session: kunci 'usage_tag' harus berbentuk '[USAGE: …]'")
    if "is_initiative" in payload and not isinstance(payload["is_initiative"], bool):
        raise ValueError("work_session: kunci 'is_initiative' harus bernilai boolean")


def ledger_body(payload: dict) -> str:
    """The LEDGER entry for a ``work_session`` row: action, step id, phase and
    every tag, so keyword recall finds a step by any of them (DATA_MODEL §6.4)."""
    action = str(payload.get("action") or "")
    head = f"[{action}] {payload.get('step_id', '')} ({payload.get('session_id', '')})"
    if action == "step_status":
        return f"{head} -> {payload.get('status', '')}"
    parts = [str(payload.get(k) or "") for k in ("phase", "ws_tag", "initiative_tag",
                                                  "usage_tag", "status")]
    return head + " " + " ".join(p for p in parts if p)


# --- the deterministic projections ------------------------------------------

def _payload_of(ev) -> dict | None:
    """A ``work_session`` payload filed under its own event's session, else None."""
    if ev.type != "work_session" or not isinstance(ev.payload, dict) or not ev.session_id:
        return None
    return ev.payload if str(ev.payload.get("session_id") or ev.session_id) == ev.session_id \
        else None


def step_append_payload(ev) -> dict | None:
    """The payload of a ``step_append`` the folds accept, else None: its own
    session, a canonical phase, and a non-empty string step id and ``ws_tag``.
    `initiatives.initiatives` reads steps through this, so the two agree."""
    p = _payload_of(ev)
    if p is None or p.get("action") != "step_append" or p.get("phase") not in CANONICAL_PHASES:
        return None
    ok = all(isinstance(p.get(k), str) and p.get(k) for k in ("step_id", "ws_tag"))
    return p if ok else None


def _step_from_payload(p: dict) -> WorkSessionStep:
    return WorkSessionStep(step_id=p["step_id"], phase_tag=p["phase"],
                           technical_action=p["ws_tag"],
                           initiative_tag=str(p.get("initiative_tag") or ""),
                           usage_tag=str(p.get("usage_tag") or ""),
                           status=p["status"] if p.get("status") in STEP_STATUSES else "pending",
                           timestamp=str(p.get("timestamp") or ""))


def fold(evs, project: str | None = None) -> dict:
    """session id -> `WorkSession`, from ``work_session`` events in order. Total:
    a malformed row, a row naming another session, a repeated step id, an
    unknown action or a status for a step the session never appended is
    skipped, never raised on. The title is the first step's tag;
    ``active_step_index`` points at the last step still ``active`` (else the
    last step); the session is ``completed`` once every step is."""
    sessions: dict = {}
    ids: dict = {}
    for ev in evs:
        p = _payload_of(ev)
        if p is None:
            continue
        sid, ws = ev.session_id, sessions.get(ev.session_id)
        if p.get("action") == "step_append":
            if step_append_payload(ev) is None or p["step_id"] in ids.get(sid, ()):
                continue
            step = _step_from_payload(p)
            if ws is None:
                ws = sessions[sid] = WorkSession(session_id=sid, project=str(project or ""),
                                                 title=step.technical_action)
            ws.steps.append(step)
            ids.setdefault(sid, set()).add(step.step_id)
        elif (p.get("action") == "step_status" and ws is not None
              and p.get("status") in STEP_STATUSES):
            for i, step in enumerate(ws.steps):
                if step.step_id == p.get("step_id"):
                    ws.steps[i] = replace(step, status=str(p["status"]))
                    break
    for ws in sessions.values():
        active = [i for i, s in enumerate(ws.steps) if s.status == "active"]
        ws.active_step_index = active[-1] if active else max(0, len(ws.steps) - 1)
        ws.status = ("completed" if ws.steps and all(s.status == "completed" for s in ws.steps)
                     else "active")
    return sessions


def session_rows(evs, project: str | None = None) -> list:
    """One summary dict per folded session, newest step first (a tie keeps the
    later-folded session first): what ``hive worksession list`` prints and
    ``/api/worksessions`` serves."""
    rows = []
    for w in fold(evs, project).values():
        cur = w.steps[w.active_step_index] if w.steps else None
        rows.append({
            "session_id": w.session_id, "project": project or "", "title": w.title,
            "status": w.status, "steps": len(w.steps),
            "active_step_index": w.active_step_index,
            "phase": cur.phase_tag if cur else "",
            "ws_tag": cur.technical_action if cur else "",
            "initiative_tag": cur.initiative_tag if cur else "",
            "usage_tag": cur.usage_tag if cur else "",
            "last_ts": w.steps[-1].timestamp if w.steps else "",
        })
    order = sorted(range(len(rows)), key=lambda i: (rows[i]["last_ts"], i), reverse=True)
    return [rows[i] for i in order]


__all__ = [
    "CANONICAL_PHASES", "WORK_STAGES", "ACTION_CATALOG_BAB5", "ACTION_CATALOG_BAB8",
    "ACTION_CATALOG", "STEP_STATUSES", "ACTIONS", "TS_FORMAT", "DEFAULT_TOKEN_BUDGET",
    "CONTEXT_BUDGET_TOKENS", "CHARS_PER_TOKEN", "WS_ACTION_MAX_CHARS", "TAG_TEXT_MAX_CHARS",
    "INITIATIVE_DEFAULT_REASON", "USER_SPEC_DEFAULT",
    "expand_action", "clean_tag_text", "make_work_session_tag", "make_initiative_tag",
    "make_user_spec_tag", "occupancy_pct", "make_usage_tag", "parse_work_session_tag",
    "parse_initiative_tag", "parse_user_spec_tag",
    "WorkSessionStep", "WorkSession", "create_work_session_step", "session_events",
    "record_work_session_step", "record_step_status", "validate_payload", "ledger_body",
    "step_append_payload", "fold", "session_rows",
]
