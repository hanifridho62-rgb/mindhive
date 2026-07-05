# src/mindhive/projector.py
import hashlib
from . import paths, events, chain, review, incidents, reflect

LEDGER_TYPES = events.SEMANTIC_TYPES - {"state_set"}


def _short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def _ts_compact(ts: str) -> str:
    return ts.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")


def build_state(evs, pending=None, health_note="") -> str:
    latest_state = None
    last_decision = None
    for ev in evs:
        if ev.type == "state_set":
            latest_state = ev
        elif ev.type == "decision":
            last_decision = ev
    s = latest_state.payload if latest_state else {}
    out = ["# STATE", ""]
    # Pending thermo-nuclear review rides at the top so the next session sees it
    # first when session_start injects STATE.md via `hive state`.
    note = review.notice(pending)
    if note:
        out.append(note)
    # Reflection-health warning (one line, KTD3): auto-reflection failing for
    # too long must be visible, not silent.
    if health_note:
        out.append(health_note)
        out.append("")
    out.append(f"**Active task:** {s.get('task', '(none)')}")
    out.append(f"**Last verified:** {s.get('verified', '(none)')}")
    out.append(f"**Next step:** {s.get('next_step', '(none)')}")
    out.append("")
    out.append("**Open questions:**")
    for q in (s.get("open_questions") or ["(none)"]):
        out.append(f"- {q}")
    out.append("")
    out.append("**Deferred:**")
    for d in (s.get("deferred") or ["(none)"]):
        out.append(f"- {d}")
    out.append("")
    if last_decision:
        out.append(f"**Last decision ({last_decision.ts}):** "
                   f"{last_decision.payload.get('text', '')}")
    out.append("")
    out.append(f"_events: {len(evs)}_")
    return "\n".join(out) + "\n"


def compose_state(evs, project=None, include_rules=True) -> str:
    """Full STATE.md text: the state body (with any reflection-health notice)
    plus the capped prevention-rules block appended when non-empty (KTD2 —
    STATE.md is the single delivery choke point for rules). sync embeds the
    include_rules=False variant so rules appear exactly once in MEMORY.md."""
    body = build_state(evs, pending=review.load_pending(project),
                       health_note=reflect.health_note(project))
    if include_rules:
        block = incidents.inject(project, evs=evs)
        if block:
            body = body + "\n" + block + "\n"
    return body


def build_ledger_entries(evs):
    entries = []
    for ev in evs:
        if ev.type not in LEDGER_TYPES:
            continue
        body = ev.payload.get("text", "") if isinstance(ev.payload, dict) else str(ev.payload)
        fname = f"{_ts_compact(ev.ts)}-{ev.type}-{_short(ev.ts + ev.type + body)}.md"
        content = (f"---\nts: {ev.ts}\ntype: {ev.type}\n"
                   f"tags: {','.join(ev.tags)}\nsession: {ev.session_id}\n---\n\n{body}\n")
        entries.append((fname, content, ev))
    return entries


def build_index(entries) -> dict:
    idx = {"by_tag": {}, "by_type": {}}
    for fname, _content, ev in entries:
        idx["by_type"].setdefault(ev.type, []).append(fname)
        for t in ev.tags:
            idx["by_tag"].setdefault(t, []).append(fname)
    return idx


def project(project=None, evs=None) -> dict:
    # Accept pre-read events so a caller (e.g. sync) that already has them need
    # not read the whole event log again.
    if evs is None:
        evs = events.read_all(project)
    pdir = paths.project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)

    tmp = pdir / "STATE.md.tmp"
    tmp.write_text(compose_state(evs, project), encoding="utf-8")
    tmp.replace(pdir / "STATE.md")

    ldir = pdir / "LEDGER"
    ldir.mkdir(exist_ok=True)
    entries = build_ledger_entries(evs)
    keep = set()
    for fname, content, _ev in entries:
        (ldir / fname).write_text(content, encoding="utf-8")
        keep.add(fname)
    pruned = 0
    for f in ldir.glob("*.md"):
        if f.name not in keep:
            f.unlink()
            pruned += 1

    (pdir / "index.json").write_text(
        events.canonical_json(build_index(entries)), encoding="utf-8")
    chain.write_chain(project, evs=evs)  # reuse the events we already read
    return {"events": len(evs), "ledger": len(entries), "pruned": pruned}
