# Learning Loop — Reflection Stage Implementation Plan (Plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn captured incidents into human-overridable prevention rules: filter capture noise, key signatures to the *real* recurring failures, run a Sonnet reflection pass that writes an Auto Evaluation, and preserve a human's authoritative Human Evaluation across re-projection.

**Architecture:** Extends Plan 1. `failures.py` gains a noise filter; `signature.py` gains the real behavioral families found in the corpus replay. `incidents.py` becomes section-aware so re-projection preserves Auto/Human sections and derives the Active rule (Human overrides Auto). A new `reflect.py` builds a strict prompt, parses a JSON verdict, and (via an injectable runner defaulting to a one-shot `claude` CLI call on Sonnet 4.6) writes the Auto Evaluation. `reconcile` records human edits as append-only `rule_revised` events.

**Tech Stack:** Python 3.11+, stdlib only. Reflection reuses the existing `claude` CLI subprocess pattern (`src/mindhive/loop/driver.py`). pytest; the LLM call is injected as a fake in tests (the real runner is not unit-tested, matching `CLIDriver`).

**Depends on:** Plan 1 (`signature.py`, `failures.py`, `incidents.py`, `tool_failed` event). **Spec:** `docs/superpowers/specs/2026-07-01-learning-loop-architecture.md` (D9/D10), ADR 0007, and Plan 1's "Task 9 Findings".

**Out of scope (Plan 3):** injection into sessions, `hive rules promote` to canonical SOUL.md, demotion/efficacy (`prevented`/`leaked`) counters.

---

## File Structure

- Modify: `src/mindhive/failures.py` — add noise exclusion in `extract_failures`.
- Modify: `src/mindhive/signature.py` — add real behavioral families (before the fallback).
- Modify: `src/mindhive/incidents.py` — section-aware read/preserve; `derive_active`; `apply_auto`.
- Modify: `src/mindhive/events.py` — register `rule_revised` event type.
- Create: `src/mindhive/reflect.py` — `build_prompt`, `parse_reflection`, `reflect_incident`, default `claude` runner.
- Modify: `src/mindhive/__main__.py` — `hive rules reflect|reconcile|list|show`.
- Create/modify tests: `tests/test_failures.py`, `tests/test_signature.py`, `tests/test_incidents.py`, `tests/test_reflect.py`, `tests/test_events.py`, `tests/test_rules_cli.py`.

---

## Task 1: Capture noise filter (drop non-preventable failures)

The corpus replay showed ~48/224 captured "failures" were not preventable coding mistakes: model-temporarily-unavailable and user-rejected tool uses. Exclude them like `interrupted`.

**Files:**
- Modify: `src/mindhive/failures.py`
- Test: `tests/test_failures.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_failures.py
def test_model_unavailable_is_not_a_failure():
    line = _tool_result_line(
        True, stderr="claude-opus-4-8 is temporarily unavailable, so auto mode "
                     "cannot determine the safety of Bash right now. Wait briefly")
    assert failures.extract_failures(line) == []


def test_user_rejection_is_not_a_failure():
    line = _tool_result_line(
        True, stderr="The user doesn't want to proceed with this tool use. "
                     "The tool use was rejected")
    assert failures.extract_failures(line) == []


def test_real_failure_still_captured_after_noise_filter():
    line = _tool_result_line(True, stderr="No module named 'mindhive'")
    assert len(failures.extract_failures(line)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures.py -k "noise or unavailable or rejection" -v`
Expected: FAIL — the two noise tests return a 1-element list instead of `[]`.

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/failures.py`, add the noise table near the top constants:

```python
# is_error results that are NOT preventable coding mistakes — transient infra
# or user choices. Matched case-insensitively as substrings. Excluded like
# `interrupted` (see Plan 1 Task 9 findings).
_NOISE = (
    "auto mode cannot determine the safety",
    "is temporarily unavailable",
    "the user doesn't want to proceed",
    "tool use was rejected",
    "the user chose not to",
)


def _is_noise(text: str) -> bool:
    low = (text or "").lower()
    return any(n in low for n in _NOISE)
