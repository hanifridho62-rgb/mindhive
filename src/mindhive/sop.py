# src/mindhive/sop.py
"""Managed standard operating procedures (SOPs).

SOPs are the hive's operating layer: standing procedures — distilled from the
ledger's own history — that every future session (developer or AI) inherits.
The registry is event-sourced on the append-only ledger: each add / update /
delete is one semantic ``sop`` event, and the current set is a deterministic
fold over those events in timestamp order. Nothing is ever rewritten in
place, so the full lifecycle of every procedure stays auditable, and because
``sop`` is a semantic type it rides the LEDGER, the hash chain, the Memory
view, and keyword recall for free.

Delivery: the active set renders as one block appended to STATE.md
(projector.compose_state), which sync embeds into the auto-loaded MEMORY.md —
so every session starts with the SOPs in context. The block is budgeted:
bodies beyond BLOCK_FULL_BUDGET render as title-only pointers instead of
growing context cost forever (full text stays one `hive sop show` away).

Like the code change-log, SOPs are registry/audit content: consolidation
never absorbs them and display-time dedupe never collapses them.
"""
import re
from . import events as ev_mod

ACTIONS = ("add", "update", "delete")

# sop_id is a stable slug: it names the procedure in STATE.md, the CLI, the
# dashboard, and cross-references — keep it short, kebab-case, permanent.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

MIN_TITLE_CHARS = 3
MAX_TITLE_CHARS = 120
# A body shorter than this is a label, not a procedure; one longer than this
# is documentation and belongs in docs/ with the SOP pointing at it. The cap
# protects the auto-loaded context budget (see block()).
MIN_TEXT_CHARS = 20
MAX_TEXT_CHARS = 4000

# Total characters of SOP bodies rendered in full inside the STATE.md block.
# Once the running total would exceed this, remaining SOPs render as one-line
# pointers. Deterministic: same registry in, same block out.
BLOCK_FULL_BUDGET = 16000


def validate_payload(payload) -> None:
    """Enforce the sop event contract on any append surface (CLI `hive sop`,
    raw `hive append sop`, dashboard append). The event store itself stays
    type-agnostic — the contract lives here, mirroring changelog.py."""
    if not isinstance(payload, dict):
        raise ValueError("sop payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError(
            "sop requires an action — one of: " + ", ".join(ACTIONS))
    sop_id = payload.get("sop_id") or ""
    if not ID_RE.match(sop_id):
        raise ValueError(
            "sop requires a kebab-case sop_id (lowercase letters, digits, "
            "hyphens; max 64 chars), e.g. 'memo-hygiene'")
    title = payload.get("title")
    text = payload.get("text")
    if title is not None and not (
            MIN_TITLE_CHARS <= len(title.strip()) <= MAX_TITLE_CHARS):
        raise ValueError(
            f"sop title must be {MIN_TITLE_CHARS}-{MAX_TITLE_CHARS} chars")
    if action == "add":
        if not (title or "").strip():
            raise ValueError("sop add requires a title")
        if len((text or "").strip()) < MIN_TEXT_CHARS:
            raise ValueError(
                "sop add requires a real procedure body "
                f"(at least {MIN_TEXT_CHARS} characters)")
    elif action == "update":
        if not (title or "").strip() and not (text or "").strip():
            raise ValueError("sop update requires --title and/or a new body")
        if text is not None and len(text.strip()) < MIN_TEXT_CHARS:
            raise ValueError(
                "sop body too short to be a procedure "
                f"(at least {MIN_TEXT_CHARS} characters)")
    # delete: sop_id is enough; text (if given) is the reason.
    if text is not None and len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            f"sop body exceeds {MAX_TEXT_CHARS} chars — keep the procedure "
            "compact (it auto-loads into every session) and move long-form "
            "detail to docs/, with the SOP pointing at it")


def fold(evs):
    """Deterministic fold of sop events into ``(active, deleted)`` dicts of
    sop_id -> record. Records carry title/text plus lifecycle metadata:
    created (first add), updated (last change), version (events applied),
    session_id and tags of the latest event.

    The fold is total — it never raises on historical data. An ``update``
    carrying both title and text upserts (self-heals a missing add); a
    partial update on an unknown or deleted id is skipped; ``delete`` on an
    unknown id is a no-op. Re-adding a deleted id revives it, keeping the
    original created ts and continuing its version count.
    """
    active: dict = {}
    deleted: dict = {}
    for ev in evs:
        if ev.type != "sop" or not isinstance(ev.payload, dict):
            continue
        p = ev.payload
        action, sop_id = p.get("action"), p.get("sop_id")
        if action not in ACTIONS or not sop_id:
            continue
        rec = active.get(sop_id) or deleted.get(sop_id)
        if action == "delete":
            if sop_id in active:
                rec = active.pop(sop_id)
                rec = dict(rec, deleted_at=ev.ts, version=rec["version"] + 1,
                           delete_reason=(p.get("text") or "").strip())
                deleted[sop_id] = rec
            continue
        title = (p.get("title") or "").strip()
        text = (p.get("text") or "").strip()
        if rec is None:
            if not title or not text:
                continue  # partial update on an unknown id: nothing to build
            active[sop_id] = {
                "sop_id": sop_id, "title": title, "text": text,
                "created": ev.ts, "updated": ev.ts, "version": 1,
                "session_id": ev.session_id, "tags": list(ev.tags or []),
            }
            continue
        rec = dict(rec)
        rec.pop("deleted_at", None)
        rec.pop("delete_reason", None)
        if title:
            rec["title"] = title
        if text:
            rec["text"] = text
        rec["updated"] = ev.ts
        rec["version"] = rec["version"] + 1
        rec["session_id"] = ev.session_id
        rec["tags"] = list(ev.tags or [])
        deleted.pop(sop_id, None)
        active[sop_id] = rec
    return active, deleted


