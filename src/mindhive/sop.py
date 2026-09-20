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

Delivery: TWO renderings of the same registry, because the two files they
reach are read in different ways.

* ``block()`` → STATE.md (``projector.compose_state``). Read on demand and in
  full, so it carries the bodies; beyond ``BLOCK_FULL_BUDGET`` a body renders
  as a title-only pointer instead of growing context cost forever.
* ``digest_block()`` → the MEMORY.md digest ``sync`` writes
  (``compose_state(..., digest=True)``). Auto-loaded, and the native loader
  stops partway through it — so this one carries titles and `hive sop show`
  pointers, bounded by ``DIGEST_ITEM_CAP``, and elides a mandatory body only
  when ``sopgate`` is verifiably re-asserting it verbatim every prompt.

The split exists because one rendering could not be both: at 404a7c0 the
STATE.md block was 58.7% of an auto-loaded file that the loader was already
cutting short, and the sections it pushed past the cut were the lessons and
decisions the digest exists to deliver.

Two tiers. An ordinary SOP is scope-matched: follow it when the task falls in
its scope. A **mandatory** SOP (``mandatory: true`` on the event, `hive sop
add --mandatory`) holds on every task in every session, and gets the delivery
to match — it renders first and un-elided in the STATE.md block, and its body
is re-asserted on every user prompt by ``sopgate``, which reads the
``mandatory_sops.json`` sidecar this module writes rather than folding the
ledger on the hook's hot path. The tier exists because a session-start
instruction about behaviour that must hold at turn 80 is exactly the kind that
decays; ``style.py`` documents that failure and its per-prompt remedy.

Like the code change-log, SOPs are registry/audit content: consolidation
never absorbs them and display-time dedupe never collapses them.
"""
import json
import re
from . import events as ev_mod
from . import paths
from . import provenance

ACTIONS = ("add", "update", "delete")

# sop_id is a stable slug: it names the procedure in STATE.md, the CLI, the
# dashboard, and cross-references — keep it short, kebab-case, permanent.
#
# `\Z`, not `$`. In Python `$` also matches just before a trailing newline, so
# `ID_RE.match("ops-rule\n")` succeeded: `hive append sop` took that id, and the
# fold kept it as a SECOND procedure rendering under the same visible name as
# `ops-rule`. Other modules import this pattern for plan, task and telemetry ids
# (`tasks`, `phases`, `changelog`, `autoresume`, ...) and had the same hole.
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")

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

# How many scope-matched SOPs the MEMORY.md digest names before it stops
# listing and says how many it dropped.
#
# An ITEM cap, not a byte cap, and it belongs to a different file than
# BLOCK_FULL_BUDGET on purpose. STATE.md is read on demand and has no cut;
# MEMORY.md is auto-loaded and the native loader stops partway through it, so
# the digest is the copy that must stay small. A byte bound would also pick
# which procedures keep their bodies by `(created, sop_id)` — i.e. by age,
# which has nothing to do with relevance — and would move the cliff every time
# a body was edited. Naming every SOP and eliding every body makes the section
# a near-constant instead: it grows one line per SOP, not one procedure.
#
# Measured, and the reason this exists: at 404a7c0 `block()` rendered 15,463
# chars into a 202-line digest against a ~200-line loader budget — 58.7% of the
# file — while BLOCK_FULL_BUDGET (16,000) had never once bitten, because the
# ordinary bodies totalled 11,425. A ceiling set above the delivery channel is
# not a budget.
DIGEST_ITEM_CAP = 12


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
    sop_id = payload.get("sop_id")
    # A non-string is refused here rather than handed to `re`, which raised
    # TypeError past every `except ValueError` on the append surfaces.
    if not isinstance(sop_id, str) or not ID_RE.match(sop_id):
        raise ValueError(
            "sop requires a kebab-case sop_id (lowercase letters, digits, "
            "hyphens; max 64 chars), e.g. 'memo-hygiene'")
    if "mandatory" in payload and not isinstance(payload["mandatory"], bool):
        raise ValueError(
            "sop mandatory must be true or false — a mandatory SOP is "
            "re-asserted on every prompt, so the flag is validated rather "
            "than coerced (1 / 'yes' / 'true' are rejected on purpose)")
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
        # A tier change alone is a real update — `hive sop update x
        # --no-mandatory` must not be rejected for carrying no new prose.
        if (not (title or "").strip() and not (text or "").strip()
                and "mandatory" not in payload):
            raise ValueError("sop update requires --title, a new body, "
                             "and/or --mandatory / --no-mandatory")
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
    partial update on an unknown id is skipped, and on a deleted id revives it
    with the fields it does not replace; ``delete`` on an unknown id is a
    no-op. Re-adding a deleted id revives it, keeping the original created ts
    and continuing its version count.

    The ``mandatory`` flag follows the same never-lose-data discipline: it is
    written only when the event actually carries the key, so a title-only
    update can never silently demote a mandatory procedure to an ordinary one.
    A first ``add`` that omits it defaults to False; an ``add`` that revives a
    deleted id and omits it keeps whatever the id had, because there the key's
    absence means "unchanged" rather than "unset".

    An event whose ``sop_id`` is not a string matching ``ID_RE`` is skipped, as
    the append surfaces refuse it. Before ``ID_RE`` was anchored with ``\\Z`` an
    id with a trailing newline was accepted, and folded into a second procedure
    rendering under the same visible name as the real one.
    """
    active, deleted, _written = _fold(evs)
    return active, deleted


