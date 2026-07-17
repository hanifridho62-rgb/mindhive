# Learning Loop — Capture Foundation Implementation Plan (Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deterministically capture Claude Code tool failures from transcripts, normalize each to a root-cause *signature*, and project them into per-signature incident records with a recurrence gate — no LLM involved.

**Architecture:** Three pure/near-pure modules extend MIND HIVE's existing event store. `signature.py` maps an error string to a canonical key. `failures.py` extracts failures from transcript lines (keyed off the verified `is_error` flag, excluding `interrupted`) and appends `tool_failed` events. `incidents.py` projects those events into `rules/incidents/<signature>.md` records, setting status `candidate`/`active` at recurrence threshold N=2. A `hive failures` CLI runs capture over transcripts and lists incidents.

**Tech Stack:** Python 3.11+, stdlib only (no new deps), pytest. Follows existing `mindhive` module + test conventions.

**Spec references:** `docs/superpowers/specs/2026-07-01-learning-loop-architecture.md` (D1–D3), `...-glossary.md`, `docs/adr/0007-*.md`. This plan implements the **Capture** stage + the **Signature** normalizer + the incident **store** skeleton (Raw Signal only; Auto/Human evaluation sections are stubs filled by Plan 2).

**Out of scope for Plan 1 (later plans):** Sonnet reflection / Auto Evaluation, Human Evaluation round-trip (`reconcile`), injection, `hive rules` CRUD, promotion, demotion/efficacy counters.

---

## File Structure

- Create: `src/mindhive/signature.py` — pure `signature(error_text) -> str`. One responsibility: error → canonical key.
- Create: `src/mindhive/failures.py` — extract failures from transcript objects; correlate `tool_use_id → action`; append `tool_failed` events.
- Create: `src/mindhive/incidents.py` — project `tool_failed` events into `rules/incidents/<signature>.md` records with recurrence status.
- Modify: `src/mindhive/events.py` — register the `tool_failed` deterministic event type.
- Modify: `src/mindhive/__main__.py` — add the `hive failures` subcommand (`capture`, `list`).
- Create: `tests/test_signature.py`, `tests/test_failures.py`, `tests/test_incidents.py`, `tests/test_failures_cli.py`.

Data lives under the existing store: `~/.mindhive/<project>/events/*.jsonl` (events) and a new `~/.mindhive/<project>/rules/incidents/` (records).

---

## Task 1: Signature normalizer — known error families

**Files:**
- Create: `src/mindhive/signature.py`
- Test: `tests/test_signature.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signature.py
from mindhive.signature import signature


def test_python_missing_module_collapses_to_top_level_package():
    # All submodules of the same package share ONE signature — same root cause,
    # same fix (package not importable from here).
    assert signature("ModuleNotFoundError: No module named 'mindhive'") == "pymodule:mindhive"
    assert signature("ModuleNotFoundError: No module named 'mindhive.paths'") == "pymodule:mindhive"
    assert signature("ModuleNotFoundError: No module named 'scripts.core.event_cli'") == "pymodule:scripts"


def test_distinct_packages_get_distinct_signatures():
    assert signature("ModuleNotFoundError: No module named 'pypdf'") == "pymodule:pypdf"
    assert signature("No module named 'hive_pipeline'") == "pymodule:hive_pipeline"
    assert signature("ModuleNotFoundError: No module named 'pypdf'") != signature("No module named 'tools'")


def test_powershell_unknown_command():
    err = "head : The term 'head' is not recognized as the name of a cmdlet, function, ..."
    assert signature(err) == "pscmd:head"
    assert signature("'hive' is not recognized as an internal or external command") == "pscmd:hive"


def test_python_unterminated_string():
    assert signature("SyntaxError: unterminated string literal (detected at line 4)") == "pysyntax:unterminated-string"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_signature.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mindhive.signature'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/mindhive/signature.py
"""Map a raw error string to a canonical root-cause SIGNATURE.

Pure, deterministic, stdlib-only. The signature — not raw stderr — is what
recurrence, dedup, and rules key off. Two failures with the same root cause
(same fix) must share one signature; genuinely different bugs must not.
"""
import hashlib
import re

_NO_MODULE = re.compile(r"No module named '([^']+)'")
_PS_TERM = re.compile(r"The term '([^']+)'")
_PS_NOTRECOG = re.compile(r"'([^']+)' is not recognized")


def signature(error_text) -> str:
    t = str(error_text or "")

    m = _NO_MODULE.search(t)
    if m:
        top = m.group(1).split(".")[0]          # collapse submodules to package root
        return f"pymodule:{top}"

    m = _PS_TERM.search(t) or _PS_NOTRECOG.search(t)
    if m:
        return f"pscmd:{m.group(1)}"

    if "unterminated string literal" in t:
        return "pysyntax:unterminated-string"
    if "SyntaxError" in t:
        return "pysyntax:other"

    # Fallback: hash the first line with volatile bits (numbers, paths) masked so
    # the same error with different line numbers/paths still collapses together.
    first = next((ln for ln in t.splitlines() if ln.strip()), "")
    norm = first.strip().lower()
    norm = re.sub(r"[a-z]:\\[^\s]+|/[^\s]+", "PATH", norm)
    norm = re.sub(r"\d+", "N", norm)
    return "raw:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_signature.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/signature.py tests/test_signature.py
git commit -m "feat(learning-loop): signature normalizer for error root-cause keys"
```

