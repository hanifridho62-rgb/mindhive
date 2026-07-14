# Sessions Notification Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dashboard Sessions page into a permanent auditable notification center: collapsible per-session cards with an activity summary (incl. +N −M line stats), expandable rows (touched files, commands, notes, failures, loop iterations), and lazily-fetched granular detail (diffs, command output) parsed from Claude Code transcripts.

**Architecture:** Enrich both capture paths (live hook + harvest) with line stats and command descriptions while keeping the payload `text` field byte-identical (dedup invariant). A new `taskview.py` owns all aggregation (L1 tallies, L2 rows capped to 100, L3 transcript detail) plus the cards' CSS/JS fragments; `dashboard.py` adds two thin routes and injects the fragments; `dashboard_page.py` stays sibling-free via template placeholders.

**Tech Stack:** Python 3.12+ stdlib only (http.server, difflib), vanilla JS, pytest. Run everything from the worktree root: `C:\Users\hanif\mindhive\.claude\worktrees\pedantic-haibt-82400b`. The root `conftest.py` puts local `src/` on `sys.path`; tests are store-isolated by the autouse `isolate_store` fixture.

---

## Background the engineer needs

- Events are append-only JSONL per session: `events.append_event(type, payload, session_id=..., tags=..., ts=..., project=...)`; `events.read_all(project)` returns them time-ordered. `file_touched`/`command_run` payloads today are `{"text": <path|cmd[:180]>}`.
- Harvest dedups against hook-captured sessions with the key `(session_id, type, payload["text"])` — see `_existing_event_keys` in `src/mindhive/harvest.py`. **Never change what goes in `text`.**
- `loop_iteration` payloads (`src/mindhive/loop/engine.py:88-97`): `iteration, summary, progress, cost_usd, total_tokens, context_fill_ratio, breaker_state, session_id`.
- The dashboard is one hand-rolled HTTP handler (`src/mindhive/dashboard.py`, route tables at the bottom of `make_handler`) plus a pure-string page module (`src/mindhive/dashboard_page.py`, must not import siblings). The page live-refreshes the current view every 2.5 s via `liveFn` — **all interaction state must live in module-level JS**, not the DOM.
- Claude Code transcripts live at `~/.claude/projects/<encoded-dir>/<session_id>.jsonl`. Assistant lines carry `message.content[]` blocks of `{"type":"tool_use","id":...,"name":...,"input":{...}}`; the paired result arrives on a later user line as `{"type":"tool_result","tool_use_id":...,"content":...}`.
- File caps: `dashboard.py` is at 919 lines and `dashboard_page.py` at 905 against a 1000-line project cap — hence the new module.

## File Structure

- Create: `src/mindhive/taskview.py` — status/tally/items/detail + `CARDS_CSS`/`CARDS_JS` fragments
- Create: `tests/test_taskview.py`
- Modify: `src/mindhive/hookcapture.py` — `line_stats()`, enriched `extract_events`
- Modify: `src/mindhive/harvest.py` — enrichment in `_items_from_obj` / `_event_for_item`
- Modify: `src/mindhive/dashboard.py` — `/api/task`, `/api/task_detail`, sessions `activity`/`status`, fragment injection
- Modify: `src/mindhive/dashboard_page.py` — placeholders + `renderSessions` delegation
- Modify: `tests/test_hookcapture.py`, `tests/test_harvest.py`, `tests/test_dashboard.py`
- Modify: `README.md` — Sessions section

---

### Task 1: Capture enrichment (data tracking)

**Files:**
- Modify: `src/mindhive/hookcapture.py`
- Modify: `src/mindhive/harvest.py:110-165` (`_items_from_obj`), `harvest.py:213-226` (`_event_for_item`)
- Test: `tests/test_hookcapture.py`, `tests/test_harvest.py`

- [ ] **Step 1: Update the existing hookcapture tests to the new payload contract and add the enrichment tests**

`extract_events` changes contract: it now returns `(type, payload_dict)` pairs instead of `(type, value_string)`. In `tests/test_hookcapture.py`, replace `test_extract_file_touched_for_edit_write_notebook` and `test_extract_command_run_truncated_to_180` with:

```python
def test_extract_file_touched_for_edit_write_notebook():
    for tool in ("Edit", "Write", "NotebookEdit"):
        payload = {"tool_name": tool, "tool_input": {"file_path": "src/foo.py"}}
        out = hookcapture.extract_events(payload)
        # No old/new strings and no patch -> stats unknown, event still logged.
        assert out == [("file_touched", {"text": "src/foo.py", "tool": tool})]


def test_extract_command_run_truncated_to_180():
    long_cmd = "echo " + "x" * 300
    payload = {"tool_name": "Bash", "tool_input": {"command": long_cmd}}
    out = hookcapture.extract_events(payload)
    assert out[0][0] == "command_run"
    assert len(out[0][1]["text"]) == 180


def test_line_stats_prefers_structured_patch():
    resp = {"structuredPatch": [
        {"lines": ["-old line", "+new line", "+added", " context"]},
        {"lines": ["-gone"]},
    ]}
    assert hookcapture.line_stats("Edit", {}, resp) == (2, 2)


def test_line_stats_falls_back_to_old_new_strings():
    inp = {"old_string": "a\nb", "new_string": "a\nb\nc"}
    assert hookcapture.line_stats("Edit", inp) == (3, 2)


def test_line_stats_write_uses_content_line_count():
    assert hookcapture.line_stats("Write", {"content": "x\ny\nz"}) == (3, 0)


def test_line_stats_unknown_when_underivable():
    assert hookcapture.line_stats("Edit", {}) == (None, None)


def test_zero_change_edit_is_skipped():
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "a.py", "old_string": "", "new_string": ""},
               "tool_response": {"structuredPatch": []}}
    assert hookcapture.extract_events(payload) == []


def test_edit_payload_carries_plus_minus():
    payload = {"tool_name": "Edit",
               "tool_input": {"file_path": "a.py",
                              "old_string": "one\ntwo", "new_string": "one\ntwo\nthree"}}
    out = hookcapture.extract_events(payload)
    assert out == [("file_touched",
                    {"text": "a.py", "tool": "Edit", "plus": 3, "minus": 2})]


def test_command_payload_carries_description():
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "pytest -q",
                              "description": "Run full test suite"}}
    out = hookcapture.extract_events(payload)
    assert out == [("command_run",
                    {"text": "pytest -q", "desc": "Run full test suite"})]
```