```

Then in `extract_failures`, skip noise after computing `text`:

```python
        text = (stderr or "").strip() or _block_text(b.get("content")).strip()
        if _is_noise(text):
            continue
        out.append({
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_failures.py -v`
Expected: PASS (all failures tests)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/failures.py tests/test_failures.py
git commit -m "feat(learning-loop): filter non-preventable noise from capture"
```

---

## Task 2: Signature families for the real recurring failures

The dominant real failures are Claude tool-behavior errors, not the hypothesized Python/PowerShell ones. Add families for them, keeping the Plan 1 families.

**Files:**
- Modify: `src/mindhive/signature.py`
- Test: `tests/test_signature.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_signature.py
def test_real_behavioral_families():
    assert signature("<tool_use_error>File has not been read yet. Read it first "
                     "before writing to it.</tool_use_error>") == "edit:not-read"
    assert signature("<tool_use_error>String to replace not found in file. "
                     "String: empty = L1OBI(...)</tool_use_error>") == "edit:string-not-found"
    assert signature("Ripgrep search timed out after 20 seconds. The search may "
                     "have matched") == "tool:ripgrep-timeout"
    assert signature("File content (1.5MB) exceeds maximum allowed size (256KB). "
                     "Use offset and limit") == "read:file-too-large"


def test_behavioral_families_take_priority_over_fallback():
    # These must NOT collapse into raw: hashes.
    for err in ("File has not been read yet", "String to replace not found",
                "Ripgrep search timed out", "exceeds maximum allowed size"):
        assert not signature(err).startswith("raw:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_signature.py -k "behavioral" -v`
Expected: FAIL — these currently return `raw:*` hashes.

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/signature.py`, insert a behavioral-family block **before** the fallback (after the `pysyntax` checks, before the `# Fallback` comment):

```python
    low = t.lower()
    if "file has not been read yet" in low:
        return "edit:not-read"
    if "string to replace not found" in low:
        return "edit:string-not-found"
    if "ripgrep search timed out" in low:
        return "tool:ripgrep-timeout"
    if "exceeds maximum allowed size" in low:
        return "read:file-too-large"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_signature.py -v`
Expected: PASS (all signature tests, old + new)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/signature.py tests/test_signature.py
git commit -m "feat(learning-loop): signature families for real behavioral failures"
```

---

## Task 3: Section-aware incident records (preserve Auto/Human; derive Active rule)

The Plan 1 record is overwritten wholesale on every `project()`. Reflection (Auto) and human edits (Human) live only in the file, so re-projection must PRESERVE them and derive the Active rule (Human overrides Auto).

**Files:**
- Modify: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
def test_derive_active_prefers_human(tmp_path):
    from mindhive import incidents
    # human non-empty overrides auto
    active, src = incidents.derive_active(
        "- Prevention rule: use -m from repo root",
        "always cd to repo root first")
    assert active == "always cd to repo root first"
    assert src == "human"


def test_derive_active_falls_back_to_auto(tmp_path):
    from mindhive import incidents
    active, src = incidents.derive_active(
        "- Prevention rule: use -m from repo root",
        "<!-- leave blank to accept Auto -->")
    assert active == "use -m from repo root"
    assert src == "auto"


def test_derive_active_none_when_empty(tmp_path):
    from mindhive import incidents
    active, src = incidents.derive_active("", "<!-- blank -->")
    assert active == ""
    assert src == "none"


def test_reprojection_preserves_human_and_auto(tmp_path):
    _add_failure("pymodule:mindhive", "python mindhive.py", "s1")
    _add_failure("pymodule:mindhive", "python mindhive.py state", "s2")
    incidents.project(project="test", threshold=2)
    # simulate reflection + a human edit by hand-writing sections into the file
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    txt = path.read_text(encoding="utf-8")
    txt = incidents._replace_section(txt, "Auto Evaluation",
                                     "- Prevention rule: run with -m from repo root")
    txt = incidents._replace_section(txt, "Human Evaluation",
                                     "ALWAYS activate the venv first")
    path.write_text(txt, encoding="utf-8")
    # a THIRD failure arrives; re-project must not wipe Auto/Human
    _add_failure("pymodule:mindhive", "python x", "s3")
    incidents.project(project="test", threshold=2)
    body = path.read_text(encoding="utf-8")
    assert "run with -m from repo root" in body           # Auto preserved
    assert "ALWAYS activate the venv first" in body        # Human preserved
    assert "> ALWAYS activate the venv first" in body      # Active = Human
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["rule_source"] == "human"
    assert rec["occurrences"] == 3                          # frontmatter refreshed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k "derive or preserve" -v`
Expected: FAIL — `AttributeError: module 'mindhive.incidents' has no attribute 'derive_active'` / `_replace_section`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/mindhive/incidents.py` (and update `_render` + `project`):

```python
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
```

Replace the old `_render` and `project` with section-preserving versions:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -v`
Expected: PASS (Plan 1 incident tests + the new preservation/derive tests). Note: the Plan 1 test `test_record_file_has_required_sections` still passes because all four `## ` headers remain.

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): section-aware records preserve Auto/Human, derive Active rule"
```

---

## Task 4: Register the `rule_revised` event type (audit trail for human edits)

**Files:**
- Modify: `src/mindhive/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_events.py
def test_rule_revised_is_a_valid_deterministic_type():
    assert "rule_revised" in events.DETERMINISTIC_TYPES
    assert "rule_revised" not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_revised",
                             {"signature": "pymodule:x", "text": "do Y", "source": "human"},
                             session_id="s1", tags=["reconcile"], project="test")
    assert ev.type == "rule_revised"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_events.py::test_rule_revised_is_a_valid_deterministic_type -v`
Expected: FAIL — `rule_revised` not in the set (or `append_event` raises `ValueError`).

- [ ] **Step 3: Write minimal implementation**

Add `"rule_revised"` to `DETERMINISTIC_TYPES` in `src/mindhive/events.py`:

```python
    "loop_run_start", "loop_iteration", "loop_run_end",
    "tool_failed", "rule_revised",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_events.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/events.py tests/test_events.py