---

## Task 2: Signature fallback determinism

**Files:**
- Modify: `src/mindhive/signature.py` (no code change expected — this locks behavior)
- Test: `tests/test_signature.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_signature.py

def test_fallback_masks_line_numbers_and_paths_so_variants_collapse():
    a = signature("Traceback: error at C:\\Users\\hanif\\a.py line 42: boom")
    b = signature("Traceback: error at C:\\Users\\hanif\\b.py line 99: boom")
    assert a == b
    assert a.startswith("raw:")


def test_empty_error_is_stable():
    assert signature("") == signature(None)
    assert signature("").startswith("raw:")
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_signature.py -v`
Expected: PASS (behavior already implemented in Task 1). If FAIL, the masking regex order is wrong — ensure path masking runs before digit masking (paths may contain digits), matching the code in Task 1 Step 3.

- [ ] **Step 3: Commit**

```bash
git add tests/test_signature.py
git commit -m "test(learning-loop): lock signature fallback determinism"
```

---

## Task 3: Register the `tool_failed` event type

**Files:**
- Modify: `src/mindhive/events.py:12-17` (the `DETERMINISTIC_TYPES` set)
- Test: `tests/test_events.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_events.py
from mindhive import events


def test_tool_failed_is_a_valid_deterministic_type():
    assert "tool_failed" in events.VALID_TYPES
    assert "tool_failed" in events.DETERMINISTIC_TYPES
    # deterministic, not semantic: must stay out of the LEDGER/graph intent layer
    assert "tool_failed" not in events.SEMANTIC_TYPES
    ev = events.append_event("tool_failed", {"text": "boom", "signature": "pymodule:x"},
                             session_id="s1", tags=["auto", "failure"], project="test")
    assert ev.type == "tool_failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_events.py::test_tool_failed_is_a_valid_deterministic_type -v`