Also update the assertion in `test_record_appends_events_with_session_and_auto_tag` — it already checks `payload.get("text") == "a/b.py"`, which still passes unchanged.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_hookcapture.py -q`
Expected: FAIL — `AttributeError: module 'mindhive.hookcapture' has no attribute 'line_stats'` and tuple-vs-dict assertion failures.

- [ ] **Step 3: Implement the enrichment in hookcapture.py**

Replace the `extract_events` function and add `line_stats` + a `_DESC_MAX` constant in `src/mindhive/hookcapture.py` (keep `_FILE_TOOLS`, `_CMD_TOOLS`, `_CMD_MAX` as-is):

```python
_DESC_MAX = 120


def line_stats(tool, inp, resp=None):
    """(plus, minus) line counts for a file-tool call, or (None, None) when
    they can't be derived. Prefers the exact structuredPatch from the tool
    response; falls back to new/old string line counts, then Write content.
    Unknown is NOT zero — callers only skip on a computed (0, 0)."""
    patch = resp.get("structuredPatch") if isinstance(resp, dict) else None
    if isinstance(patch, list) and patch:
        plus = minus = 0
        for hunk in patch:
            for ln in (hunk.get("lines") or []) if isinstance(hunk, dict) else []:
                if isinstance(ln, str) and ln.startswith("+"):
                    plus += 1
                elif isinstance(ln, str) and ln.startswith("-"):
                    minus += 1
        return plus, minus
    old, new = inp.get("old_string"), inp.get("new_string")
    if isinstance(old, str) and isinstance(new, str):
        return len(new.splitlines()), len(old.splitlines())
    if tool == "Write" and isinstance(inp.get("content"), str):
        return len(inp["content"].splitlines()), 0
    return None, None


def extract_events(payload) -> list:
    """Pure extraction: map one PostToolUse payload to ``(type, payload)``
    pairs. The payload's ``text`` field is byte-identical to the historical
    value (path / command[:180]) — harvest's dedup key depends on it. Extra
    keys (plus/minus/tool/desc) ride alongside. A file change that computes
    to +0 −0 is not a change and is skipped (locked design rule).

    Returns ``[]`` for unknown tools or missing fields — never raises on a
    well-formed dict with absent keys.
    """
    if not isinstance(payload, dict):
        return []
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") or {}
    if tool in _FILE_TOOLS:
        path = inp.get("file_path")
        if not path:
            return []
        plus, minus = line_stats(tool, inp, payload.get("tool_response"))
        p = {"text": path, "tool": tool}
        if plus is not None:
            if plus == 0 and minus == 0:
                return []
            p["plus"], p["minus"] = plus, minus
        return [("file_touched", p)]
    if tool in _CMD_TOOLS:
        cmd = inp.get("command")
        if not cmd:
            return []
        p = {"text": cmd[:_CMD_MAX]}
        desc = inp.get("description")
        if isinstance(desc, str) and desc.strip():
            p["desc"] = desc.strip()[:_DESC_MAX]
        return [("command_run", p)]
    return []
```

Then update `record_post_tool_use` to append the payload dict directly — replace the loop body:

```python
        for etype, ep in extract_events(payload):
            events.append_event(etype, ep, session_id=sid,
                                tags=["auto"], ts=now, project=project)
            appended += 1
```

- [ ] **Step 4: Run the hookcapture tests**

Run: `python -m pytest tests/test_hookcapture.py -q`
Expected: PASS (all).

- [ ] **Step 5: Add the harvest parity tests**

Append to `tests/test_harvest.py` (it already has helpers for writing transcript lines; if not, the `_tooluse` helper below is self-contained):

```python
def _tooluse_line(name, inp):
    return json.dumps({"type": "assistant", "message": {
        "content": [{"type": "tool_use", "name": name, "input": inp}]}})


def test_harvest_items_carry_line_stats_and_desc():
    o = json.loads(_tooluse_line("Edit", {
        "file_path": "src/a.py", "old_string": "x", "new_string": "x\ny"}))
    items = harvest._items_from_obj(o)
    assert items == [{"kind": "file_touched", "value": "src/a.py",
                      "tool": "Edit", "plus": 2, "minus": 1}]
    o2 = json.loads(_tooluse_line("Bash", {
        "command": "pytest -q", "description": "Run tests"}))
    items2 = harvest._items_from_obj(o2)
    assert items2 == [{"kind": "command_run", "value": "pytest -q",
                       "desc": "Run tests"}]


def test_harvest_skips_zero_change_edit():
    o = json.loads(_tooluse_line("Edit", {
        "file_path": "src/a.py", "old_string": "", "new_string": ""}))
    assert harvest._items_from_obj(o) == []


def test_event_for_item_passes_extra_keys_but_text_is_unchanged():
    etype, payload, tags = harvest._event_for_item(
        {"kind": "file_touched", "value": "src/a.py",
         "tool": "Edit", "plus": 2, "minus": 1})
    assert etype == "file_touched"
    assert payload["text"] == "src/a.py"          # dedup key stability
    assert payload["plus"] == 2 and payload["minus"] == 1 and payload["tool"] == "Edit"
    etype2, payload2, _ = harvest._event_for_item(
        {"kind": "command_run", "value": "pytest -q", "desc": "Run tests"})
    assert payload2 == {"text": "pytest -q", "desc": "Run tests"}