git commit -m "feat(learning-loop): register rule_revised deterministic event type"
```

---

## Task 5: Reflection prompt + parser (pure)

**Files:**
- Create: `src/mindhive/reflect.py`
- Test: `tests/test_reflect.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reflect.py
from mindhive import reflect


def test_build_prompt_contains_error_and_demands_json():
    p = reflect.build_prompt(signature="pymodule:mindhive",
                             error_excerpt="No module named 'mindhive'",
                             actions=["python mindhive.py state"])
    assert "No module named 'mindhive'" in p
    assert "python mindhive.py state" in p
    assert "JSON" in p
    assert "prevention_rule" in p


def test_parse_reflection_extracts_json_even_with_fences():
    raw = ('Sure!\n```json\n{"root_cause": "wrong cwd", "lesson": "run as module", '
           '"prevention_rule": "use -m from repo root", "task_category": "dependency"}\n```')
    out = reflect.parse_reflection(raw)
    assert out["prevention_rule"] == "use -m from repo root"
    assert out["task_category"] == "dependency"


def test_parse_reflection_returns_empty_on_garbage():
    assert reflect.parse_reflection("no json here") == {}
    assert reflect.parse_reflection("") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_reflect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mindhive.reflect'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/mindhive/reflect.py
"""Reflection pass: turn one incident into a structured prevention rule.

Pure helpers (build_prompt/parse_reflection) are unit-tested. reflect_incident
calls an injectable runner; the default shells out to the `claude` CLI on Sonnet
(mirroring loop/driver.py) and is NOT unit-tested.
"""
import json
import subprocess

MODEL = "claude-sonnet-4-6"

_SYSTEM = (
    "You are the Reflection Engine. Analyze ONE execution failure and output ONLY "
    "a JSON object — no prose, no code fences — with keys: root_cause, lesson, "
    "prevention_rule, task_category. task_category is one of: architecture, syntax, "
    "git_merge, engine_logic, dependency, tool_use. prevention_rule is a single "
    "imperative sentence a future agent can follow to avoid this failure."
)


def build_prompt(signature, error_excerpt, actions) -> str:
    acts = "\n".join(f"- {a}" for a in (actions or [])) or "- (unknown)"
    return (f"{_SYSTEM}\n\nSIGNATURE: {signature}\nFAILING ACTIONS:\n{acts}\n"
            f"ERROR:\n{error_excerpt}\n\nReturn the JSON now.")