Expected: FAIL — `assert 'tool_failed' in events.VALID_TYPES` is False (or `append_event` raises `ValueError: unknown event type: tool_failed`)

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/events.py`, add `"tool_failed"` to the `DETERMINISTIC_TYPES` set (it flows into `VALID_TYPES` automatically). The set becomes:

```python
DETERMINISTIC_TYPES = {
    "file_touched", "command_run", "git_diff",
    "session_start", "session_end", "compaction",
    "plugin_catalog", "user_prompt", "assistant_response",
    "loop_run_start", "loop_iteration", "loop_run_end",
    "tool_failed",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_events.py::test_tool_failed_is_a_valid_deterministic_type -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/events.py tests/test_events.py
git commit -m "feat(learning-loop): register tool_failed deterministic event type"
```

---

## Task 4: Extract failures from a single transcript line

**Files:**
- Create: `src/mindhive/failures.py`
- Test: `tests/test_failures.py`

Failures live in a user-role message whose `message.content` list contains a `tool_result` block with `is_error: true`. The stderr text is on the sibling top-level `toolUseResult` object; `toolUseResult.interrupted: true` means the user cancelled — NOT a failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_failures.py
from mindhive import failures


def _tool_result_line(is_error, stderr="", interrupted=False, content_text=""):
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "is_error": is_error, "content": content_text},
        ]},
        "toolUseResult": {"stdout": "", "stderr": stderr, "interrupted": interrupted},
    }


def test_extracts_failure_with_signature_from_stderr():
    line = _tool_result_line(True, stderr="ModuleNotFoundError: No module named 'mindhive'")
    out = failures.extract_failures(line)
    assert len(out) == 1
    assert out[0]["tool_use_id"] == "tu_1"
    assert out[0]["signature"] == "pymodule:mindhive"
    assert "mindhive" in out[0]["error_excerpt"]


def test_success_and_interrupted_are_not_failures():
    assert failures.extract_failures(_tool_result_line(False, stderr="")) == []
    assert failures.extract_failures(
        _tool_result_line(True, stderr="boom", interrupted=True)) == []


def test_uses_block_content_when_stderr_empty():
    line = _tool_result_line(True, stderr="",
                             content_text="ModuleNotFoundError: No module named 'pypdf'")
    out = failures.extract_failures(line)
    assert out[0]["signature"] == "pymodule:pypdf"


def test_non_tool_result_lines_yield_nothing():
    assert failures.extract_failures({"type": "assistant", "message": {"content": []}}) == []
    assert failures.extract_failures({}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mindhive.failures'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/mindhive/failures.py
"""Deterministic capture of tool FAILURES from Claude Code transcripts.

A failure is a tool_result block with is_error==True that was NOT interrupted
(user cancels are excluded). The error text comes from the sibling
`toolUseResult.stderr`, falling back to the block's own content. Each failure is
keyed to a root-cause signature and stored as a `tool_failed` event.
"""
from . import events, paths, signature as sig

_EXCERPT_MAX = 4000
_ACTION_MAX = 500
_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
_CMD_TOOLS = ("Bash", "PowerShell")


def _block_text(content) -> str:
    """A tool_result block's content is a string or a list of {type,text} parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def extract_failures(obj) -> list:
    """Pure: map one transcript line to a list of failure dicts.

    Each dict: {tool_use_id, error_excerpt, signature}. Returns [] for
    non-failures, successes, interrupts, or malformed input — never raises.
    """
    if not isinstance(obj, dict):
        return []
    msg = obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    tur = obj.get("toolUseResult") or {}
    interrupted = isinstance(tur, dict) and tur.get("interrupted") is True
    stderr = tur.get("stderr") if isinstance(tur, dict) else ""
    out = []
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        if b.get("is_error") is not True or interrupted:
            continue
        text = (stderr or "").strip() or _block_text(b.get("content")).strip()
        out.append({
            "tool_use_id": b.get("tool_use_id"),
            "error_excerpt": text[:_EXCERPT_MAX],
            "signature": sig.signature(text),
        })
    return out
```

The file ends here for Task 4. `capture_dir()` and its helpers are added in Task 5 — do not stub them now.

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/failures.py tests/test_failures.py
git commit -m "feat(learning-loop): extract tool failures from transcript lines"
```

---

## Task 5: Capture a transcript into `tool_failed` events (with action correlation)

**Files:**
- Modify: `src/mindhive/failures.py`
- Test: `tests/test_failures.py`

Correlate each failing `tool_use_id` to the command/action that produced it (from the earlier assistant `tool_use` block in the same transcript), then append one `tool_failed` event per failure.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_failures.py
import json
from mindhive import events


def _assistant_tool_use(tool_use_id, name, cmd=None, file_path=None):
    inp = {}
    if cmd is not None:
        inp["command"] = cmd
    if file_path is not None:
        inp["file_path"] = file_path
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": inp}]}}


def test_capture_writes_tool_failed_event_with_action(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [
        _assistant_tool_use("tu_1", "Bash", cmd="python mindhive.py state"),
        _tool_result_line(True, stderr="ModuleNotFoundError: No module named 'mindhive'"),
    ]
    # patch the tool_use_id on the result to match
    lines[1]["message"]["content"][0]["tool_use_id"] = "tu_1"
    (tdir / "sess.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")

    res = failures.capture_dir(str(tdir), project="test")
    assert res["new_failures"] == 1
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
    assert evs[0].payload["signature"] == "pymodule:mindhive"
    assert evs[0].payload["action"] == "python mindhive.py state"
    assert "failure" in evs[0].tags


def test_capture_is_idempotent(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [_assistant_tool_use("tu_1", "Bash", cmd="x"),
             _tool_result_line(True, stderr="No module named 'tools'")]
    lines[1]["message"]["content"][0]["tool_use_id"] = "tu_1"
    (tdir / "sess.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    failures.capture_dir(str(tdir), project="test")
    res2 = failures.capture_dir(str(tdir), project="test")
    assert res2["new_failures"] == 0
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures.py -k capture -v`
Expected: FAIL — `AttributeError: module 'mindhive.failures' has no attribute 'capture_dir'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/mindhive/failures.py`:

```python
import json
from pathlib import Path


def action_from_tool_use(block) -> str:
    """Human-readable action string for a tool_use content block."""
    name = block.get("name")
    inp = block.get("input") or {}
    if name in _CMD_TOOLS and inp.get("command"):
        return str(inp["command"])[:_ACTION_MAX]
    if name in _FILE_TOOLS and inp.get("file_path"):
        return f"{name} {inp['file_path']}"[:_ACTION_MAX]
    return (name or "unknown")[:_ACTION_MAX]


def _iter_lines(path):
    for raw in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue


def _existing_failure_keys(project) -> set:
    """(session, signature, action) already stored — makes capture idempotent."""
    keys = set()
    for ev in events.read_all(project):
        if ev.type != "tool_failed":
            continue
        p = ev.payload if isinstance(ev.payload, dict) else {}
        keys.add((ev.session_id, p.get("signature"), p.get("action")))
    return keys


def capture_dir(transcript_dir, project=None, now=None) -> dict:
    """Scan a transcript directory, append a `tool_failed` event per new failure.

    Correlates each failing tool_use_id to its action (the assistant tool_use
    earlier in the same session). Idempotent: a (session, signature, action)
    already stored is skipped.
    """
    seen = _existing_failure_keys(project)
    new_failures = 0
    d = Path(transcript_dir)
    if not d.exists():
        return {"new_failures": 0, "files": 0}
    files = 0
    for f in sorted(d.glob("*.jsonl")):
        files += 1
        sid = f.stem
        actions = {}                    # tool_use_id -> action string
        for obj in _iter_lines(f):
            msg = obj.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        actions[b["id"]] = action_from_tool_use(b)
            for fail in extract_failures(obj):
                action = actions.get(fail["tool_use_id"], "unknown")
                key = (sid, fail["signature"], action)
                if key in seen:
                    continue
                seen.add(key)
                events.append_event(
                    "tool_failed",
                    {"text": fail["error_excerpt"], "signature": fail["signature"],
                     "action": action},
                    session_id=sid, tags=["auto", "failure"], ts=now, project=project)
                new_failures += 1
    return {"new_failures": new_failures, "files": files}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures.py -v`
Expected: PASS (all failures tests)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/failures.py tests/test_failures.py
git commit -m "feat(learning-loop): capture transcript failures into tool_failed events"
```

---

## Task 6: Project failures into incident records with the recurrence gate

**Files:**
- Create: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

One record per signature at `rules/incidents/<safe-signature>.md`. Status is `candidate` below N=2 occurrences, `active` at/above. Auto/Human/Active sections are stubs here (Plan 2 fills them).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_incidents.py
from mindhive import events, incidents, paths


def _add_failure(sig, action, session):
    events.append_event("tool_failed",
                        {"text": f"err {sig}", "signature": sig, "action": action},
                        session_id=session, tags=["auto", "failure"], project="test")


def test_single_occurrence_is_candidate(tmp_path):
    _add_failure("pymodule:mindhive", "python mindhive.py", "s1")
    res = incidents.project(project="test", threshold=2)
    assert res["records"] == 1
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "candidate"
    assert rec["occurrences"] == 1


def test_second_occurrence_activates(tmp_path):
    _add_failure("pymodule:mindhive", "python mindhive.py", "s1")
    _add_failure("pymodule:mindhive", "python mindhive.py state", "s2")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "active"
    assert rec["occurrences"] == 2


def test_one_record_per_signature_across_sessions(tmp_path):
    for i in range(5):
        _add_failure("pymodule:tools", "x", f"s{i}")
    res = incidents.project(project="test", threshold=2)
    assert res["records"] == 1
    incident_dir = paths.project_dir("test") / "rules" / "incidents"
    assert len(list(incident_dir.glob("*.md"))) == 1


def test_record_file_has_required_sections(tmp_path):
    _add_failure("pscmd:head", "head foo.txt", "s1")
    incidents.project(project="test", threshold=2)
    incident_dir = paths.project_dir("test") / "rules" / "incidents"
    body = list(incident_dir.glob("*.md"))[0].read_text(encoding="utf-8")
    for section in ("## Raw Signal", "## Auto Evaluation",
                    "## Human Evaluation", "## Active Prevention Rule"):
        assert section in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mindhive.incidents'`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): project failures into incident records with recurrence gate"
