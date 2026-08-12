# src/mindhive/roadmap.py
"""Event-sourced roadmap registry: the ask, the plan, and the progress.

A roadmap captures one piece of work as the user framed it: the original
prompt (**what was asked**), the ordered plan steps built from it (**what the
plan is**), and per-step statuses (**how far along it is**). The registry is
event-sourced on the append-only ledger — every ``set`` / ``step`` /
``interrupt`` / ``resume`` / ``complete`` is one semantic ``roadmap`` event,
and the current registry is a deterministic, total fold over those events in
timestamp order (the ``sop.py`` idiom).

Two projections consume it:

- The dashboard's Oversee section renders each plan as an interactive roadmap
  (prompt, progress bar, clickable step nodes) via ``/api/oversee``.
- ``notice()`` renders every *interrupted* plan as a STATE.md block — the
  out-of-usage marker. When a run is cut off mid-plan (Claude usage limits:
  the loop engine's ``api_cap`` / ``budget_cap``, or a manual
  ``hive roadmap interrupt``), the marker preserves the creation context so
  the next session auto-loads the big picture and resumes from the next step
  instead of starting over.

Like the change-log and SOPs, roadmap events are registry/audit content:
consolidation never absorbs them and display-time dedupe never collapses them.
"""
from . import events as ev_mod
from .sop import ID_RE

ACTIONS = ("set", "step", "interrupt", "resume", "complete", "archive", "link")
STATUSES = ("pending", "active", "done", "skipped")

# `archive` is the honest exit for a plan whose last step no one can check.
# Before it, a plan had exactly two ways out: every step done (`complete`), or
# nothing — so a plan ending in "merge the PR" or "watch it run for a week" sat
# in Oversee forever, because the fold had no way to say "we stopped tracking
# this, and here is why". Marking such a step done instead would have been a
# lie, and the ledger is the audit trail. So `archive` retires the plan and
# REQUIRES a reason, following the `loop_run_dismissed` tombstone precedent:
# the retirement is an append-only event carrying its own justification, never
# an edit to the history that pinned it open.
ARCHIVED = "archived"

# Size caps bound every event (and therefore the STATE.md notice): the marker
# must preserve context, not become a context-budget regression (invariant 4).
MAX_PROMPT_CHARS = 2000
MAX_TITLE_CHARS = 120
MAX_STEPS = 50
MAX_STEP_CHARS = 300
MAX_NOTE_CHARS = 500
MAX_CAUSE_CHARS = 120
MAX_REASON_CHARS = 300

# The work link: how a plan points at the code it produced.
#
# `roadmap.fold` carried no branch, PR, or commit field, so nothing checkable
# connected a plan to its work — which is the whole reason a finished card could
# not retire itself. These three caps bound the link the same way every
# neighbouring field is bounded, and they are REJECTED rather than truncated: a
# silently shortened commit sha would probe as an unknown object and report
# `probe_failed` forever, a failure whose cause would be invisible.
MAX_WORK_BRANCH_CHARS = 250
MAX_WORK_COMMITS = 20
MAX_WORK_COMMIT_CHARS = 40      # a full sha; abbreviations are shorter

# Notice rendering caps: done steps collapse to a tail, pending to a head, so
# one interrupted plan costs a bounded slice of the auto-loaded MEMORY.md.
_NOTICE_DONE_TAIL = 3
_NOTICE_PENDING_HEAD = 3

# The provenance marker for an injected step, in ONE place. It shipped as three
# separate string literals and three of the renderers that read the same step
# dicts never got one — including `autoresume.resume_prompt`, the leg that
# delivers an interrupted plan back into Claude, so the resumed agent could not
# tell injected work from the plan it started with. A shared helper is what makes
# "marked wherever rendered" checkable instead of aspirational.
NEW_MARKER = "(new) "


def new_prefix(step) -> str:
    """``NEW_MARKER`` when *step* was added by the replenishment injector, else
    "". Every text renderer of a roadmap step goes through this."""
    return NEW_MARKER if (step or {}).get("new") else ""


# --- the work link ------------------------------------------------------------
# Three flat keys rather than one nested object, matching the relay fields below:
# a flat payload folds forward through every historical event unchanged, and each
# key is independently optional because the three become knowable at different
# moments — the branch when work starts, the commits as it lands, the PR number
# only once one is opened.
WORK_KEYS = ("work_branch", "work_pr", "work_commits")


def work_link(rec) -> dict:
    """The recorded pointer to the code this plan produced, shaped as the
    keyword arguments ``repo.merge_state`` takes. One reader for the three keys,
    so no caller has to know which of them a given plan happens to carry."""
    rec = rec or {}
    return {"branch": rec.get("work_branch") or "",
            "pr": rec.get("work_pr") or 0,
            "commits": list(rec.get("work_commits") or ())}


def has_work_link(rec) -> bool:
    """True when *rec* points at anything checkable."""
    link = work_link(rec)
    return bool(link["branch"] or link["pr"] or link["commits"])