```

Ensure `import json` and `from mindhive import harvest` are present at the top of the file (they already are).

- [ ] **Step 6: Run to verify failure, then implement the harvest side**

Run: `python -m pytest tests/test_harvest.py -q` — expected: FAIL on the new tests.

In `src/mindhive/harvest.py`, add `hookcapture` to the module import (`from . import events, paths` → `from . import events, hookcapture, paths`). In `_items_from_obj`, replace the tool_use branch:

```python
                if name in ("Edit", "Write", "NotebookEdit"):
                    fp = inp.get("file_path")
                    if fp:
                        plus, minus = hookcapture.line_stats(name, inp)
                        item = {"kind": "file_touched", "value": fp, "tool": name}
                        if plus is not None:
                            if plus == 0 and minus == 0:
                                continue      # +0 −0 is not a change (locked rule)
                            item["plus"], item["minus"] = plus, minus
                        items.append(item)
                elif name in ("Bash", "PowerShell"):
                    cmd = inp.get("command")
                    if cmd:
                        item = {"kind": "command_run", "value": cmd[:180]}
                        desc = inp.get("description")
                        if isinstance(desc, str) and desc.strip():
                            item["desc"] = desc.strip()[:120]
                        items.append(item)
```

In `_event_for_item`, replace the first two branches:

```python
    if kind == "file_touched":
        payload = {"text": item["value"]}
        for k in ("plus", "minus", "tool"):
            if k in item:
                payload[k] = item[k]
        return "file_touched", payload, ["harvest", "auto"]
    if kind == "command_run":
        payload = {"text": item["value"]}
        if "desc" in item:
            payload["desc"] = item["desc"]
        return "command_run", payload, ["harvest", "auto"]
```

- [ ] **Step 7: Run both test files plus the events/autosync suites (dedup safety)**

Run: `python -m pytest tests/test_hookcapture.py tests/test_harvest.py tests/test_autosync.py tests/test_events.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mindhive/hookcapture.py src/mindhive/harvest.py tests/test_hookcapture.py tests/test_harvest.py
git commit -m "feat(capture): line stats + command descriptions; skip zero-change edits"
```

---

### Task 2: `taskview.py` backend module

**Files:**
- Create: `src/mindhive/taskview.py`
- Test: `tests/test_taskview.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_taskview.py`:

```python
# tests/test_taskview.py
import json
import time
from mindhive import taskview
from mindhive.events import Event


def _ev(etype, payload, ts="2026-07-09T10:00:00Z", sid="s1"):
    return Event(ts=ts, session_id=sid, type=etype, payload=payload)


def test_status_of_boundary():
    now = time.time()
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60)) + "Z"
    stale = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 3600)) + "Z"
    assert taskview.status_of(fresh, now=now) == "ongoing"
    assert taskview.status_of(stale, now=now) == "completed"
    assert taskview.status_of(None, now=now) == "completed"
    assert taskview.status_of("garbage", now=now) == "completed"


def test_tally_counts_and_line_sums():
    evs = [
        _ev("file_touched", {"text": "a.py", "plus": 10, "minus": 4}),
        _ev("file_touched", {"text": "a.py", "plus": 2, "minus": 1}),  # same file
        _ev("file_touched", {"text": "b.py"}),                          # no stats
        _ev("command_run", {"text": "pytest -q"}),
        _ev("caveat", {"text": "plan FIRST"}),
        _ev("tool_failed", {"signature": "skill:x", "text": "boom"}),
        _ev("loop_iteration", {"iteration": 1, "summary": "did a thing"}),
        _ev("user_prompt", {"text": "ignored"}),
    ]
    t = taskview.tally(evs)
    assert t == {"files": 2, "plus": 12, "minus": 5, "commands": 1,
                 "notes": 1, "failures": 1, "loops": 1}


def test_items_maps_kinds_and_caps_to_last_100():
    evs = [_ev("command_run", {"text": f"cmd {i}"}) for i in range(150)]
    evs.append(_ev("file_touched", {"text": "z.py", "plus": 1, "minus": 0,
                                    "tool": "Edit"}))
    out = taskview.items(evs)
    assert out["total"] == 151
    assert out["truncated"] == 51
    assert len(out["items"]) == 100
    assert out["items"][-1] == {"kind": "touched", "text": "z.py",
                                "ts": "2026-07-09T10:00:00Z",
                                "plus": 1, "minus": 0, "tool": "Edit"}
    assert out["items"][0]["text"] == "cmd 51"     # oldest 51 dropped


def test_items_loop_row_carries_engine_fields():
    evs = [_ev("loop_iteration", {
        "iteration": 3, "summary": "fixed breaker", "progress": True,
        "cost_usd": 0.12, "total_tokens": 900,
        "context_fill_ratio": 0.4, "breaker_state": "CLOSED"})]
    row = taskview.items(evs)["items"][0]
    assert row["kind"] == "loop" and row["text"] == "fixed breaker"
    assert row["iteration"] == 3 and row["progress"] is True
    assert row["breaker_state"] == "CLOSED"
    assert row["cost_usd"] == 0.12 and row["total_tokens"] == 900


def test_find_transcript_rejects_traversal_ids(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "good-1.jsonl").write_text("{}", encoding="utf-8")
    assert taskview.find_transcript("good-1", root=tmp_path) is not None
    assert taskview.find_transcript("../evil", root=tmp_path) is None
    assert taskview.find_transcript("..\\evil", root=tmp_path) is None
    assert taskview.find_transcript("", root=tmp_path) is None
    assert taskview.find_transcript(None, root=tmp_path) is None


def _transcript_lines():
    use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Edit",
         "input": {"file_path": "src/a.py", "old_string": "x = 1",
                   "new_string": "x = 2"}},
        {"type": "tool_use", "id": "t2", "name": "Bash",
         "input": {"command": "pytest -q", "description": "Run tests"}},
    ]}}
    res = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2",
         "content": [{"type": "text", "text": "3 passed"}]},
    ]}}
    return [json.dumps(use), "not json {", json.dumps(res)]


