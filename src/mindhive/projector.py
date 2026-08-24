# src/mindhive/projector.py
import hashlib
import threading
from . import paths, events, chain, review, changelog, incidents, reflect, sop
from . import roadmap
# `sopgate` reaches only `outcomes` (stdlib), `sop` and `promptgate` (-> `paths`),
# so this introduces no cycle either. Importing it here also means the
# `sop-injection` surface is registered by anything that can rebuild a
# projection, rather than only by the CLI entry point.
from . import sopgate
# Both reach only `sop` -> `events`, so a module-level import here introduces no
# cycle (the same reason `roadmap` is imported this way rather than lazily).
from .mindnet import phases as mn_phases, tasks as mn_tasks

LEDGER_TYPES = events.SEMANTIC_TYPES - {"state_set"}

SUGGEST_CAP = 3  # max suggested-skill lines embedded in STATE.md (R9)


def _short(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def _ts_compact(ts: str) -> str:
    return ts.replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")


def build_state(evs, pending=None, health_note="", changelog_pending=None,
                injection=None) -> str:
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
    # Unlogged-code-changes gate rides just below it: the change-log entry is
    # mandatory before the work is called done (see changelog.py).
    cl_note = changelog.notice(changelog_pending)
    if cl_note:
        out.append(cl_note)
    # Interrupted-plan marker (out-of-usage): the original ask + plan progress
    # ride the top of STATE.md so the next session resumes with the big
    # picture instead of starting cold (see roadmap.py). Pure fold over evs.
    rm_note = roadmap.notice(evs)
    if rm_note:
        out.append(rm_note)
    # What the per-prompt mandatory-SOP injection is actually delivering — but
    # ONLY when it is degraded. A procedure that outgrew the prompt budget
    # degrades to a pointer silently, per prompt, forever, and the block still
    # arrives so nothing looks broken; `hive sop injection` reports it but has to
    # be typed. Zero lines when healthy: a notice present in every digest is a
    # notice every reader learns to skip. The guard for a junk value lives in
    # `sopgate.notice`, not here.
    inj_note = sopgate.notice(injection)
    if inj_note:
        out.append(inj_note)
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


def suggested_skills_block(project=None, evs=None) -> str:
    """Compact suggested-skills block (<= SUGGEST_CAP lines, same brief format
    as `hive skills recommend --brief`) refreshed at every projection rebuild
    (R9). Empty string on ANY failure — a recommendation problem must never
    break the state build. Imports are lazy so projector keeps no hard
    dependency on the plugins/recommend stack (and no import cycles).

    Entries are DIRECTIVES, not FYI: a proven skill (execution_mode auto —
    measured completion >= the auto gate, no open incident) is injected as a
    MUST-USE line the agent is expected to follow; an unmeasured need-match
    (try_to_measure) is injected as an explicit instruction to invoke it via
    the Skill tool so the call gets measured. That closes the loop the
    suggestion-only wording left open: recommendations existed but nothing
    ever compelled an invocation, so nothing ever became measured."""
    try:
        from . import plugins as plugins_mod
        from . import recommend as recommend_mod
        payload = plugins_mod.recommend_for(project=project, evs=evs,
                                            top=SUGGEST_CAP)
        recs = (payload.get("recommendations") or [])[:SUGGEST_CAP]
        if not recs:
            return ""
        # The nudge line makes intended-use adherence explicit; it is not a
        # bullet, so it never counts against SUGGEST_CAP. Every tier carries
        # the skill's intended use (via intended_use_clause / brief_line) so
        # the agent grasps scope from the block alone.
        lines = ["# SUGGESTED SKILLS (deterministic — hive skills recommend)",
                 "_Consult each skill's intended use before invoking — match the "
                 "task to the skill's stated scope._"]
        for e in recs:
            use = recommend_mod.intended_use_clause(e)
            if e.get("execution_mode") == "auto":
                lines.append(
                    f"- MUST USE: {e['name']} — {e['reason']}. Proven skill: "
                    "when its trigger matches the task at hand, invoke it via "
                    f"the Skill tool before starting that work.{use}")
            elif e.get("try_to_measure"):
                lines.append(
                    f"- TRY & MEASURE: {e['name']} — {e['reason']}. If it fits "
                    "the current task, invoke it via the Skill tool (not "
                    f"ad-hoc) so its completion rate gets measured.{use}")
            else:
                lines.append(f"- {recommend_mod.brief_line(e)}")
        return "\n".join(lines)
    except Exception:
        return ""


def compose_state(evs, project=None, include_rules=True, suggestions=None,
                  injection=None, digest=False) -> str:
    """Full STATE.md text: the state body (with any reflection-health notice)
    plus the capped prevention-rules block appended when non-empty (KTD2 —
    STATE.md is the single delivery choke point for rules), plus the compact
    suggested-skills block (R9). sync embeds the include_rules=False variant so
    rules appear exactly once in MEMORY.md; suggestions ride the embedded state
    body in both variants (they exist nowhere else in MEMORY.md).

    ``suggestions``: None -> compute the suggested-skills block here; an
    explicit str (possibly "") -> use as-is, so a caller composing the state
    twice per pass (sync) runs the recommend chain exactly once.

    ``injection``: None -> measure the per-prompt SOP injection here; an explicit
    dict -> use as-is (``{}`` therefore means "render no injection notice").
    Measured HERE and not in `cmd_state`, because `cmd_state` does not build
    STATE.md — it prints the file. This function is `build_state`'s only caller
    and it serves both projections: the STATE.md rebuild and `sync`'s embedded
    digest, which is the copy MEMORY.md carries and the only one a session loads
    without being asked. The cost is one small JSON read (`sop.read_global_sidecar`)
    plus pure counting — not the two git subprocesses the tracking probe pays for
    — and it is paid at rebuild, never on the prompt path."""
    if injection is None:
        injection = sopgate.probe(project=project)
    body = build_state(evs, pending=review.load_pending(project),
                       health_note=reflect.health_note(project),
                       changelog_pending=changelog.load_pending(project),
                       injection=injection)
    if include_rules:
        # Promoted (proven-permanent) rules ride FIRST — SOUL.md is only
        # meaningful if every session actually re-reads it.
        soul = incidents.soul_block(project)
        if soul:
            body = body + "\n" + soul + "\n"
        block = incidents.inject(project, evs=evs)
        if block:
            body = body + "\n" + block + "\n"
    # SOPs ride BOTH variants (unlike rules): sync embeds the
    # include_rules=False state body into MEMORY.md and appends the rules
    # block separately, so gating SOPs on include_rules would drop them from
    # the one file every session auto-loads.
    #
    # `digest` picks the RENDERING, never whether they ride: STATE.md gets the
    # bodies, MEMORY.md gets titles + pointers, because only MEMORY.md is
    # auto-loaded into a channel the native loader cuts short. The ids whose
    # mandatory bodies may be elided come from the `injection` measurement this
    # function already computes above — a seam that exists, not a new file read
    # — and anything unparseable yields an empty set, which keeps the bodies.
    if digest:
        injected = frozenset(
            str(r.get("sop_id"))
            for r in ((injection or {}).get("sops") or [])
            if isinstance(r, dict) and r.get("delivered") == "body")
        sop_block = sop.digest_block(evs, injected_ids=injected)
    else:
        sop_block = sop.block(evs)
    if sop_block:
        body = body + "\n" + sop_block + "\n"
    sugg = (suggested_skills_block(project, evs=evs)
            if suggestions is None else suggestions)
    if sugg:
        body = body + "\n" + sugg + "\n"
    return body


def build_ledger_entries(evs):
    entries = []
    for ev in evs:
        if ev.type not in LEDGER_TYPES:
            continue
        body = ev.payload.get("text", "") if isinstance(ev.payload, dict) else str(ev.payload)
        if ev.type == "code_change" and isinstance(ev.payload, dict):
            # An audit entry is more than its prose: category and affected
            # files must survive into the projected LEDGER/export markdown.
            files = ev.payload.get("files") or []
            body = (f"[{ev.payload.get('category', '')}] {body}\n\n**Files:**\n"
                    + "\n".join(f"- {f}" for f in files))
        elif ev.type == "sop" and isinstance(ev.payload, dict):
            # Action, id, and title must survive into the LEDGER entry so
            # keyword recall can find a procedure by any of them.
            body = sop.ledger_body(ev.payload)
        elif ev.type == "roadmap" and isinstance(ev.payload, dict):
            # Plan id, prompt, and step text must survive so keyword recall
            # finds a plan by what was asked or what it contains.
            body = roadmap.ledger_body(ev.payload)
        elif ev.type == "mindnet_task" and isinstance(ev.payload, dict):
            # Structured, text-less payloads like roadmap's. Without a body
            # renderer these project as EMPTY entries — and, worse, two events
            # in the same second hash to the same filename (see `fname` below,
            # which mixes the body into the digest) and one silently
            # overwrites the other.
            body = mn_tasks.ledger_body(ev.payload)
        elif ev.type == "mindnet_phase" and isinstance(ev.payload, dict):
            body = mn_phases.ledger_body(ev.payload)
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


_PROJECT_LOCKS: dict = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


def _project_lock(key: str):
    """One re-entrant lock per project directory, created on first use."""
    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(key, threading.RLock())


def project(project=None, evs=None, suggestions=None) -> dict:
    """Rebuild a project's derived files, one writer at a time per project.

    The lock lives HERE rather than at the call sites because there are nine of
    them and they do not agree: `dashboard_post._post_project` held `_PROJECT_LOCK`,
    `_post_sync` held `_SYNC_LOCK`, `_post_harvest` held `_HARVEST_LOCK`,
    `_post_append` and the background `_auto_harvest_loop` held nothing, and
    `autosync`, `consolidate` and two CLI paths are not handlers at all. Four
    different locks over one critical section exclude nothing; the resource has
    to own its own lock.

    What two concurrent rebuilds actually did:
      * both wrote the SAME `STATE.md.tmp` (a fixed per-project path), so the
        file that got `.replace`d into STATE.md could hold interleaved halves of
        two different renders;
      * worse, both ran the LEDGER prune below. `keep` is computed from the
        caller's `evs`, and `sync` passes a pre-read list — so a rebuild holding
        slightly older events would `unlink()` entry files a fresher concurrent
        rebuild had just written. That is data loss, not a torn temp file.

    Reachable from a single tab only after PR-1a removed the nav disable, which
    had been the sole thing stopping two of these buttons being clicked at once.
    The race itself is older than that: the auto-harvest thread has always run
    unlocked alongside them.

    NOT fixed here, and deliberately not claimed: this is a threading lock, so
    it covers one process. `hive sync` in a terminal racing the dashboard is a
    second process and still races. The unique temp name below removes the torn
    STATE.md half of that; the LEDGER prune half needs a file lock and is out of
    scope for PR-1a.
    """
    pdir = paths.project_dir(project)
    with _project_lock(str(pdir)):
        return _project(project=project, evs=evs, suggestions=suggestions)


def _project(project=None, evs=None, suggestions=None) -> dict:
    # Accept pre-read events (and a pre-computed suggested-skills block) so a
    # caller (e.g. sync) that already has them need not recompute either.
    if evs is None:
        evs = events.read_all(project)
    pdir = paths.project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)

    # The mandatory-SOP sidecar is derived state like everything else written
    # here, and this is the one place that already holds `evs` — so a full
    # rebuild refreshes it for free rather than paying a second `read_all`.
    # Best-effort: STATE.md and the LEDGER are this function's product, and a
    # hot-path cache must never be able to fail the rebuild that feeds them.
    #
    # Refreshed BEFORE composing the state, not after, since `compose_state` now
    # measures the injection off this sidecar. With the write after, STATE.md
    # reported the verdict as of the PREVIOUS rebuild while the MEMORY.md digest
    # `sync` composes next reported the current one — the two files disagreeing
    # about the same measurement inside a single `hive sync`.
    try:
        sop.write_sidecar(project, evs=evs)
    except Exception:                                       # noqa: BLE001
        pass

    # Unique per writer, not the fixed "STATE.md.tmp" this used to be: the lock
    # above serialises threads in THIS process, but `hive sync` in a terminal is
    # another process and shares the directory. Two of them writing one fixed
    # temp path interleave their bytes and then both `.replace()` it, so the
    # atomic rename lands a file that is atomically corrupt. A unique name makes
    # each render internally consistent no matter who else is writing; the
    # rename stays atomic, so the loser simply loses cleanly.
    # The shared writer already does what this block hand-rolled: a temp name
    # unique per process AND per call (so concurrent renders never interleave),
    # cleanup of the temp on any failure, and a retry of the Windows publish the
    # dashboard's constant STATE.md reads can otherwise refuse.
    paths.write_text_atomic(pdir / "STATE.md",
                            compose_state(evs, project, suggestions=suggestions))

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

    # Atomic, for the same reason as chain.json below it: this is a
    # projection the dashboard parses while `hive project` is rewriting it.
    paths.write_text_atomic(pdir / "index.json",
                            events.canonical_json(build_index(entries)))
    chain.write_chain(project, evs=evs)  # reuse the events we already read
    return {"events": len(evs), "ledger": len(entries), "pruned": pruned}
