# SPDX-License-Identifier: Apache-2.0
# src/mindhive/briefing.py
import re
from pathlib import Path
from . import events as ev_mod, paths, projector, dedupe, provenance
from . import state as state_mod
from . import sop as sop_mod


SEMANTIC_TYPES = ev_mod.SEMANTIC_TYPES  # decision, experiment_result, caveat, next_step, open_question, state_set
# roadmap events carry structured payloads (no "text") and their delivery is
# the STATE.md interrupted-plan notice + Oversee — a timeline line would be a
# blank headline eating the cap.
# Work-session rows (blueprint P1b) carry tags, not text, and a migration writes
# one per roadmap step: as timeline lines they would be blank headlines flooding
# the cap. `hive worksession list` and Oversee are where they are read.
_TIMELINE_TYPES = SEMANTIC_TYPES - {"state_set", "roadmap", "work_session",
                                    "initiative_declared"}

# Cap on how many distinct (post-dedup) entries each list section emits. The
# briefing is wrapped verbatim into the native MEMORY.md that auto-loads every
# session (see sync.py), so an unbounded list grows context cost forever. Full
# history always remains on disk in the append-only ledger.
_TIMELINE_CAP = 40


def _absorbed_keys(evs) -> set:
    """Identity keys of events consolidated away by prior dreaming passes (see
    consolidate.py): the originals stay in the append-only ledger, but the
    briefing shows only each cluster's survivor. Keys are precise
    (ts, type, fingerprint) tuples — a bare timestamp would also hide
    unrelated same-second events — plus legacy bare timestamps from the
    feature's first payload shape."""
    out: set = set()
    for ev in evs:
        if ev.type != "memory_consolidated" or not isinstance(ev.payload, dict):
            continue
        for entry in ev.payload.get("absorbed") or []:
            if isinstance(entry, (list, tuple)) and len(entry) == 3:
                out.add(tuple(entry))
        out.update(ev.payload.get("absorbed_ts") or [])   # legacy shape
    return out


def _is_absorbed(ev, keys) -> bool:
    text = _payload_text(ev.payload)
    return ((ev.ts, ev.type, dedupe.text_fingerprint(text)) in keys
            or ev.ts in keys)


def _collapsed_entries(evs, types, cap=None, absorbed=None):
    """Collapse near-duplicate events of *types* into representatives.

    Returns a list of ``(Event, occurrences)`` newest-first. Display-time only:
    the append-only ledger is never touched. Uses :func:`dedupe.collapse`
    (deterministic Jaccard) so reworded repeats of the same note merge, and the
    newest of each cluster is kept. Capped to the most-recent *cap* distinct
    entries when given.
    """
    # dedupe.collapse expects newest-first; read_all yields oldest-first.
    absorbed = absorbed if absorbed is not None else set()
    items = [
        {"type": ev.type, "project": None,
         "text": _payload_text(ev.payload), "_ev": ev}
        for ev in evs
        if ev.type in types and not _is_absorbed(ev, absorbed)
    ]
    items.reverse()
    reps = dedupe.collapse(items)
    if cap is not None:
        reps = reps[:cap]
    return [(r["_ev"], r.get("similar_count", 0) + 1) for r in reps]


def _count_suffix(occurrences) -> str:
    return f" (×{occurrences})" if occurrences > 1 else ""


def headline(text: str) -> str:
    """Return first non-empty content line after any frontmatter, collapsed + truncated to 140."""
    lines = text.splitlines()
    # Strip frontmatter (--- ... ---)
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        # skip past closing ---
        lines = lines[i + 1:]
    # Find first non-empty line
    first = ""
    for line in lines:
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    # Collapse whitespace
    collapsed = re.sub(r"\s+", " ", first).strip()
    # Truncate
    if len(collapsed) > 140:
        return collapsed[:140] + "…"
    return collapsed


def _payload_text(payload) -> str:
    if isinstance(payload, dict):
        return payload.get("text", "")
    return str(payload)


# The timeline and the two memo sections below render event text a later session
# reads as project history. A spawned orchestration agent can write any semantic
# event -- every child holds `hive` on PATH with its own `MINDHIVE_SESSION` -- so
# without this those rows are indistinguishable from a human's.
#
# MARKED, not withheld, and the difference is the point. `sop.partition_authored`
# withholds, because an SOP is composed as a DIRECTIVE: it says "follow this", so
# an agent-written one has to be kept off the surface entirely. These sections are
# an AUDIT view: they say "this happened", and hiding an event from the record of
# what happened damages the very thing the record is for. Naming the author leaves
# the history complete and removes the only property that made it dangerous, which
# is that it read as somebody else's.
#
# Re-exported, not redefined: `projector`, `roadmap` and this module all mark
# with the same token, and a second literal is how two of them end up marking
# with different ones.
AGENT_MARK = provenance.AGENT_MARK


def _authorship_mark(ev) -> str:
    """`AGENT_MARK` when a spawned orchestration agent wrote *ev*, else "".

    Total and fails open, matching `advice.is_orchestration_session`: an
    unreadable id, or an import that raises, marks nothing. Absent authorship is
    unknown authorship, and labelling every pre-stamping event as agent-written
    would be a false accusation about most of the ledger. The lazy import that
    makes that true now lives in `provenance.is_agent_authored`, for the reason
    `sop.is_agent_authored` states -- `mindnet.advice` drags in the controller
    subtree, and this module is on the projection path."""
    return provenance.mark_for(getattr(ev, "session_id", ""))