def test_detail_parses_transcript_and_pairs_results(tmp_path):
    d = tmp_path / "enc-proj"
    d.mkdir()
    (d / "sess-9.jsonl").write_text("\n".join(_transcript_lines()),
                                    encoding="utf-8")
    out = taskview.detail("sess-9", root=tmp_path)
    assert "error" not in out
    kinds = [(i["kind"], i["text"]) for i in out["items"]]
    assert kinds == [("touched", "src/a.py"), ("ran", "pytest -q")]
    assert "-x = 1" in out["items"][0]["diff"]
    assert "+x = 2" in out["items"][0]["diff"]
    assert out["items"][1]["output"] == "3 passed"
    assert out["items"][1]["desc"] == "Run tests"


def test_detail_missing_transcript_degrades(tmp_path):
    out = taskview.detail("nope", root=tmp_path)
    assert out == {"error": "transcript_unavailable", "items": []}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_taskview.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mindhive.taskview'`.

- [ ] **Step 3: Create `src/mindhive/taskview.py`**

```python
# src/mindhive/taskview.py
"""Backend + page fragments for the Sessions notification center.

Three disclosure levels: L1 per-session tallies (the summary chip), L2 the
ordered activity rows capped to the last ITEM_CAP entries, and L3 granular
detail (full diffs / command output) parsed lazily from the Claude Code
transcript so the append-only ledger never stores heavy payloads. Nothing
here deletes anything — the Sessions page is a deliberately auditable track
record. Also owns the cards' CSS/JS fragments, injected into the page by
dashboard.py so dashboard_page.py stays sibling-free."""
import calendar
import difflib
import json
import re
import time
from pathlib import Path

ACTIVE_WINDOW_SECS = 600      # newest event younger than this -> "ongoing"
ITEM_CAP = 100                # locked: detail history shows the last 100 rows
DIFF_CAP_LINES = 400
OUTPUT_CAP_CHARS = 10_000
_SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NOTE_TYPES = ("caveat", "decision", "experiment_result",
               "next_step", "open_question")


def _epoch(ts):
    if not isinstance(ts, str):
        return None
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def status_of(last_ts, now=None) -> str:
    """"ongoing" when the session's newest event is fresher than
    ACTIVE_WINDOW_SECS, else "completed". Unparseable -> completed."""
    e = _epoch(last_ts)
    if e is None:
        return "completed"
    return "ongoing" if (now or time.time()) - e < ACTIVE_WINDOW_SECS else "completed"


def tally(evs) -> dict:
    """L1 aggregate for one session: row-kind counts plus summed line stats.
    Zero-valued keys are kept — the JS omits them from the chip."""
    t = {"files": 0, "plus": 0, "minus": 0, "commands": 0,
         "notes": 0, "failures": 0, "loops": 0}
    seen_files = set()
    for ev in evs:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.type == "file_touched":
            seen_files.add(p.get("text"))
            t["plus"] += int(p.get("plus") or 0)
            t["minus"] += int(p.get("minus") or 0)
        elif ev.type == "command_run":
            t["commands"] += 1
        elif ev.type in _NOTE_TYPES:
            t["notes"] += 1
        elif ev.type == "tool_failed":
            t["failures"] += 1
        elif ev.type == "loop_iteration":
            t["loops"] += 1
    t["files"] = len(seen_files)
    return t


def items(evs, cap=ITEM_CAP) -> dict:
    """L2 rows in ledger order, capped to the LAST ``cap``. The ledger keeps
    everything; ``truncated`` tells the UI how many older rows exist."""
    rows = []
    for ev in evs:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.type == "file_touched":
            row = {"kind": "touched", "text": p.get("text", ""), "ts": ev.ts}
            for k in ("plus", "minus", "tool"):
                if k in p:
                    row[k] = p[k]
        elif ev.type == "command_run":
            row = {"kind": "ran", "text": p.get("text", ""), "ts": ev.ts}
            if p.get("desc"):
                row["desc"] = p["desc"]
        elif ev.type in _NOTE_TYPES:
            row = {"kind": ev.type, "text": p.get("text", ""), "ts": ev.ts}
        elif ev.type == "tool_failed":
            row = {"kind": "failed", "ts": ev.ts,
                   "text": p.get("text") or p.get("signature", "")}
        elif ev.type == "loop_iteration":
            row = {"kind": "loop", "ts": ev.ts,
                   "text": p.get("summary") or f"iteration {p.get('iteration')}",
                   "iteration": p.get("iteration"),
                   "progress": p.get("progress"),
                   "breaker_state": p.get("breaker_state"),
                   "cost_usd": p.get("cost_usd"),
                   "total_tokens": p.get("total_tokens"),
                   "context_fill_ratio": p.get("context_fill_ratio")}
        else:
            continue
        rows.append(row)
    total = len(rows)
    return {"items": rows[-cap:], "total": total,
            "truncated": max(0, total - cap)}


# --- L3: lazy granular detail from the Claude Code transcript --------------

def transcripts_root() -> Path:
    return Path.home() / ".claude" / "projects"


def find_transcript(session_id, root=None):
    """Locate ``<root>/<any dir>/<session_id>.jsonl``. The id becomes a
    filename, so anything but a strict token is rejected (path traversal)."""
    if not isinstance(session_id, str) or not _SID_RE.match(session_id):
        return None
    root = Path(root) if root else transcripts_root()
    if not root.exists():
        return None
    for d in sorted(root.iterdir()):
        f = d / f"{session_id}.jsonl"
        if d.is_dir() and f.exists():
            return f
    return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _diff(old, new, path) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=path, tofile=path, lineterm=""))
    if len(lines) > DIFF_CAP_LINES:
        extra = len(lines) - DIFF_CAP_LINES
        lines = lines[:DIFF_CAP_LINES] + [f"… +{extra} more lines"]
    return "\n".join(lines)


def _detail_for_use(block):
    name = block.get("name")
    inp = block.get("input") or {}
    if name in ("Edit", "Write", "NotebookEdit"):
        fp = inp.get("file_path")
        if not fp:
            return None
        d = {"kind": "touched", "text": fp, "tool": name}
        old, new = inp.get("old_string"), inp.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            d["diff"] = _diff(old, new, fp)
        elif name == "Write" and isinstance(inp.get("content"), str):
            head = inp["content"].splitlines()[:DIFF_CAP_LINES]
            d["diff"] = "\n".join("+" + ln for ln in head)
        return d
    if name in ("Bash", "PowerShell"):
        cmd = inp.get("command")
        if not cmd:
            return None
        d = {"kind": "ran", "text": cmd[:180], "command": cmd[:2000]}
        desc = inp.get("description")
        if isinstance(desc, str) and desc.strip():
            d["desc"] = desc.strip()[:120]
        return d
    return None


def detail(session_id, root=None) -> dict:
    """L3: ordered granular details parsed from the session transcript.
    Items carry the same (kind, text) identity as the L2 rows so the UI can
    zip them in order. A missing transcript degrades — old sessions must
    render, not 500."""
    f = find_transcript(session_id, root=root)
    if f is None:
        return {"error": "transcript_unavailable", "items": []}
    try:
        lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {"error": "transcript_unavailable", "items": []}
    uses, order = {}, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if o.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    d = _detail_for_use(b)
                    if d is not None:
                        uses[b.get("id")] = d
                        order.append(b.get("id"))
        elif o.get("type") == "user":
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                d = uses.get(b.get("tool_use_id"))
                if d is not None and d["kind"] == "ran":
                    d["output"] = _result_text(b.get("content"))[:OUTPUT_CAP_CHARS]
    out = [uses[i] for i in order if i in uses]
    return {"items": out[-ITEM_CAP:], "total": len(out)}
```