_VALIDATED_TAG = "sop-validated"      # == advice.VALIDATED_TAG (pinned by a test)


def _fold(evs):
    """`fold`, plus the writes it APPLIED: ``(active, deleted, written)``.

    *written* holds ``(sop_id, session_id)`` for every add or update the fold
    applied to a record, in ledger order -- the history `agent_touched_ids`
    reads authorship from. A skipped event is not in it (it changed nothing),
    and neither is a delete (it removes content rather than writing any)."""
    active: dict = {}
    deleted: dict = {}
    written: list = []
    for ev in evs:
        if ev.type != "sop" or not isinstance(ev.payload, dict):
            continue
        p = ev.payload
        action, sop_id = p.get("action"), p.get("sop_id")
        if (action not in ACTIONS or not isinstance(sop_id, str)
                or not ID_RE.match(sop_id)):
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
                "mandatory": bool(p.get("mandatory")),
                "created": ev.ts, "updated": ev.ts, "version": 1,
                "session_id": ev.session_id, "tags": list(ev.tags or []),
                "validated": _VALIDATED_TAG in (ev.tags or ()),
            }
            written.append((sop_id, ev.session_id))
            continue
        rec = dict(rec)
        rec.pop("deleted_at", None)
        rec.pop("delete_reason", None)
        if title:
            rec["title"] = title
        if text:
            rec["text"] = text
        if "mandatory" in p:
            rec["mandatory"] = bool(p["mandatory"])
        # No else: every record reaching here was built by the literal above,
        # which always sets the key, so the absence of `mandatory` on THIS
        # event means "unchanged" and there is nothing to default.
        rec["updated"] = ev.ts
        rec["version"] = rec["version"] + 1
        rec["session_id"] = ev.session_id
        rec["tags"] = list(ev.tags or [])
        # ACCUMULATED, like the agent flag and for its reason: `tags` above is
        # the LAST event's, so one unstamped title-only edit would relabel a
        # token-validated body as the operator's. It survives a delete too --
        # a revival keeps what the reviving event does not replace.
        rec["validated"] = bool(rec.get("validated")) or _VALIDATED_TAG in (ev.tags or ())
        deleted.pop(sop_id, None)
        active[sop_id] = rec
        written.append((sop_id, ev.session_id))
    return active, deleted, written


def current(evs) -> list:
    """Active SOP records in stable delivery order: (created, sop_id)."""
    active, _ = fold(evs)
    return sorted(active.values(),
                  key=lambda r: (r["created"], r["sop_id"]))


def mandatory(evs) -> list:
    """Active MANDATORY SOP records, in the same ``(created, sop_id)`` delivery
    order ``current()`` uses.

    Mandatory is a tier, not a label: these are the procedures that hold on
    every task in every session, so they render first and un-elided in the
    STATE.md block (see ``block``) and are the only ones re-asserted on every
    prompt (see ``sopgate``). Deriving them here rather than at each consumer
    keeps one definition of "mandatory" for the block, the sidecar, and the
    CLI."""
    return [r for r in current(evs) if r.get("mandatory")]


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
           session_id=None, project=None, evs=None, mandatory=None):
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
    if mandatory is not None:
        # Passed through uncoerced so `validate_payload` can reject a
        # non-boolean. ``None`` means "leave the tier as it is", which is what
        # keeps a title-only update from demoting a mandatory procedure.
        payload["mandatory"] = mandatory
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
    ev = ev_mod.append_event(
        "sop", payload, session_id=session_id,
        tags=["sop"] + [t for t in (tags or []) if t != "sop"],
        project=project)
    # Refresh the hot-path cache from the events we ALREADY hold plus the one
    # just appended, rather than re-reading the store: `read_all` measures
    # 0.36 s on the live ledger, and paying that twice per write to derive a
    # file we can derive for free would be pure waste. Best-effort — the
    # ledger is the source of truth and an unwritable sidecar must never fail
    # an append that already succeeded (the same contract
    # `promptgate.train_and_store` keeps for its audit event).
    try:
        write_sidecar(project=project, evs=list(evs) + [ev])
    except Exception:                                       # noqa: BLE001
        pass
    return ev