```

---

## Task 7: `hive failures` CLI (capture + list)

**Files:**
- Modify: `src/mindhive/__main__.py` (add handlers + subparser, following the existing `set_defaults(func=...)` pattern)
- Test: `tests/test_failures_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_failures_cli.py
import json
from mindhive import __main__ as cli, events


def _write_transcript(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu_1", "name": "Bash",
             "input": {"command": "python mindhive.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "is_error": True, "content": ""}]},
         "toolUseResult": {"stderr": "No module named 'mindhive'", "interrupted": False}},
    ]
    (tdir / "s.jsonl").write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    return tdir


def test_failures_capture_then_list(tmp_path, capsys):
    tdir = _write_transcript(tmp_path)
    rc = cli.main(["failures", "capture", "--transcripts", str(tdir), "--project", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"new_failures": 1' in out

    rc = cli.main(["failures", "list", "--project", "test"])
    assert rc == 0
    listing = capsys.readouterr().out
    assert "pymodule:mindhive" in listing
    assert "candidate" in listing        # 1 occurrence < threshold
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures_cli.py -v`
Expected: FAIL — argparse errors with `invalid choice: 'failures'`

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/__main__.py`, add two handlers next to the other `cmd_*` functions:

```python
def cmd_failures_capture(args) -> int | None:
    from . import failures as failures_mod, incidents as incidents_mod
    res = failures_mod.capture_dir(args.transcripts, project=args.project)
    incidents_mod.project(project=args.project)
    print(json.dumps(res))
    return None


def cmd_failures_list(args) -> int | None:
    from . import incidents as incidents_mod
    acc = incidents_mod._collect(args.project)
    if not acc:
        print("(no failures captured yet)")
        return None
    for signature, rec in sorted(acc.items(), key=lambda kv: -kv[1]["occurrences"]):
        status = "active" if rec["occurrences"] >= incidents_mod.THRESHOLD else "candidate"
        print(f"{rec['occurrences']:>3}x  [{status:9}] {signature}")
    return None
```

Then register the subcommand inside `_build_parser()`, before `return p`:

```python
    # --- failures (Learning Loop capture stage) ---
    fp = sub.add_parser("failures", help="capture tool failures + list incidents")
    fsub = fp.add_subparsers(dest="failures_cmd", required=True)
    fc = fsub.add_parser("capture", help="scan a transcript dir, store tool_failed events")
    fc.add_argument("--transcripts", required=True, help="transcript DIRECTORY")
    fc.add_argument("--project", default=None, help="target namespace")
    fc.set_defaults(func=cmd_failures_capture)
    fl = fsub.add_parser("list", help="list incident signatures by occurrence")
    fl.add_argument("--project", default=None)
    fl.set_defaults(func=cmd_failures_list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/__main__.py tests/test_failures_cli.py
git commit -m "feat(learning-loop): hive failures capture/list CLI"
```

---

## Task 8: End-to-end recurrence + full-suite regression

**Files:**
- Test: `tests/test_incidents.py` (add one end-to-end test)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
import json
from mindhive import failures


def test_end_to_end_two_sessions_same_signature_activates(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    def line(tu, cmd, err):
        return [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tu, "name": "Bash", "input": {"command": cmd}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tu, "is_error": True, "content": ""}]},
             "toolUseResult": {"stderr": err, "interrupted": False}},
        ]
    (tdir / "s1.jsonl").write_text("\n".join(
        json.dumps(o) for o in line("a", "python mindhive.py", "No module named 'mindhive'")),
        encoding="utf-8")
    (tdir / "s2.jsonl").write_text("\n".join(
        json.dumps(o) for o in line("b", "python mindhive.py state", "No module named 'mindhive.paths'")),
        encoding="utf-8")

    failures.capture_dir(str(tdir), project="test")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["occurrences"] == 2
    assert rec["status"] == "active"
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py::test_end_to_end_two_sessions_same_signature_activates -v`
Expected: PASS (logic already built; this locks the cross-session recurrence path — two DIFFERENT submodule errors collapse to one active incident).

- [ ] **Step 3: Run the full suite (regression)**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass (new suites + no regressions in existing ones).

- [ ] **Step 4: Commit**

```bash
git add tests/test_incidents.py
git commit -m "test(learning-loop): end-to-end cross-session recurrence activation"
```

---

## Task 9: Manual corpus-replay verification (Tier-4 proof)

Not a unit test — this is the ground-truth check from the spec (TESTING.md Test 16): capture over your REAL transcripts and confirm it reproduces the recurrence counts measured during design.

- [ ] **Step 1: Run capture over a real project's transcripts**

Run (adjust the transcript dir to a real one, e.g. the current project's):
```bash
C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m mindhive failures capture \
  --transcripts "$HOME/.claude/projects/C--Users-hanif" --project corpus-check