(The `CARDS_CSS` / `CARDS_JS` constants are added in Task 4 — this task is backend only.)

- [ ] **Step 4: Run the taskview tests**

Run: `python -m pytest tests/test_taskview.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/taskview.py tests/test_taskview.py
git commit -m "feat(taskview): session tallies, capped activity rows, lazy transcript detail"
```

---

### Task 3: API wiring in dashboard.py

**Files:**
- Modify: `src/mindhive/dashboard.py` (imports, `_get_sessions:412-428`, new handlers, `_GET_ROUTES:729-746`)
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing endpoint tests**

Append to `tests/test_dashboard.py`:

```python
def _seed_session(project="dashproj", sid="sess-t1"):
    from mindhive import events
    events.append_event("file_touched",
                        {"text": "src/a.py", "plus": 5, "minus": 2, "tool": "Edit"},
                        session_id=sid, project=project)
    events.append_event("command_run",
                        {"text": "pytest -q", "desc": "Run tests"},
                        session_id=sid, project=project)
    events.append_event("caveat", {"text": "plan FIRST"},
                        session_id=sid, project=project)


def test_sessions_carry_activity_and_status(dashboard_server):
    base, token = dashboard_server
    _seed_session()
    status, body = _get(base, "/api/sessions?project=dashproj", token)
    assert status == 200
    (s,) = json.loads(body)
    assert s["status"] in ("ongoing", "completed")
    assert s["activity"]["files"] == 1
    assert s["activity"]["plus"] == 5 and s["activity"]["minus"] == 2
    assert s["activity"]["commands"] == 1 and s["activity"]["notes"] == 1


def test_task_endpoint_returns_capped_rows(dashboard_server):
    base, token = dashboard_server
    _seed_session()
    status, body = _get(base, "/api/task?project=dashproj&session=sess-t1", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["total"] == 3 and obj["truncated"] == 0
    assert [r["kind"] for r in obj["items"]] == ["touched", "ran", "caveat"]


def test_task_endpoint_hivemind_requires_session_project(dashboard_server):
    base, token = dashboard_server
    _seed_session()
    try:
        _get(base, "/api/task?project=*&session=sess-t1", token)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400
    status, body = _get(
        base, "/api/task?project=*&session=sess-t1&session_project=dashproj",
        token)
    assert status == 200
    assert json.loads(body)["total"] == 3


def test_task_detail_degrades_without_transcript(dashboard_server):
    base, token = dashboard_server
    status, body = _get(
        base, "/api/task_detail?project=dashproj&session=no-such-sess", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["error"] == "transcript_unavailable" and obj["items"] == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_dashboard.py -q`
Expected: the four new tests FAIL (404 on `/api/task`, missing `activity` key).

- [ ] **Step 3: Wire the routes**

In `src/mindhive/dashboard.py`, add `taskview` to the package import near the top (the module already imports siblings like `events`, `chain`, `dedupe` — extend that import). Then:

**(a)** Replace `_get_sessions` with:

```python
        def _get_sessions(self, proj, am, qs):
            by: dict = {}
            for name, e in event_pairs(proj, am):
                key = (name, e.session_id)
                s = by.get(key)
                if s is None:
                    s = by[key] = {"session_id": e.session_id, "project": name,
                                   "events": 0, "memory": 0,
                                   "first_ts": e.ts, "last_ts": e.ts, "_evs": []}
                s["events"] += 1
                s["_evs"].append(e)
                if e.type in events.MEMORY_VIEW_TYPES:
                    s["memory"] += 1
                if e.ts and e.ts < s["first_ts"]:
                    s["first_ts"] = e.ts
                if e.ts and e.ts > s["last_ts"]:
                    s["last_ts"] = e.ts
            out = sorted(by.values(), key=lambda s: s["last_ts"] or "", reverse=True)
            for s in out:
                s["activity"] = taskview.tally(s.pop("_evs"))
                s["status"] = taskview.status_of(s["last_ts"])
            return self._json(out)
```

**(b)** Add two handlers after `_get_sessions`:

```python
        def _get_task(self, proj, am, qs):
            sid = (qs.get("session") or [""])[0]
            # In HIVE MIND mode each card carries its own project — a concrete
            # namespace is required to read the right ledger.
            tproj = (qs.get("session_project") or [None])[0]
            if am and not tproj:
                return self._json({"ok": False,
                                   "error": "session_project required"}, 400)
            evs = [e for e in events.read_all(tproj or proj)
                   if e.session_id == sid]
            return self._json(taskview.items(evs))

        def _get_task_detail(self, proj, am, qs):
            sid = (qs.get("session") or [""])[0]
            return self._json(taskview.detail(sid))
```