# --- the mandatory-SOP sidecar (hot-path cache) --------------------------------
#
# `sopgate` re-asserts mandatory SOPs on EVERY user prompt. It must not fold the
# ledger to do it: `events.read_all` measures 0.36 s on this store (26k events)
# and the whole UserPromptSubmit hook currently costs about that much, so a live
# fold there would double the latency of every prompt the user types. So the
# mandatory set is materialized here, exactly as `promptgate` materializes
# `rules/triggers.json` and `playbook` is built at train time — derived,
# regenerable data stays out of the append-only chain and off the hot path.
#
# Written on every `record()` and refreshed at session start (`hive hook
# session_start`), which is the ONLY self-healing path: reads deliberately have
# no ledger fallback, because a fallback would reintroduce the 0.36 s cost on
# precisely the path this cache exists to protect.

SIDECAR_NAME = "mandatory_sops.json"


def sidecar_path(project=None):
    return paths.project_dir(project) / SIDECAR_NAME


def global_sidecar_path():
    """The cross-namespace sidecar, one level above every project dir.

    "Mandatory" was asked for as *every session*, and a per-project file cannot
    mean that. `paths.detect_project` resolves a namespace per repo, so a
    procedure registered while working in one repo would be invisible in the
    next — even though the UserPromptSubmit hook is registered globally in
    ~/.claude/settings.json and fires in all of them. The global file is what
    closes that gap; the per-project one stays as the registry's own view.

    A FILE at the store root, declared in `paths.STORE_ROOT_FILES` so no
    namespace can be named after it: a project called `mandatory_sops.json`
    used to become a directory standing where `sopgate` reads this file on
    every prompt."""
    return paths.store_file(SIDECAR_NAME)


def _project_rows(project, evs) -> list:
    """The mandatory slice for one project, as sidecar rows.

    `session_id` rides along because the sidecar is the ONLY thing `sopgate`
    reads, and `sopgate` composes MANDATORY procedure text into the operator's
    prompt on every turn — the highest-trust surface in the system. Dropping the
    author here is what left that surface with no authorship concept at all:
    `advice.resolve_sops` could partition agent-written rows out of a spawned
    agent's brief because it folds the ledger and sees `ev.session_id`, while
    the operator's own prompt could not, because this projection had already
    thrown it away. `""` for rows folded from events written before session
    stamping — absent authorship stays unknown authorship (R8).

    `agent_authored` rides along BECAUSE `session_id` alone is the wrong
    question, and this is the only place that can answer the right one. `fold`
    overwrites `session_id` on every event, so the folded value names the LAST
    editor: one human title-only edit on an agent-written procedure made the row
    read as human, and `sopgate` -- which sees only this file -- then composed
    that agent's body inside the operator's per-prompt directive fence while
    `sop.block` was correctly withholding the same row from STATE.md. Nothing
    reported the disagreement, because `probe()` counted the withheld rows it
    could see and there were none.

    Computed here, once, from the history `write_sidecar` already holds. That is
    what keeps `sopgate` off the ledger: its standing constraint is that the
    prompt path must not fold events, and a boolean in the projection is how an
    accumulated fact crosses to a surface that may not re-derive it.

    Absent on an older sidecar, deliberately -- `sopgate.is_agent_authored`
    falls back to the prefix test when the key is missing, so a sidecar written
    before this change behaves exactly as it did rather than failing shut.

    `validated` rides along for the same reason (blueprint P1d): the row's tags
    stay in the ledger, `sopgate` may not fold it, and `advice.classify_author`
    needs to know ANY applied write was stamped `sop-validated` to label the body
    `[SYSTEM SOP - SYSTEM VALIDATED]`. Absent on an older sidecar, where the
    row reads as the operator's -- the label it had before there were labels."""
    name = project or paths.detect_project()
    tainted = agent_touched_ids(evs)
    return [{"project": name, "sop_id": r["sop_id"], "title": r["title"],
             "text": r["text"], "version": r["version"],
             "session_id": str(r.get("session_id") or ""),
             "agent_authored": bool(is_agent_authored(r)
                                    or r["sop_id"] in tainted),
             "validated": r.get("validated") is True}
            for r in mandatory(evs)]


