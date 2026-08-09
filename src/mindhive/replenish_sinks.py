"""Where replenished work lands.

`replenish.py` decides WHAT is outstanding and refuses to know where it goes; a
sink knows one surface and nothing about lenses or gating. They live in separate
modules so the split is structural rather than a convention someone can quietly
break — the core's own test asserts it does not import a sink.

The roadmap sink is the interactive session's. It appends steps to the plan whose
`plan_id` is the session id, which is the existing convention (`sync_from_todos`
writes the session's TodoWrite state there). Three things fall out of that reuse
rather than being built:

* the steps ride the session's own Oversee card while it is alive;
* `roadmap.autoclose_session`, already wired into the SessionEnd hook, marks the
  plan interrupted when steps remain unfinished;
* `roadmap.notice` then renders it into STATE.md, which `sync` folds into the
  auto-loaded MEMORY.md the next session reads before anything else.

That last step is the forcing mechanism on this surface. Nothing asks an agent to
keep going; the work is simply in front of the next session, marked `new`.

Phase 3's loop sink lands checklist items instead, so that
`loop.task.required_remaining_count` rises and `loop.safeguards.should_exit`
refuses to exit. Same core, different sink.
"""
from pathlib import Path

from . import events as ev_mod
from . import paths as paths_mod
from . import roadmap as rm_mod
from .loop import task as task_mod
from .replenish import Placement
from .sop import ID_RE


def _checklist_line(title: str) -> str:
    """One `fix_plan.md` line for an injected candidate.

    The title is flattened to a single line and stripped of anything that would
    change the file's STRUCTURE rather than its content: an embedded newline
    would split one item into two (the second of which is not a checklist item
    at all, so it would silently stop being required), and a leading checklist
    marker would produce `- [ ] - [ ] …`."""
    text = " ".join(str(title or "").split())
    while True:
        stripped = task_mod._UNCHECKED_ITEM.match(text) \
            or task_mod._CHECKED_ITEM.match(text)
        if not stripped:
            break
        text = stripped.group(1).strip()
    # The marker is written here rather than carried in the title, so a title
    # that happens to start with "(new)" cannot masquerade as injected work and
    # an injected item cannot lose its marker to a title rewrite.
    text = task_mod._NEW_PREFIX.sub("", text, count=1).strip()
    return f"- [ ] {task_mod.NEW_MARKER} {text}" if text else ""


def _insert_at(lines) -> int:
    """Index of the first Future/Optional heading, else the end.

    Items after that heading are non-blocking (`task.required_remaining_count`
    stops counting there), so landing below it would write work the exit gate
    never sees — the injection would look successful and force nothing."""
    for i, line in enumerate(lines):
        if task_mod._FUTURE_HEADING.match(line):
            return i
    return len(lines)


def fix_plan_sink(loop_dir, run_id="", append=True):
    """A sink that appends kept candidates to a loop run's required checklist.

    This is the loop surface's forcing mechanism, and it is entirely structural:
    an unchecked item above the Future heading raises
    `loop.task.required_remaining_count`, and `loop.safeguards.should_exit`
    already refuses to exit while any remains. Nothing in the prompt asks the
    agent to keep going (SOP `agentic-loops` §1).

    Returns ``(placements, dropped)``. A missing `fix_plan.md` drops everything
    rather than creating one: the file is the run's only source of required
    work, and inventing it would hand the loop a plan nobody wrote.

    ``append=False`` runs the identical placement math and writes nothing."""
    path = Path(loop_dir) / "fix_plan.md"

    def sink(kept):
        kept = list(kept or [])
        if not kept or not path.is_file():
            return [], len(kept)
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            return [], len(kept)
        lines = body.splitlines()
        at = _insert_at(lines)
        placed, new_lines = [], []
        # The index a placement reports is the item's position among the
        # REQUIRED items, which is what `steps_from_plan_items` orders by — not
        # its line number, which counts headings and blank lines too.
        base = len(task_mod.plan_items(body)["planned"])
        for c in kept:
            line = _checklist_line(c.title)
            if not line:
                continue
            placed.append(Placement(c.fingerprint, str(run_id or ""),
                                    base + len(new_lines)))
            new_lines.append(line)
        if not new_lines:
            return [], len(kept)
        merged = lines[:at] + new_lines + lines[at:]
        text = "\n".join(merged)
        if body.endswith("\n") or not body:
            text += "\n"
        if append:
            try:
                paths_mod.write_text_atomic(path, text)
            except OSError:
                # Known on Windows: os.replace raises PermissionError on the
                # DESTINATION while any process holds a handle to it (the
                # dashboard reads this file for the Oversee card). A half-
                # written fix_plan.md would corrupt the run's only source of
                # required work, so report everything unplaced — the
                # fingerprints stay unspent and the next round tries again.
                return [], len(kept)
        return placed, len(kept) - len(placed)

    return sink