**(c)** Add to `_GET_ROUTES`:

```python
            "/api/task": _get_task,
            "/api/task_detail": _get_task_detail,
```

- [ ] **Step 4: Run the dashboard tests**

Run: `python -m pytest tests/test_dashboard.py -q`
Expected: PASS (old and new).

- [ ] **Step 5: Commit**

```bash
git add src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): /api/task + /api/task_detail; sessions carry activity/status"
```

---

### Task 4: Notification-card UI

**Files:**
- Modify: `src/mindhive/taskview.py` (append `CARDS_CSS`, `CARDS_JS`)
- Modify: `src/mindhive/dashboard_page.py` (placeholders, `render_page` signature, `renderSessions` delegation)
- Modify: `src/mindhive/dashboard.py:503` (pass fragments)
- Test: `tests/test_dashboard.py`

- [ ] **Step 1: Write the failing test (page carries the injected fragments)**

Append to `tests/test_dashboard.py`:

```python
def test_root_page_includes_task_cards_fragments(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "renderTaskCards" in body        # injected JS arrived
    assert ".tcard" in body                 # injected CSS arrived
    assert "__TASKS_JS__" not in body       # placeholders were replaced
    assert "__TASKS_CSS__" not in body
```

Run: `python -m pytest tests/test_dashboard.py::test_root_page_includes_task_cards_fragments -q` — expected: FAIL.

- [ ] **Step 2: Append the CSS fragment to `src/mindhive/taskview.py`**

```python
# --- page fragments (injected into dashboard_page by dashboard.py) ----------

CARDS_CSS = """
  .tcard { background:#0c0e14; border:1px solid #2a2f3d; border-radius:10px;
    padding:10px 14px; margin:10px 0; }
  .tcard .thead { display:flex; gap:10px; align-items:center; cursor:pointer;
    flex-wrap:wrap; }
  .tstat { font-size:11px; border-radius:999px; padding:1px 8px;
    border:1px solid #2a2f3d; white-space:nowrap; }
  .tstat.ongoing { color:#e0b341; animation: pulse 1.6s infinite; }
  .tstat.done { color:#46c46a; }
  .tsid { font-size:12px; color:#7fb2ff; word-break:break-all; }
  .tchip { font-size:12px; color:#e6e8ef; }
  .tspan { font-size:11px; color:#6b7488; margin-left:auto; }
  .trows { list-style:none; padding:0; margin:8px 0 0; }
  .trow { border-top:1px solid #1d2230; padding:5px 2px; }
  .tlab { cursor:pointer; }
  .tpath { color:#7fb2ff; } .tplus { color:#46c46a; } .tminus { color:#ff8a8a; }
  .tbad { color:#ff8a8a; } .tnote { color:#e0b341; }
  .tbody { margin:6px 0 4px 16px; }
  .tdiff { background:#0a0c11; border:1px solid #1d2230; border-radius:6px;
    padding:8px 10px; max-height:40vh; overflow:auto; white-space:pre-wrap;
    font-size:12px; }
  .dplus { color:#46c46a; } .dminus { color:#ff8a8a; } .dctx { color:#9aa3b8; }
  .tdet td { padding:1px 10px 1px 0; color:#9aa3b8; }
  .tdet td+td { color:#e6e8ef; }
  .tmem { color:#9ecbff; font-size:12px; }
"""
```

- [ ] **Step 3: Append the JS fragment to `src/mindhive/taskview.py`**