def write_sidecar(project=None, evs=None) -> dict:
    """Materialize the active mandatory SOPs and return the per-project view.

    Writes TWO files. The per-project sidecar is the registry's own
    materialization. The global one is what `sopgate` actually reads, so a
    mandatory SOP reaches every session on the machine rather than only
    sessions whose detected project happens to be the one it was registered in.

    The global write is a read-modify-write that replaces only THIS project's
    slice, leaving other projects' rows intact. Raises on an unwritable path —
    callers on a best-effort path wrap it; callers that want to know (the CLI)
    do not."""
    if evs is None:
        evs = ev_mod.read_all(project)
    rows = _project_rows(project, evs)
    data = {
        "written_at": ev_mod.now_iso(),
        "sops": [{k: v for k, v in r.items() if k != "project"} for r in rows],
    }
    paths.write_text_atomic(sidecar_path(project),
                            json.dumps(data, indent=2, ensure_ascii=False))

    name = project or paths.detect_project()
    merged = [r for r in (read_global_sidecar().get("sops") or [])
              if isinstance(r, dict) and r.get("project") != name]
    merged += rows
    # Group by project, and ONLY by project: the sort is stable, so each
    # project's rows keep the `(created, sop_id)` order `mandatory()` gave
    # them. Sorting on sop_id too would re-alphabetize within a project and
    # reshuffle procedures already in force whenever a new one is added —
    # which is exactly the delivery order the registry promises not to churn.
    merged.sort(key=lambda r: str(r.get("project") or ""))
    # KNOWN RACE, accepted rather than fixed — the same one `style._write_modes`
    # documents. Each write is atomic, but the read-modify-write around it is
    # not, so two projects writing simultaneously can drop one slice. Blast
    # radius is one project's mandatory SOPs missing until the slice is next
    # rewritten: `hive project` always rewrites it, and `hive state` does through
    # `heal_sidecar`, which compares the slice with the per-project file. That
    # comparison is what makes the second true -- before it, the heal read a
    # MISSING slice as current and left it missing.
    paths.write_text_atomic(
        global_sidecar_path(),
        json.dumps({"written_at": data["written_at"], "sops": merged},
                   indent=2, ensure_ascii=False))
    return data


def read_global_sidecar() -> dict:
    """The cross-namespace sidecar as a dict, or ``{}`` when missing,
    unreadable, corrupt, or wrong-shaped. Total, for the same reason
    ``read_sidecar`` is: its caller runs on every prompt."""
    try:
        data = json.loads(global_sidecar_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("sops"), list):
            return {}
        return data
    except Exception:                                       # noqa: BLE001
        return {}


def read_sidecar(project=None) -> dict:
    """The sidecar as a dict, or ``{}`` for missing, unreadable, corrupt, or
    wrong-shaped. Total, like ``style._read_modes``: the caller is a hook on
    every prompt, and injecting nothing is always a safe failure while raising
    there is a user-visible outage."""
    try:
        data = json.loads(sidecar_path(project).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("sops"), list):
            return {}
        return data
    except Exception:                                       # noqa: BLE001
        return {}


def _rows_current(rows) -> bool:
    """Every row a dict carrying a boolean `agent_authored` and a boolean
    `validated` -- the two keys `sopgate` labels a row from without folding."""
    return all(isinstance(r, dict) and isinstance(r.get("agent_authored"), bool)
               and isinstance(r.get("validated"), bool) for r in rows)


def _slice_key(rows) -> list:
    """What a slice of sidecar rows DELIVERS, as comparable data: one
    ``(sop_id, version, agent_authored, validated)`` per row, None for a
    non-dict row."""
    return [(r.get("sop_id"), r.get("version"), r.get("agent_authored"),
             r.get("validated"))
            if isinstance(r, dict) else None for r in (rows or [])]


