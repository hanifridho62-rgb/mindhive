# MindHive Assistant Directives Injection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the committed MINDHIVE ASSISTANT operating spec into every new session so the in-session assistant actually reads and follows its safety directives.

**Architecture:** A new `directives` module holds the canonical directive text as the single runtime source of truth. A new `hive directives` CLI subcommand prints it. The existing `session_start.ps1` hook — which already pipes `hive state` into each new session — is extended to emit `hive directives` immediately ahead of `STATE.md`, so the directives lead the injected context.

**Tech Stack:** Python 3.12, argparse CLI (`src/mindhive/__main__.py`), pytest (`tests/`), PowerShell session hook (`hooks/session_start.ps1`).

**Source spec:** `docs/superpowers/specs/2026-06-23-mindhive-assistant-operating-spec.md`

---

## File Structure

- **Create** `src/mindhive/directives.py` — holds `OPERATING_SPEC` (canonical directive text) and `directives() -> str`. Single responsibility: be the one place the runtime directive text lives. The committed spec doc remains the human-facing rationale copy; this module is the injected copy.
- **Create** `tests/test_directives.py` — unit tests for the module and the CLI command.
- **Modify** `src/mindhive/__main__.py` — register the `directives` subparser and dispatch branch.
- **Modify** `hooks/session_start.ps1` — emit `hive directives` before `hive state`.

Why a Python constant rather than reading the docs `.md` at runtime: the package must work when installed/frozen (`MindHive.exe`), where `docs/` is not shipped. A module constant is import-safe in every deployment and is unit-testable.

---

## Task 1: Directives module (single source of truth)

**Files:**
- Create: `src/mindhive/directives.py`
- Test: `tests/test_directives.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_directives.py`:

```python
# tests/test_directives.py
from mindhive import directives


def test_directives_returns_nonempty_string():
    text = directives.directives()
    assert isinstance(text, str)
    assert text.strip()


def test_directives_has_a_recognizable_marker():
    # The assistant must be able to spot these as operating directives.
    assert "MINDHIVE ASSISTANT" in directives.directives()


def test_directives_carry_core_safety_invariants():
    """If a future edit drops a core safety directive, this test fails loud.
    These phrases encode the non-negotiable guardrails from the spec."""
    text = directives.directives().lower()
    for phrase in [
        "confirm with the user first",   # §4 mutate only after confirmation
        "report failures honestly",      # §5 no hidden failures
        "append-only",                   # §6 auditable memory
        "do not fabricate",              # §2 no hallucinated history
    ]:
        assert phrase in text, f"missing core directive: {phrase!r}"


def test_directives_is_stable():
    # Pure constant accessor — repeated calls return identical text.
    assert directives.directives() == directives.directives()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_directives.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mindhive.directives'`

- [ ] **Step 3: Write minimal implementation**

Create `src/mindhive/directives.py`:

```python
# src/mindhive/directives.py
"""Canonical operating directives injected into each session by the
session_start hook (ahead of STATE.md). This module is the single runtime
source of truth; docs/superpowers/specs/2026-06-23-mindhive-assistant-
operating-spec.md is the human-facing rationale copy. Keep the two in sync."""

OPERATING_SPEC = """\
# MINDHIVE ASSISTANT — OPERATING DIRECTIVES

## 1. Role
You assist a user inside mindhive, a deterministic, event-sourced memory tool.
You are stateless between calls; your "memory" is whatever the harness injects
from the event store. You do not claim continuity you don't have. You retrieve,
reason, recommend, and — only with confirmation — mutate.

## 2. Using injected context
- Treat injected recall results as relevant, not authoritative or complete —
  a retrieval projection, not ground truth. Other relevant events may not have
  surfaced.
- Ground claims in the injected events and the current message. If something
  isn't supported there, say so. Do not fabricate entities, values, or history.
- If injected events conflict (different values for the same fact, or a new
  request that contradicts a standing instruction), do not silently pick a
  winner. Surface the conflict with timestamps/provenance and ask — unless the
  current message explicitly resolves it.

## 3. Cold start (new session/device)
With only a compressed summary, state what you know versus what you're
inferring. Offer to confirm key parameters; don't pretend to "know" the user.
Personalization must be disclosed, not concealed.

## 4. Plugins / actions
- mindhive recommends tools; it does not auto-execute them. Keep that boundary.
- Before any read-only tool call, a brief note is enough.
- Before any state-mutating or destructive action (writes, deletes, network/
  firewall/deploy changes, config edits), confirm with the user first.
  "Implied intent" is a reason to confirm, not to skip confirming.
- If a required parameter isn't in context, ask — never guess a value for a
  mutating call.

## 5. Failure handling
- Report failures honestly and promptly, with the actual error. Never hide a
  failure or silently retry a mutating action.
- A bounded retry of a read-only, idempotent action is fine; say you're doing
  it. Anything that changes state stops and surfaces on first failure.

## 6. Writing to memory
- Memory is append-only and auditable. Never silently discard, overwrite, or
  merge events.
- When superseding a fact, append the new event and let projections deprecate
  the old one — preserving the chain.
- Don't auto-merge "similar" concepts; if two labels might be the same thing,
  ask. Merging is irreversible and sometimes wrong.

## 7. Omitted by design
No zero-hallucination guarantee (unachievable), no claim of statefulness
(false), no autonomous mutation (unsafe), no concealed personalization
(untrustworthy). Determinism and auditability are the product.
"""


def directives() -> str:
    """Return the canonical operating directives text."""
    return OPERATING_SPEC
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_directives.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/directives.py tests/test_directives.py
git commit -m "feat(directives): add canonical operating-spec module"
```

