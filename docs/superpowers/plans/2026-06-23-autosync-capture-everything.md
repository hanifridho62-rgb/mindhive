# Autosync Capture-Everything Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `autosync.run_all()` auto-discover and capture every project from Claude transcripts into the MIND HIVE store, rebuild projections/graph for each changed project, and write the native `MEMORY.md` only for registered projects.

**Architecture:** Replace the registry-driven harvest loop with `harvest.catch_up(skip_captured=True, known_only=False)` (discovers all projects), then per changed project rebuild projections + graph; native-sync only registered projects (the registry becomes the native-sync allow-list, no longer required for capture).

**Tech Stack:** Python 3.12+, pytest. Tests run from the worktree; the root `conftest.py` forces the local `src/` onto `sys.path`. Run all commands from the worktree root: `C:\Users\hanif\mindhive\.claude\worktrees\mystifying-bhabha-7c0671`.

---

## Background the engineer needs

- `harvest.catch_up(now=None, skip_captured=True, known_only=True)` scans every Claude transcript dir, maps each to a project via the session `cwd` (`paths.detect_project`), harvests new lines, and returns:
  ```python
  {"projects": {proj: {"new_events": int, "dirs": int}, ...},  # only projects with new events
   "total_new": int, "skipped_unknown": [str, ...]}
  ```
  With `known_only=False` it harvests into ALL discovered namespaces (creating new ones), not just existing ones.
- `skip_captured=True` makes harvest skip any session the live hooks already recorded (no double-count). In the app/SDK harness no hooks fire, so everything is captured.
- `sync.sync(folder, project=None)` already runs `projector.project(project)` + builds the briefing internally, then writes the native `MEMORY.md` to `~/.claude/projects/<enc(folder)>/memory/MEMORY.md`. So for registered projects we do NOT need a separate `projector.project()` call.
- `graph.build(project, semantic=False)` reads events directly and writes `graph.json`; independent of projections.
- `events.read_all(project)` returns the project's events (used by the end-to-end test).

## File Structure

- Modify: `src/mindhive/autosync.py` — rewrite `run_all()`; drop the now-unused `briefing` import (sync builds the briefing internally).
- Modify: `tests/test_autosync.py` — replace the three old `run_all` tests (which asserted the registry-loop behavior) with new orchestration tests + one end-to-end test. Keep the two `register`/`load_registry` round-trip tests unchanged.

No other files change. The scheduled-task retune and the one-time mindhive registration are operational steps (Task 4), applied with user confirmation.

---

### Task 1: Replace the old `run_all` tests with new failing tests

**Files:**
- Test: `tests/test_autosync.py`

- [ ] **Step 1: Delete the three obsolete `run_all` tests and the `_stub_downstream` helper**

In `tests/test_autosync.py`, delete the `_stub_downstream` helper and these three functions in full (they assert the old registry-loop behavior):
- `test_run_all_passes_skip_captured_true`
- `test_run_all_runs_full_pipeline_per_project`
- `test_run_all_isolates_per_entry_errors`

Keep `test_register_and_load_roundtrip` and `test_register_replaces_existing_entry`.

- [ ] **Step 2: Update the imports at the top of the file**

Ensure the top of `tests/test_autosync.py` reads exactly:

```python
# tests/test_autosync.py
import json
from mindhive import autosync, events


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")


def _tooluse(name, inp):
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _write_transcript(dirpath, sid, lines):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
```

(The two surviving `register` tests use only `autosync` + `_isolate`, which remain available.)

- [ ] **Step 3: Add the new orchestration + end-to-end tests**

Append these tests to `tests/test_autosync.py`:

```python
# --- run_all: auto-discover capture (new behavior) ---

def test_run_all_discovers_all_via_catch_up(tmp_path, monkeypatch):
    """run_all must harvest via catch_up with skip_captured=True (no double-count)
    and known_only=False (discover brand-new projects, not just registered ones)."""
    _isolate(tmp_path, monkeypatch)
    seen = {}

    def spy_catch_up(now=None, skip_captured=True, known_only=True):
        seen["skip_captured"] = skip_captured
        seen["known_only"] = known_only
        return {"projects": {}, "total_new": 0, "skipped_unknown": []}

    monkeypatch.setattr(autosync.harvest, "catch_up", spy_catch_up)
    monkeypatch.setattr(autosync, "load_registry", lambda: [])
    autosync.run_all()
    assert seen["skip_captured"] is True
    assert seen["known_only"] is False


def test_run_all_rebuilds_every_touched_project(tmp_path, monkeypatch):
    """Every project that gained events gets projector.project + graph.build.
    With nothing registered, NO native sync happens."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(autosync.harvest, "catch_up",
                        lambda **k: {"projects": {"alpha": {"new_events": 2},
                                                  "beta": {"new_events": 1}},
                                     "total_new": 3, "skipped_unknown": []})
    monkeypatch.setattr(autosync, "load_registry", lambda: [])
    calls = []
    monkeypatch.setattr(autosync.projector, "project", lambda p: calls.append(("project", p)))
    monkeypatch.setattr(autosync.graph, "build", lambda p, semantic=False: calls.append(("graph", p)))
    monkeypatch.setattr(autosync.sync, "sync", lambda folder, project=None: calls.append(("sync", project)))
    autosync.run_all()
    assert ("project", "alpha") in calls and ("graph", "alpha") in calls
    assert ("project", "beta") in calls and ("graph", "beta") in calls
    assert not any(c[0] == "sync" for c in calls)   # nothing registered -> no MEMORY.md


def test_run_all_native_syncs_only_registered(tmp_path, monkeypatch):
    """Registered projects get a native MEMORY.md (via sync.sync); unregistered
    auto-discovered projects do NOT (store capture only)."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(autosync.harvest, "catch_up",
                        lambda **k: {"projects": {"mindhive": {"new_events": 1},
                                                  "randoproj": {"new_events": 1}},
                                     "total_new": 2, "skipped_unknown": []})
    monkeypatch.setattr(autosync, "load_registry",
                        lambda: [{"project": "mindhive", "transcripts": "T",
                                  "folder": "C:/Users/hanif/mindhive"}])
    synced, projected, graphed = [], [], []
    monkeypatch.setattr(autosync.sync, "sync",
                        lambda folder, project=None: synced.append((project, folder)))
    monkeypatch.setattr(autosync.projector, "project", lambda p: projected.append(p))
    monkeypatch.setattr(autosync.graph, "build", lambda p, semantic=False: graphed.append(p))
    autosync.run_all()
    # registered -> sync (not a direct projector call, sync does that internally)
    assert synced == [("mindhive", "C:/Users/hanif/mindhive")]
    # unregistered -> projector.project, never sync
    assert projected == ["randoproj"]
    assert set(graphed) == {"mindhive", "randoproj"}


def test_run_all_isolates_per_project_errors(tmp_path, monkeypatch):
    """One failing project must not abort the others."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(autosync.harvest, "catch_up",
                        lambda **k: {"projects": {"good": {"new_events": 1},
                                                  "bad": {"new_events": 1}},
                                     "total_new": 2, "skipped_unknown": []})
    monkeypatch.setattr(autosync, "load_registry", lambda: [])

    def proj(p):
        if p == "bad":
            raise RuntimeError("boom")

    monkeypatch.setattr(autosync.projector, "project", proj)
    monkeypatch.setattr(autosync.graph, "build", lambda p, semantic=False: None)
    results = autosync.run_all()
    by = {r["project"]: r for r in results}
    assert "error" in by["bad"]
    assert "error" not in by["good"]


def test_run_all_end_to_end_captures_unregistered_without_memory_md(tmp_path, monkeypatch):
    """Real path: a transcript session for an unregistered project is captured in
    the store, but no native MEMORY.md is written for it."""
    _isolate(tmp_path, monkeypatch)
    root = tmp_path / "projects"
    _write_transcript(root / "enc", "s1", [
        json.dumps({"type": "attachment", "cwd": "C:/whatever", "sessionId": "s1"}),
        _tooluse("Edit", {"file_path": "a.py"}),
    ])
    monkeypatch.setattr(autosync.harvest, "claude_projects_root", lambda: root)
    monkeypatch.setattr(autosync, "load_registry", lambda: [])   # nothing registered
    synced = []
    monkeypatch.setattr(autosync.sync, "sync",
                        lambda folder, project=None: synced.append(project))
    autosync.run_all()
    # MINDHIVE_PROJECT=test forces the namespace to "test" regardless of cwd
    evs = events.read_all("test")
    assert any(e.type == "file_touched" for e in evs)   # captured in the store
    assert synced == []                                  # unregistered -> no MEMORY.md
```

- [ ] **Step 4: Run the new tests to verify they fail**

Run: `python -m pytest tests/test_autosync.py -v`
Expected: the two `register` tests PASS; the five new tests FAIL. The mocked ones fail with `TypeError`/`AttributeError` (current `run_all` takes no `now`, calls `harvest.harvest`, not `catch_up`); `test_run_all_discovers_all_via_catch_up` fails because `catch_up` is never called; the end-to-end test fails because current `run_all` iterates an empty registry and captures nothing.

- [ ] **Step 5: Commit the failing tests**

```bash
git add tests/test_autosync.py
git commit -m "test(autosync): specify auto-discover capture + registered-only native sync"
```

---

### Task 2: Rewrite `run_all()` to auto-discover and capture everything

**Files:**
- Modify: `src/mindhive/autosync.py:5` (imports) and `src/mindhive/autosync.py:32-47` (`run_all`)

- [ ] **Step 1: Drop the unused `briefing` import**

Change line 5 of `src/mindhive/autosync.py` from:

```python
from . import paths, harvest, projector, briefing, graph, sync
```

to:

```python
from . import paths, harvest, projector, graph, sync
```

(`sync.sync` builds the briefing internally, so `autosync` no longer needs it.)

- [ ] **Step 2: Replace `run_all()` with the auto-discovery version**

Replace the entire `run_all` function (lines 32-47) with:

```python
def run_all(now=None) -> list:
    """Auto-discover and capture EVERY project from Claude transcripts, then
    rebuild projections + graph for each project that changed this run. Write the
    native MEMORY.md only for REGISTERED projects (the registry is the native-sync
    allow-list; it is no longer required for capture).

    ``skip_captured=True`` keeps capture safe alongside the live hooks (no
    double-count); ``known_only=False`` lets brand-new projects be discovered
    without prior registration.
    """
    cu = harvest.catch_up(now=now, skip_captured=True, known_only=False)
    folders = {e["project"]: e["folder"]
               for e in load_registry() if e.get("project") and e.get("folder")}
    results = []
    for proj in sorted(cu.get("projects", {}).keys()):
        info = cu["projects"][proj]
        try:
            if proj in folders:
                # sync.sync runs projector.project + briefing + writes MEMORY.md
                sync.sync(folders[proj], project=proj)
            else:
                projector.project(proj)   # store capture only, no external file
            graph.build(proj, semantic=False)
            results.append({"project": proj,
                            "new_events": info.get("new_events", 0),
                            "synced": proj in folders})
        except Exception as e:  # one bad project must not abort the rest
            results.append({"project": proj, "error": str(e)})
    return results
```

- [ ] **Step 3: Run the autosync tests to verify they pass**

Run: `python -m pytest tests/test_autosync.py -v`
Expected: all tests PASS (2 register + 5 new).

- [ ] **Step 4: Run the full suite to confirm no regressions**

Run: `python -m pytest -q`
Expected: PASS (the previously-flaky `test_post_requires_token` may intermittently report a Windows `ConnectionAbortedError`; re-run it in isolation with `python -m pytest tests/test_dashboard.py::test_post_requires_token -q` to confirm it passes).

- [ ] **Step 5: Commit the implementation**

```bash
git add src/mindhive/autosync.py
git commit -m "feat(autosync): auto-discover and capture every project; native sync only for registered"
```

---

### Task 3: Update the autosync capture memory note

**Files:**
- Modify: `C:\Users\hanif\.claude\projects\C--Users-hanif-mindhive\memory\mindhive-session-capture.md`

This is a documentation/memory update so future sessions know capture is now automatic for all projects.

- [ ] **Step 1: Append the new behavior to the memory note**

Add a line to `mindhive-session-capture.md` recording: `autosync.run_all()` now auto-discovers and captures every project via `catch_up(known_only=False)`; the registry is only the native-`MEMORY.md` allow-list (fabio + mindhive). No commit needed (memory files live outside the repo).

---

### Task 4: Rollout (operational — apply with user confirmation)

These steps change the live machine/store, not the repo. Confirm with the user before running each.

- [ ] **Step 1: Register mindhive once (joins the native-sync allow-list)**

Run:

```bash
python -m mindhive autosync register \
  --project mindhive \
  --transcripts "C:\\Users\\hanif\\.claude\\projects\\C--Users-hanif-mindhive" \
  --folder "C:\\Users\\hanif\\mindhive"
```

Expected: prints the registry JSON now containing both `fabio-hft-v18` and `mindhive`.

- [ ] **Step 2: Retune the scheduled task — 2 min interval, run on battery**

Run (PowerShell):

```powershell
$t = '"C:\Users\hanif\mindhive\.venv\Scripts\python.exe" -m mindhive autosync'
schtasks /Create /TN "\MindHive-AutoSync" /TR $t /SC MINUTE /MO 2 /F
Set-ScheduledTask -TaskName "MindHive-AutoSync" -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
```

Expected: `SUCCESS: The scheduled task "\MindHive-AutoSync" has ...`. Verify with:
`schtasks /query /tn "\MindHive-AutoSync" /fo LIST /v` — `Repeat: Every:` shows 2 minutes and battery settings are no longer "No Start On Batteries".

- [ ] **Step 3: Verify end-to-end on the real store**

Run: `python -m mindhive autosync`
Then confirm:
- `ls ~/.mindhive/mindhive/events/` includes a segment for this session and new `file_touched`/`command_run` events appear.
- `~/.claude/projects/C--Users-hanif-mindhive/memory/MEMORY.md` was refreshed (mindhive is registered).
- Any unregistered project discovered shows in `~/.mindhive/<proj>/` but has no `MEMORY.md` written for it.

---

## Self-Review notes

- **Spec coverage:** auto-discover all (Task 2 `catch_up known_only=False`); existing event types only (no new append types added); native sync registered-only incl. fabio + mindhive (Task 2 `folders` map, Task 4 registration); 2-min/on-battery task (Task 4 Step 2); tests for discovery, skip_captured, registered-only sync, error isolation, end-to-end (Task 1). All covered.
- **Placeholders:** none — every step has full code/commands.
- **Type consistency:** `run_all(now=None)`; `catch_up(now=, skip_captured=, known_only=)` matches `harvest.catch_up` signature; `cu["projects"][proj]["new_events"]` matches `catch_up`'s return shape; `sync.sync(folder, project=)` matches `sync.sync` signature; `graph.build(p, semantic=False)` matches.
