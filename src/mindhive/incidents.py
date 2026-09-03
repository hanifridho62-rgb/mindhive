# src/mindhive/incidents.py
"""Project `tool_failed` events into per-signature incident records.

One Markdown file per signature under rules/incidents/. The event log is the
canonical truth; these files are the human-editable workspace/projection. Plan 1
writes the immutable Raw Signal and frontmatter; the Auto/Human evaluation
sections are stubs that Plan 2 populates.
"""
import calendar
import os
import re
import threading
import time
from contextlib import contextmanager
# `sig_mod`, not `sig`: auto_promote() binds a local `sig` for the signature
# string, and a module shadowed by a loop variable is a trap, not a shortcut.
from . import events, paths, signature as sig_mod

# The record's Markdown format - parsing and rendering - lives in
# `incidentfile`; see that module's docstring for why the seam is there and not
# at the write lock. Re-imported under the same names on purpose: reflect,
# resolve, advice and eleven test files reach these through `incidents`, none of
# them monkeypatches one, and they are functions plus a single int constant, so
# there is no mutable state a second module's globals could hold stale.
from .incidentfile import (MAX_SESSION_IDS, _auto_field,  # noqa: F401
                           _auto_rule, _frontmatter, _human_text, _id_list,
                           _raw_excerpt, _render, _replace_section,
                           _split_sections, derive_active)

THRESHOLD = 2                       # recurrence gate N (D2)
DECAY_DAYS = 14                     # candidate staleness horizon (KTD4 decay)
_FLAG_PREFIX = "judgment:flag:"     # explicit human self-flags (hive flag)
_SAFE = re.compile(r"[^a-z0-9]+")


def _threshold_for(signature, threshold=THRESHOLD) -> int:
    """Explicit judgment flags are rare, high-value, and human-initiated — they
    activate at the first occurrence. Everything else (including
    judgment:rejected:*) keeps the recurrence gate."""
    return 1 if str(signature).startswith(_FLAG_PREFIX) else threshold

# Serializes every writer of an incident .md file. The dashboard runs the
# auto-harvest thread (calls project()) alongside request threads (reflect →
# write + project()), so two writers can otherwise interleave a read → render →
# write and silently discard a just-computed reflection verdict. Reentrant so
# reflect_incident can hold it across its own project() re-derivation.
_WRITE_LOCK = threading.RLock()

# Cross-process reentrancy depth, guarded by _WRITE_LOCK. The in-process RLock
# alone only serializes writers within ONE process; a separate `hive rules
# reflect` CLI run or a second dashboard is a distinct process and races the
# same files. The file lock below closes that gap — but O_EXCL is not
# reentrant, so only the OUTERMOST writer in this process takes/releases it
# (reflect's tail holds it across its own project() call). Depth tracks nesting.
_LOCK_DEPTH = 0


def _incidents_lock_path(project=None):
    return _incident_dir(project) / ".write.lock"


def _acquire_file_lock(project, timeout: float = 30.0, stale_secs: float = 60.0) -> None:
    """Block until this process holds the per-project incident-write file lock.
    Steals a lock older than ``stale_secs`` (crashed writer). Best-effort: on
    timeout it proceeds unlocked rather than dropping a paid write — the
    in-process lock still holds, and a lost cross-process update is strictly
    less bad than refusing to persist a reflection verdict."""
    p = _incidents_lock_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if p.exists() and (time.time() - p.stat().st_mtime) > stale_secs:
                p.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            return
        except OSError:                 # held by another process
            if time.monotonic() >= deadline:
                return                  # give up the cross-process guard, proceed
            time.sleep(0.05)


def _release_file_lock(project) -> None:
    try:
        _incidents_lock_path(project).unlink()
    except OSError:
        pass


@contextmanager
def _write_guard(project):
    """Serialize incident-file writers both within this process (the RLock) and
    across processes (the file lock, taken once at the outermost depth)."""
    global _LOCK_DEPTH
    with _WRITE_LOCK:
        outermost = _LOCK_DEPTH == 0
        if outermost:
            _acquire_file_lock(project)
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
            if outermost:
                _release_file_lock(project)


def _atomic_write(path, text) -> None:
    """Write via a temp file + os.replace so a concurrent reader (the 2.5s
    Improvements poll) always sees a whole file, never a torn one."""
    paths.write_text_atomic(path, text)


LEAK_LIMIT = 2


