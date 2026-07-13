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
from . import events, paths

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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


def _has_lifecycle(project, etype, signature, evs=None) -> bool:
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == etype and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            return True
    return False


def _activation_ts(project, signature, evs=None) -> str | None:
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_activated" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            return ev.ts
    return None


def _latest_expiry_ts(project, signature, evs=None) -> str | None:
    """ts of the most recent rule_expired event for a signature, or None.
    Occurrences for status purposes count only failures after this point, so
    re-entry after decay starts a fresh candidate→active climb. Pass evs to
    reuse an events.read_all() result."""
    latest = None
    for ev in (evs if evs is not None else events.read_all(project)):
        if ev.type == "rule_expired" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            if latest is None or ev.ts > latest:
                latest = ev.ts
    return latest


def _live_failures(project, signature, failures, evs=None) -> list:
    """The status-relevant slice of a signature's failures: everything after
    the latest rule_expired, or all of them when it never expired. ``failures``
    is that signature's tool_failed list; ``evs`` optionally reuses the full
    events.read_all() result for the expiry lookup."""
    exp = _latest_expiry_ts(project, signature, evs=evs)
    return [e for e in failures if e.ts > exp] if exp else failures


def _ts_secs(ts) -> float | None:
    """Epoch seconds for a store ISO timestamp, or None when unparseable."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def leaks(project=None, signature=None) -> int:
    """Count tool_failed events for a signature that occurred AFTER its rule
    activated — i.e. the same error recurred despite an active rule."""
    act = _activation_ts(project, signature)
    if not act:
        return 0
    evs = _failures_by_sig(project).get(signature, [])
    return sum(1 for e in evs if e.ts > act)


def _status_and_flag(project, signature, rec, threshold, source):
    if _has_lifecycle(project, "rule_promoted", signature):
        return "promoted", ""
    occurrences = rec["occurrences"]
    if _latest_expiry_ts(project, signature) is not None:
        # Post-expiry occurrences only: 0 -> expired (stays expired across a
        # full project() rebuild), 1 -> candidate again, threshold -> active.
        occurrences = len(_live_failures(
            project, signature, _failures_by_sig(project).get(signature, [])))
        if occurrences == 0:
            return "expired", ""
    if occurrences < _threshold_for(signature, threshold):
        return "candidate", ""
    if _has_lifecycle(project, "rule_demoted", signature):
        return "demoted", ""
    lk = leaks(project, signature)
    if source == "auto" and lk >= LEAK_LIMIT:
        return "demoted", ""
    flag = "needs_review" if source == "human" and lk >= 1 else ""
    return "active", flag


def audit(project=None, threshold=THRESHOLD, now=None) -> dict:
    """Emit a one-time rule_activated event (stamped at the threshold-crossing
    failure's ts) for any signature that has reached its recurrence threshold
    (judgment:flag:* activates at 1) and isn't activated yet. Also decays stale
    noise: a candidate with exactly one live occurrence whose latest failure is
    older than DECAY_DAYS gains a rule_expired lifecycle event — except
    judgment:flag:* incidents, which never expire. Idempotent: an expiry zeroes
    the live count, so re-running appends nothing new."""
    activated = expired = 0
    now_secs = _ts_secs(now) if now else time.time()
    for signature, evs in _failures_by_sig(project).items():
        live = _live_failures(project, signature, evs)
        thr = _threshold_for(signature, threshold)
        if len(live) >= thr and not _has_lifecycle(project, "rule_activated", signature):
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
    return {"activated": activated, "expired": expired}


def _safe_name(signature: str) -> str:
    return _SAFE.sub("-", signature.lower()).strip("-") or "unknown"


def _incident_dir(project=None):
    return paths.project_dir(project) / "rules" / "incidents"


def _collect(project):
    """signature -> {occurrences, actions[], excerpt, sessions[]} from events."""
    acc = {}
    for ev in events.read_all(project):
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


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RULE_LINE = re.compile(r"Prevention rule:\s*(.+)", re.IGNORECASE)


def _split_sections(text: str) -> dict:
    """Map '## Name' headers to their body text."""
    out, cur, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = line[3:].strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def _replace_section(text: str, name: str, new_body: str) -> str:
    """Return text with the '## name' section body replaced by new_body."""
    lines = text.splitlines()
    out, i, replaced = [], 0, False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].strip() == f"## {name}":
            out.append(new_body)
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1                       # skip old body
            replaced = True
            continue
        i += 1
    if not replaced:                          # append the section if missing
        out += [f"## {name}", new_body]
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _human_text(section: str) -> str:
    return _COMMENT.sub("", section or "").strip()


def _auto_rule(section: str) -> str:
    m = _RULE_LINE.search(section or "")
    return m.group(1).strip() if m else ""


def derive_active(auto_section: str, human_section: str):
    """(active_rule_text, rule_source) — Human overrides Auto (D10)."""
    human = _human_text(human_section)
    if human:
        return human, "human"
    auto = _auto_rule(auto_section)
    if auto:
        return auto, "auto"
    return "", "none"


def _render(signature, rec, status, auto_body, human_body, active, source, flag="") -> str:
    actions = "\n".join(f"- `{a}`" for a in rec["actions"][:5]) or "- (unknown)"
    sessions = ", ".join(rec["sessions"][:5]) + (
        f" (+{len(rec['sessions']) - 5})" if len(rec["sessions"]) > 5 else "")
    active_body = f"> {active}" if active else "> (none yet)"
    return f"""---
signature: {signature}
status: {status}
occurrences: {rec['occurrences']}
rule_source: {source}
flag: {flag}
sessions: {len(rec['sessions'])}
---

# Incident: {signature}

## Raw Signal
- Error excerpt: `{rec['excerpt'][:300].replace('`', "'")}`
- Failing actions:
{actions}
- Sample sessions: {sessions or '(none)'}

## Auto Evaluation
{auto_body}

## Human Evaluation
{human_body}

## Active Prevention Rule
{active_body}
"""


def project(project=None, threshold=THRESHOLD) -> dict:
    """(Re)write an incident record per signature, preserving the Auto and Human
    Evaluation sections and re-deriving the Active rule. Returns {records: n}."""
    acc = _collect(project)
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
            status, flag = _status_and_flag(project, signature, rec, threshold, source)
            _atomic_write(path, _render(signature, rec, status, auto_body,
                                        human_body, active, source, flag))
    return {"records": len(acc)}


def _last_revision(project, signature) -> str:
    """The most recent human rule_revised text for a signature, or ''. """
    latest = ""
    for ev in events.read_all(project):
        if ev.type == "rule_revised" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            latest = ev.payload.get("text", "")
    return latest


def reconcile(project=None) -> dict:
    """Scan incident files; append a rule_revised event for any Human Evaluation
    that is non-empty and differs from the last recorded revision. Idempotent."""
    d = _incident_dir(project)
    revised = 0
    if not d.exists():
        return {"revised": 0}
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
        if human and human != _last_revision(project, signature):
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
    lines = ["# PREVENTION RULES (machine-learned — avoid repeating these)"]
    for signature, rule in shown:
        lines.append(f"- [{signature}] {rule}")
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


_FM_KEYS = ("signature", "status", "occurrences", "rule_source", "flag", "sessions")


def _frontmatter(text: str) -> dict:
    """Parse an incident record's frontmatter fields from its text."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if m and m.group(1) in _FM_KEYS:
            v = m.group(2)
            out[m.group(1)] = int(v) if v.isdigit() else v
        if line.startswith("# Incident:"):
            break
    return out


def load(signature, project=None) -> dict:
    """Read back a record's frontmatter as a dict (for tests/inspection)."""
    p = _incident_dir(project) / f"{_safe_name(signature)}.md"
    if not p.exists():
        return {}
    return _frontmatter(p.read_text(encoding="utf-8"))


def _auto_field(section: str, name: str) -> str:
    """One labelled value from an Auto Evaluation body ('- Name: value').
    Line-wise by format contract: a multi-line value keeps its first line."""
    m = re.search(rf"{name}:\s*(.+)", section or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _raw_excerpt(section: str) -> str:
    """The error excerpt line from a Raw Signal body, backtick quoting removed."""
    m = re.search(r"Error excerpt:\s*(.+)", section or "")
    return m.group(1).strip().strip("`") if m else ""


_LIFECYCLE_TYPES = {"rule_activated", "rule_promoted", "rule_demoted",
                    "rule_revised", "rule_expired", "rule_enforced"}


def signal_stats(project=None, evs=None) -> dict:
    """Per-signature enrichment from the event log in one pass, for the
    dashboard's Improvements view: first/last failure ts, live occurrence
    count, leak count, rule exposure, and explicit dismissal.

    Leaks count failures after the rule's activation ts from a session other
    than the one containing the threshold-crossing failure — prevention rules
    inject at session start, so same-session recurrences predate the rule
    being in play. Exposure is whether any non-lifecycle activity arrived from
    a different session after activation, i.e. whether the rule has had a
    chance to prove itself. Pass evs to reuse an events.read_all() result."""
    if evs is None:
        evs = events.read_all(project)
    fails = {}                          # sig -> [(ts, session_id), ...] in ts order
    act_ts = {}                         # sig -> first rule_activated ts
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
        elif ev.type == "rule_demoted":
            demoted.add(s)
    out = {}
    for s, hits in fails.items():
        act = act_ts.get(s)
        act_sess = next((sess for ts, sess in hits if ts == act), None) if act else None
        leaks = sum(1 for ts, sess in hits
                    if ts > act and sess != act_sess) if act else 0
        exposure = bool(act) and any(
            last > act and sess != act_sess for sess, last in sess_last.items())
        out[s] = {
            "first_seen": hits[0][0],
            "last_seen": hits[-1][0],
            "event_occurrences": len(hits),
            "leaks": leaks,
            "exposure": exposure,
            "dismissed": s in demoted,
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