def build_briefing(evs) -> str:
    """Build a Markdown briefing string from a list of Event objects (deterministic)."""
    absorbed = _absorbed_keys(evs)
    lines = ["# PROJECT BRIEFING", ""]

    # --- Current state ---
    lines.append("## Current state")
    lines.append("")
    latest_state = None
    for ev in evs:
        if ev.type == "state_set":
            latest_state = ev
    if latest_state is None:
        lines.append("(no state_set yet)")
    else:
        # Same treatment, and the same reasons, as `projector.build_state`.
        # This section was the one part of this file still rendering ledger text
        # unmarked and unsanitized while every section below it is marked, and
        # that inconsistency was the tell: the timeline, the decisions and the
        # caveats all got the control when it was added; the block above them,
        # which is the first thing a reader reads, did not.
        s = state_mod.fields(latest_state)
        state_banner = provenance.agent_banner(
            "This state block", getattr(latest_state, "session_id", ""))
        if state_banner:
            lines.append(state_banner)
            lines.append("")
        lines.append(f"**task:** "
                     f"{provenance.safe_line(s.get('task')) or '(none)'}")
        lines.append(f"**verified:** "
                     f"{provenance.safe_line(s.get('verified')) or '(none)'}")
        lines.append(f"**next_step:** "
                     f"{provenance.safe_line(s.get('next_step')) or '(none)'}")
        lines.append("**open_questions:**")
        for q in provenance.safe_bullets(s.get("open_questions"), empty=""):
            if q:
                lines.append(f"- {q}")
        lines.append("**deferred:**")
        for d in provenance.safe_bullets(s.get("deferred"), empty=""):
            if d:
                lines.append(f"- {d}")
    lines.append("")

    # --- Timeline (deduped, most-recent-first, capped) ---
    lines.append("## Timeline (deduped — most recent first)")
    lines.append("")
    for ev, n in _collapsed_entries(evs, _TIMELINE_TYPES, cap=_TIMELINE_CAP,
                                    absorbed=absorbed):
        hl = headline(_payload_text(ev.payload))
        lines.append(f"- {ev.ts} [{ev.type}] {hl}{_count_suffix(n)}"
                     f"{_authorship_mark(ev)}")
    lines.append("")

    # --- Key decisions ---
    lines.append("## Key decisions")
    lines.append("")
    for ev, n in _collapsed_entries(evs, {"decision"}, cap=_TIMELINE_CAP,
                                    absorbed=absorbed):
        lines.append(f"- {headline(_payload_text(ev.payload))}{_count_suffix(n)}"
                     f"{_authorship_mark(ev)}")
    lines.append("")

    # --- Lessons & caveats ---
    lines.append("## Lessons & caveats")
    lines.append("")
    for ev, n in _collapsed_entries(evs, {"caveat"}, cap=_TIMELINE_CAP,
                                    absorbed=absorbed):
        lines.append(f"- {headline(_payload_text(ev.payload))}{_count_suffix(n)}"
                     f"{_authorship_mark(ev)}")
    lines.append("")

    # --- Standard operating procedures (titles only — full text rides the
    # STATE.md block and `hive sop show`) ---
    # Partitioned on authorship for the reason `sop.partition_authored` states:
    # a spawned orchestration agent can reach `hive sop add`, and this list is
    # composed into a briefing a later session reads as project history. Titles
    # are sanitized on the way out so a newline in one cannot forge a second
    # bullet or a heading.
    _sop_rows = sop_mod.current(evs)
    actives, sop_withheld = sop_mod.partition_authored(_sop_rows, evs)
    _sop_kept = {r.get("sop_id") for r in actives}
    _sop_gone = [r.get("sop_id") for r in _sop_rows
                 if r.get("sop_id") not in _sop_kept]
    if actives or sop_withheld:
        lines.append("## Standard operating procedures (active)")
        lines.append("")
        for r in actives:
            lines.append(f"- `{r['sop_id']}` v{r['version']}: "
                         f"{sop_mod.safe_title(r['title'])}")
        foot = sop_mod.withheld_footer(sop_withheld, _sop_gone)
        if foot:
            lines.append(foot)
        lines.append("")

    # --- Activity ---
    lines.append("## Activity (deterministic)")
    lines.append("")
    n_file_touched = sum(1 for ev in evs if ev.type == "file_touched")
    n_command_run = sum(1 for ev in evs if ev.type == "command_run")
    n_session_start = sum(1 for ev in evs if ev.type == "session_start")
    lines.append(f"- file_touched: {n_file_touched}")
    lines.append(f"- command_run: {n_command_run}")
    lines.append(f"- session_start: {n_session_start}")

    # Collect up to 15 distinct touched file paths
    seen_paths: list[str] = []
    seen_set: set[str] = set()
    for ev in evs:
        if ev.type == "file_touched":
            p = ""
            if isinstance(ev.payload, dict):
                p = ev.payload.get("path") or ev.payload.get("text") or ""
            if p and p not in seen_set:
                seen_set.add(p)
                seen_paths.append(p)
                if len(seen_paths) >= 15:
                    break
    if seen_paths:
        lines.append("**touched files:**")
        for fp in seen_paths:
            lines.append(f"  - {fp}")
    lines.append("")

    # Footer
    lines.append(f"_events: {len(evs)}_")

    return "\n".join(lines) + "\n"


def briefing(project=None, evs=None) -> Path:
    """Build and write BRIEFING.md to the project dir; return the path.

    Accepts pre-read events so a caller (e.g. sync) that already read the event
    log can avoid a second full read.
    """
    if evs is None:
        evs = ev_mod.read_all(project)
    pdir = paths.project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    content = build_briefing(evs)
    dest = pdir / "BRIEFING.md"
    paths.write_text_atomic(dest, content)   # shared writer: private tmp + retry
    return dest