def _failures_by_sig(project, evs=None) -> dict:
    """signature -> list of tool_failed Events, in ts order (read_all is sorted).
    Pass evs to reuse an events.read_all() result."""
    out = {}
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type != "tool_failed":
            continue
        s = ev.payload.get("signature") if isinstance(ev.payload, dict) else None
        if s:
            out.setdefault(s, []).append(ev)
    return out


def _lifecycle_index(evs) -> dict:
    """(etype, signature) -> [Event, ...] in ledger order, covering the rule_*
    lifecycle events the per-signature status helpers consult. Built ONCE per
    projection rebuild and threaded through — without it project()/audit()
    re-scan the whole ledger several times per signature, which measured ~50s
    on a 6.4k-event store (the dismiss button and the 3s auto-harvest loop
    both sat on that path)."""
    idx = {}
    for ev in evs:
        if ev.type in _LIFECYCLE_TYPES and isinstance(ev.payload, dict):
            s = ev.payload.get("signature")
            if s:
                idx.setdefault((ev.type, s), []).append(ev)
    return idx


def _has_lifecycle(project, etype, signature, evs=None, life=None) -> bool:
    if life is not None:
        return bool(life.get((etype, signature)))
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == etype and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            return True
    return False


def _activation_ts(project, signature, evs=None, life=None) -> str | None:
    if life is not None:
        hits = life.get(("rule_activated", signature))
        return hits[0].ts if hits else None
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_activated" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            return ev.ts
    return None


def _latest_expiry_ts(project, signature, evs=None, life=None) -> str | None:
    """ts of the most recent rule_expired event for a signature, or None.
    Occurrences for status purposes count only failures after this point, so
    re-entry after decay starts a fresh candidate→active climb. Pass evs (or a
    prebuilt ``life`` index) to reuse an events.read_all() result."""
    if life is not None:
        hits = life.get(("rule_expired", signature))
        return max(ev.ts for ev in hits) if hits else None
    latest = None
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_expired" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            if latest is None or ev.ts > latest:
                latest = ev.ts
    return latest


def _live_failures(project, signature, failures, evs=None, life=None) -> list:
    """The status-relevant slice of a signature's failures: everything after
    the latest rule_expired, or all of them when it never expired. ``failures``
    is that signature's tool_failed list; ``evs``/``life`` optionally reuse a
    full events.read_all() result / lifecycle index for the expiry lookup."""
    exp = _latest_expiry_ts(project, signature, evs=evs, life=life)
    return [e for e in failures if e.ts > exp] if exp else failures