def describe_work_link(rec) -> str:
    """One-line human rendering of the link, or "" when there is none. Every
    surface that shows a work link goes through this — the CLI, the LEDGER body
    that keyword recall searches, and the retirement note — so a plan is
    findable by its branch or PR number from any of them (the ``new_prefix``
    precedent: one renderer, not three literals that drift)."""
    link = work_link(rec)
    parts = []
    if link["pr"]:
        parts.append(f"PR #{link['pr']}")
    if link["branch"]:
        parts.append(f"branch {link['branch']}")
    if link["commits"]:
        parts.append(", ".join(c[:12] for c in link["commits"]))
    return " · ".join(parts)


def _validate_work_link(payload) -> None:
    """Check the optional work-link fields, which ANY action may carry.

    Rejected rather than coerced, on the same reasoning as the relay below: a
    work link exists to be probed against git, and a malformed one probes as
    ``probe_failed`` — a permanent unknown whose cause is invisible from the
    card it silences. Better to refuse the write while the caller still holds
    the value."""
    branch = payload.get("work_branch")
    if branch is not None:
        if not isinstance(branch, str) or not branch.strip():
            raise ValueError("roadmap work_branch must be non-empty text")
        if len(branch) > MAX_WORK_BRANCH_CHARS:
            raise ValueError(
                f"roadmap work_branch exceeds {MAX_WORK_BRANCH_CHARS} chars")

    pr = payload.get("work_pr")
    if pr is not None:
        # bool is an int in Python, and a `work_pr` of True folding to PR #1
        # would probe a real, unrelated pull request.
        if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
            raise ValueError("roadmap work_pr must be a positive integer "
                             "pull-request number")

    commits = payload.get("work_commits")
    if commits is not None:
        if not isinstance(commits, list):
            raise ValueError("roadmap work_commits must be a list of commit "
                             "shas")
        if len(commits) > MAX_WORK_COMMITS:
            raise ValueError(
                f"roadmap work_commits exceeds {MAX_WORK_COMMITS} entries — "
                "record the merge commit, not every commit")
        for sha in commits:
            if not isinstance(sha, str) or not sha.strip():
                raise ValueError("roadmap work_commits entries must be "
                                 "non-empty commit shas")
            if len(sha.strip()) > MAX_WORK_COMMIT_CHARS:
                raise ValueError(
                    f"roadmap work_commits entry exceeds "
                    f"{MAX_WORK_COMMIT_CHARS} chars — that is not a commit sha")


def _fold_work_link(rec, payload) -> None:
    """Merge whatever work-link fields *payload* carries into *rec*, in place.

    An absent key leaves the recorded link alone, so the three can be attached
    one at a time as they become knowable, and a ``set`` that says nothing about
    the link keeps it. That carry-forward is deliberate rather than lazy:
    ``sync_from_todos`` appends a fresh ``set`` on every material TodoWrite
    change and never supplies a link, so without it one more todo edit would
    silently drop the PR number off an auto-synced plan — the same erasure the
    ``new`` flag already had to be protected from."""
    branch = str(payload.get("work_branch") or "").strip()
    if branch:
        rec["work_branch"] = branch
    pr = payload.get("work_pr")
    if isinstance(pr, int) and not isinstance(pr, bool) and pr > 0:
        rec["work_pr"] = pr
    commits = payload.get("work_commits")
    if isinstance(commits, list):
        clean = [str(c).strip() for c in commits if str(c).strip()]
        if clean:
            rec["work_commits"] = clean[:MAX_WORK_COMMITS]


def _norm_steps(raw):
    """Normalize a ``set`` payload's steps into ``[{text, status, note, new}]``.
    Plain strings default to pending; when no step carries an explicit
    status, the first becomes active so the roadmap always shows where work
    starts. Assumes validate_payload already accepted *raw*.

    ``new`` marks a step that was NOT in the plan the work started from — one
    the replenishment injector added because the record proved it outstanding
    (see replenish.py). It defaults to False, so every historical event folds
    exactly as it did before the key existed, and it is provenance rather than
    recency: nothing clears it, including completing the step."""
    steps, explicit = [], False
    for s in raw:
        if isinstance(s, dict):
            status = s.get("status") or "pending"
            explicit = explicit or bool(s.get("status"))
            steps.append({"text": str(s.get("text", "")).strip(),
                          "status": status,
                          "note": str(s.get("note") or "").strip(),
                          "new": bool(s.get("new"))})
        else:
            steps.append({"text": str(s).strip(), "status": "pending",
                          "note": "", "new": False})
    if steps and not explicit:
        steps[0]["status"] = "active"
    return steps


