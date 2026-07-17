# Learning Loop — Injection, Promotion & Demotion Plan (Plan 3 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop — inject active prevention rules into new sessions, let the human promote a rule into a canonical `SOUL.md` with one command, and auto-demote auto-rules that keep leaking (while never silently demoting human-authored ones).

**Architecture:** Adds lifecycle events (`rule_activated`, `rule_promoted`, `rule_demoted`) to the existing event log. `incidents.audit()` stamps activation at the threshold-crossing failure's timestamp, making "did the same error recur after the rule went active?" (a *leak*) fully deterministic. `project()` derives status (candidate/active/demoted/promoted) and a `needs_review` flag from those events. A per-project `SOUL.md` is the human-owned canonical file; `hive rules promote` appends to it (the one human gate). `hive rules inject` emits the block a session-start hook reads.

**Tech Stack:** Python 3.11+, stdlib only. pytest. No LLM (injection/promotion/demotion are all deterministic).

**Depends on:** Plans 1 & 2. **Spec:** architecture D4/D5/D6/D10, ADR 0007, glossary (`status`, `Quarantine`, `Canonical`, `Promotion`, `Prevented/Leaked`).

**Out of scope:** the "prevented" counter (absence-of-error is not per-rule observable deterministically — deferred; leak-based demotion is the actionable half). Live session-start hook wiring is Task 8 and is optional (changes live behavior).

---

## File Structure

- Modify: `src/mindhive/events.py` — register `rule_activated`, `rule_promoted`, `rule_demoted`.
- Modify: `src/mindhive/incidents.py` — `audit()`, `leaks()`, status derivation w/ demotion + `needs_review` flag, `inject()`, `promote()`, `demote()`, `soul_path()`.
- Modify: `src/mindhive/__main__.py` — `hive rules inject|promote|demote` (+ `audit`).
- Modify (optional, Task 8): `hooks/session_start.ps1` — call `hive rules inject`.
- Tests: `tests/test_events.py`, `tests/test_incidents.py`, `tests/test_rules_cli.py`.

---

## Task 1: Register lifecycle event types

**Files:**
- Modify: `src/mindhive/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_events.py
def test_lifecycle_event_types_are_valid():
    for t in ("rule_activated", "rule_promoted", "rule_demoted"):
        assert t in events.DETERMINISTIC_TYPES
        assert t not in events.SEMANTIC_TYPES
    ev = events.append_event("rule_promoted", {"signature": "pymodule:x"},
                             session_id="s", tags=["promote"], project="test")
    assert ev.type == "rule_promoted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_events.py::test_lifecycle_event_types_are_valid -v`
Expected: FAIL — types not in the set.

- [ ] **Step 3: Write minimal implementation**

In `src/mindhive/events.py`, extend `DETERMINISTIC_TYPES`:

```python
    "tool_failed", "rule_revised",
    "rule_activated", "rule_promoted", "rule_demoted",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_events.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/events.py tests/test_events.py
git commit -m "feat(learning-loop): register rule lifecycle event types"
```

---

## Task 2: `audit()` stamps activation at the threshold-crossing failure

**Files:**
- Modify: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
def _add_failure_ts(sig, action, session, ts):
    events.append_event("tool_failed",
                        {"text": f"err {sig}", "signature": sig, "action": action},
                        session_id=session, tags=["auto", "failure"], ts=ts, project="test")


def test_audit_stamps_activation_at_threshold_crossing():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    res = incidents.audit(project="test", threshold=2)
    assert res["activated"] == 1
    acts = [e for e in events.read_all("test") if e.type == "rule_activated"]
    assert len(acts) == 1
    assert acts[0].payload["signature"] == "pymodule:mindhive"
    assert acts[0].ts == "2026-07-01T11:00:00Z"          # the 2nd (crossing) failure
    # idempotent — no second activation
    assert incidents.audit(project="test", threshold=2)["activated"] == 0