def parse_reflection(text: str) -> dict:
    """Extract the JSON verdict from a model response, tolerating fences/prose."""
    if not text:
        return {}
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return {}
    try:
        obj = json.loads(text[s:e + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _claude_runner(prompt: str, timeout_s: int = 120) -> str:
    """Default runner: one-shot headless `claude` call, returns the result text."""
    argv = ["claude", "--output-format", "json", "--model", MODEL, "-p", prompt]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    try:
        return json.loads((proc.stdout or "").strip()).get("result", "") or ""
    except (json.JSONDecodeError, ValueError):
        return (proc.stdout or "").strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_reflect.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/reflect.py tests/test_reflect.py
git commit -m "feat(learning-loop): reflection prompt builder + JSON verdict parser"
```

---

## Task 6: `reflect_incident` writes the Auto Evaluation into the record

**Files:**
- Modify: `src/mindhive/reflect.py`
- Test: `tests/test_reflect.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_reflect.py
from mindhive import events, incidents, paths


def _seed_active_incident():
    for s in ("s1", "s2"):
        events.append_event("tool_failed",
                            {"text": "No module named 'mindhive'",
                             "signature": "pymodule:mindhive", "action": "python x"},
                            session_id=s, tags=["auto", "failure"], project="test")
    incidents.project(project="test", threshold=2)


def test_reflect_incident_writes_auto_and_sets_active(tmp_path):
    _seed_active_incident()

    def fake_runner(prompt, timeout_s=120):
        return ('{"root_cause": "wrong cwd", "lesson": "run as module", '
                '"prevention_rule": "use -m from repo root", "task_category": "dependency"}')

    res = reflect.reflect_incident("pymodule:mindhive", project="test", runner=fake_runner)
    assert res["reflected"] is True
    body = (paths.project_dir("test") / "rules" / "incidents"
            / "pymodule-mindhive.md").read_text(encoding="utf-8")
    assert "use -m from repo root" in body
    # with no human override, Active == Auto rule
    assert "> use -m from repo root" in body
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["rule_source"] == "auto"


def test_reflect_incident_noop_on_empty_verdict(tmp_path):
    _seed_active_incident()
    res = reflect.reflect_incident("pymodule:mindhive", project="test",
                                   runner=lambda p, timeout_s=120: "garbage")
    assert res["reflected"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_reflect.py -k reflect_incident -v`
Expected: FAIL — `AttributeError: module 'mindhive.reflect' has no attribute 'reflect_incident'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/mindhive/reflect.py`:

```python
from . import incidents


def _auto_section(verdict: dict) -> str:
    return (f"- Root cause: {verdict.get('root_cause', '').strip()}\n"
            f"- Lesson: {verdict.get('lesson', '').strip()}\n"
            f"- Prevention rule: {verdict.get('prevention_rule', '').strip()}\n"
            f"- Category: {verdict.get('task_category', '').strip()}")


def reflect_incident(signature, project=None, runner=_claude_runner) -> dict:
    """Reflect one incident: build prompt from its Raw Signal, run the model,
    write the Auto Evaluation section, and re-derive the Active rule."""
    d = incidents._incident_dir(project)
    path = d / f"{incidents._safe_name(signature)}.md"
    if not path.exists():
        return {"reflected": False, "reason": "no such incident"}
    secs = incidents._split_sections(path.read_text(encoding="utf-8"))
    raw = secs.get("Raw Signal", "")
    verdict = parse_reflection(runner(build_prompt(signature, raw, [])))
    if not verdict.get("prevention_rule"):
        return {"reflected": False, "reason": "empty verdict"}
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Auto Evaluation", _auto_section(verdict))
    path.write_text(txt, encoding="utf-8")
    incidents.project(project)          # re-derive Active + rule_source, preserve sections
    return {"reflected": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_reflect.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/reflect.py tests/test_reflect.py
git commit -m "feat(learning-loop): reflect_incident writes Auto Evaluation + Active rule"
```

---

## Task 7: `reconcile` — record human edits as append-only events

**Files:**
- Modify: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
def test_reconcile_appends_rule_revised_for_human_edits(tmp_path):
    _add_failure("pymodule:mindhive", "python x", "s1")
    _add_failure("pymodule:mindhive", "python y", "s2")
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Human Evaluation", "activate venv first")
    path.write_text(txt, encoding="utf-8")

    res = incidents.reconcile(project="test")
    assert res["revised"] == 1
    revs = [e for e in events.read_all("test") if e.type == "rule_revised"]
    assert revs and revs[0].payload["source"] == "human"
    assert revs[0].payload["text"] == "activate venv first"
    assert revs[0].payload["signature"] == "pymodule:mindhive"

    # idempotent: re-running with no new edit adds nothing
    assert incidents.reconcile(project="test")["revised"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k reconcile -v`
Expected: FAIL — `AttributeError: module 'mindhive.incidents' has no attribute 'reconcile'`

- [ ] **Step 3: Write minimal implementation**

Append to `src/mindhive/incidents.py` (needs `from . import events` — already imported):

```python
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
        secs = _split_sections(path.read_text(encoding="utf-8"))
        signature = None
        for line in path.read_text(encoding="utf-8").splitlines():
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): reconcile records human edits as rule_revised events"
```

---

## Task 8: `hive rules` CLI (reflect / reconcile / list / show)

**Files:**
- Modify: `src/mindhive/__main__.py`
- Test: `tests/test_rules_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rules_cli.py
from mindhive import __main__ as cli, events, incidents


def _seed(project="test"):
    for s in ("s1", "s2"):
        events.append_event("tool_failed",
                            {"text": "No module named 'mindhive'",
                             "signature": "pymodule:mindhive", "action": "python x"},
                            session_id=s, tags=["auto", "failure"], project=project)
    incidents.project(project=project, threshold=2)


def test_rules_list_and_show(tmp_path, capsys):
    _seed()
    assert cli.main(["rules", "list", "--project", "test"]) == 0
    out = capsys.readouterr().out
    assert "pymodule:mindhive" in out and "active" in out

    assert cli.main(["rules", "show", "pymodule:mindhive", "--project", "test"]) == 0
    shown = capsys.readouterr().out
    assert "## Raw Signal" in shown


def test_rules_reconcile_cli(tmp_path, capsys):
    _seed()
    from mindhive import paths
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Human Evaluation", "venv first")
    path.write_text(txt, encoding="utf-8")
    assert cli.main(["rules", "reconcile", "--project", "test"]) == 0
    assert '"revised": 1' in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_rules_cli.py -v`
Expected: FAIL — argparse `invalid choice: 'rules'`

- [ ] **Step 3: Write minimal implementation**

Add handlers next to the other `cmd_*` functions in `src/mindhive/__main__.py`:

```python
def cmd_rules_list(args) -> int | None:
    from . import incidents as inc
    acc = inc._collect(args.project)
    if not acc:
        print("(no incidents yet)")
        return None
    for signature, rec in sorted(acc.items(), key=lambda kv: -kv[1]["occurrences"]):
        status = "active" if rec["occurrences"] >= inc.THRESHOLD else "candidate"
        print(f"{rec['occurrences']:>3}x  [{status:9}] {signature}")
    return None


def cmd_rules_show(args) -> int | None:
    from . import incidents as inc
    path = inc._incident_dir(args.project) / f"{inc._safe_name(args.signature)}.md"
    print(path.read_text(encoding="utf-8") if path.exists() else "(no such incident)")
    return None


def cmd_rules_reflect(args) -> int | None:
    from . import reflect as reflect_mod, incidents as inc
    acc = inc._collect(args.project)
    done = 0
    for signature, rec in acc.items():
        if rec["occurrences"] >= inc.THRESHOLD:      # active only
            if reflect_mod.reflect_incident(signature, project=args.project).get("reflected"):
                done += 1
    print(json.dumps({"reflected": done}))
    return None


def cmd_rules_reconcile(args) -> int | None:
    from . import incidents as inc
    print(json.dumps(inc.reconcile(project=args.project)))
    return None
```

Register the subcommand inside `_build_parser()` before `return p`:

```python
    # --- rules (Learning Loop reflection stage) ---
    rp = sub.add_parser("rules", help="reflect on incidents; manage prevention rules")
    rsub = rp.add_subparsers(dest="rules_cmd", required=True)
    rl = rsub.add_parser("list", help="list incidents by occurrence")
    rl.add_argument("--project", default=None)
    rl.set_defaults(func=cmd_rules_list)
    rsh = rsub.add_parser("show", help="print one incident record")
    rsh.add_argument("signature")
    rsh.add_argument("--project", default=None)
    rsh.set_defaults(func=cmd_rules_show)
    rr = rsub.add_parser("reflect", help="run the reflection pass over active incidents")
    rr.add_argument("--project", default=None)
    rr.set_defaults(func=cmd_rules_reflect)
    rc = rsub.add_parser("reconcile", help="record human edits as rule_revised events")
    rc.add_argument("--project", default=None)
    rc.set_defaults(func=cmd_rules_reconcile)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_rules_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/__main__.py tests/test_rules_cli.py
git commit -m "feat(learning-loop): hive rules list/show/reflect/reconcile CLI"
```

---

## Task 9: Full-suite regression + manual reflection smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the full suite**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest -q`
Expected: all tests pass (Plan 1 + Plan 2), no regressions.

- [ ] **Step 2: Manual reflection smoke test (real `claude` CLI)**

Only if `claude` is on PATH. Seed a throwaway project, reflect for real, inspect the Auto Evaluation:
```bash
PY="C:/Users/hanif/mindhive/.venv/Scripts/python.exe"
MINDHIVE_HOME="$HOME/.mindhive" MINDHIVE_PROJECT=reflect-smoke "$PY" -m mindhive failures capture \
  --transcripts "$HOME/.claude/projects/C--Users-hanif" --project reflect-smoke
MINDHIVE_HOME="$HOME/.mindhive" "$PY" -m mindhive rules reflect --project reflect-smoke
MINDHIVE_HOME="$HOME/.mindhive" "$PY" -m mindhive rules show tool:ripgrep-timeout --project reflect-smoke
rm -rf "$HOME/.mindhive/reflect-smoke"
```
Expected: the shown record has a populated `## Auto Evaluation` (root cause / lesson / prevention rule) and an `## Active Prevention Rule` line derived from it. If `claude` is not on PATH, skip — the unit tests already cover the logic via the fake runner.

- [ ] **Step 3: Commit (if any doc/notes updated)**

```bash
git add -A && git commit -m "test(learning-loop): Plan 2 full-suite regression green" || echo "nothing to commit"
```

---

## Task 9 Smoke Result (2026-07-01)

Full suite: **370 passed**, only the pre-existing flaky `test_dashboard::test_post_requires_token` (unrelated socket flake; learning-loop touched no dashboard files).

Real reflection via the live `claude` CLI returned `{"reflected": 0}`. Root cause: a **headless `claude -p` subprocess launched from inside the agent harness fails auth (HTTP 401)** — the outer session's credentials aren't propagated to the child process. The invocation itself is correct (proper JSON envelope returned); this is an environment limitation of running `claude -p` from within this sandbox, not a code defect. In a normally-authenticated terminal (same context where `hive loop` works) `hive rules reflect` will complete.

The pipeline degraded gracefully — no Auto Evaluation written, no bad rule created. The smoke test also surfaced a hardening gap now fixed + tested: `_result_text` treats any `is_error` envelope (e.g. the 401) as empty, so an API-error string is never fed into reflection.

## Self-Review

**1. Spec coverage (Plan 2 = corrected capture + reflection + dual-eval + reconcile):**
- Task 9 findings: noise filter → Task 1; real signature families → Task 2. ✅
- D9 dual-evaluation record, Auto (Sonnet) + Human, one file per signature → Tasks 3, 5, 6. ✅
- D9 Human overrides Auto; Active rule derivation + `rule_source` → Task 3 (`derive_active`). ✅
- D9 human edits round-trip as append-only events → Task 4 (`rule_revised`) + Task 7 (`reconcile`). ✅
- D7 reflection on Sonnet, deferred/out-of-band, executor≠evaluator → Task 5/6 (`MODEL="claude-sonnet-4-6"`, injectable runner, CLI-triggered not in-hook). ✅
- The flagged Plan 1 trap (re-projection clobbering Human) → fixed in Task 3, asserted by `test_reprojection_preserves_human_and_auto`. ✅
- **Deferred to Plan 3 (correctly):** injection into live sessions, promotion to canonical SOUL.md, demotion/efficacy counters.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases". Every code step is complete. `_claude_runner` is a real implementation (not a stub); it is simply not unit-tested, mirroring `loop/driver.py::CLIDriver`.

**3. Type/name consistency:** `derive_active(auto_section, human_section) -> (str, str)` used in Task 3 and by `project()`; `_replace_section(text, name, body)` / `_split_sections(text)` / `_human_text` / `_auto_rule` consistent across Tasks 3/6/7; `reflect.build_prompt(signature, error_excerpt, actions)`, `reflect.parse_reflection(text)`, `reflect.reflect_incident(signature, project, runner)` consistent across Tasks 5/6/8; `incidents.reconcile(project)` consistent across Tasks 7/8; event types `tool_failed` (Plan 1) and `rule_revised` (Task 4) match their payload keys (`signature`/`text`/`source`) everywhere. Verified.

**Note for Plan 3:** injection reads the Active Prevention Rule of every `active` incident; promotion copies it into canonical directives; both must ignore `rule_source: none` records (no rule yet).