def heal_sidecar(project=None) -> bool:
    """Rebuild the mandatory-SOP sidecars when missing, corrupt or STALE; True
    when it rebuilt. Total: session start calls it, and printing STATE.md is
    that command's job.

    `record` writes both files on every change, so this fires only for a store
    that never had one (fresh clone, new worktree), one that was corrupted, or
    one an older build wrote -- and each would otherwise fail SILENTLY forever,
    since `sopgate` deliberately has no ledger fallback. Gated on a parse of the
    small files rather than on `exists()`, so the 0.36 s `read_all` is paid only
    when one of them is actually wrong.

    STALE is a row without a boolean `agent_authored`. A sidecar from before the
    flag carries only the FOLDED `session_id`, and `sopgate` falls back to the
    prefix test over that -- which is the laundering case the flag closes: one
    human edit on an agent-written procedure reads as human. Healing only on a
    missing file left such a store delivering the laundered body until some
    unrelated `hive sop` write happened to rebuild it.

    STALE, too, is a row without a boolean `validated` (blueprint P1d): a build
    from before the key rewrites the sidecar without it, and such a row would
    be labelled HUMAN VERIFIED over a ledger that says SYSTEM VALIDATED.

    MISSING, too, is a slice of the global file that no longer DELIVERS what the
    per-project file does, compared per row as `(sop_id, version,
    agent_authored, validated)`. `write_sidecar`'s read-modify-write can drop
    one project's slice when
    two projects write at once, and a check of the rows alone passed the empty
    slice as current -- so the prompt fence went without this project's
    mandatory procedures until an unrelated write rebuilt them.

    Only THIS namespace's slice of the global file is checked. Rebuilding
    another namespace's rows means folding its ledger, which session start here
    must not pay; each namespace heals its own slice at its own session start.
    Until then another namespace's missing or flag-less slice stays as it is:
    healed by that namespace's own session start, a `hive sop` write there, or
    a projector rebuild of it -- and `autosync.run_all` rebuilds only the
    namespaces with new transcript activity."""
    try:
        name = project or paths.detect_project()
        local = read_sidecar(project)
        shared = read_global_sidecar()
        mine = [r for r in (shared.get("sops") or [])
                if not isinstance(r, dict) or r.get("project") == name]
        if (local and shared and _rows_current(local["sops"])
                and _rows_current(mine)
                and _slice_key(mine) == _slice_key(local["sops"])):
            return False
        write_sidecar(project)
        return True
    except Exception:                                       # noqa: BLE001
        return False


def ledger_body(payload) -> str:
    """Self-describing markdown body for this event's LEDGER entry, so keyword
    recall finds SOPs by id, title, or body text. Used by the projector."""
    head = f"[{payload.get('action', '')}] SOP {payload.get('sop_id', '')}"
    if payload.get("mandatory") is True:
        head += " (MANDATORY)"
    title = (payload.get("title") or "").strip()
    if title:
        head += f" — {title}"
    text = (payload.get("text") or "").strip()
    return head + ("\n\n" + text if text else "")


# --- authorship partition for the composed surfaces --------------------------------
#
# `block` and `digest_block` compose SOP TITLES and BODIES into STATE.md and the
# auto-loaded MEMORY.md digest. That is the highest-trust surface in the system:
# every session on this machine reads it without being asked, and nothing in it
# is labelled as coming from anywhere in particular.
#
# The amplification is the threat, not the row. Every spawned orchestration child
# holds `hive` on PATH with its own `MINDHIVE_SESSION`, and `hive sop add` is one
# command; so a prompt-injected agent in one wave could write a procedure that
# briefed every later session as standing project history.
#
# `advice.resolve_sops` closed this for a spawned agent's brief and `sopgate.plan`
# closed it for the operator's per-prompt fence. Both cited this same reasoning,
# and both left THIS path open -- the two guarded surfaces were the narrower ones.
#
# One definition of the prefix family, deliberately: `advice.is_orchestration_session`
# owns it, `sopgate` already delegates there rather than keeping a copy, and this
# does the same. The import is LAZY because `mindnet.advice` pulls in `mindnet.inject`
# -> `controller` / `sidecar` / `tasks`, and `sop` is imported by the projector on a
# path that must not drag that subtree in. Same idiom as `plugins.recommend_for`.


def safe_title(value) -> str:
    """Whitespace-collapsed, length-capped title.

    Titles flow verbatim into agent-steering context, so a newline in one must
    not be able to forge a second directive line -- or, here specifically, a
    second `### MANDATORY SOP` heading. `sopgate._safe_title` delegates to this
    rather than restating it: two spellings of a sanitizer is exactly the drift
    that makes one of them stop working.

    That argument applies one level up as well, which is why the RULE now lives
    in `provenance.safe_line` and this is a named, width-bound view of it: five
    surfaces needed the same collapse, and five copies of it is the same drift
    at a larger scale. Behaviour is unchanged -- `provenance.TITLE_CHARS` is
    120 and the cut is still bare.

    A title is also where a forged provenance label rides (blueprint P1d,
    review round 3): every surface that shows one -- STATE.md, MEMORY.md,
    `hive sop preflight|list|show`, the briefing, the prompt fence -- reads it
    through here, so this is where `provenance.defang_title` runs, AFTER the
    collapse (a label split across lines is one line by then) and the cut. The
    swap is bracket for parenthesis, so the cap still holds."""
    return provenance.defang_title(
        provenance.safe_line(value, provenance.TITLE_CHARS))


