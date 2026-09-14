# src/mindhive/roadmap_render.py
"""How a roadmap record becomes text a human (or a later session) reads.

`roadmap.py` owns the event contract, the deterministic fold, the registry
projections and the writer. This module owns the last step after all of those:
turning a folded record into markdown. Two surfaces consume it —
`notice()` renders every interrupted plan as the "⚠ Interrupted plan" block at
the top of STATE.md (and therefore of the auto-loaded MEMORY.md), and
`ledger_body()` renders one event for `LEDGER/*.md`, where keyword recall finds
plans by id, prompt, step text or cause.

WHY THE CUT IS HERE. `roadmap.py` reached the 1000-line cap
(`tests/test_file_size_cap.py`), and of every candidate seam this is the only
one whose cross-module edges all point ONE way. Rendering reaches back for four
names — `interrupted`, `next_step`, `MAX_PROMPT_CHARS`, `describe_work_link` —
and after the split `roadmap.py`'s own function bodies reference nothing here at
all. The alternative that looked obvious, carving out the work link and
retirement, would have put `fold` and `validate_payload` — the two most central
functions in the file — on the far side of the cut from helpers they call, so
neither could be read in one place.

`provenance` corroborates it: every use of that module in the old file was
inside `_step_line` and `notice`, so it is now imported here and nowhere else in
the pair. A cut that a whole dependency falls cleanly to one side of is a real
boundary rather than a line-count convenience. It is also where the pressure
came from — the change that pushed the file to 1046 lines added an authorship
banner and field sanitization to exactly these two functions, and injection
defence is a live concern whose next increment now lands in a small file.

Every public name here is re-exported from `roadmap`, which is how callers
reach them (`roadmap.notice` in `projector`, `roadmap.ledger_body` in
`projector` and `dashboard_get`, `roadmap.new_prefix` in `autoresume`,
`roadmap.NEW_MARKER` in `replenish_sinks`). `tests/test_roadmap_render.py`
asserts those re-exports are the SAME objects: splitting a module in this repo
has already moved a monkeypatch address silently once.

`roadmap` is imported INSIDE the two functions that need it, never at module
scope — the rule `roadmap_auto` had to learn, where a module-scope import made
the pair work only when one of them happened to be touched first.
"""
from . import provenance


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


def _step_line(s, marker) -> str:
    # A newline in `text`/`note` would open a second `- [x] ...` step line.
    note = f" — {provenance.safe_line(s.get('note'))}" if s.get("note") else ""
    text = provenance.safe_line(s.get("text"))
    return f"- {marker} {new_prefix(s)}{text}{note}"


def notice(evs) -> str:
    """The "⚠ Interrupted plan" STATE.md block — the out-of-usage marker.
    Empty string when no plan is interrupted. Deterministic; each plan's step
    list is capped (done tail + pending head) so the marker preserves the big
    picture without regressing the auto-loaded context budget."""
    # Lazy, for the `roadmap_auto` reason: this module was carved out of
    # `roadmap`, and a module-scope import here would make the pair loadable
    # in only one order. Resolved at call time, so neither is a prerequisite
    # of the other at import.
    from . import roadmap as rm_mod
    marks = rm_mod.interrupted(evs)
    if not marks:
        return ""
    # Authorship over the whole history (`provenance.agent_touched`), and
    # MARKED not withheld: this marker is what recovers work after a usage
    # cut-off, so withholding an agent-touched plan would delete the operator's
    # own recovery path. Everything below is sanitized because the title is a
    # `###` heading, the prompt a bullet, and each step a `- [x]` line.
    # BOTH types, keyed by `plan_id`. The block renders `autoresume` delivery
    # telemetry for the plan as well as the plan's own rows, so an agent that
    # wrote only an `autoresume` row got its text into a human's plan block
    # with no banner on it -- which is the laundering case this fold exists to
    # close, arriving through the one event type it was not reading.
    tainted = provenance.agent_touched(evs, ("roadmap", "autoresume"),
                                       "plan_id")
    sl = provenance.safe_line          # every field below is rendered raw-free
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
        head = sl(r["title"] or r["plan_id"], provenance.TITLE_CHARS)
        plan_id = sl(r["plan_id"], provenance.TITLE_CHARS)
        prog = r["progress"]
        out.append(f"### {head}  `[{plan_id}]`")
        who = tainted.get(r["plan_id"])
        if who:
            out.append(provenance.banner("This plan", who))
        out.append(f"- **Interrupted:** {sl(r['interrupted_at'], 40)} "
                   f"(cause: {sl(r['cause'], 80)})")
        # `hive append roadmap` never calls `set`'s cap; re-applied here.
        out.append(f"- **Asked:** "
                   f"{sl(r['prompt'], rm_mod.MAX_PROMPT_CHARS)}")
        out.append(f"- **Progress:** {prog['done']}/{prog['total']} steps done")
        ar = ar_recs.get(r["plan_id"])
        # Through `sl` like everything else above, and the comment at the top
        # of this function is only true because of it. `detail` is the one that
        # was live: `autoresume.validate_payload` length-caps it and nothing
        # collapses whitespace, so a `fired` row forged a `###` heading and a
        # `- [x]` done step inside this block. The other five are fold-
        # constrained today and go through anyway -- an invariant that holds
        # for five of six fields is the shape that produced this bug.
        if ar and ar["status"] == "scheduled":
            out.append(
                f"- **Auto-resume:** marker re-enters Claude at "
                f"{sl(ar['due'], 40)} (attempt {sl(ar['attempt'], 8)}, "
                f"timer: {sl(ar['timer'], 40)}) — deliver now: "
                f"`hive autoresume fire {plan_id}`")
        elif ar and ar["status"] == "fired":
            detail = (f" — {sl(ar['detail'], provenance.FIELD_CHARS)}"
                      if ar.get("detail") else "")
            out.append(f"- **Auto-resume:** delivery {sl(ar['outcome'], 40)} @ "
                       f"{sl(ar['updated'], 40)}{detail}")
        done = [s for s in r["steps"] if s["status"] == "done"]
        hidden = len(done) - _NOTICE_DONE_TAIL
        if hidden > 0:
            out.append(f"- [x] … {hidden} earlier step(s) done")
        for s in done[-_NOTICE_DONE_TAIL:]:
            out.append(_step_line(s, "[x]"))
        nxt = rm_mod.next_step(r)
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
    from . import roadmap as rm_mod          # call-time; see `notice`
    link = rm_mod.describe_work_link(payload)
    if link:
        parts.append(f"work: {link}")
    reason = str(payload.get("reason") or "").strip()
    if reason:
        parts.append(f"reason: {reason}")
    note = str(payload.get("note") or "").strip()
    if note:
        parts.append(note)
    return "\n\n".join(parts)