```python
CARDS_JS = """
  // --- Sessions notification center (injected by taskview.py) ---
  // Expansion state and fetched levels live at module level so the 2.5s live
  // re-render never collapses what the user opened (recorded lesson). There is
  // deliberately NO delete affordance anywhere in this view: the history is an
  // auditable track record.
  const TASK_OPEN = new Set();       // session keys with the card expanded
  const TASK_ROW_OPEN = new Set();   // key + '#' + row index
  const TASK_ITEMS = {};             // key -> /api/task response
  const TASK_DETAIL = {};            // key -> /api/task_detail (null = loading)
  let TASK_SESSIONS = [];
  function taskKey(s){ return (s.project||'') + '|' + s.session_id; }
  function chipText(a){
    const seg = [];
    if (a.files) seg.push('edited ' + a.files + ' file' + (a.files>1?'s':'')
      + ((a.plus||a.minus) ? ' (+'+(a.plus||0)+' \\u2212'+(a.minus||0)+')' : ''));
    if (a.commands) seg.push('ran ' + a.commands + ' command' + (a.commands>1?'s':''));
    if (a.notes) seg.push(a.notes + ' note' + (a.notes>1?'s':''));
    if (a.failures) seg.push(a.failures + ' failure' + (a.failures>1?'s':''));
    if (a.loops) seg.push(a.loops + ' loop iteration' + (a.loops>1?'s':''));
    return seg.join(' \\u00b7 ') || 'no tracked activity';
  }
  function rowLabel(r){
    if (r.kind === 'touched'){
      const base = r.text.split(/[\\\\/]/).pop();
      const stats = (r.plus!=null)
        ? ' <span class="tplus">+'+r.plus+'</span> <span class="tminus">\\u2212'+r.minus+'</span>' : '';
      return 'touched <span class="tpath" title="'+escAttr(r.text)+'">'+esc(base)+'</span>'+stats;
    }
    if (r.kind === 'ran') return 'ran '+esc(r.desc || r.text);
    if (r.kind === 'failed') return '<span class="tbad">[failed]</span> '+esc(r.text);
    if (r.kind === 'loop'){
      const glyph = r.breaker_state === 'OPEN' ? '\\u26a0 '
        : (r.progress ? '\\u2713 ' : '\\u25cb ');
      return glyph+'iter '+r.iteration+' \\u00b7 '+esc(r.text);
    }
    return '<span class="tnote">['+esc(r.kind)+']</span> '+esc(r.text);
  }
  function rowDetailHTML(r, det){
    if (r.kind === 'loop'){
      return '<table class="tdet">'
        + '<tr><td>progress</td><td>'+(r.progress ? 'advanced' : 'no progress')+'</td></tr>'
        + '<tr><td>breaker</td><td>'+esc(r.breaker_state||'\\u2014')+'</td></tr>'
        + '<tr><td>tokens</td><td>'+(r.total_tokens!=null ? r.total_tokens : '\\u2014')+'</td></tr>'
        + '<tr><td>cost</td><td>'+(r.cost_usd!=null ? '$'+r.cost_usd : '\\u2014')+'</td></tr>'
        + '<tr><td>context fill</td><td>'+(r.context_fill_ratio!=null
            ? Math.round(r.context_fill_ratio*100)+'%' : '\\u2014')+'</td></tr>'
        + '</table>';
    }
    if (r.kind === 'touched'){
      if (!det || !det.diff)
        return '<div class="lbl">'+esc(r.text)
          + ' \\u2014 line-level detail unavailable (transcript gone)</div>';
      return '<pre class="tdiff">'+det.diff.split('\\n').map(l =>
        '<span class="'+(l[0]==='+'?'dplus':l[0]==='-'?'dminus':'dctx')+'">'
        +esc(l)+'</span>').join('\\n')+'</pre>';
    }
    if (r.kind === 'ran'){
      let h = '<pre class="tdiff dctx">$ '+esc((det&&det.command)||r.text)+'</pre>';
      if (det && det.output) h += '<pre class="tdiff">'+esc(det.output)+'</pre>';
      else h += '<div class="lbl">output unavailable (transcript gone)</div>';
      return h;
    }
    return '<div class="snip">'+esc(r.text)+'</div>';
  }
  function matchDetails(rows, detItems){
    // zip L2 rows to transcript details in order, keyed by kind+text
    const q = {};
    (detItems||[]).forEach(d => {
      const k = d.kind+'|'+d.text; (q[k]=q[k]||[]).push(d); });
    return rows.map(r => {
      const k = r.kind+'|'+r.text;
      return (q[k]&&q[k].length) ? q[k].shift() : null; });
  }
  async function loadTaskItems(key, s){
    const extra = (PROJECT === '*')
      ? '&session_project='+encodeURIComponent(s.project) : '';
    TASK_ITEMS[key] = await apiGet(
      '/api/task?session='+encodeURIComponent(s.session_id)+extra);
  }
  async function toggleTaskCard(key, s){
    if (TASK_OPEN.has(key)) TASK_OPEN.delete(key);
    else {
      TASK_OPEN.add(key);
      if (!TASK_ITEMS[key]) await loadTaskItems(key, s);
    }
    paintTaskCards();
  }
  async function toggleTaskRow(key, idx, s){
    const rk = key+'#'+idx;
    if (TASK_ROW_OPEN.has(rk)){ TASK_ROW_OPEN.delete(rk); paintTaskCards(); return; }
    TASK_ROW_OPEN.add(rk);
    const data = TASK_ITEMS[key];
    const row = data && data.items ? data.items[idx] : null;
    if (row && (row.kind==='touched'||row.kind==='ran')
        && TASK_DETAIL[key] === undefined){
      TASK_DETAIL[key] = null; paintTaskCards();
      try {
        TASK_DETAIL[key] = await apiGet(
          '/api/task_detail?session='+encodeURIComponent(s.session_id));
      } catch(_){ TASK_DETAIL[key] = {error:'fetch_failed', items:[]}; }
    }
    paintTaskCards();
  }
  async function renderTaskCards(){
    TASK_SESSIONS = await apiGet('/api/sessions');
    for (const s of TASK_SESSIONS){
      const key = taskKey(s);
      if (TASK_OPEN.has(key) && (s.status === 'ongoing' || !TASK_ITEMS[key]))
        await loadTaskItems(key, s);   // ongoing cards refresh on each tick
    }
    paintTaskCards();
  }
  function cardHTML(s){
    const key = taskKey(s);
    const open = TASK_OPEN.has(key);
    const badge = s.status === 'ongoing'
      ? '<span class="tstat ongoing">\\u25cf ongoing</span>'
      : '<span class="tstat done">\\u2713 completed</span>';
    let body = '';
    if (open){
      const data = TASK_ITEMS[key];
      if (!data) body = '<div class="lbl">loading\\u2026</div>';
      else {
        const dets = matchDetails(data.items,
          TASK_DETAIL[key] && TASK_DETAIL[key].items);
        body = (data.truncated
            ? '<div class="lbl">'+data.truncated+' older entr'
              +(data.truncated>1?'ies':'y')
              +' beyond the last 100 \\u2014 the full history stays in the ledger</div>'
            : '')
          + '<ul class="trows">' + data.items.map((r,i) => {
              const rk = key+'#'+i, ropen = TASK_ROW_OPEN.has(rk);
              return '<li class="trow" data-k="'+escAttr(key)+'" data-i="'+i+'">'
                + '<div class="tlab">'+(ropen?'\\u25be':'\\u203a')+' '+rowLabel(r)+'</div>'
                + (ropen ? '<div class="tbody">'+rowDetailHTML(r, dets[i])+'</div>' : '')
                + '</li>';
            }).join('') + '</ul>'
          + '<div class="row"><a href="#" class="tmem" data-sid="'
          + escAttr(s.session_id)+'">open in Memory \\u2192</a></div>';
      }
    }
    return '<div class="tcard'+(open?' open':'')+'" data-k="'+escAttr(key)+'">'
      + '<div class="thead">'+badge
      + '<span class="tsid">'+(PROJECT==='*'&&s.project ? esc(s.project)+' \\u00b7 ' : '')
      + esc(s.session_id)+'</span>'
      + '<span class="tchip">'+chipText(s.activity||{})+'</span>'
      + '<span class="tspan">'+esc(s.first_ts||'')+' \\u2192 '+esc(s.last_ts||'')+'</span>'
      + '</div>' + body + '</div>';
  }
  function paintTaskCards(){
    if (!TASK_SESSIONS.length){ showText('No sessions yet.'); return; }
    showHTML('<h2 class="h">'+TASK_SESSIONS.length+' task'
      +(TASK_SESSIONS.length>1?'s':'')+'</h2>'
      + '<div class="legend">Append-only, auditable history \\u2014 entries cannot'
      + ' be deleted. Expanded cards show the last 100 actions.</div>'
      + TASK_SESSIONS.map(cardHTML).join(''));
    view.querySelectorAll('.tcard .thead').forEach(el => {
      const key = el.parentElement.dataset.k;
      const s = TASK_SESSIONS.find(x => taskKey(x) === key);
      el.onclick = () => toggleTaskCard(key, s);
    });
    view.querySelectorAll('.trow .tlab').forEach(el => {
      const li = el.parentElement, key = li.dataset.k, idx = +li.dataset.i;
      const s = TASK_SESSIONS.find(x => taskKey(x) === key);
      el.onclick = (ev) => { ev.stopPropagation(); toggleTaskRow(key, idx, s); };
    });
    view.querySelectorAll('.tmem').forEach(el => el.onclick = (ev) => {
      ev.preventDefault(); openSession(el.dataset.sid);
    });
  }
"""
```