def is_agent_authored(row) -> bool:
    """Whether an SOP record was written by a spawned orchestration agent.

    Total and FAILS OPEN, matching `advice.is_orchestration_session`'s own
    contract: a junk row, a missing id, or an import that raises answers False
    and the row composes. Absent authorship is unknown authorship -- refusing
    every SOP written before session stamping would delete the feature to close
    a second-order hole.

    **A MITIGATION, NOT A BOUNDARY. Do not build on this as if it were one.**
    The predicate is a prefix test over `session_id`, and `session_id` is
    attacker-supplied: `hive sop add --session <anything>` is a documented flag
    and `MINDHIVE_SESSION` is a plain environment variable, both reachable by
    any spawned agent with a shell. An agent that knows this rule can write
    under a human-looking id and compose normally.

    What the partition removes is the DEFAULT case -- an agent doing what it
    ordinarily does, inheriting the id its parent gave it, no longer briefs
    every later session as project history. Closing it properly means stamping
    authorship somewhere the child cannot reach, which is a change to how the
    orchestrator spawns children, not to how a renderer composes them."""
    if not isinstance(row, dict):
        return False
    # The predicate itself lives in `provenance`, which every composing surface
    # now shares; this stays as the ROW-shaped spelling because `partition_authored`
    # and `sopgate.probe` both hold rows, not ids.
    return provenance.is_agent_authored(row.get("session_id"))


def agent_touched_ids(evs) -> set:
    """Every ``sop_id`` whose content an orchestration agent has written.

    Accumulated over the history, for `provenance.agent_touched`'s reason:
    `fold` keeps the LATEST editor's `session_id`, so a single human edit would
    otherwise launder an agent's procedure onto the auto-loaded surface.

    Counted over the writes the fold APPLIED (`_fold`), not over every event
    that carries the id. sop ids are chosen by whoever writes, and guessable --
    the registry's slugs are its own documentation -- so the event-level rule
    let an agent pre-poison an id it had never written: a `delete` of an id not
    yet registered, or a title-only `update` the fold skips, changed nothing,
    and the operator's later `hive sop add` under that id was withheld from
    every surface as agent-written. A delete is not a write either: it removes
    content rather than composing any, so an agent deleting the operator's
    procedure does not taint the operator's re-add of it.

    Writes DO accumulate across a delete. A revival keeps what the reviving
    event does not replace -- a title-only `update` on a deleted id brings its
    old body back -- so clearing at a delete would launder in two steps what a
    human edit cannot launder in one. The cost is the one
    `test_an_agent_edit_withholds_the_operators_own_sop_and_names_it` already
    accepts: an id an agent really wrote stays withheld under the operator's
    later re-add, and the footer names it.

    `roadmap_render` keeps the event-level rule on purpose: its marker LABELS a
    record rather than withholding it, so over-approximating there costs a
    banner, not the operator's own procedure. The ids are what the withheld
    footer names, so the session values are dropped here."""
    return {sop_id for sop_id, session in _fold(evs or [])[2]
            if provenance.is_agent_authored(session)}


def partition_authored(rows, evs=None):
    """``(composed, withheld)`` -- the rows a rendering may show, and the count
    it must not show but must still ADMIT to.

    Returning a count rather than dropping silently is the project's established
    rule for this exact control (`Advice.withheld`, `sopgate.probe`'s
    `withheld`): exclusion that leaves no trace is indistinguishable from a
    registry that was always empty, so an operator could never tell an attack
    from a quiet week."""
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    # `evs` is optional so the predicate stays callable on rows alone, but every
    # composing surface passes it -- without the history, authorship collapses
    # back to "whoever edited last".
    tainted = agent_touched_ids(evs) if evs is not None else set()

    def _agent(row):
        return is_agent_authored(row) or (row.get("sop_id") in tainted)

    composed = [r for r in rows if not _agent(r)]
    withheld = [r for r in rows if _agent(r)]
    return composed, len(withheld)


def withheld_footer(n, ids=None) -> str:
    """The one line that keeps the partition above from being silent, or "".

    It NAMES the ids when it has them. A bare count tells an operator that
    something is missing without telling them what, and the availability
    trade-off this partition accepts -- an agent that writes into a procedure
    keeps it off the delivery surface, including a procedure the operator
    wrote -- is only acceptable while the operator can see exactly which one
    went and go read it."""
    if not n:
        return ""
    named = ""
    if ids:
        # `safe_title`, not `str`: the footer is composed into the block, so an
        # id renders through the same collapse as a title, whatever validated
        # it upstream.
        shown = sorted(safe_title(i) for i in ids if i)[:6]
        if shown:
            named = " (" + ", ".join("`%s`" % s for s in shown)
            if len(ids) > len(shown):
                named += ", and %d more" % (len(ids) - len(shown))
            named += ")"
    return (f"_{n} procedure(s){named} withheld from this block: an "
            f"orchestration agent, not a human, wrote into them. They are still "
            f"in the ledger and in `hive sop list` -- they are only kept off the "
            f"surface every session auto-loads. Read one with `hive sop show "
            f"<id>`._")