C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m mindhive failures list --project corpus-check
```
Expected: signatures such as `pymodule:mindhive`, `pymodule:hive_pipeline`, `pymodule:tools`, `pscmd:head`, `pysyntax:unterminated-string` appear, `active`, with occurrence counts in the same ballpark as the design measurements (mindhive ~13, hive_pipeline ~14, tools ~10). Exact counts differ because signature collapses submodules — that is correct behavior.

- [ ] **Step 2: Clean up the throwaway namespace**

```bash
rm -rf "$HOME/.mindhive/corpus-check"
```

- [ ] **Step 3: Record the result**

Note the observed top signatures + counts in the PR description (or `hive append`), as the empirical proof the capture stage would have caught the real historical mistakes.

---

## Task 9 Findings (corpus replay, 2026-07-01) — reshapes Plan 2

Ran capture over all real transcript dirs: **224 real failures, 55 signatures.** The mechanism works. But the ground truth differs sharply from the design-time grep, which was **contaminated by prose** (this very ULT-IDEA session + the spec docs repeatedly *quote* `ModuleNotFoundError: 'mindhive'`, inflating the grep). Real *tool failures* (`is_error=true`) tell a different story. Top real signatures:

| count | what it actually is | verdict |
|------:|---------------------|---------|
| 90× | `Exit code 1` from a validation script (py_compile/pyflakes/selftest) | real recurring — worth a rule |
| 39× | `claude-opus-4-8 is temporarily unavailable, auto mode cannot determine safety` | **NOISE** — transient infra, not preventable |
| 15× | `File has not been read yet. Read it first before writing` | real, preventable behavioral rule |
| 9× | `The user doesn't want to proceed... tool use was rejected` | **NOISE** — user rejection, like `interrupted` |
| 8× | `String to replace not found in file` (stale Edit `old_string`) | real, preventable |
| 8× | `Ripgrep search timed out after 20 seconds` | real, preventable (narrow the search) |
| 2× | `File content exceeds maximum allowed size` | real, preventable |
| 2× | `pymodule:tools` | the only `pymodule:*` that genuinely recurred |