def validate_payload(payload) -> None:
    """Enforce the roadmap event contract at every append surface (CLI
    ``hive roadmap``, raw ``hive append roadmap``, engine snapshot). The
    event store stays type-agnostic — the contract lives here, mirroring
    changelog.py / sop.py."""
    if not isinstance(payload, dict):
        raise ValueError("roadmap payload must be an object")
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError(
            "roadmap requires an action — one of: " + ", ".join(ACTIONS))
    plan_id = payload.get("plan_id") or ""
    if not ID_RE.match(plan_id):
        raise ValueError(
            "roadmap requires a kebab-case plan_id (lowercase letters, "
            "digits, hyphens; max 64 chars), e.g. a run id or 'auth-refactor'")
    title = payload.get("title")
    if title is not None and len(str(title).strip()) > MAX_TITLE_CHARS:
        raise ValueError(f"roadmap title exceeds {MAX_TITLE_CHARS} chars")
    note = payload.get("note")
    if note is not None and len(str(note)) > MAX_NOTE_CHARS:
        raise ValueError(f"roadmap note exceeds {MAX_NOTE_CHARS} chars")
    _validate_work_link(payload)
    if action == "set":
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(
                "roadmap set requires a prompt — the original ask this plan "
                "was built from")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(
                f"roadmap prompt exceeds {MAX_PROMPT_CHARS} chars — keep the "
                "headline of the ask; long context belongs in the ledger's "
                "own prompt capture")
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("roadmap set requires a non-empty steps list")
        if len(steps) > MAX_STEPS:
            raise ValueError(f"roadmap set exceeds {MAX_STEPS} steps — plan "
                             "at a coarser grain")
        for s in steps:
            if isinstance(s, dict):
                text = str(s.get("text") or "").strip()
                status = s.get("status")
                if status is not None and status not in STATUSES:
                    raise ValueError(
                        "roadmap step status must be one of: "
                        + ", ".join(STATUSES))
                # Checked rather than coerced: `_norm_steps` runs bool() over
                # whatever arrives, so a string "false" would fold as True and
                # permanently mislabel a step as injected work.
                if "new" in s and not isinstance(s.get("new"), bool):
                    raise ValueError(
                        "roadmap step 'new' must be true or false — it marks a "
                        "step the replenishment injector added, not one from "
                        "the original plan")
            else:
                text = str(s).strip()
            if not text:
                raise ValueError("roadmap steps must be non-empty text")
            if len(text) > MAX_STEP_CHARS:
                raise ValueError(
                    f"roadmap step exceeds {MAX_STEP_CHARS} chars")
    elif action == "step":
        index = payload.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError("roadmap step requires a non-negative integer "
                             "index (0-based)")
        if payload.get("status") not in STATUSES:
            raise ValueError(
                "roadmap step requires a status — one of: "
                + ", ".join(STATUSES))
    elif action == "interrupt":
        cause = str(payload.get("cause") or "").strip()
        if not cause:
            raise ValueError(
                "roadmap interrupt requires a cause, e.g. 'usage_limit'")
        if len(cause) > MAX_CAUSE_CHARS:
            raise ValueError(
                f"roadmap cause exceeds {MAX_CAUSE_CHARS} chars")
        _validate_relay(payload)
    elif action == "archive":
        # The reason is REQUIRED, and that is the whole point of the action. An
        # archive without one would retire the card and destroy the only thing
        # that made retiring it honest — why a human decided the unverifiable
        # step was settled.
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError(
                "roadmap archive requires a reason — what settled the step "
                "nothing can verify, e.g. 'PR #103 merged' or 'superseded by "
                "the async-jobs plan'")
        if len(reason) > MAX_REASON_CHARS:
            raise ValueError(f"roadmap reason exceeds {MAX_REASON_CHARS} chars")
    elif action == "link":
        if not any(payload.get(k) is not None for k in WORK_KEYS):
            raise ValueError(
                "roadmap link requires at least one of work_branch, work_pr, "
                "work_commits — it exists to point the plan at its code")
    # resume / complete: plan_id is enough.


# The relay: what an interrupted loop hands the next session.
MAX_RELAY_CONTEXT_CHARS = 1000
MAX_RELAY_FINGERPRINTS = 64


def _validate_relay(payload) -> None:
    """Check the optional relay fields an interrupted LOOP plan may carry.

    Capped and rejected rather than truncated, like every neighbouring field: a
    silently shortened fingerprint list would be a cache that looks complete and
    is not, and the failure it causes — work re-proposed that was already spent —
    would be invisible.

    All three are optional. An interrupt written by `hive roadmap interrupt`, or
    by any version of this code before the relay existed, carries none of them."""
    context = payload.get("relay_context")
    if context is not None:
        if not isinstance(context, str):
            raise ValueError("roadmap relay_context must be text")
        if len(context) > MAX_RELAY_CONTEXT_CHARS:
            raise ValueError(
                f"roadmap relay_context exceeds {MAX_RELAY_CONTEXT_CHARS} chars")

    fingerprints = payload.get("spent_fingerprints")
    if fingerprints is not None:
        if not isinstance(fingerprints, list):
            raise ValueError("roadmap spent_fingerprints must be a list")
        if len(fingerprints) > MAX_RELAY_FINGERPRINTS:
            raise ValueError(
                f"roadmap spent_fingerprints exceeds {MAX_RELAY_FINGERPRINTS} "
                f"entries")
        for entry in fingerprints:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    "roadmap spent_fingerprints entries must be non-empty text")

    dry_rounds = payload.get("dry_rounds")
    if dry_rounds is not None:
        if isinstance(dry_rounds, bool) or not isinstance(dry_rounds, int) \
                or dry_rounds < 0:
            raise ValueError("roadmap dry_rounds must be a non-negative integer")


