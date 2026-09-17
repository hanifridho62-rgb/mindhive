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
from . import events as ev_mod, outcomes
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


def by_session(registry) -> dict:
    """``{session_id: plan}`` — the plan each session is currently WORKING.

    The counterpart to `auto_plan_id`, which answers the same question for a
    writer. `fold` stamps every record with the session of its last event, so
    this is that stamp inverted -- and it is what lets a plan ride the card of
    the session continuing it rather than only the card of the session that
    created it.

    RETIRED plans -- completed AND archived -- are dropped: a finished plan is
    not what anyone is working on, and an archived one is the user saying so
    out loud. An INTERRUPTED plan is kept, because a session resuming one is
    working it and the card is exactly where the operator should see that.

    The predicates and the tie-break are `auto_plan_id`'s, deliberately: this
    is the same question asked by a reader instead of a writer, and the two
    answering differently is a bug the page cannot show its way out of.

    Two endpoints need this index and each was about to build its own copy;
    two copies of "whose plan is this" is the defect this module just fixed."""
    out: dict = {}
    for rec in (registry or {}).values():
        if not isinstance(rec, dict):
            continue                  # a shape this fold did not produce
        owner = rec.get("session_id")
        if not owner:
            continue
        # RETIRED, not just completed. `archive` is the other way out and it
        # is the deliberate one -- the user attached a reason -- so reading
        # only `completed` here put a plan the user had explicitly retired
        # back on a live card and back into the session cat's phase.
        if rec.get("status") in RETIRED_STATUSES:
            continue
        # AUTO only, the predicate `auto_plan_id` already applies. A session
        # that ran `hive roadmap step` against a HUMAN plan was otherwise
        # indexed here as working it, while the card kept showing the
        # session's own auto plan -- two surfaces, two answers, one session.
        if AUTO_TAG not in (rec.get("tags") or []):
            continue
        # The writer's total order, verbatim: `(updated, plan_id)`. Comparing
        # `updated` alone left same-second writes to be resolved by whichever
        # the fold happened to yield first, and same-second writes are
        # routine enough here that the ledger has its own tie-break for them.
        key = (str(rec.get("updated") or ""), str(rec.get("plan_id") or ""))
        seen = out.get(owner)
        if seen is None or key > (str(seen.get("updated") or ""),
                                  str(seen.get("plan_id") or "")):
            out[owner] = rec
    return out


def plan_for_session(registry, session_id, index=None):
    """The plan to show for one session -- its own if it has one, else the
    one it adopted. ``None`` when neither exists.

    This composition used to live only on the Oversee card, spelled inline,
    while the session cat used the adoption index alone. That is two answers
    to one question on the same page, and the card's own comment promised
    there would be one. `index` is passed in when the caller already built it,
    because both surfaces fold the registry once and neither should fold it
    twice to ask this.

    A RETIRED own plan falls through to the index rather than being returned:
    completing or archiving a plan is how the user says not-this-one, and it
    has to mean that on every surface, not just in `plans()`.
    """
    sid = str(session_id or "")
    idx = by_session(registry) if index is None else index
    own = (registry or {}).get(sid)
    if isinstance(own, dict) and own.get("status") not in RETIRED_STATUSES:
        return own
    return idx.get(sid)


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


# --- the session auto-sync layer, re-exported ---------------------------------
#
# It lives in `roadmap_auto` (this file was one line under the 1000-line cap
# before the resolver was written), and it is re-exported here because every
# caller reaches it through this module: `roadmap.sync_from_todos` in the hook
# path, `rm_mod.auto_plan_id` in the replenishment sink, and a monkeypatch on
# `roadmap.sync_from_todos` in tests/test_hookcapture.py. Imported at the
# BOTTOM on purpose: `roadmap_auto` reads this module's contract and fold, so
# the names it needs have to exist before it is loaded.
from .roadmap_auto import (  # noqa: E402,F401
    AUTO_TAG,
    WORK_TAG,
    RESUME_WINDOW_HOURS,
    _hours_before,
    _session_prompt,
    _step_shape,
    auto_plan_id,
    autoclose_session,
    sync_from_todos,
    todos_to_steps,
)