**Two required changes this surfaced (fold into Plan 2, or a small Plan 1.5):**
1. **Noise exclusion filter** — drop `is_error` results that are not preventable coding mistakes: model-unavailable/"auto mode cannot determine safety", and "user doesn't want to proceed / tool use was rejected". Same category as the existing `interrupted` exclusion. ~48 of 224 captured were noise.
2. **Rewrite the signature family table around the REAL top failures** — add families: `edit:not-read` (Read before Edit/Write), `edit:string-not-found`, `tool:ripgrep-timeout`, `read:file-too-large`, and a normalizer for the `Exit code N` script case. The hypothesized `pymodule:*`/`pscmd:*` families are correct but rare in practice; the behavioral Claude-tool errors dominate.

This is a **success of the verification step**, not a defect: Plan 1's pipeline is proven on real data, and it corrected our model of what actually recurs before we built reflection on top of the wrong assumptions.

## Self-Review

**1. Spec coverage (Plan 1 scope = Capture + Signature + incident store skeleton):**
- D1 capture (deterministic, `is_error`, exclude `interrupted`, harvest path) → Tasks 3–5. ✅
- D2 recurrence gate N=2 → Task 6 (`threshold`), Tasks 6/8 assert candidate→active. ✅
- D3 signature = normalized root cause, submodule collapse → Tasks 1–2, asserted in Task 8. ✅
- Incident record with the four required sections → Task 6. ✅ (Auto/Human are intentional stubs — Plan 2.)
- Corpus replay (TESTING.md Test 16) → Task 9. ✅
- **Deferred (correctly out of scope):** reflection/Auto Evaluation, Human round-trip/reconcile, injection, `hive rules` CRUD, promotion, demotion/efficacy — Plans 2 & 3.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases" placeholders. Every code step shows complete code. Task 4's file is intentionally partial (only `extract_failures`); `capture_dir()` is added in Task 5 — stated explicitly at the end of Task 4 Step 3 so the implementer doesn't stub it early.

**3. Type/name consistency:** `signature()` (Task 1) used everywhere; `extract_failures()` (Task 4) consumed by `capture_dir()` (Task 5); `capture_dir(transcript_dir, project, now)` signature identical across Tasks 5/7/8/9; `incidents.project(project, threshold)`, `incidents._collect(project)`, `incidents.THRESHOLD`, `incidents.load(signature, project)` consistent across Tasks 6/7/8; event type string `tool_failed` and payload keys `text`/`signature`/`action` identical in Tasks 3/5/6. Verified.

**Known follow-ups for Plan 2:** the incident file is currently overwritten wholesale on each `project()` — Plan 2 must preserve the human-edited `## Human Evaluation` section across re-projection (read-merge, not clobber). Flagged so it is not lost.