def _progress(steps) -> dict:
    return {"done": sum(1 for s in steps if s["status"] == "done"),
            "total": len(steps)}


def _enforce_single_active(steps, changed_index):
    """One current step at a time. When *changed_index* was set active, demote
    every other active step to pending; when nothing is active anymore (the
    active step just finished), promote the first pending step so the roadmap
    keeps pointing at where work is."""
    if steps and 0 <= changed_index < len(steps) \
            and steps[changed_index]["status"] == "active":
        for i, s in enumerate(steps):
            if i != changed_index and s["status"] == "active":
                s["status"] = "pending"
        return
    if any(s["status"] == "active" for s in steps):
        return
    for s in steps:
        if s["status"] == "pending":
            s["status"] = "active"
            return


def fold(evs) -> dict:
    """Deterministic fold of roadmap events into plan_id -> record. Total —
    it never raises on historical data: a step on an unknown plan or an
    out-of-range index is skipped, unknown actions are skipped, and lifecycle
    actions on unknown plans are no-ops. ``set`` upserts (a re-plan replaces
    the steps wholesale); re-``set`` after complete or archive revives the plan.

    Every record predates the work link, and every record predates the relay
    before that, so the fold reads both defensively: the ``work_*`` keys appear
    on a record only when some event actually supplied them, and their absence
    is the ordinary case rather than a degraded one."""
    plans: dict = {}
    for ev in evs:
        if ev.type != "roadmap" or not isinstance(ev.payload, dict):
            continue
        p = ev.payload
        action, plan_id = p.get("action"), p.get("plan_id")
        if action not in ACTIONS or not plan_id:
            continue
        rec = plans.get(plan_id)
        if action == "set":
            steps_raw = p.get("steps")
            if not isinstance(steps_raw, list) or not steps_raw:
                continue
            steps = _norm_steps(steps_raw)
            if not all(s["text"] and s["status"] in STATUSES for s in steps):
                continue
            prompt = str(p.get("prompt") or "").strip()
            if not prompt:
                continue
            fresh = {
                "plan_id": plan_id,
                "prompt": prompt,
                "title": str(p.get("title") or "").strip(),
                "steps": steps,
                "status": "active",
                "cause": "",
                "interrupted_at": "",
                "created": rec["created"] if rec else ev.ts,
                "updated": ev.ts,
                "version": (rec["version"] + 1) if rec else 1,
                "session_id": ev.session_id,
                "tags": list(ev.tags or []),
                "progress": _progress(steps),
            }
            # The work link survives a re-plan. A `set` replaces the STEPS
            # wholesale — it says nothing about where the code went, and the
            # auto-sync writer that re-`set`s on every TodoWrite change never
            # supplies one.
            for key in WORK_KEYS:
                if rec and key in rec:
                    fresh[key] = rec[key]
            _fold_work_link(fresh, p)
            plans[plan_id] = fresh
            continue
        if rec is None:
            continue  # lifecycle action on an unknown plan: nothing to do
        rec = dict(rec, steps=[dict(s) for s in rec["steps"]])
        if action == "step":
            index, status = p.get("index"), p.get("status")
            if not isinstance(index, int) or isinstance(index, bool) \
                    or not (0 <= index < len(rec["steps"])) \
                    or status not in STATUSES:
                continue
            rec["steps"][index]["status"] = status
            note = str(p.get("note") or "").strip()
            if note:
                rec["steps"][index]["note"] = note
            _enforce_single_active(rec["steps"], index)
        elif action == "interrupt":
            cause = str(p.get("cause") or "").strip()
            if not cause:
                continue
            rec["status"] = "interrupted"
            rec["cause"] = cause
            rec["interrupted_at"] = ev.ts
            note = str(p.get("note") or "").strip()
            if note:
                rec["interrupt_note"] = note
            # The relay, carried only when the interrupt supplied it. Read
            # defensively: an interrupt written by `hive roadmap interrupt`, or by
            # any version before the relay existed, has none of these, and the
            # fold must not invent them.
            context = str(p.get("relay_context") or "").strip()
            if context:
                rec["relay_context"] = context
            spent = p.get("spent_fingerprints")
            if isinstance(spent, list):
                rec["spent_fingerprints"] = [f for f in spent
                                             if isinstance(f, str) and f.strip()]
            dry = p.get("dry_rounds")
            if isinstance(dry, int) and not isinstance(dry, bool) and dry >= 0:
                rec["dry_rounds"] = dry
        elif action == "resume":
            rec["status"] = "active"
            rec["cause"] = ""
            rec["interrupted_at"] = ""
            rec.pop("interrupt_note", None)
            # The relay was consumed by the resume; keeping it would let a later
            # reader treat a live plan's stale baton as current.
            for key in ("relay_context", "spent_fingerprints", "dry_rounds"):
                rec.pop(key, None)
        elif action == "complete":
            rec["status"] = "completed"
        elif action == "archive":
            reason = str(p.get("reason") or "").strip()
            if not reason:
                continue        # an archive with no reason is not an archive
            rec["status"] = ARCHIVED
            rec["archive_reason"] = reason
            rec["archived_at"] = ev.ts
        elif action == "link":
            # `link` records where the work went and touches nothing else, so a
            # plan can be pointed at its branch or PR without disturbing its
            # status, its steps, or the interruption marker it may be carrying.
            pass
        _fold_work_link(rec, p)
        rec["updated"] = ev.ts
        rec["version"] += 1
        rec["session_id"] = ev.session_id
        rec["tags"] = list(ev.tags or [])
        rec["progress"] = _progress(rec["steps"])
        plans[plan_id] = rec
    return plans


