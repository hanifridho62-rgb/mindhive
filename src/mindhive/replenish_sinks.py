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
import re
from pathlib import Path

from . import events as ev_mod
from . import paths as paths_mod
from . import roadmap as rm_mod
from .loop.task import _FUTURE_HEADING
from .replenish import Placement
from .sop import ID_RE


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
        # The plan being CONTINUED, which is not always this session's own.
        # Landing evidence-backed steps on a freshly minted plan while the
        # work sat interrupted elsewhere is exactly what made injected steps
        # look unrelated to the session that received them.
        registry = rm_mod.fold(rows)
        plan_id = rm_mod.auto_plan_id(sid, registry, now=ts)
        rec = registry.get(plan_id)
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
        # The injector ADDS work; it does not rename it. `sync_from_todos`
        # titles a continued plan with the resuming session's headline, and
        # deriving the title from the prompt here would overwrite that on the
        # next injection -- the card would snap back to the original ask.
        title = (rec.get("title") if rec else "") or prompt
        payload = {"action": "set", "plan_id": plan_id, "prompt": prompt,
                   "title": title[:rm_mod.MAX_TITLE_CHARS], "steps": steps}
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
        # `plan_id`, not `sid`: a Placement IS the audit pointer from a spent
        # fingerprint to the work it produced, and aiming it at a plan that
        # never received the step leaves the trail dead-ended.
        placements = [Placement(c.fingerprint, plan_id, len(existing) + i)
                      for i, c in enumerate(take)]
        return placements, dropped

    return sink


# --- the loop surface ---------------------------------------------------------

# The marker, written exactly as `roadmap.NEW_MARKER` spells it and exactly as
# `loop.task._NEW_MARKER` parses it back.
_NEW = rm_mod.NEW_MARKER

# Anything that could turn one line into two checklist items, or forge a heading.
# A title is derived from ledger text and file paths, so it is not hostile — but
# it is not controlled either, and a title able to inflate
# `required_remaining_count` would let one candidate refuse the exit forever.
_LINE_BREAKING = re.compile(r"\s+")
_ITEM_MARKER = re.compile(r"^\s*(?:[-*]\s*\[[^\]]*\]|#+)\s*")

# A checklist line long enough to wrap is a line nobody reads. Matches
# `roadmap.MAX_STEP_CHARS` so the same work reads identically on both surfaces.
MAX_LINE_CHARS = rm_mod.MAX_STEP_CHARS


def _one_line(text) -> str:
    """Collapse *text* to a single safe checklist fragment."""
    flat = _LINE_BREAKING.sub(" ", str(text or "")).strip()
    # Repeatedly, because "- [ ] - [ ] x" would otherwise survive one pass and
    # still forge an item.
    while True:
        stripped = _ITEM_MARKER.sub("", flat)
        if stripped == flat:
            break
        flat = stripped
    return " ".join(flat.split())


def fix_plan_line(candidate) -> str:
    """One `fix_plan.md` checklist line for a kept candidate.

    Shaped `- [ ] (new) <title>: <why>`. The `why` is the lens plus its first
    piece of evidence, which is what makes the line actionable for the agent that
    reads this file — it can see both the task and the fact that produced it
    without going to the ledger."""
    title = _one_line(candidate.title) or "(untitled)"
    first_evidence = next((e for e in (candidate.evidence or ())
                           if isinstance(e, str) and e.strip()), "")
    why = _one_line(f"{candidate.lens} lens"
                    + (f" — {first_evidence}" if first_evidence else ""))
    line = f"- [ ] {_NEW}{title}: {why}"
    return line[:MAX_LINE_CHARS]


def _insertion_index(lines) -> int:
    """Where a required item goes: immediately before the first Future/Optional
    heading, or at the end when there is none.

    `_FUTURE_HEADING` is imported from `loop.task` rather than re-expressed. That
    regex is what `required_remaining_count` and `plan_items` already split on, so
    a second copy here would diverge the first time the scaffold's wording moved —
    and the symptom would be injected work landing below the line that makes it
    non-blocking, i.e. an injector that no longer forces anything."""
    for index, line in enumerate(lines):
        if _FUTURE_HEADING.match(line):
            return index
    return len(lines)


def fix_plan_sink(loop_dir, run_id="", append=True):
    """A sink that lands kept candidates as REQUIRED checklist items.

    Returns ``(placements, dropped)``, the same contract `roadmap_sink` honours.
    The forcing mechanism is the count, not the marker: an unchecked item above
    the Future heading raises `loop.task.required_remaining_count`, and
    `loop.safeguards.should_exit` already refuses to exit while any remains.
    Nothing in a prompt asks the agent to keep going (SOP `agentic-loops` §1).

    A missing `fix_plan.md` reports everything as dropped and writes nothing. It
    does not create the file: the loop scaffolds its own directory, so an absent
    checklist means this sink was pointed somewhere that is not a loop — and
    inventing one there would put required work where no run will ever read it.
    The dropped candidates keep their fingerprints unspent, so the next round may
    try again."""
    plan_path = Path(loop_dir) / "fix_plan.md"

    def sink(kept):
        candidates = list(kept)
        if not candidates:
            return [], 0
        try:
            original = plan_path.read_text(encoding="utf-8")
        except OSError:
            return [], len(candidates)

        lines = original.splitlines()
        # Index among REQUIRED items, which is what a placement addresses — a raw
        # line number would move whenever anything above it was edited.
        required_before = sum(
            1 for line in lines[:_insertion_index(lines)]
            if line.lstrip().startswith(("- [", "* [")))

        at = _insertion_index(lines)
        new_lines = lines[:at] + [fix_plan_line(c) for c in candidates] + lines[at:]
        text = "\n".join(new_lines)
        if original.endswith("\n"):
            text += "\n"

        if append:
            # Atomic: a torn write would corrupt the loop's only source of
            # required work, and the run reads this file every iteration.
            paths_mod.write_text_atomic(plan_path, text)
        placements = [Placement(c.fingerprint, str(run_id or ""),
                                required_before + i)
                      for i, c in enumerate(candidates)]
        return placements, 0

    return sink
