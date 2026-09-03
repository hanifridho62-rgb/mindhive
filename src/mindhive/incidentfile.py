# src/mindhive/incidentfile.py
"""The incident record's on-disk Markdown format: parse it, and render it.

Split out of `incidents.py` when that module crossed the 1000-line cap
`tests/test_file_size_cap.py` enforces. The cap's own failure message offers
two ways out and calls one of them debt: "Split the file, or add it to ALLOWED
with its count and a reason - but adding it is a debt entry, not a fix." This
is the split.

The seam is the one the module already had. Everything here is a pure function
of text: given a record's bytes it answers what the sections say, and given a
projection it answers what the record should say. Nothing here knows where a
record lives, which project it belongs to, or when it should be rewritten -
that stays in `incidents`, along with `load`, which needs the directory.

Chosen over the other candidate seam, the cross-process write lock, for one
concrete reason: that cluster owns `_LOCK_DEPTH`, an int mutated through
`global`. Re-exporting a name for it would have left `incidents._LOCK_DEPTH`
frozen at zero while the real counter moved in another module's globals, and
`tests/test_incidents.py` asserts on exactly that name - a split that makes an
existing assertion lie is worse than the file being long. This cluster holds no
mutable state, so `incidents` re-imports these names and every existing address
keeps working.
"""
import re


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


# How many contributing session ids the frontmatter carries. Bounded because a
# signature hit by hundreds of sessions would otherwise put a kilobyte of ids in
# a header nobody reads — and because `sessions:` already carries the TRUE count,
# a reader can tell a complete list from a truncated one by comparing the two.
# That comparison is what lets `advice.resolve_constraints` fail open rather than
# withhold a record on a partial view of who wrote it.
MAX_SESSION_IDS = 20


def _render(signature, rec, status, auto_body, human_body, active, source, flag="") -> str:
    actions = "\n".join(f"- `{a}`" for a in rec["actions"][:5]) or "- (unknown)"
    sessions = ", ".join(rec["sessions"][:5]) + (
        f" (+{len(rec['sessions']) - 5})" if len(rec["sessions"]) > 5 else "")
    # The ids themselves, not just how many. The body's "Sample sessions" line has
    # carried them since this file was written, but it is prose in a section a
    # parser has no contract with — and `summaries()` dropped them entirely, so
    # nothing downstream could ask WHO produced a failure. `advice` needs that to
    # tell a constraint distilled from a human's failures from one an injected
    # agent manufactured (TODOS 26).
    #
    # A NEW key. `sessions:` keeps its meaning — an int the dashboard's
    # Improvements view reads — because changing a live consumer's type to serve
    # a new reader breaks the old one.
    session_ids = ", ".join(rec["sessions"][:MAX_SESSION_IDS])
    active_body = f"> {active}" if active else "> (none yet)"
    return f"""---
signature: {signature}
status: {status}
occurrences: {rec['occurrences']}
rule_source: {source}
flag: {flag}
sessions: {len(rec['sessions'])}
session_ids: {session_ids}
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


_FM_KEYS = ("signature", "status", "occurrences", "rule_source", "flag",
            "sessions", "session_ids")


def _id_list(value) -> list:
    """A comma-separated frontmatter value as a list of ids. Total.

    `_frontmatter` coerces an all-digit value to `int`, so a record whose only
    contributing session id was numeric arrives here as a number rather than a
    string — hence the `str()` rather than a `.split` on whatever was decoded."""
    try:
        return [p for p in (s.strip() for s in str(value or "").split(",")) if p]
    except (ValueError, TypeError):
        return []


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


def _auto_field(section: str, name: str) -> str:
    """One labelled value from an Auto Evaluation body ('- Name: value').
    Line-wise by format contract: a multi-line value keeps its first line."""
    m = re.search(rf"{name}:\s*(.+)", section or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _raw_excerpt(section: str) -> str:
    """The error excerpt line from a Raw Signal body, backtick quoting removed."""
    m = re.search(r"Error excerpt:\s*(.+)", section or "")
    return m.group(1).strip().strip("`") if m else ""