RETIRED_STATUSES = ("completed", ARCHIVED)


def plans(evs, include_completed=False) -> list:
    """Plan records in stable delivery order: (created, plan_id). Retired plans
    — completed AND archived — are hidden by default; both earned their exit
    from every view, one by finishing and one by saying why it stopped.

    The flag keeps its original name because it is the argument every caller
    already passes; what changed is that "completed" was never the only way out."""
    out = [r for r in fold(evs).values()
           if include_completed or r["status"] not in RETIRED_STATUSES]
    return sorted(out, key=lambda r: (r["created"], r["plan_id"]))


def get(evs, plan_id):
    """The record for *plan_id* regardless of status, or None."""
    return fold(evs).get(plan_id)


def interrupted(evs) -> list:
    """Interrupted plans in delivery order — the out-of-usage markers."""
    return [r for r in plans(evs) if r["status"] == "interrupted"]


# --- retirement, on evidence ---------------------------------------------------

def retire_landed(project=None, evs=None, probe=None, root=None,
                  session_id=None) -> list:
    """Retire every live plan whose work link PROVES the work is finished, and
    report on the ones it could not.

    This is the payoff of the work link: the card that would otherwise sit in
    Oversee forever, waiting on a step no automation can tick, retires because
    something checkable said so. A merged PR completes the plan; a PR closed
    without merging archives it with that as the reason. Anything else is left
    exactly where it was.

    Returns one outcome per live plan — including the untouched ones, each
    carrying the state and, when unknown, the reason. A sweep that retires
    nothing must not look like a sweep that failed, and the only way to tell
    them apart is to report what was examined."""
    from . import repo as repo_mod          # lazy: the registry has no need of
    probe = probe or repo_mod.merge_state   # git plumbing until a sweep runs
    if evs is None:
        evs = ev_mod.read_all(project)
    outcomes = []
    for rec in plans(evs):
        answer = probe(root=root, **work_link(rec))
        outcome = {"plan_id": rec["plan_id"], "action": "",
                   "state": answer["state"], "reason": answer["reason"],
                   "detail": answer["detail"], "evidence": answer["evidence"]}
        if answer["state"] == repo_mod.MERGED:
            outcome["action"] = "complete"
            record("complete", rec["plan_id"], note=answer["evidence"],
                   session_id=session_id, project=project, evs=evs)
        elif answer["state"] == repo_mod.CLOSED:
            outcome["action"] = "archive"
            record("archive", rec["plan_id"],
                   reason=f"work abandoned — {answer['evidence']}",
                   session_id=session_id, project=project, evs=evs)
        outcomes.append(outcome)
    return outcomes


def next_step(rec):
    """The step the work should pick back up at: the active step, else the
    first pending one, else None (everything done/skipped)."""
    for s in rec["steps"]:
        if s["status"] == "active":
            return s
    for s in rec["steps"]:
        if s["status"] == "pending":
            return s
    return None


def steps_from_plan_items(items) -> list:
    """Build explicit-status steps from a loop fix_plan split
    (``task.plan_items``): checked work is done, the first unchecked-required
    item is where work stopped (active), the rest stay pending, and
    Future/Optional items are skipped — the engine's interruption snapshot.
    Texts are truncated to the contract cap so an oversized checklist item can
    never invalidate (and thereby silently drop) the whole marker."""
    steps = []
    for it in items.get("done") or []:
        steps.append(_step_from_item(it, "done"))
    for i, it in enumerate(items.get("planned") or []):
        steps.append(_step_from_item(it, "active" if i == 0 else "pending"))
    for it in items.get("not_yet") or []:
        steps.append(_step_from_item(it, "skipped"))
    return steps[:MAX_STEPS]


