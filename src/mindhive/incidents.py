# src/mindhive/incidents.py
"""Project `tool_failed` events into per-signature incident records.

One Markdown file per signature under rules/incidents/. The event log is the
canonical truth; these files are the human-editable workspace/projection. Plan 1
writes the immutable Raw Signal and frontmatter; the Auto/Human evaluation
sections are stubs that Plan 2 populates.
"""
import re
from . import events, paths

THRESHOLD = 2                       # recurrence gate N (D2)
_SAFE = re.compile(r"[^a-z0-9]+")


LEAK_LIMIT = 2


def _failures_by_sig(project) -> dict:
    """signature -> list of tool_failed Events, in ts order (read_all is sorted)."""
    out = {}
    for ev in events.read_all(project):
        if ev.type != "tool_failed":
            continue
        s = ev.payload.get("signature") if isinstance(ev.payload, dict) else None
        if s:
            out.setdefault(s, []).append(ev)
    return out


def _has_lifecycle(project, etype, signature) -> bool:
    for ev in events.read_all(project):
        if ev.type == etype and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            return True
    return False


def _activation_ts(project, signature) -> str | None:
    for ev in events.read_all(project):
        if ev.type == "rule_activated" and isinstance(ev.payload, dict) \
                and ev.payload.get("signature") == signature:
            return ev.ts
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
    if rec["occurrences"] < threshold:
        return "candidate", ""
    if _has_lifecycle(project, "rule_demoted", signature):
        return "demoted", ""
    lk = leaks(project, signature)
    if source == "auto" and lk >= LEAK_LIMIT:
        return "demoted", ""
    flag = "needs_review" if source == "human" and lk >= 1 else ""
    return "active", flag


def audit(project=None, threshold=THRESHOLD) -> dict:
    """Emit a one-time rule_activated event (stamped at the threshold-crossing
    failure's ts) for any signature that has reached the recurrence threshold and
    isn't activated yet. Idempotent."""
    activated = 0
    for signature, evs in _failures_by_sig(project).items():
        if len(evs) >= threshold and not _has_lifecycle(project, "rule_activated", signature):
            crossing_ts = evs[threshold - 1].ts
            events.append_event("rule_activated", {"signature": signature},
                                tags=["lifecycle"], ts=crossing_ts, project=project)
            activated += 1
    return {"activated": activated}


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
        path.write_text(_render(signature, rec, status, auto_body, human_body,
                                active, source, flag), encoding="utf-8")
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


def inject(project=None) -> str:
    """The machine-learned prevention-rules block for session-start injection.
    Empty string when there is nothing active to inject."""
    rules = _active_rules(project)
    if not rules:
        return ""
    lines = ["# PREVENTION RULES (machine-learned — avoid repeating these)"]
    for signature, rule in rules:
        lines.append(f"- [{signature}] {rule}")
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
    incident promoted. The only write to canonical memory (human-invoked)."""
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


def demote(signature, project=None) -> dict:
    """Manually demote a rule (leaves injection). Records a rule_demoted event."""
    events.append_event("rule_demoted", {"signature": signature},
                        tags=["demote"], project=project)
    globals()["project"](project)
    return {"demoted": True}


def load(signature, project=None) -> dict:
    """Read back a record's frontmatter as a dict (for tests/inspection)."""
    p = _incident_dir(project) / f"{_safe_name(signature)}.md"
    out = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if m and m.group(1) in ("signature", "status", "occurrences",
                                "rule_source", "flag", "sessions"):
            v = m.group(2)
            out[m.group(1)] = int(v) if v.isdigit() else v
        if line.startswith("# Incident:"):
            break
    return out
