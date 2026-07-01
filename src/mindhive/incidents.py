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


def _render(signature, rec, status, auto_body, human_body, active, source) -> str:
    actions = "\n".join(f"- `{a}`" for a in rec["actions"][:5]) or "- (unknown)"
    sessions = ", ".join(rec["sessions"][:5]) + (
        f" (+{len(rec['sessions']) - 5})" if len(rec["sessions"]) > 5 else "")
    active_body = f"> {active}" if active else "> (none yet)"
    return f"""---
signature: {signature}
status: {status}
occurrences: {rec['occurrences']}
rule_source: {source}
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
        status = "active" if rec["occurrences"] >= threshold else "candidate"
        path.write_text(_render(signature, rec, status, auto_body, human_body,
                                active, source), encoding="utf-8")
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


def load(signature, project=None) -> dict:
    """Read back a record's frontmatter as a dict (for tests/inspection)."""
    p = _incident_dir(project) / f"{_safe_name(signature)}.md"
    out = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\w+):\s*(.*)$", line)
        if m and m.group(1) in ("signature", "status", "occurrences", "rule_source", "sessions"):
            v = m.group(2)
            out[m.group(1)] = int(v) if v.isdigit() else v
        if line.startswith("# Incident:"):
            break
    return out
