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
    sop_id = payload.get("sop_id") or ""
    if not ID_RE.match(sop_id):
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
    partial update on an unknown or deleted id is skipped; ``delete`` on an
    unknown id is a no-op. Re-adding a deleted id revives it, keeping the
    original created ts and continuing its version count.

    The ``mandatory`` flag follows the same never-lose-data discipline: it is
    written only when the event actually carries the key, so a title-only
    update can never silently demote a mandatory procedure to an ordinary one.
    A first ``add`` that omits it defaults to False; an ``add`` that revives a
    deleted id and omits it keeps whatever the id had, because there the key's
    absence means "unchanged" rather than "unset".
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
                "mandatory": bool(p.get("mandatory")),
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
        if "mandatory" in p:
            rec["mandatory"] = bool(p["mandatory"])
        # No else: every record reaching here was built by the literal above,
        # which always sets the key, so the absence of `mandatory` on THIS
        # event means "unchanged" and there is nothing to default.
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

    A FILE at the store root, which `paths.list_namespaces` ignores because it
    only walks directories."""
    return paths.store_home() / SIDECAR_NAME


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
    stamping — absent authorship stays unknown authorship (R8)."""
    name = project or paths.detect_project()
    return [{"project": name, "sop_id": r["sop_id"], "title": r["title"],
             "text": r["text"], "version": r["version"],
             "session_id": str(r.get("session_id") or "")}
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
    # radius is one project's mandatory SOPs missing until its next `hive state`
    # or `hive project`, both of which rewrite the slice.
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
            out.append(f"### MANDATORY SOP {i}: {r['title']}  "
                       f"`[{r['sop_id']} v{r['version']}]`")
            out.append(r["text"])
            out.append("")
    spent = 0
    for i, r in enumerate(rest, 1):
        out.append(f"## SOP {i}: {r['title']}  `[{r['sop_id']} v{r['version']}]`")
        if spent + len(r["text"]) <= BLOCK_FULL_BUDGET:
            out.append(r["text"])
            spent += len(r["text"])
        else:
            out.append(f"_(body elided for context budget — read it with: "
                       f"`hive sop show {r['sop_id']}`)_")
        out.append("")
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
            out.append(f"### MANDATORY SOP {i}: {r['title']}  "
                       f"`[{r['sop_id']} v{r['version']}]`")
            if r["sop_id"] in injected:
                # Wording constraint, not style: tests/test_sync.py asserts the
                # literal "SOP injection" is ABSENT from a healthy digest, and
                # healthy is exactly when this line renders.
                out.append("_Body omitted here because the prompt hook "
                           "re-asserts it verbatim every turn — read it with: "
                           f"`hive sop show {r['sop_id']}`._")
            else:
                out.append(r["text"])
            out.append("")
    if rest:
        out += ["## Scope-matched SOPs — follow when the task falls in scope",
                ""]
        for r in rest[:DIGEST_ITEM_CAP]:
            out.append(f"- `{r['sop_id']}` v{r['version']}: {r['title']} "
                       f"— `hive sop show {r['sop_id']}`")
        extra = len(rest) - DIGEST_ITEM_CAP
        if extra > 0:
            out.append(f"- _(+{extra} more active SOP(s) — `hive sop list`)_")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"