def preflight_mandatory(evs) -> dict:
    """The mandatory-SOP half of `hive sop preflight`'s report, PARTITIONED
    like every other delivery surface.

    `docs/sops/execution-discipline.md` opens with "BEFORE ANY WORK -- run
    `hive sop preflight` and read the output", so this list is read into every
    session as standing directives rather than browsed as a registry. It has
    three duties, and the command once lacked each of them:

    * **the partition** -- an agent's mandatory row that STATE.md withheld was
      listed here as one of the operator's standing procedures;
    * **`safe_title` on the id and the title**, in `--json` as in text -- a
      newline in an agent-written title forged a second `- [id vN] ...` line;
    * **the admission, with the ids** -- an exclusion that leaves no trace is
      indistinguishable from a registry that was always empty, and a bare count
      says something is missing without saying what to go and read.

    Here rather than beside the handler that prints it
    (`commands/registry_sop.py`; `__main__.py` before the P9 split): it is
    rendering policy shared with `block` and `digest_block`, and the handler
    modules are on a size cap."""
    rows = [r for r in (mandatory(evs) or []) if isinstance(r, dict)]
    composed, withheld = partition_authored(rows, evs)
    kept = {id(r) for r in composed}
    return {
        "mandatory_sops": [{"sop_id": safe_title(r["sop_id"]),
                            "title": safe_title(r["title"]),
                            "version": r["version"]} for r in composed],
        "mandatory_withheld": withheld,
        "mandatory_withheld_ids": [safe_title(r["sop_id"]) for r in rows
                                   if id(r) not in kept],
    }


def preflight_mandatory_lines(rep) -> list:
    """The text rendering of `preflight_mandatory`'s half of *rep*.

    The header counts what is SHOWN, so the admission names what is not.
    Without it the header read `(0)` on a registry holding procedures, followed
    by the "add one" hint -- the one reading an operator must never get from
    this command. The hint now prints only when nothing was withheld either."""
    must = rep.get("mandatory_sops") or []
    out = [f"\nMandatory SOPs ({len(must)}):"]
    out += [f"  - [{r['sop_id']} v{r['version']}] {r['title']}" for r in must]
    footer = withheld_footer(rep.get("mandatory_withheld") or 0,
                             rep.get("mandatory_withheld_ids"))
    if footer:
        out.append("  " + footer.strip("_"))
    if not must and not footer:
        out.append("  (none — add one: hive sop add <id> --title … --file … "
                   "--mandatory)")
    return out


def block(evs) -> str:
    """The STANDARD OPERATING PROCEDURES block for STATE.md (and therefore the
    auto-loaded MEMORY.md). Empty string when no SOP is active. Deterministic;
    budgeted by BLOCK_FULL_BUDGET (see module docstring).

    MANDATORY SOPs render FIRST and are EXEMPT from BLOCK_FULL_BUDGET. Both
    halves matter. A procedure declared to hold on every task, degraded to a
    one-line pointer because ten optional SOPs happened to be created before
    it, is a silent failure of the exact property the tier exists to state —
    and it would fail quietly, since an elided body still looks like a
    delivered SOP. The budget then applies to the ordinary SOPs alone, so it
    still bounds the part of the block that can grow without limit."""
    sops = current(evs)
    if not sops:
        return ""
    _all_ids = [r.get("sop_id") for r in sops]
    sops, withheld = partition_authored(sops, evs)
    _kept = {r.get("sop_id") for r in sops}
    _gone = [i for i in _all_ids if i not in _kept]
    must = [r for r in sops if r.get("mandatory")]
    rest = [r for r in sops if not r.get("mandatory")]
    out = [
        "# STANDARD OPERATING PROCEDURES (managed — `hive sop`)",
        "_Standing procedures for this project: when a task matches an SOP's "
        "scope, follow it. Full registry & history: `hive sop list` / "
        "`hive sop show <id>`. Manage: `hive sop add|update|delete`._",
        "",
    ]
    if must:
        out += [
            "## MANDATORY — follow these on every task, without being asked",
            "_Not scope-matched like the SOPs below: these hold for every "
            "prompt in every session. They are also re-asserted on each prompt "
            "so they cannot decay over a long session._",
            "",
        ]
        for i, r in enumerate(must, 1):
            out.append(f"### MANDATORY SOP {i}: {safe_title(r['title'])}  "
                       f"`[{r['sop_id']} v{r['version']}]`")
            # Verbatim, unless the body carries a label-shaped bracket: this
            # surface shows no provenance label, so a forged one would be
            # the only one a reader sees (blueprint P1d, review round 3).
            out.append(provenance.defang_labels(r["text"]))
            out.append("")
    spent = 0
    for i, r in enumerate(rest, 1):
        out.append(f"## SOP {i}: {safe_title(r['title'])}  "
                   f"`[{r['sop_id']} v{r['version']}]`")
        if spent + len(r["text"]) <= BLOCK_FULL_BUDGET:
            out.append(provenance.defang_labels(r["text"]))
            spent += len(r["text"])
        else:
            out.append(f"_(body elided for context budget — read it with: "
                       f"`hive sop show {r['sop_id']}`)_")
        out.append("")
    foot = withheld_footer(withheld, _gone)
    if foot:
        out += [foot, ""]
    return "\n".join(out).rstrip("\n") + "\n"