---

## Task 2: `hive directives` CLI command

**Files:**
- Modify: `src/mindhive/__main__.py:5` (imports) and the subparser/dispatch sections
- Test: `tests/test_directives.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_directives.py`:

```python
from mindhive import __main__ as cli_mod


def test_cli_directives_prints_spec(capsys):
    ret = cli_mod.main(["directives"])
    assert ret == 0
    out = capsys.readouterr().out
    assert "MINDHIVE ASSISTANT" in out
    assert "confirm with the user first" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_directives.py::test_cli_directives_prints_spec -v`
Expected: FAIL — argparse exits non-zero because `directives` is an unknown subcommand (`SystemExit` / exit code 2).

- [ ] **Step 3: Register the subparser**

In `src/mindhive/__main__.py`, find this line (around line 53):

```python
    sub.add_parser("state", help="print STATE.md")
```

Add immediately after it:

```python
    sub.add_parser("directives", help="print the session operating directives")
```

- [ ] **Step 4: Add the dispatch branch**

In `src/mindhive/__main__.py`, find the `state` dispatch branch (around line 154):

```python
    elif args.cmd == "state":
        s = paths.project_dir() / "STATE.md"
        print(s.read_text(encoding="utf-8") if s.exists()
              else "(no STATE.md — run: hive project)")
```

Add immediately after that branch:

```python
    elif args.cmd == "directives":
        from . import directives as directives_mod
        print(directives_mod.directives())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_directives.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Verify the command end-to-end**

Run: `python -m mindhive directives`
Expected: prints the directives starting with `# MINDHIVE ASSISTANT — OPERATING DIRECTIVES`.

- [ ] **Step 7: Commit**

```bash
git add src/mindhive/__main__.py tests/test_directives.py
git commit -m "feat(cli): add 'hive directives' command"
```

---

## Task 3: Inject directives at session start

**Files:**
- Modify: `hooks/session_start.ps1`

The hook currently appends a `session_start` event and prints `STATE.md`:

```powershell
try {
    & $py -m mindhive append session_start "session opened" | Out-Null
    & $py -m mindhive state
} catch {}
```

- [ ] **Step 1: Emit directives before STATE.md**

Replace that `try { ... } catch {}` block with:

```powershell
try {
    & $py -m mindhive append session_start "session opened" | Out-Null
    & $py -m mindhive directives
    & $py -m mindhive state
} catch {}
```

Rationale: directives lead the injected context so they frame everything the
assistant reads after them. The `append` stays first so the session is recorded
even if a later print fails; both prints are inside the same fail-safe `catch`.

- [ ] **Step 2: Verify the hook composes the intended output**

Run (simulates what the hook pipes into the session):

```bash
python -m mindhive directives && python -m mindhive state
```

Expected: the directives block prints first (starting `# MINDHIVE ASSISTANT —
OPERATING DIRECTIVES`), immediately followed by `STATE.md` (or the
`(no STATE.md — run: hive project)` fallback). No errors.

- [ ] **Step 3: Confirm the hook file contains the new line**

Run: `grep -n "mindhive directives" hooks/session_start.ps1`
Expected: one match showing the inserted line between the `append` and `state`
calls.

- [ ] **Step 4: Commit**

```bash
git add hooks/session_start.ps1
git commit -m "feat(hook): inject operating directives at session start"
```

---

## Task 4: Full regression check

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest -q`
Expected: all tests pass (existing suite + the new `tests/test_directives.py`).

- [ ] **Step 2: Run the built-in selftest**

Run: `python -m mindhive selftest`
Expected: exit 0.

- [ ] **Step 3: Final commit (only if anything was left uncommitted)**

```bash
git add -A
git commit -m "test: verify directives injection passes full suite" || echo "nothing to commit"
```

---

## Self-Review notes

- **Spec coverage:** All 7 sections of the operating spec are carried verbatim-in-substance by `OPERATING_SPEC` (Task 1, Step 3). The four core invariants (§2 no fabrication, §4 confirm-before-mutate, §5 honest failures, §6 append-only) are pinned by `test_directives_carry_core_safety_invariants` so future edits can't silently drop them.
- **Delivery:** §"make it real" goal is met by Task 2 (CLI) + Task 3 (hook) — the directives reach a live session.
- **Drift control:** the docs spec and the module constant are two copies by design (docs ships rationale + header; module ships runtime text). The invariant test guards the safety-critical phrases; full byte-for-byte sync is intentionally not enforced because the docs copy carries an extra header.
- **Type/name consistency:** `directives.directives()` and `directives.OPERATING_SPEC` are referenced identically across Tasks 1–2; CLI imports the module as `directives_mod` locally (matches the file's existing `*_mod` local-import pattern, e.g. `recall_mod`, `export_mod`).
- **No placeholders:** every code/command step shows complete content.