def test_audit_ignores_below_threshold():
    _add_failure_ts("pymodule:tools", "x", "s1", "2026-07-01T10:00:00Z")
    assert incidents.audit(project="test", threshold=2)["activated"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k audit -v`
Expected: FAIL — `AttributeError: ... has no attribute 'audit'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/mindhive/incidents.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k audit -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): audit stamps rule activation at threshold crossing"
```

---

## Task 3: Leak counting + demotion status

**Files:**
- Modify: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
def test_leaks_counts_failures_after_activation():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")   # activation
    incidents.audit(project="test", threshold=2)
    assert incidents.leaks(project="test", signature="pymodule:mindhive") == 0
    _add_failure_ts("pymodule:mindhive", "z", "s3", "2026-07-01T12:00:00Z")   # leak 1
    _add_failure_ts("pymodule:mindhive", "w", "s4", "2026-07-01T13:00:00Z")   # leak 2
    assert incidents.leaks(project="test", signature="pymodule:mindhive") == 2


def test_auto_rule_demoted_after_two_leaks():
    for i, ts in enumerate(["10", "11", "12", "13"]):
        _add_failure_ts("pymodule:mindhive", "x", f"s{i}", f"2026-07-01T{ts}:00:00Z")
    incidents.audit(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    incidents.project(project="test", threshold=2)
    # give it an AUTO rule
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Auto Evaluation", "- Prevention rule: do X")
    path.write_text(txt, encoding="utf-8")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "demoted"           # auto + 2 leaks


def test_human_rule_flagged_not_demoted_on_leak():
    for i, ts in enumerate(["10", "11", "12", "13"]):
        _add_failure_ts("pymodule:mindhive", "x", f"s{i}", f"2026-07-01T{ts}:00:00Z")
    incidents.audit(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    incidents.project(project="test", threshold=2)
    txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                     "Human Evaluation", "always cd first")
    path.write_text(txt, encoding="utf-8")
    incidents.project(project="test", threshold=2)
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "active"            # human never auto-demoted
    assert rec["flag"] == "needs_review"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k "leaks or demoted or flagged" -v`
Expected: FAIL — `leaks` missing / status still "active" / no `flag`.

- [ ] **Step 3: Write minimal implementation**

Add `leaks()` to `src/mindhive/incidents.py`:

```python
def leaks(project=None, signature=None) -> int:
    """Count tool_failed events for a signature that occurred AFTER its rule
    activated — i.e. the same error recurred despite an active rule."""
    act = _activation_ts(project, signature)
    if not act:
        return 0
    evs = _failures_by_sig(project).get(signature, [])
    return sum(1 for e in evs if e.ts > act)
```

Replace `project()`'s status computation and `_render` signature to add demotion + flag. Update `_render` to accept and emit `flag`, and update `project()`:

```python
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
```

In `_render`, add a `flag` param and include it in frontmatter:

```python
def _render(signature, rec, status, auto_body, human_body, active, source, flag="") -> str:
    ...
    return f"""---
signature: {signature}
status: {status}
occurrences: {rec['occurrences']}
rule_source: {source}
flag: {flag}
sessions: {len(rec['sessions'])}
---
... (unchanged body) ...
"""
```

In `project()`, compute status/flag via the new helper and pass through:

```python
        active, source = derive_active(auto_body, human_body)
        status, flag = _status_and_flag(project, signature, rec, threshold, source)
        path.write_text(_render(signature, rec, status, auto_body, human_body,
                                active, source, flag), encoding="utf-8")
```

Add `flag` to the fields `load()` parses:

```python
        if m and m.group(1) in ("signature", "status", "occurrences",
                                "rule_source", "flag", "sessions"):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -v`
Expected: PASS (existing incident tests still green — `flag` defaults to `""`, prior assertions unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): leak-based demotion; human rules flagged not demoted"
```

---

## Task 4: `inject()` — the active-rules block

**Files:**
- Modify: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
def test_inject_lists_only_active_rules_with_text():
    # active with an auto rule -> included
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    # candidate (1 occurrence) -> excluded
    _add_failure_ts("pymodule:tools", "z", "s3", "2026-07-01T10:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    block = incidents.inject(project="test")
    assert "PREVENTION RULES" in block
    assert "run with -m" in block                 # active + has rule
    assert "pymodule:tools" not in block          # candidate excluded


def test_inject_empty_when_no_active_rules():
    assert incidents.inject(project="test") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k inject -v`
Expected: FAIL — `AttributeError: ... 'inject'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/mindhive/incidents.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k inject -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): inject active prevention-rules block"
```

---

## Task 5: `promote()` to canonical `SOUL.md` (the human gate)

**Files:**
- Modify: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
def test_promote_appends_to_soul_and_marks_promoted():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    res = incidents.promote("pymodule:mindhive", project="test")
    assert res["promoted"] is True
    soul = incidents.soul_path("test").read_text(encoding="utf-8")
    assert "run with -m" in soul
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "promoted"
    # excluded from Quarantine injection once promoted
    assert "pymodule:mindhive" not in incidents.inject(project="test")