Note the escaping rules: the fragment is a Python string embedded in a JS `<script>`, so JS string escapes that must survive (`\\u00b7`, `\\n` inside `.split('\\n')`, the `[\\\\/]` regex) are double-escaped exactly as shown, matching the existing style in `dashboard_page.py`.

- [ ] **Step 4: Add the placeholders to `dashboard_page.py`**

Three edits in `src/mindhive/dashboard_page.py`:

**(a)** Signature and replacements — change line 12 and the final line:

```python
def render_page(token: str, tasks_css: str = "", tasks_js: str = "") -> str:
```

```python
</body></html>""".replace("__TOKEN__", json.dumps(token)) \
    .replace("/*__TASKS_CSS__*/", tasks_css) \
    .replace("//__TASKS_JS__", tasks_js)
```

**(b)** Inside `<style>`, add a line just before `</style>`:

```
  /*__TASKS_CSS__*/
```

**(c)** Replace the body of `renderSessions` (lines 566-576) with a delegation, and add the JS placeholder just above the boot IIFE (line ~902):

```javascript
  // --- Sessions: rendered by the notification-center fragment (taskview.py).
  async function renderSessions(){ return renderTaskCards(); }
```

```javascript
  //__TASKS_JS__
```

(Keep `showSessions` and `openSession` unchanged — `openSession` is the "open in Memory" hook the fragment calls back into.)

- [ ] **Step 5: Inject the fragments in `dashboard.py`**

Change line 503:

```python
            if route == "/":
                return self._send(200,
                                  render_page(token,
                                              tasks_css=taskview.CARDS_CSS,
                                              tasks_js=taskview.CARDS_JS),
                                  "text/html; charset=utf-8")
```

- [ ] **Step 6: Run the dashboard tests, then eyeball it live**

Run: `python -m pytest tests/test_dashboard.py -q` — expected: PASS.

Manual check: `python -m mindhive dashboard` (or the project's launch command), open Sessions, confirm: cards collapse/expand, chip text, +N −M coloring, row expand fetches detail, ongoing badge pulses, expansion survives the 2.5 s refresh, and there is no delete affordance.

- [ ] **Step 7: Commit**

```bash
git add src/mindhive/taskview.py src/mindhive/dashboard_page.py src/mindhive/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): Sessions notification cards with expandable granular detail"
```

---

### Task 5: Full suite, line caps, README

**Files:**
- Modify: `README.md` (Sessions/dashboard section)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS, no regressions (dedup keys unchanged, page still serves).

- [ ] **Step 2: Verify the file-size caps held**

Run (PowerShell): `(Get-Content src/mindhive/dashboard.py | Measure-Object -Line).Lines; (Get-Content src/mindhive/dashboard_page.py | Measure-Object -Line).Lines`
Expected: both < 1000. If `dashboard_page.py` exceeded the cap, move more of the Sessions-specific JS into `taskview.CARDS_JS` (it is designed to absorb it).

- [ ] **Step 3: Document the notification center in README.md**

In the dashboard section of `README.md`, replace the Sessions bullet (or add one if absent) with:

```markdown
- **Sessions — notification center.** Every session renders as a collapsible
  task card: status badge (`● ongoing` / `✓ completed`), a summary chip
  ("edited 8 files (+120 −44) · ran 3 commands · 1 note"), and expandable
  rows for touched files (+N −M line stats), commands (with descriptions),
  notes/caveats, failures, and loop iterations (progress · breaker · tokens ·
  cost). Expanding a file or command row fetches the full diff or command
  output lazily from the Claude Code transcript — nothing heavy is stored in
  the ledger, and sessions whose transcript is gone degrade to a notice.
  The history is append-only and deliberately has no delete button; expanded
  cards show the last 100 actions (older rows stay in the ledger). Zero-change
  file writes (+0 −0) are not logged.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document the Sessions notification center"
```

---

## Self-review notes

- Spec rule 2 (zero-change skip) → Task 1 Steps 3/6; unknown stats still log (tested).
- Spec rule 3 (100-cap) → `ITEM_CAP` in Task 2 (`items` + `detail`), truncation notice in Task 4 JS.
- Spec rule 4 (no delete) → no delete affordance anywhere in Task 4; legend line states it; README documents it.
- Loop-iteration UI gap → `items()` loop rows (Task 2) + `rowLabel`/`rowDetailHTML` loop branches (Task 4).
- Dedup invariant → asserted in Task 1 tests (`text` unchanged in both paths).
- Type consistency: row kinds are `touched|ran|failed|loop|<note types>` in both `taskview.items` and the JS; detail identity is `(kind, text)` in both `detail()` and `matchDetails`.