def digest_block(evs, injected_ids=()) -> str:
    """The MEMORY.md rendering of the same registry: titles and pointers.

    A sibling of `block()` rather than a `digest=` branch inside it, so "the
    STATE.md rendering did not move" is structural rather than something a test
    has to keep proving. STATE.md is the full-fidelity read every pointer here
    points at; this is the copy that has to survive a loader which stops partway
    through the file.

    Two tiers, handled differently because their delivery differs:

    * **Scope-matched SOPs** render as one line each — id, version, title, and
      the command that prints the body — up to `DIGEST_ITEM_CAP`, then a count
      of what was not listed. Nothing is lost: `hive sop list` and `hive sop
      show <id>` reach all of it, and STATE.md still carries every body inline.
    * **Mandatory SOPs** keep their bodies UNLESS `injected_ids` says that same
      body is already being re-asserted verbatim on every prompt. That
      condition is the whole safety argument. `sopgate.probe` can report
      `pointers_only` or `partial` — states in which the per-prompt pass ships
      a pointer rather than the text — and eliding the body here in that state
      would leave it in front of no reader at all. The caller derives the ids
      from that measurement; an empty set (missing or corrupt sidecar, verdict
      `silent`) therefore keeps every body, which is the fail-safe direction.

    Pure: same `(evs, injected_ids)` in, same bytes out. `injected_ids` is used
    only as a membership test, so a set, frozenset, list or tuple all render
    identically.
    """
    sops = current(evs)
    if not sops:
        return ""
    _all_ids = [r.get("sop_id") for r in sops]
    sops, withheld = partition_authored(sops, evs)
    _kept = {r.get("sop_id") for r in sops}
    _gone = [i for i in _all_ids if i not in _kept]
    injected = set(injected_ids or ())
    must = [r for r in sops if r.get("mandatory")]
    rest = [r for r in sops if not r.get("mandatory")]
    out = [
        "# STANDARD OPERATING PROCEDURES (managed — `hive sop`)",
        "_Digest view: titles and pointers. Full bodies ride STATE.md "
        "(`hive state`); read one with `hive sop show <id>`, list them all with "
        "`hive sop list`._",
        "",
    ]
    if must:
        out += [
            "## MANDATORY — follow these on every task, without being asked",
            "",
        ]
        for i, r in enumerate(must, 1):
            out.append(f"### MANDATORY SOP {i}: {safe_title(r['title'])}  "
                       f"`[{r['sop_id']} v{r['version']}]`")
            if r["sop_id"] in injected:
                # Wording constraint, not style: tests/test_sync.py asserts the
                # literal "SOP injection" is ABSENT from a healthy digest, and
                # healthy is exactly when this line renders.
                out.append("_Body omitted here because the prompt hook "
                           "re-asserts it verbatim every turn — read it with: "
                           f"`hive sop show {r['sop_id']}`._")
            else:
                out.append(provenance.defang_labels(r["text"]))
            out.append("")
    if rest:
        out += ["## Scope-matched SOPs — follow when the task falls in scope",
                ""]
        for r in rest[:DIGEST_ITEM_CAP]:
            out.append(f"- `{r['sop_id']}` v{r['version']}: {safe_title(r['title'])} "
                       f"— `hive sop show {r['sop_id']}`")
        extra = len(rest) - DIGEST_ITEM_CAP
        if extra > 0:
            out.append(f"- _(+{extra} more active SOP(s) — `hive sop list`)_")
        out.append("")
    foot = withheld_footer(withheld, _gone)
    if foot:
        out += [foot, ""]
    return "\n".join(out).rstrip("\n") + "\n"