def _step_from_item(item, status: str) -> dict:
    """One roadmap step from one parsed checklist item.

    This is where the loop surface's `(new)` marker becomes the roadmap step's
    `new` boolean — the single hop that lets injected work survive an interruption
    with its provenance intact and render through the five existing consumers of
    that flag rather than needing a sixth.

    Accepts a bare string as well as a `PlanItem` so a caller holding a plain
    checklist line (and every stored payload written before the parse existed)
    still folds."""
    text = getattr(item, "text", item)
    return {"text": str(text)[:MAX_STEP_CHARS], "status": status,
            "new": bool(getattr(item, "is_new", False))}


# --- session auto-sync (TodoWrite -> roadmap) --------------------------------
# The deterministic writer behind the Oversee panel for interactive sessions:
# the session's own TodoWrite telemetry IS its plan + live progress, piped in
# by the PostToolUse hook (hookcapture.record_post_tool_use). plan_id equals
# the session id, so the plan rides the session's Oversee card unchanged.

AUTO_TAG = "auto"
_TODO_STATUS = {"completed": "done", "in_progress": "active",
                "pending": "pending"}


def todos_to_steps(todos) -> list:
    """Explicit-status steps from a TodoWrite ``todos`` list. Malformed items
    degrade to nothing rather than raising — the hook path must never fail a
    session over a payload quirk — and texts are truncated to the contract cap
    so one oversized todo can't invalidate the whole sync."""
    steps = []
    if not isinstance(todos, list):
        return steps
    for t in todos:
        if not isinstance(t, dict):
            continue
        text = str(t.get("content") or "").strip()
        status = _TODO_STATUS.get(t.get("status"))
        if text and status:
            steps.append({"text": text[:MAX_STEP_CHARS], "status": status})
    return steps[:MAX_STEPS]


def _session_prompt(evs, session_id) -> str:
    """The session's original ask: headline of its first user_prompt event.
    Capture may lag the first TodoWrite, so a stable placeholder stands in —
    sync_from_todos treats the later prompt upgrade as a material change."""
    from .briefing import headline
    for e in evs:
        if e.session_id == session_id and e.type == "user_prompt":
            text = e.payload.get("text", "") if isinstance(e.payload, dict) \
                else str(e.payload)
            t = headline(str(text))
            if t:
                return t[:MAX_PROMPT_CHARS]
    return f"session {session_id}"      # ID_RE caps sid at 64 chars — fits


def sync_from_todos(session_id, todos, project=None, evs=None, ts=None):
    """Auto-declare/update the session's roadmap from its TodoWrite state.
    Appends at most one ``set`` per call (the fold's upsert replaces steps
    wholesale and revives interrupted plans), and only on a material change —
    identical steps and prompt are a no-op so routine re-writes don't bloat
    the ledger. Plans not tagged ``auto`` are human-declared and never
    touched. Returns the appended event, or None when nothing applies.

    Steps marked ``new`` are CARRIED FORWARD. A ``set`` replaces the step list
    wholesale, and this writer builds that list from TodoWrite alone — so
    without this, one more TodoWrite after the replenishment injector had
    written (a resumed session is enough) would silently delete the injected
    work. TodoWrite stays authoritative for its own steps; the injected ones
    ride along behind them.

    **Injected steps win the ``MAX_STEPS`` tie-break, and todos are trimmed to
    make room.** The first cut appended them and re-capped, which meant a
    session at the 50-todo cap dropped every injected step — permanently, since
    the fingerprint was already spent and every later round refuses the
    condition as a duplicate. The two losses are not symmetric: a trimmed todo
    reappears on the next TodoWrite, an evidence-backed injected step never
    comes back."""
    sid = str(session_id or "")
    if not ID_RE.match(sid):
        return None
    steps = todos_to_steps(todos)
    if not steps:
        return None
    if evs is None:
        evs = ev_mod.read_all(project)
    rec = fold(evs).get(sid)
    if rec is not None and AUTO_TAG not in (rec.get("tags") or []):
        return None
    if rec is not None:
        carried = {s["text"]: dict(s) for s in rec["steps"] if s.get("new")}
        # A todo whose text matches an injected step INHERITS the flag rather
        # than replacing it. Copying the step into TodoWrite is the natural
        # response to reading "(new) …" in STATE.md, and dropping the carried
        # copy there would quietly erase provenance the flag promises to keep.
        merged = [dict(s, new=True) if carried.pop(s["text"], None) else s
                  for s in steps]
        room = max(0, MAX_STEPS - len(carried))
        steps = merged[:room] + list(carried.values())[:MAX_STEPS]
    prompt = _session_prompt(evs, sid)
    if rec is not None and rec["prompt"] == prompt \
            and [(s["text"], s["status"], bool(s.get("new")))
                 for s in rec["steps"]] \
            == [(s["text"], s["status"], bool(s.get("new")))
                for s in steps]:
        return None
    payload = {"action": "set", "plan_id": sid, "prompt": prompt,
               "title": prompt[:MAX_TITLE_CHARS], "steps": steps}
    validate_payload(payload)
    return ev_mod.append_event("roadmap", payload, session_id=sid,
                               tags=["roadmap", AUTO_TAG], ts=ts,
                               project=project)