def _ts_secs(ts) -> float | None:
    """Epoch seconds for a store ISO timestamp, or None when unparseable."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


# Reroute attempts per signature before active resolution gives up and hands
# the incident to a human (flag: reroute_exhausted). Lives here (not resolve.py)
# because _status_and_flag renders the flag during every projection rebuild.
MAX_REROUTES = 3


def _latest_reroute(project, signature, evs=None, life=None):
    """(ts, session_id) of the most recent rule_rerouted event for a signature,
    or (None, None). A reroute replaces a failed rule with an alternative route,
    so it re-anchors leak/exposure accounting (see signal_stats)."""
    ts = sess = None
    if life is not None:
        for ev in life.get(("rule_rerouted", signature)) or []:
            if ts is None or ev.ts > ts:
                ts, sess = ev.ts, ev.session_id
        return ts, sess
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_rerouted" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            if ts is None or ev.ts > ts:
                ts, sess = ev.ts, ev.session_id
    return ts, sess


def reroute_count(project=None, signature=None, evs=None, life=None) -> int:
    """How many times a signature's rule has been rerouted (alternative route
    deployed after the previous rule leaked)."""
    if life is not None:
        return len(life.get(("rule_rerouted", signature)) or [])
    return sum(1 for ev in (evs if evs is not None else events.read_all(project))
               if ev.type == "rule_rerouted" and isinstance(ev.payload, dict)
               and ev.payload.get("signature") == signature)


def leaks(project=None, signature=None, by_sig=None, life=None) -> int:
    """Count tool_failed events for a signature that occurred AFTER its rule
    activated — i.e. the same error recurred despite an active rule. A reroute
    re-anchors the count: only failures after the latest rule_rerouted (from a
    session other than the one that deployed the route) count, so the new route
    gets a fair chance to prove itself. Pass by_sig/life to reuse a prebuilt
    _failures_by_sig map and lifecycle index."""
    act = _activation_ts(project, signature, life=life)
    if not act:
        return 0
    # A rule_expired wipes the slate the same way a reroute does. Failures from
    # before a signature decayed — or was retired as a classifier artifact —
    # are not leaks against the rule that re-entered afterwards; counting them
    # would grade a re-climbing incident on history it has already paid for,
    # and no recurrence could ever clear it.
    exp = _latest_expiry_ts(project, signature, life=life)
    if exp and exp > act:
        act = exp
    r_ts, r_sess = _latest_reroute(project, signature, life=life)
    anchor, skip = (r_ts, r_sess) if (r_ts and r_ts > act) else (act, None)
    if by_sig is None:
        by_sig = _failures_by_sig(project)
    evs = by_sig.get(signature, [])
    return sum(1 for e in evs
               if e.ts > anchor and (skip is None or e.session_id != skip))


def _status_and_flag(project, signature, rec, threshold, source,
                     by_sig=None, life=None):
    if by_sig is None:
        by_sig = _failures_by_sig(project)
    if _has_lifecycle(project, "rule_promoted", signature, life=life):
        return "promoted", ""
    occurrences = rec["occurrences"]
    if _latest_expiry_ts(project, signature, life=life) is not None:
        # Post-expiry occurrences only: 0 -> expired (stays expired across a
        # full project() rebuild), 1 -> candidate again, threshold -> active.
        occurrences = len(_live_failures(
            project, signature, by_sig.get(signature, []), life=life))
        if occurrences == 0:
            return "expired", ""
    if occurrences < _threshold_for(signature, threshold):
        return "candidate", ""
    if _has_lifecycle(project, "rule_demoted", signature, life=life):
        return "demoted", ""
    # leaks() is reroute-anchored, so deploying an alternative route resets the
    # count and un-demotes an auto rule that leaked under its OLD text — the
    # new route re-enters injection and gets its own chance to hold.
    lk = leaks(project, signature, by_sig=by_sig, life=life)
    exhausted = (source == "auto" and lk >= 1
                 and reroute_count(project, signature, life=life) >= MAX_REROUTES)
    if source == "auto" and lk >= LEAK_LIMIT:
        return "demoted", ("reroute_exhausted" if exhausted else "")
    flag = "needs_review" if source == "human" and lk >= 1 else ""
    if exhausted:
        flag = "reroute_exhausted"
    return "active", flag


def reclassified(project=None, evs=None, by_sig=None, life=None) -> dict:
    """``raw:`` buckets whose OWN stored excerpts no longer classify to them.

    A signature is frozen into its tool_failed event at capture time, so fixing
    signature.py splits only FUTURE failures — the old bucket keeps every
    occurrence and every leak it ever had. Those counts can only rise, so the
    incident is red forever: it can never be resolved (no new failure can reach
    it) and it never stops injecting its prevention rule. raw:37c59e9cae was
    exactly that — 45 occurrences, all 45 re-classifying into named families
    once the classifier learned to read the line that ends a traceback.

    Replaying the CURRENT classifier over the excerpts the bucket itself stored
    is what tells a stale bucket from a live one. Returns ``signature ->
    {"into": {new_sig: n}, "retirable": bool}`` for each ``raw:`` bucket where
    NO live excerpt still classifies to it. ``retirable`` is true only when
    every one of them now lands in a NAMED family: a bucket still holding text
    nothing can diagnose keeps its red card for a human, because retiring it
    would drop the evidence rather than re-file it. Named families are never
    candidates — ``pymodule:tools`` means the same thing whatever this module
    learns next, and signatures minted elsewhere (``judgment:*``,
    ``workspace:*``) were never this classifier's to re-read.
    """
    if evs is None:
        evs = events.read_all(project)
    if life is None:
        life = _lifecycle_index(evs)
    if by_sig is None:
        by_sig = _failures_by_sig(project, evs=evs)
    out = {}
    for signature, sig_evs in by_sig.items():
        if sig_mod.is_diagnostic(signature):
            continue
        live = _live_failures(project, signature, sig_evs, life=life)
        texts = [str(e.payload.get("text") or "") if isinstance(e.payload, dict)
                 else "" for e in live]
        if not texts or not all(t.strip() for t in texts):
            continue          # nothing to replay, or an excerpt we never stored
        into = {}
        for t in texts:
            s = sig_mod.signature(t)
            into[s] = into.get(s, 0) + 1
        if signature in into:
            continue                      # still reachable — a live bucket
        out[signature] = {
            "into": into,
            "retirable": all(sig_mod.is_diagnostic(s) for s in into),
            "last_ts": live[-1].ts,
        }
    return out


def audit(project=None, threshold=THRESHOLD, now=None, evs=None) -> dict:
    """Emit a one-time rule_activated event (stamped at the threshold-crossing
    failure's ts) for any signature that has reached its recurrence threshold
    (judgment:flag:* activates at 1) and isn't activated yet. Also decays stale
    noise: a candidate with exactly one live occurrence whose latest failure is
    older than DECAY_DAYS gains a rule_expired lifecycle event — except
    judgment:flag:* incidents, which never expire. Idempotent: an expiry zeroes
    the live count, so re-running appends nothing new. Pass evs to reuse an
    events.read_all() result (one ledger pass total instead of several per
    signature).

    Retirement rides the same rule_expired lifecycle: a ``raw:`` bucket the
    current classifier can no longer mint (see reclassified) expires with
    ``reason: reclassified``, which drops it out of injection and off the red
    list while leaving its record on disk. Nothing is lost: should that error
    recur under the current classifier the signature climbs candidate→active
    again on its post-expiry occurrences alone, and leaks() re-anchors at the
    expiry so the re-entered rule is not graded on the retired bucket's
    history. What it does NOT do is mint a second rule_activated — a signature
    activated before it was retired keeps that first activation, so its very
    first post-expiry recurrence already reads as a leak. Retirement is a
    lifecycle reset for accounting, not a fresh identity."""
    activated = expired = retired = 0
    now_secs = _ts_secs(now) if now else time.time()
    if evs is None:
        evs = events.read_all(project)
    life = _lifecycle_index(evs)
    by_sig = _failures_by_sig(project, evs=evs)
    stale = {s: info for s, info in
             reclassified(project, evs=evs, by_sig=by_sig, life=life).items()
             if info["retirable"]}
    for signature in sorted(stale):
        # Stamped at the bucket's LAST failure, not at wall-clock now: a
        # failure that already happened but has not been harvested yet would
        # otherwise land in the dead zone between the two and never be counted.
        # It also keeps the operation deterministic, which is what lets the
        # tests assert the ts instead of a clock reading.
        events.append_event(
            "rule_expired",
            {"signature": signature, "reason": "reclassified",
             "into": stale[signature]["into"]},
            tags=["lifecycle"], ts=stale[signature]["last_ts"], project=project)
        retired += 1
    for signature, sig_evs in by_sig.items():
        if signature in stale:
            continue                      # just retired; do not also activate it
        live = _live_failures(project, signature, sig_evs, life=life)
        thr = _threshold_for(signature, threshold)
        if len(live) >= thr and not _has_lifecycle(
                project, "rule_activated", signature, life=life):
            crossing_ts = live[thr - 1].ts
            events.append_event("rule_activated", {"signature": signature},
                                tags=["lifecycle"], ts=crossing_ts, project=project)
            activated += 1
        elif len(live) == 1 and not signature.startswith(_FLAG_PREFIX):
            last = _ts_secs(live[-1].ts)
            if last is not None and now_secs is not None \
                    and (now_secs - last) > DECAY_DAYS * 86400:
                events.append_event("rule_expired", {"signature": signature},
                                    tags=["lifecycle"], project=project)
                expired += 1
    return {"activated": activated, "expired": expired, "retired": retired}


def _safe_name(signature: str) -> str:
    return _SAFE.sub("-", signature.lower()).strip("-") or "unknown"


def _incident_dir(project=None):
    return paths.project_dir(project) / "rules" / "incidents"


def _collect(project, evs=None):
    """signature -> {occurrences, actions[], excerpt, sessions[]} from events."""
    acc = {}
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type != "tool_failed":
            continue
        p = ev.payload if isinstance(ev.payload, dict) else {}
        s = p.get("signature")
        if not s:
            continue
        rec = acc.setdefault(s, {"occurrences": 0, "actions": [], "excerpt": "",
                                 "sessions": []})
        rec["occurrences"] += 1
        if p.get("action") and p["action"] not in rec["actions"]:
            rec["actions"].append(p["action"])
        if ev.session_id not in rec["sessions"]:
            rec["sessions"].append(ev.session_id)
        rec["excerpt"] = rec["excerpt"] or p.get("text", "")
    return acc


def project(project=None, threshold=THRESHOLD, evs=None) -> dict:
    """(Re)write an incident record per signature, preserving the Auto and Human
    Evaluation sections and re-deriving the Active rule. Returns {records: n}.
    Reads the ledger ONCE and threads the pass through every per-signature
    status helper — the previous shape re-read the whole ledger several times
    per signature and took ~50s on the live store."""
    if evs is None:
        evs = events.read_all(project)
    acc = _collect(project, evs=evs)
    by_sig = _failures_by_sig(project, evs=evs)
    life = _lifecycle_index(evs)
    d = _incident_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    with _write_guard(project):
        for signature, rec in acc.items():
            path = d / f"{_safe_name(signature)}.md"
            auto_body = "<!-- filled by the reflection pass (hive rules reflect) -->"
            human_body = "<!-- leave blank to accept Auto; fill to override -->"
            if path.exists():
                secs = _split_sections(path.read_text(encoding="utf-8"))
                auto_body = secs.get("Auto Evaluation", auto_body) or auto_body
                human_body = secs.get("Human Evaluation", human_body) or human_body
            active, source = derive_active(auto_body, human_body)
            status, flag = _status_and_flag(project, signature, rec, threshold,
                                            source, by_sig=by_sig, life=life)
            _atomic_write(path, _render(signature, rec, status, auto_body,
                                        human_body, active, source, flag))
    return {"records": len(acc)}


def _last_revisions(project, evs=None) -> dict:
    """signature -> most recent human rule_revised text, in one ledger pass."""
    latest = {}
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_revised" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature"):
            latest[ev.payload["signature"]] = ev.payload.get("text", "")
    return latest


def reconcile(project=None) -> dict:
    """Scan incident files; append a rule_revised event for any Human Evaluation
    that is non-empty and differs from the last recorded revision. Idempotent."""
    d = _incident_dir(project)
    revised = 0
    if not d.exists():
        return {"revised": 0}
    revisions = _last_revisions(project)
    for path in sorted(d.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        secs = _split_sections(text)
        signature = None
        for line in text.splitlines():
            m = re.match(r"^signature:\s*(.+)$", line)
            if m:
                signature = m.group(1).strip()
                break
        if not signature:
            continue
        human = _human_text(secs.get("Human Evaluation", ""))
        if human and human != revisions.get(signature, ""):
            events.append_event("rule_revised",
                                {"signature": signature, "text": human, "source": "human"},
                                tags=["reconcile"], project=project)
            revised += 1
    return {"revised": revised}


def _active_rules(project) -> list:
    """(signature, active_rule) for each injectable incident: status active,
    rule_source != none. Demoted/candidate/promoted are excluded."""
    out = []
    d = _incident_dir(project)
    if not d.exists():
        return out
    for path in sorted(d.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fm = {}
        for line in text.splitlines():
            m = re.match(r"^(\w+):\s*(.*)$", line)
            if m:
                fm[m.group(1)] = m.group(2).strip()
            if line.startswith("# Incident:"):
                break
        if fm.get("status") != "active" or fm.get("rule_source") in (None, "none", ""):
            continue
        secs = _split_sections(text)
        active, _ = derive_active(secs.get("Auto Evaluation", ""),
                                  secs.get("Human Evaluation", ""))
        if active:
            out.append((fm.get("signature", ""), active))
    return out


# Hard cap on injected rules (KTD2): the block rides into every session via
# STATE.md and the native MEMORY.md digest, so a backlog drain that activates
# dozens of rules must not be able to blow the per-session context budget.
INJECT_CAP = 10


def inject(project=None, evs=None) -> str:
    """The machine-learned prevention-rules block for session-start injection.
    Empty string when there is nothing active to inject. Capped at INJECT_CAP
    rules ranked by occurrences then recency, with a `+N more` trailer when
    rules were cut. Pass evs to reuse an events.read_all() result."""
    rules = _active_rules(project)
    if not rules:
        return ""
    by_sig = _failures_by_sig(project, evs=evs)
    # Rank: occurrences desc, then last-failure recency desc. Two passes with a
    # stable sort (ts desc first, then occurrences desc) instead of one mixed key
    # because an ISO ts string cannot be negated.
    ranked = sorted(rules, key=lambda it: (
        by_sig[it[0]][-1].ts if by_sig.get(it[0]) else ""), reverse=True)
    ranked.sort(key=lambda it: -len(by_sig.get(it[0], [])))
    shown = ranked[:INJECT_CAP]
    # A rerouted rule replaced one that demonstrably failed — mark it so the
    # model treats the chosen route as mandatory, not advisory.
    rr_counts = {}
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_rerouted" and isinstance(ev.payload, dict):
            s = ev.payload.get("signature")
            if s:
                rr_counts[s] = rr_counts.get(s, 0) + 1
    lines = ["# PREVENTION RULES (machine-learned — avoid repeating these)"]
    for signature, rule in shown:
        n = rr_counts.get(signature, 0)
        mark = (f" (rerouted ×{n} — the previous rule failed; "
                "follow this route exactly)" if n else "")
        lines.append(f"- [{signature}]{mark} {rule}")
    cut = len(ranked) - len(shown)
    if cut > 0:
        lines.append(f"+{cut} more: hive rules list")
    return "\n".join(lines)


def soul_path(project=None):
    return paths.project_dir(project) / "SOUL.md"


def _active_rule_for(signature, project):
    path = _incident_dir(project) / f"{_safe_name(signature)}.md"
    if not path.exists():
        return ""
    secs = _split_sections(path.read_text(encoding="utf-8"))
    active, _ = derive_active(secs.get("Auto Evaluation", ""),
                              secs.get("Human Evaluation", ""))
    return active


def promote(signature, project=None) -> dict:
    """Append a signature's Active rule to the canonical SOUL.md and mark the
    incident promoted. The only write to canonical memory — invoked by a human
    or by auto_promote once a rule has proven itself."""
    rule = _active_rule_for(signature, project)
    if not rule:
        return {"promoted": False, "reason": "no active rule"}
    if _has_lifecycle(project, "rule_promoted", signature):
        return {"promoted": False, "reason": "already promoted"}
    sp = soul_path(project)
    sp.parent.mkdir(parents=True, exist_ok=True)
    header = "" if sp.exists() else "# SOUL — canonical operating rules\n\n"
    with open(sp, "a", encoding="utf-8") as f:
        f.write(f"{header}- [{signature}] {rule}\n")
    events.append_event("rule_promoted", {"signature": signature, "text": rule},
                        tags=["promote"], project=project)
    globals()["project"](project)          # re-project -> status promoted
    return {"promoted": True}


def auto_promote(project=None, evs=None) -> dict:
    """Self-acting promotion — the 'permanent SOP' rung of the learning loop.

    Any ACTIVE rule whose lesson classifies as ``resolved`` (at the recurrence
    threshold, rule present, tested after activation, zero leaks) has proven it
    actually changes behavior, so it graduates into SOUL.md without waiting for
    a human. This is what makes improvements self-sustaining: capture ->
    reflect -> inject -> prove -> promote, with no manual step anywhere.
    Idempotent (``promote`` refuses already-promoted signatures). Returns
    ``{"promoted": [signatures]}``. Pass evs to reuse a read_all() result."""
    stats = signal_stats(project, evs=evs)
    done = []
    for s in summaries(project):
        if s.get("status") != "active":
            continue
        sig = s.get("signature", "")
        if classify_resolution(s, stats.get(sig, {})) != "resolved":
            continue
        if promote(sig, project=project).get("promoted"):
            done.append(sig)
    return {"promoted": done}


SOUL_CAP = 10   # max promoted rules injected per session (newest first)


def soul_block(project=None) -> str:
    """Canonical operating rules (promoted SOPs) for session-start injection.

    This is the permanence mechanism promote() was missing a consumer for:
    SOUL.md was written but nothing ever read it back into a session, so a
    'promoted' rule was effectively archived, not permanent. Injecting the
    block through STATE.md (the single delivery choke point) makes promotion
    mean what it says. Empty string when no rules were ever promoted."""
    try:
        text = soul_path(project).read_text(encoding="utf-8")
    except OSError:
        return ""
    rules = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    if not rules:
        return ""
    lines = ["# SOUL — permanent operating rules (proven; always follow)"]
    shown = rules[-SOUL_CAP:]
    lines += shown
    cut = len(rules) - len(shown)
    if cut > 0:
        lines.append(f"+{cut} more in SOUL.md")
    return "\n".join(lines)


def demote(signature, project=None) -> dict:
    """Manually demote a rule (leaves injection). Records a rule_demoted event."""
    events.append_event("rule_demoted", {"signature": signature},
                        tags=["demote"], project=project)
    globals()["project"](project)
    return {"demoted": True}


def load(signature, project=None) -> dict:
    """Read back a record's frontmatter as a dict (for tests/inspection)."""
    p = _incident_dir(project) / f"{_safe_name(signature)}.md"
    if not p.exists():
        return {}
    return _frontmatter(p.read_text(encoding="utf-8"))


_LIFECYCLE_TYPES = {"rule_activated", "rule_promoted", "rule_demoted",
                    "rule_revised", "rule_expired", "rule_enforced",
                    "rule_rerouted"}


def signal_stats(project=None, evs=None) -> dict:
    """Per-signature enrichment from the event log in one pass, for the
    dashboard's Improvements view: first/last failure ts, live occurrence
    count, leak count, rule exposure, and explicit dismissal.

    Leaks count failures after the rule's activation ts from a session other
    than the one containing the threshold-crossing failure — prevention rules
    inject at session start, so same-session recurrences predate the rule
    being in play. A rule_rerouted event re-anchors both leaks and exposure at
    the reroute (excluding the rerouting session the same way), so a freshly
    deployed alternative route starts with a clean slate and can classify
    resolved once it holds. Exposure is whether any non-lifecycle activity
    arrived from a different session after the anchor, i.e. whether the rule
    has had a chance to prove itself. Pass evs to reuse an events.read_all()
    result."""
    if evs is None:
        evs = events.read_all(project)
    fails = {}                          # sig -> [(ts, session_id), ...] in ts order
    act_ts = {}                         # sig -> first rule_activated ts
    exp_ts = {}                         # sig -> latest rule_expired ts
    reroutes = {}                       # sig -> {n, ts, sess} (latest reroute)
    demoted = set()                     # sigs with an explicit rule_demoted event
    sess_last = {}                      # session_id -> latest activity ts
    for ev in evs:
        if ev.ts and ev.type not in _LIFECYCLE_TYPES:
            if ev.ts > sess_last.get(ev.session_id, ""):
                sess_last[ev.session_id] = ev.ts
        p = ev.payload if isinstance(ev.payload, dict) else {}
        s = p.get("signature")
        if not s:
            continue
        if ev.type == "tool_failed":
            fails.setdefault(s, []).append((ev.ts, ev.session_id))
        elif ev.type == "rule_activated" and s not in act_ts:
            act_ts[s] = ev.ts
        elif ev.type == "rule_rerouted":
            rr = reroutes.setdefault(s, {"n": 0, "ts": None, "sess": None})
            rr["n"] += 1
            if rr["ts"] is None or (ev.ts or "") > rr["ts"]:
                rr["ts"], rr["sess"] = ev.ts, ev.session_id
        elif ev.type == "rule_demoted":
            demoted.add(s)
        elif ev.type == "rule_expired":
            if ev.ts and ev.ts > exp_ts.get(s, ""):
                exp_ts[s] = ev.ts
    out = {}
    for s, hits in fails.items():
        act = act_ts.get(s)
        # Same re-anchor as leaks(): an expiry (decay, or retirement of a
        # bucket the classifier can no longer mint) starts the accounting over,
        # so the Improvements view and the record agree about what leaked.
        ex = exp_ts.get(s)
        if act and ex and ex > act:
            act = ex
        rr = reroutes.get(s) or {}
        if act and rr.get("ts") and rr["ts"] > act:
            anchor, anchor_sess = rr["ts"], rr.get("sess")
        else:
            anchor = act
            anchor_sess = next(
                (sess for ts, sess in hits if ts == act), None) if act else None
        leaks = sum(1 for ts, sess in hits
                    if ts > anchor and sess != anchor_sess) if anchor else 0
        # total_leaks ignores the reroute anchor (cumulative since activation):
        # the nudge->enforce ladder keys on it so rerouting cannot postpone
        # auto-enforcement for matcher-covered signatures.
        act_sess = next(
            (sess for ts, sess in hits if ts == act), None) if act else None
        total = sum(1 for ts, sess in hits
                    if ts > act and sess != act_sess) if act else 0
        exposure = bool(anchor) and any(
            last > anchor and sess != anchor_sess
            for sess, last in sess_last.items())
        out[s] = {
            "first_seen": hits[0][0],
            "last_seen": hits[-1][0],
            "event_occurrences": len(hits),
            "leaks": leaks,
            "total_leaks": total,
            "exposure": exposure,
            "dismissed": s in demoted,
            "reroutes": rr.get("n", 0),
            "last_reroute": rr.get("ts"),
        }
    return out


def classify_resolution(summary: dict, stats: dict) -> str:
    """Outcome axis for a lesson card: explicit dismissal wins; below the
    recurrence threshold (live-count aware) it's watching; at threshold with
    no rule it's unresolved; a leaking rule is unresolved; an untested rule
    (no post-activation exposure) is watching; a tested, holding rule is
    resolved. `stats` is signal_stats().get(signature, {}) — with a stats miss
    (projection outlived its events) a record can never classify as resolved."""
    if stats.get("dismissed"):
        return "dismissed"
    occurrences = max(int(summary.get("occurrences") or 0),
                      int(stats.get("event_occurrences") or 0))
    if occurrences < _threshold_for(summary.get("signature", "")):
        return "watching"
    if summary.get("rule_source") in (None, "", "none"):
        return "unresolved"
    if int(stats.get("leaks") or 0) > 0:
        return "unresolved"
    if not stats.get("exposure"):
        return "watching"
    return "resolved"


def classify_triage(resolution: str, summary: dict) -> dict:
    """Who should act on a lesson card, so the Improvements view can say it
    outright: ``{"triage": "claude"|"dismiss"|"", "triage_hint": str}``.

    ``claude`` — pending agent work exists: the lesson is unresolved (at
    threshold with no rule yet, or its rule keeps leaking); hand it to Claude
    (or the Reflect / resolve-red buttons) to root-cause and fix.
    ``dismiss`` — no agent work: watching (below threshold / unproven — may
    just be noise) or resolved (rule active and holding); safe for the user
    to clear manually with the dismiss button whenever they judge it done.
    ``""`` — already dismissed; nothing to mark."""
    if resolution == "dismissed":
        return {"triage": "", "triage_hint": ""}
    if resolution == "unresolved":
        no_rule = summary.get("rule_source") in (None, "", "none")
        hint = ("no rule yet — run Reflect, or hand this signature to Claude "
                "to root-cause and fix" if no_rule else
                "the rule keeps leaking — run resolve red, or ask Claude to "
                "fix the root cause")
        return {"triage": "claude", "triage_hint": hint}
    hint = ("below threshold / unproven — dismiss it if you judge it noise"
            if resolution == "watching"
            else "rule active and holding — safe to dismiss whenever you like")
    return {"triage": "dismiss", "triage_hint": hint}


def _as_int(v) -> int:
    """Best-effort int for a frontmatter field; 0 when missing or non-numeric."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def summaries(project=None, include_expired=False) -> list:
    """Structured lesson dicts for every incident record — the dashboard's
    Improvements view reads these. One dict per record: frontmatter status
    fields plus the parsed Auto Evaluation verdict (empty strings until the
    reflection pass fills it) and the derived active rule. Decayed (expired)
    records are excluded by default; pass include_expired=True to see them."""
    out = []
    d = _incident_dir(project)
    if not d.exists():
        return out
    for path in sorted(d.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _frontmatter(text)
        if not fm.get("signature"):
            continue
        if fm.get("status") == "expired" and not include_expired:
            continue
        secs = _split_sections(text)
        auto = secs.get("Auto Evaluation", "")
        active, source = derive_active(auto, secs.get("Human Evaluation", ""))
        out.append({
            "signature": fm["signature"],
            "status": fm.get("status", ""),
            # Coerce to int: a hand-edited non-numeric value must not reach
            # innerHTML unescaped or break the occurrences sort (int vs str).
            "occurrences": _as_int(fm.get("occurrences")),
            "sessions": _as_int(fm.get("sessions")),
            # The ids behind that count, bounded by `MAX_SESSION_IDS`. EMPTY for
            # every record written before this key existed, which is the honest
            # answer — authorship was not captured then, and a consumer must not
            # read "no ids" as "no human wrote this". `sessions` above is the
            # true count, so a reader can tell a complete list from a truncated
            # or absent one by comparing lengths.
            "session_ids": _id_list(fm.get("session_ids")),
            "rule_source": source,
            "flag": fm.get("flag", ""),
            "excerpt": _raw_excerpt(secs.get("Raw Signal", "")),
            "root_cause": _auto_field(auto, "Root cause"),
            "lesson": _auto_field(auto, "Lesson"),
            "prevention_rule": _auto_rule(auto),
            "category": _auto_field(auto, "Category"),
            "active_rule": active,
        })
    return out