def current(evs) -> list:
    """Active SOP records in stable delivery order: (created, sop_id)."""
    active, _ = fold(evs)
    return sorted(active.values(),
                  key=lambda r: (r["created"], r["sop_id"]))


def get(evs, sop_id):
    """The active record for *sop_id*, or None (deleted counts as absent)."""
    active, _ = fold(evs)
    return active.get(sop_id)


def entries(evs, project=None) -> list:
    """Every sop event as a display dict, newest first (ties break by append
    order, later first) — the change history the dashboard and `hive sop show`
    render. One source of truth for shape and ordering, like
    changelog.entries()."""
    out = []
    for i, ev in enumerate(evs):
        if ev.type != "sop" or not isinstance(ev.payload, dict):
            continue
        p = ev.payload
        d = {
            "ts": ev.ts,
            "session": ev.session_id,
            "action": p.get("action", ""),
            "sop_id": p.get("sop_id", ""),
            "title": p.get("title", ""),
            "text": p.get("text", ""),
        }
        if project is not None:
            d["project"] = project
        out.append((i, d))
    out.sort(key=lambda pair: (pair[1]["ts"] or "", pair[0]), reverse=True)
    return [e for _i, e in out]


def record(action, sop_id, title=None, text=None, tags=None,
           session_id=None, project=None, evs=None):
    """Append one sop event after registry-aware guards: adding an existing id
    or updating/deleting a missing one is almost always a typo — fail with the
    corrective command instead of silently forking the registry. (The raw
    `hive append sop` path skips these guards on purpose: validate_payload
    still holds the shape contract, and fold() stays total.)"""
    payload = {"action": action, "sop_id": sop_id}
    if title is not None:
        payload["title"] = title.strip()
    if text is not None:
        payload["text"] = text.strip()
    validate_payload(payload)
    if evs is None:
        evs = ev_mod.read_all(project)
    active, deleted = fold(evs)
    if action == "add" and sop_id in active:
        raise ValueError(
            f"SOP '{sop_id}' already exists — detail it with: "
            f"hive sop update {sop_id} …")
    if action in ("update", "delete") and sop_id not in active:
        hint = (" (it was deleted; re-add it with: hive sop add)"
                if sop_id in deleted else "")
        known = ", ".join(sorted(active)) or "(none)"
        raise ValueError(
            f"no active SOP '{sop_id}'{hint} — active ids: {known}")
    return ev_mod.append_event(
        "sop", payload, session_id=session_id,
        tags=["sop"] + [t for t in (tags or []) if t != "sop"],
        project=project)


def ledger_body(payload) -> str:
    """Self-describing markdown body for this event's LEDGER entry, so keyword
    recall finds SOPs by id, title, or body text. Used by the projector."""
    head = f"[{payload.get('action', '')}] SOP {payload.get('sop_id', '')}"
    title = (payload.get("title") or "").strip()
    if title:
        head += f" — {title}"
    text = (payload.get("text") or "").strip()
    return head + ("\n\n" + text if text else "")


def block(evs) -> str:
    """The STANDARD OPERATING PROCEDURES block for STATE.md (and therefore the
    auto-loaded MEMORY.md). Empty string when no SOP is active. Deterministic;
    budgeted by BLOCK_FULL_BUDGET (see module docstring)."""
    sops = current(evs)
    if not sops:
        return ""
    out = [
        "# STANDARD OPERATING PROCEDURES (managed — `hive sop`)",
        "_Standing procedures for this project: when a task matches an SOP's "
        "scope, follow it. Full registry & history: `hive sop list` / "
        "`hive sop show <id>`. Manage: `hive sop add|update|delete`._",
        "",
    ]
    spent = 0
    for i, r in enumerate(sops, 1):
        out.append(f"## SOP {i}: {r['title']}  `[{r['sop_id']} v{r['version']}]`")
        if spent + len(r["text"]) <= BLOCK_FULL_BUDGET:
            out.append(r["text"])
            spent += len(r["text"])
        else:
            out.append(f"_(body elided for context budget — read it with: "
                       f"`hive sop show {r['sop_id']}`)_")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"