def autoclose_session(session_id, project=None, evs=None, ts=None):
    """Session-end lifecycle for auto-synced plans (run by the SessionEnd
    hook): every step done/skipped -> ``complete`` (earned exit from every
    view); unfinished steps -> ``interrupt`` with cause ``session_end`` so
    the STATE.md marker carries the plan's creation context to the next
    session. Human-declared plans are left alone. Returns the appended
    event, or None when nothing applies."""
    sid = str(session_id or "")
    if not ID_RE.match(sid):
        return None
    if evs is None:
        evs = ev_mod.read_all(project)
    rec = fold(evs).get(sid)
    if rec is None or rec["status"] == "completed" \
            or AUTO_TAG not in (rec.get("tags") or []):
        return None
    if any(s["status"] in ("pending", "active") for s in rec["steps"]):
        if rec["status"] == "interrupted":
            return None
        payload = {"action": "interrupt", "plan_id": sid,
                   "cause": "session_end"}
    else:
        payload = {"action": "complete", "plan_id": sid}
    validate_payload(payload)
    return ev_mod.append_event("roadmap", payload, session_id=sid,
                               tags=["roadmap", AUTO_TAG], ts=ts,
                               project=project)


def record(action, plan_id, prompt=None, title=None, steps=None, index=None,
           status=None, note=None, cause=None, reason=None, work_branch=None,
           work_pr=None, work_commits=None, tags=None, session_id=None,
           project=None, evs=None):
    """Append one roadmap event after registry-aware guards: lifecycle
    actions on a missing plan (or in the wrong state) are almost always typos
    — fail with the corrective command instead of appending a dead event.
    (The raw ``hive append roadmap`` path skips these guards on purpose:
    validate_payload still holds the shape contract, and fold() stays total.)"""
    payload = {"action": action, "plan_id": plan_id}
    if prompt is not None:
        payload["prompt"] = prompt.strip()
    if title is not None:
        payload["title"] = title.strip()
    if steps is not None:
        payload["steps"] = steps
    if index is not None:
        payload["index"] = index
    if status is not None:
        payload["status"] = status
    if note is not None:
        payload["note"] = note.strip()
    if cause is not None:
        payload["cause"] = cause.strip()
    if reason is not None:
        payload["reason"] = reason.strip()
    if work_branch is not None:
        payload["work_branch"] = work_branch.strip()
    if work_pr is not None:
        payload["work_pr"] = work_pr
    if work_commits is not None:
        payload["work_commits"] = [str(c).strip() for c in work_commits
                                   if str(c).strip()]
    validate_payload(payload)
    if evs is None:
        evs = ev_mod.read_all(project)
    current = fold(evs)
    rec = current.get(plan_id)
    if action != "set":
        if rec is None:
            known = ", ".join(sorted(current)) or "(none)"
            raise ValueError(
                f"no roadmap '{plan_id}' — declare it first with: "
                f"hive roadmap set {plan_id} --prompt … --step … "
                f"(known ids: {known})")
        # A retired plan (completed or archived) accepts only the two actions
        # that name its own state — so each still reaches its precise "already …"
        # error below — plus `link`, because a PR number is routinely learned
        # after the plan has left every view.
        if rec["status"] in RETIRED_STATUSES \
                and action not in ("complete", "archive", "link"):
            raise ValueError(
                f"roadmap '{plan_id}' is {rec['status']} — revive it with a "
                f"new: hive roadmap set {plan_id} …")
        if action == "step" and not (
                isinstance(index, int) and 0 <= index < len(rec["steps"])):
            raise ValueError(
                f"step index out of range — roadmap '{plan_id}' has "
                f"{len(rec['steps'])} steps (0-based)")
        if action == "interrupt" and rec["status"] == "interrupted":
            raise ValueError(
                f"roadmap '{plan_id}' is already interrupted "
                f"(cause: {rec['cause']}) — resume it with: "
                f"hive roadmap resume {plan_id}")
        if action == "resume" and rec["status"] != "interrupted":
            raise ValueError(
                f"roadmap '{plan_id}' is not interrupted — nothing to resume")
        if action == "complete" and rec["status"] == "completed":
            raise ValueError(f"roadmap '{plan_id}' is already completed")
        if action == "archive" and rec["status"] == ARCHIVED:
            raise ValueError(
                f"roadmap '{plan_id}' is already archived "
                f"(reason: {rec.get('archive_reason', '')})")
        if action == "archive" and rec["status"] == "completed":
            raise ValueError(
                f"roadmap '{plan_id}' is already completed — it finished, so "
                f"there is no unverifiable step left to archive")
    return ev_mod.append_event(
        "roadmap", payload, session_id=session_id,
        tags=["roadmap"] + [t for t in (tags or []) if t != "roadmap"],
        project=project)