def test_promote_noop_without_active_rule():
    _add_failure_ts("pymodule:tools", "x", "s1", "2026-07-01T10:00:00Z")
    incidents.project(project="test", threshold=2)
    assert incidents.promote("pymodule:tools", project="test")["promoted"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k promote -v`
Expected: FAIL — `AttributeError: ... 'promote'` / `'soul_path'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/mindhive/incidents.py`:

```python
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
    sp = soul_path(project)
    sp.parent.mkdir(parents=True, exist_ok=True)
    header = "" if sp.exists() else "# SOUL — canonical operating rules\n\n"
    with open(sp, "a", encoding="utf-8") as f:
        f.write(f"{header}- [{signature}] {rule}\n")
    events.append_event("rule_promoted", {"signature": signature, "text": rule},
                        tags=["promote"], project=project)
    project_fn = project
    globals()["project"](project_fn)          # re-project -> status promoted
    return {"promoted": True}
```

> Implementation note: `promote` calls the module-level `project()` function. Because the parameter is also named `project`, call it via `globals()["project"](project)` as shown (or rename the param). Keep the name to match the rest of the module's `project=` convention.

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k promote -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): promote active rule into canonical SOUL.md"
```

---

## Task 6: `demote()` (manual)

**Files:**
- Modify: `src/mindhive/incidents.py`
- Test: `tests/test_incidents.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_incidents.py
def test_manual_demote_sets_status_and_excludes_from_inject():
    _add_failure_ts("pymodule:mindhive", "x", "s1", "2026-07-01T10:00:00Z")
    _add_failure_ts("pymodule:mindhive", "y", "s2", "2026-07-01T11:00:00Z")
    incidents.audit(project="test", threshold=2)
    incidents.project(project="test", threshold=2)
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    assert incidents.demote("pymodule:mindhive", project="test")["demoted"] is True
    rec = incidents.load("pymodule:mindhive", project="test")
    assert rec["status"] == "demoted"
    assert "pymodule:mindhive" not in incidents.inject(project="test")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -k manual_demote -v`
Expected: FAIL — `AttributeError: ... 'demote'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/mindhive/incidents.py`:

```python
def demote(signature, project=None) -> dict:
    """Manually demote a rule (leaves injection). Records a rule_demoted event."""
    events.append_event("rule_demoted", {"signature": signature},
                        tags=["demote"], project=project)
    globals()["project"](project)
    return {"demoted": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_incidents.py -v`
Expected: PASS (full incidents suite)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/incidents.py tests/test_incidents.py
git commit -m "feat(learning-loop): manual rule demotion"
```

---

## Task 7: CLI — `hive rules audit|inject|promote|demote`

**Files:**
- Modify: `src/mindhive/__main__.py`
- Test: `tests/test_rules_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_rules_cli.py
def test_rules_inject_and_promote_cli(tmp_path, capsys):
    from mindhive import paths
    _seed()                                     # 2 failures -> active pymodule:mindhive
    assert cli.main(["rules", "audit", "--project", "test"]) == 0
    capsys.readouterr()
    path = (paths.project_dir("test") / "rules" / "incidents" / "pymodule-mindhive.md")
    path.write_text(incidents._replace_section(path.read_text(encoding="utf-8"),
                    "Auto Evaluation", "- Prevention rule: run with -m"), encoding="utf-8")
    incidents.project(project="test", threshold=2)

    assert cli.main(["rules", "inject", "--project", "test"]) == 0
    assert "run with -m" in capsys.readouterr().out

    assert cli.main(["rules", "promote", "pymodule:mindhive", "--project", "test"]) == 0
    assert '"promoted": true' in capsys.readouterr().out.lower()
    assert incidents.soul_path("test").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_rules_cli.py -k inject_and_promote -v`
Expected: FAIL — argparse `invalid choice: 'audit'` (etc.)

- [ ] **Step 3: Write minimal implementation**

Add handlers in `src/mindhive/__main__.py`:

```python
def cmd_rules_audit(args) -> int | None:
    from . import incidents as inc
    res = inc.audit(project=args.project)
    inc.project(project=args.project)
    print(json.dumps(res))
    return None


def cmd_rules_inject(args) -> int | None:
    from . import incidents as inc
    block = inc.inject(project=args.project)
    if block:
        print(block)
    return None


def cmd_rules_promote(args) -> int | None:
    from . import incidents as inc
    print(json.dumps(inc.promote(args.signature, project=args.project)))
    return None


def cmd_rules_demote(args) -> int | None:
    from . import incidents as inc
    print(json.dumps(inc.demote(args.signature, project=args.project)))
    return None
```

Register them under the existing `rules` subparser (next to list/show/reflect/reconcile):

```python
    ra = rsub.add_parser("audit", help="stamp activations for newly-recurring signatures")
    ra.add_argument("--project", default=None)
    ra.set_defaults(func=cmd_rules_audit)
    ri = rsub.add_parser("inject", help="print the active prevention-rules block")
    ri.add_argument("--project", default=None)
    ri.set_defaults(func=cmd_rules_inject)
    rpr = rsub.add_parser("promote", help="promote a rule into canonical SOUL.md")
    rpr.add_argument("signature")
    rpr.add_argument("--project", default=None)
    rpr.set_defaults(func=cmd_rules_promote)
    rdm = rsub.add_parser("demote", help="manually demote a rule")
    rdm.add_argument("signature")
    rdm.add_argument("--project", default=None)
    rdm.set_defaults(func=cmd_rules_demote)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest tests/test_rules_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/__main__.py tests/test_rules_cli.py
git commit -m "feat(learning-loop): hive rules audit/inject/promote/demote CLI"
```

---

## Task 8: Full-suite regression + optional session-start wiring

**Files:**
- Optional: `hooks/session_start.ps1`
- Tests: none (verification)

- [ ] **Step 1: Full suite**

Run: `C:/Users/hanif/mindhive/.venv/Scripts/python.exe -m pytest -q`
Expected: all pass (Plans 1–3). The pre-existing flaky `test_dashboard::test_post_requires_token` may intermittently fail — re-run it in isolation to confirm it is unrelated.

- [ ] **Step 2: (Optional) wire injection into session start**

This changes live session behavior — apply only when you want rules injected every session. In `hooks/session_start.ps1`, after the `hive directives` / `hive state` calls, add:

```powershell
try { & $py -m mindhive rules inject } catch {}
```

Manual check (throwaway project):
```bash
PY="C:/Users/hanif/mindhive/.venv/Scripts/python.exe"
export MINDHIVE_HOME="$HOME/.mindhive"
"$PY" - <<'PYEOF'
from mindhive import events, incidents
for s in ("s1", "s2"):
    events.append_event("tool_failed", {"text": "No module named 'mindhive'",
        "signature": "pymodule:mindhive", "action": "python x"},
        session_id=s, tags=["auto", "failure"], ts=f"2026-07-01T1{s[-1]}:00:00Z",
        project="inject-smoke")
incidents.audit(project="inject-smoke", threshold=2)
incidents.project(project="inject-smoke", threshold=2)
import mindhive.incidents as I
p = I._incident_dir("inject-smoke") / "pymodule-mindhive.md"
p.write_text(I._replace_section(p.read_text(encoding="utf-8"),
    "Auto Evaluation", "- Prevention rule: run mindhive with -m from repo root"),
    encoding="utf-8")
I.project(project="inject-smoke")
PYEOF
"$PY" -m mindhive rules inject --project inject-smoke
rm -rf "$HOME/.mindhive/inject-smoke"
```
Expected: prints the `# PREVENTION RULES` block containing `run mindhive with -m from repo root`.

- [ ] **Step 3: Commit (if the hook was wired)**

```bash
git add hooks/session_start.ps1 && git commit -m "feat(learning-loop): inject prevention rules at session start" || echo "hook left unwired"
```

---

## Self-Review

**1. Spec coverage (Plan 3 = injection + promotion + demotion):**
- D5 injection of the active rule set → Tasks 4, 7 (`inject`), Task 8 (hook). ✅
- D4 promotion is the only write to canonical, human-invoked → Task 5 (`promote` → `SOUL.md`, `rule_promoted`). ✅
- D6 leak-based auto-demotion (auto only) → Tasks 2/3 (`audit`, `leaks`, `_status_and_flag`). ✅
- D10 human rules never auto-demoted, flagged instead → Task 3 (`needs_review` flag). ✅
- Deterministic efficacy anchor (no wall-clock, no LLM) → Task 2 activation stamped at the crossing failure's ts. ✅
- **Deferred (documented):** the `prevented` counter (not per-rule observable); live hook wiring is opt-in (Task 8).

**2. Placeholder scan:** No TBD/TODO. Every code step complete. The `globals()["project"](project)` calls in Tasks 5/6 are real (they disambiguate the module function from the `project=` parameter) with an explicit note.

**3. Type/name consistency:** event types `rule_activated`/`rule_promoted`/`rule_demoted` (Task 1) used by `audit`/`_has_lifecycle`/`promote`/`demote` (Tasks 2/5/6); `leaks(project, signature)`, `_activation_ts`, `_failures_by_sig`, `LEAK_LIMIT` consistent (Tasks 2/3); `_render(..., flag="")` and `load()` both know `flag` (Task 3); `inject(project)`, `promote(signature, project)`, `demote(signature, project)`, `soul_path(project)` consistent across incidents + CLI (Tasks 4/5/6/7). Status values `candidate|active|demoted|promoted` + `flag: needs_review` match the glossary. Verified.

**Loop closed:** capture (P1) → signature+noise filter (P1/P2) → reflect (P2) → dual-eval records with human override (P2) → activate/inject (P3) → human promote to SOUL.md (P3) → leak-demote auto-rules (P3). The only non-offline step remains the Sonnet reflection call, which needs an authenticated terminal.
