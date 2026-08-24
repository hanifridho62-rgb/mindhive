# src/mindhive/briefing.py
import re
from pathlib import Path
from . import events as ev_mod, paths, projector, dedupe, sop as sop_mod


SEMANTIC_TYPES = ev_mod.SEMANTIC_TYPES  # decision, experiment_result, caveat, next_step, open_question, state_set
# roadmap events carry structured payloads (no "text") and their delivery is
# the STATE.md interrupted-plan notice + Oversee — a timeline line would be a
# blank headline eating the cap.
_TIMELINE_TYPES = SEMANTIC_TYPES - {"state_set", "roadmap"}

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
        s = latest_state.payload
        lines.append(f"**task:** {s.get('task', '(none)')}")
        lines.append(f"**verified:** {s.get('verified', '(none)')}")
        lines.append(f"**next_step:** {s.get('next_step', '(none)')}")
        oqs = s.get("open_questions") or []
        lines.append("**open_questions:**")
        for q in oqs:
            lines.append(f"- {q}")
        deferred = s.get("deferred") or []
        lines.append("**deferred:**")
        for d in deferred:
            lines.append(f"- {d}")
    lines.append("")

    # --- Timeline (deduped, most-recent-first, capped) ---
    lines.append("## Timeline (deduped — most recent first)")
    lines.append("")
    for ev, n in _collapsed_entries(evs, _TIMELINE_TYPES, cap=_TIMELINE_CAP,
                                    absorbed=absorbed):
        hl = headline(_payload_text(ev.payload))
        lines.append(f"- {ev.ts} [{ev.type}] {hl}{_count_suffix(n)}")
    lines.append("")

    # --- Key decisions ---
    lines.append("## Key decisions")
    lines.append("")
    for ev, n in _collapsed_entries(evs, {"decision"}, cap=_TIMELINE_CAP,
                                    absorbed=absorbed):
        lines.append(f"- {headline(_payload_text(ev.payload))}{_count_suffix(n)}")
    lines.append("")

    # --- Lessons & caveats ---
    lines.append("## Lessons & caveats")
    lines.append("")
    for ev, n in _collapsed_entries(evs, {"caveat"}, cap=_TIMELINE_CAP,
                                    absorbed=absorbed):
        lines.append(f"- {headline(_payload_text(ev.payload))}{_count_suffix(n)}")
    lines.append("")

    # --- Standard operating procedures (titles only — full text rides the
    # STATE.md block and `hive sop show`) ---
    actives = sop_mod.current(evs)
    if actives:
        lines.append("## Standard operating procedures (active)")
        lines.append("")
        for r in actives:
            lines.append(f"- `{r['sop_id']}` v{r['version']}: {r['title']}")
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