def roadmap_step_status(evs):
    """``(plan_id, step_index) -> status | None`` over the roadmap fold.

    The auto-tuner's resolver (`replenish.outcomes`). It lives here for the same
    reason the sinks do: `replenish.py` decides what is outstanding and must not
    import a surface — its own architecture test asserts that. Returns None for
    a plan or index that no longer resolves, which the tuner reads as "unknown"
    rather than as success.

    The fold runs ONCE per call, not once per lookup: `roadmap.fold` walks the
    whole ledger, and a resolver that folded per landed row would turn a linear
    pass into a quadratic one on a store with thousands of injections."""
    plans = rm_mod.fold(evs or [])

    def status(plan_id, index):
        rec = plans.get(plan_id)
        if rec is None or not (0 <= index < len(rec["steps"])):
            return None
        return rec["steps"][index]["status"]

    return status


def roadmap_sink(session_id, project=None, evs=None, ts=None, append=True):
    """A sink that appends kept candidates to *session_id*'s roadmap plan.

    Returns ``(placements, dropped)``. `dropped` is what did not fit under
    `roadmap.MAX_STEPS`; those candidates get no audit row, so their fingerprints
    stay unspent and the next round may propose them again.

    ``append=False`` runs the identical placement math and writes nothing. The
    flag lives here rather than in a separate preview sink because a dry run that
    exercised different code would be answering a different question — and the
    core's own ``append`` flag only governs the audit rows, so without this one a
    `--dry-run` would still leave a real roadmap step behind."""
    sid = str(session_id or "")
    if not ID_RE.match(sid):
        # Not silently degraded to "nothing landed": a session id that cannot be
        # a plan_id means every future round for this session lands nowhere, and
        # that is a wiring bug the caller has to see.
        raise ValueError(
            f"cannot land work for session {session_id!r}: a plan_id must be "
            "kebab-case (lowercase letters, digits, hyphens; max 64 chars)")

    def sink(kept):
        rows = ev_mod.read_all(project) if evs is None else evs
        rec = rm_mod.fold(rows).get(sid)
        if rec is not None and rm_mod.AUTO_TAG not in (rec.get("tags") or []):
            # The guard `sync_from_todos` and `autoclose_session` both enforce.
            # A `set` overwrites the record's tags and title wholesale, so
            # without this a human-declared plan that happened to share the
            # session id would be silently converted to an auto-managed one and
            # become subject to autoclose. Report the candidates as unplaced —
            # they stay unspent and the next round may try again.
            return [], len(kept)
        # dict() per step: the fold hands back its own records, and the payload
        # we are about to build must not alias them.
        existing = [dict(s) for s in (rec["steps"] if rec else [])]
        room = max(0, rm_mod.MAX_STEPS - len(existing))
        take = list(kept)[:room]
        dropped = len(kept) - len(take)
        if not take:
            return [], dropped

        steps = existing + [
            {"text": c.title[:rm_mod.MAX_STEP_CHARS], "status": "pending",
             "new": True}
            for c in take]
        # An existing plan keeps its own ask. A plan created here inherits the
        # session's first prompt, the same source `sync_from_todos` uses, so the
        # card reads as this session's work rather than as the injector's.
        prompt = rec["prompt"] if rec else rm_mod._session_prompt(rows, sid)
        payload = {"action": "set", "plan_id": sid, "prompt": prompt,
                   "title": prompt[:rm_mod.MAX_TITLE_CHARS], "steps": steps}
        # Validated even on a dry run: a preview that skipped the contract could
        # promise work the real run would then refuse to write.
        rm_mod.validate_payload(payload)
        if append:
            # AUTO_TAG is load-bearing, not decoration: `autoclose_session`
            # leaves human-declared plans alone, so without it the plan would
            # never be marked interrupted and would never reach STATE.md.
            ev_mod.append_event("roadmap", payload, session_id=sid,
                                tags=["roadmap", rm_mod.AUTO_TAG, "replenish"],
                                ts=ts, project=project)
        placements = [Placement(c.fingerprint, sid, len(existing) + i)
                      for i, c in enumerate(take)]
        return placements, dropped

    return sink