def _step_line(s, marker) -> str:
    note = f" — {s['note']}" if s.get("note") else ""
    return f"- {marker} {new_prefix(s)}{s['text']}{note}"


def notice(evs) -> str:
    """The "⚠ Interrupted plan" STATE.md block — the out-of-usage marker.
    Empty string when no plan is interrupted. Deterministic; each plan's step
    list is capped (done tail + pending head) so the marker preserves the big
    picture without regressing the auto-loaded context budget."""
    marks = interrupted(evs)
    if not marks:
        return ""
    try:
        # Delivery status per plan (autoresume.py). Lazy + guarded: the
        # delivery telemetry must never break the marker itself.
        from . import autoresume as _autoresume
        ar_recs = _autoresume.fold(evs)
    except Exception:
        ar_recs = {}
    out = ["## ⚠ Interrupted plan — creation context preserved", "",
           "Work below was interrupted before it finished (e.g. Claude usage "
           "ran out). The original ask and the plan's progress are the "
           "creation context: pick the work up at the next step instead of "
           "starting over. Resume: `hive roadmap resume <id>` · finished: "
           "`hive roadmap complete <id>` · full detail: "
           "`hive roadmap show <id>` · auto-deliver back into Claude after "
           "the usage cooldown: `hive autoresume schedule <id>`.", ""]
    for r in marks:
        head = r["title"] or r["plan_id"]
        prog = r["progress"]
        out.append(f"### {head}  `[{r['plan_id']}]`")
        out.append(f"- **Interrupted:** {r['interrupted_at']} "
                   f"(cause: {r['cause']})")
        out.append(f"- **Asked:** {r['prompt']}")
        out.append(f"- **Progress:** {prog['done']}/{prog['total']} steps done")
        ar = ar_recs.get(r["plan_id"])
        if ar and ar["status"] == "scheduled":
            out.append(
                f"- **Auto-resume:** marker re-enters Claude at {ar['due']} "
                f"(attempt {ar['attempt']}, timer: {ar['timer']}) — deliver "
                f"now: `hive autoresume fire {r['plan_id']}`")
        elif ar and ar["status"] == "fired":
            detail = f" — {ar['detail']}" if ar.get("detail") else ""
            out.append(f"- **Auto-resume:** delivery {ar['outcome']} @ "
                       f"{ar['updated']}{detail}")
        done = [s for s in r["steps"] if s["status"] == "done"]
        hidden = len(done) - _NOTICE_DONE_TAIL
        if hidden > 0:
            out.append(f"- [x] … {hidden} earlier step(s) done")
        for s in done[-_NOTICE_DONE_TAIL:]:
            out.append(_step_line(s, "[x]"))
        nxt = next_step(r)
        if nxt is not None:
            out.append(_step_line(nxt, "[→] NEXT:"))
        pending = [s for s in r["steps"]
                   if s["status"] == "pending" and s is not nxt]
        for s in pending[:_NOTICE_PENDING_HEAD]:
            out.append(_step_line(s, "[ ]"))
        if len(pending) > _NOTICE_PENDING_HEAD:
            out.append(f"- [ ] … {len(pending) - _NOTICE_PENDING_HEAD} "
                       "more step(s)")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def ledger_body(payload) -> str:
    """Self-describing markdown body for this event's LEDGER entry, so
    keyword recall finds plans by id, prompt, step text, or cause."""
    action = payload.get("action", "")
    head = f"[{action}] roadmap {payload.get('plan_id', '')}"
    title = str(payload.get("title") or "").strip()
    if title:
        head += f" — {title}"
    parts = [head]
    prompt = str(payload.get("prompt") or "").strip()
    if prompt:
        parts.append(f"Asked: {prompt}")
    steps = payload.get("steps")
    if isinstance(steps, list) and steps:
        lines = []
        for s in steps:
            if isinstance(s, dict):
                # The marker rides the LEDGER body too, so keyword recall can
                # tell injected work from planned work.
                lines.append(f"- [{s.get('status', 'pending')}] "
                             f"{new_prefix(s)}"
                             f"{str(s.get('text') or '').strip()}")
            else:
                lines.append(f"- {str(s).strip()}")
        parts.append("\n".join(lines))
    if action == "step":
        parts.append(f"step {payload.get('index')} -> "
                     f"{payload.get('status', '')}")
    cause = str(payload.get("cause") or "").strip()
    if cause:
        parts.append(f"cause: {cause}")
    link = describe_work_link(payload)
    if link:
        parts.append(f"work: {link}")
    reason = str(payload.get("reason") or "").strip()
    if reason:
        parts.append(f"reason: {reason}")
    note = str(payload.get("note") or "").strip()
    if note:
        parts.append(note)
    return "\n\n".join(parts)