# --- the rendering layer, re-exported -----------------------------------------
#
# It lives in `roadmap_render` (this file hit the 1000-line cap a second time,
# and the anti-injection work that pushed it there landed in exactly these two
# renderers). Re-exported for the same reason as the block above: every caller
# reaches them through this module -- `roadmap.notice` in `projector`,
# `roadmap.ledger_body` in `projector` and `dashboard_get`, `roadmap.new_prefix`
# in `autoresume` and `commands/registry_roadmap`, `roadmap.NEW_MARKER` in
# `replenish_sinks`.
# `tests/test_roadmap_render.py` pins each one as the SAME object, because a
# re-export that rebound the name would satisfy `hasattr` and defeat every
# monkeypatch through it -- the failure the `dashboard.py` split shipped.
from .roadmap_render import (  # noqa: E402,F401
    NEW_MARKER,
    _NOTICE_DONE_TAIL,
    _NOTICE_PENDING_HEAD,
    _step_line,
    ledger_body,
    new_prefix,
    notice,
)


def _tags_for(action, rec, tags):
    """Tags for one `record` write, so a plan cannot lose its provenance.

    `fold` sets rec["tags"] from the LATEST event and seven call sites read it
    to decide whether a plan is auto-synced, which makes the tag a live wire.
    `record` appended ["roadmap"] for every action, so ANY of them stripped
    `auto` from an auto plan: `hive roadmap resume` on an interrupted one --
    the most natural command anyone would run on it -- made `sync_from_todos`
    return None for the rest of that plan's life and `autoclose_session` skip
    it, silently, with nothing to see in any output.

    The rule is the distinction the actions already draw. A `set` DECLARES a
    plan and may re-tag it -- `replenish_sinks` depends on exactly that, which
    is why it refuses to convert a human-declared plan rather than trusting
    its own write. Every other action UPDATES a plan that already exists, and
    an update inherits what that plan already is. A caller that names its own
    tags still gets exactly those; inheriting is the default, not an override.
    """
    explicit = [t for t in (tags or []) if t != "roadmap"]
    if explicit or action == "set" or rec is None:
        return ["roadmap"] + explicit
    # `WORK_TAG` is deliberately NOT inherited. It records that a write
    # was a session syncing its own plan, which is a fact about the WRITE;
    # inherited from the plan, a hand-typed `step` on a plan some other
    # session had worked would claim that session's authorship, and
    # `_authored_plan_id` would close the wrong plan again -- the defect
    # the tag exists to fix, re-entering through the tag.
    return ["roadmap"] + [t for t in (rec.get("tags") or [])
                          if t not in ("roadmap", WORK_TAG)]

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
        tags=_tags_for(action, rec, tags),
        project=project)


# --- the outcomes-registry probe ----------------------------------------------

def _probe(project=None) -> outcomes.Report:
    """Report on retirement's own feedback loop: can a live card still be shown
    the door by something checkable?

    The answer is decided before any git or `gh` call, by whether the plans
    carry a work link at all. `merge_state` on a plan with no branch, PR or
    commit can only ever return `unknown/no_work_link` — so an unlinked card is
    one that no sweep will ever retire, however many times the sweep runs. That
    is the state worth reporting, and it is readable from the store alone.

    Reads the ledger, which `jobs._probe` deliberately does not, because here the
    surface's state IS ledger state — there is nowhere else to look. It stays
    read-only: no probing of the outside world, no events written.
    """
    live = plans(evs=ev_mod.read_all(project))
    if not live:
        return outcomes.Report(
            outcomes.NOTHING_TO_DO,
            "no live roadmap plan — nothing is waiting on a verdict")
    linked = [rec for rec in live if has_work_link(rec)]
    if not linked:
        return outcomes.Report(
            outcomes.DEGRADED,
            f"{len(live)} live plan(s), none carrying a work link — merge "
            f"detection can only answer unknown/no_work_link, so no card here "
            f"can retire on evidence")
    if len(linked) < len(live):
        return outcomes.Report(
            outcomes.DEGRADED,
            f"{len(linked)} of {len(live)} live plans carry a work link; the "
            f"other {len(live) - len(linked)} can only ever answer "
            f"unknown/no_work_link")
    return outcomes.Report(
        outcomes.DID_WORK,
        f"{len(live)} live plan(s), every one carrying a work link a sweep can "
        f"reach a verdict on")


outcomes.register("roadmap-retirement",
                  "a plan card retires because something checkable said so, or "
                  "says which of four things stopped it",
                  _probe)
