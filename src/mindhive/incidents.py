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


def _render(signature, rec, status) -> str:
    actions = "\n".join(f"- `{a}`" for a in rec["actions"][:5]) or "- (unknown)"
    sessions = ", ".join(rec["sessions"][:5]) + (
        f" (+{len(rec['sessions']) - 5})" if len(rec["sessions"]) > 5 else "")
    return f"""---
signature: {signature}
status: {status}
occurrences: {rec['occurrences']}
rule_source: none
sessions: {len(rec['sessions'])}
---

# Incident: {signature}

## Raw Signal
- Error excerpt: `{rec['excerpt'][:300].replace('`', "'")}`
- Failing actions:
{actions}
- Sample sessions: {sessions or '(none)'}

## Auto Evaluation
<!-- filled by the reflection pass (Plan 2) -->

## Human Evaluation
<!-- leave blank to accept Auto; fill to override -->

## Active Prevention Rule
> (none yet)
"""


def project(project=None, threshold=THRESHOLD) -> dict:
    """(Re)write an incident record per signature. Returns {records: n}."""
    acc = _collect(project)
    d = _incident_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    for signature, rec in acc.items():
        status = "active" if rec["occurrences"] >= threshold else "candidate"
        (d / f"{_safe_name(signature)}.md").write_text(
            _render(signature, rec, status), encoding="utf-8")
    return {"records": len(acc)}


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
