# Skill Effectiveness Ranking Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the dormant per-skill success/failure pipeline (0 `invoke_result` events today) and make plugin ranking use measured effectiveness instead of pure breadth, including a demotion gate that stops recommending skills that keep failing.

**Architecture:** Capture skill invocations at the source — the live PostToolUse hook and transcript harvest both start emitting `invoke_result` when the `Skill` tool runs, feeding the existing readers (`plugins.call_status` / `call_history`) with real data. Ranking gains an effectiveness factor (Laplace-smoothed success rate, neutral 0.5 with no history) multiplied into the breadth score, plus a hard gate: ≥3 calls with <40 % success → demoted out of recommendations with a reason.

**Tech Stack:** Python 3.12+ stdlib, pytest. Worktree root: `C:\Users\hanif\mindhive\.claude\worktrees\pedantic-haibt-82400b`.

---

## The problem being fixed (measured state, 2026-07-09)

- `plugins.call_status()` / `call_history()` (`plugins.py:417-453`) read `invoke_result` / `tool_failed(skill:*)` — but the store contains **0** `invoke_result` events, because only `invoker.py` (the `hive skills invoke` path) ever writes them and nobody invokes skills that way. Real skill usage happens through Claude Code's `Skill` tool, which nothing captures.
- Ranking (`plugins.py:175-183`) is `breadth_score = trigger_count + topic_spread * BREADTH_WEIGHT` — coverage, not effectiveness.
- No gate demotes a skill that repeatedly fails.

## Background the engineer needs

- `hookcapture.extract_events(payload)` maps one PostToolUse payload to `(type, payload_dict)` pairs (after the notification-center plan lands; on the pre-enrichment contract the pairs are `(type, value)` — this plan assumes the enriched contract, so **implement the notification-center Task 1 first**).
- The `Skill` tool's PostToolUse payload has `tool_name == "Skill"` and `tool_input == {"skill": "<plugin:name>", "args": "..."}`.
- Harvest's transcript parser `_items_from_obj` sees the same tool_use blocks (`name == "Skill"`) — adding capture there **back-fills history from every existing transcript** on the next catch-up.
- Harvest dedup key is `(session_id, type, payload["text"])` — the payload must carry `text`.
- `invoke_result` and `invoke_requested` are already valid event types (`events.py:19`).
- `plugins.evaluate()` builds the recommendation payload stored as the catalog snapshot; `ranked = sorted(free_plugins, key=...breadth...)` is at `plugins.py:218`.
- Verify the shape of `plugin["skills"]` entries in `plugins.scan()` before Task 3: this plan assumes each entry carries `name` (the invokable skill name) alongside `trigger`; `tests/test_plugins.py` fixtures show the real shape — adjust the key if it differs.

## File Structure

- Modify: `src/mindhive/hookcapture.py` — capture `Skill` tool calls
- Modify: `src/mindhive/harvest.py` — same capture for transcripts (retroactive)
- Modify: `src/mindhive/plugins.py` — `call_counts`, `effectiveness`, gated ranking
- Test: `tests/test_hookcapture.py`, `tests/test_harvest.py`, `tests/test_plugins.py`

---

### Task 1: Capture live Skill invocations

**Files:**
- Modify: `src/mindhive/hookcapture.py`
- Test: `tests/test_hookcapture.py`

- [ ] **Step 1: Write the failing test**

```python
def test_extract_skill_invocation_as_invoke_result():
    payload = {"tool_name": "Skill",
               "tool_input": {"skill": "superpowers:brainstorming",
                              "args": "topic x"}}
    out = hookcapture.extract_events(payload)
    assert out == [("invoke_result",
                    {"text": "superpowers:brainstorming",
                     "name": "superpowers:brainstorming"})]


def test_extract_skill_without_name_is_ignored():
    assert hookcapture.extract_events(
        {"tool_name": "Skill", "tool_input": {}}) == []
```

Run: `python -m pytest tests/test_hookcapture.py -q` — expected: FAIL.

- [ ] **Step 2: Implement**

In `hookcapture.py`, add after the `_CMD_TOOLS` branch of `extract_events`:

```python
    if tool == "Skill":
        name = inp.get("skill")
        if isinstance(name, str) and name.strip():
            name = name.strip()
            # PostToolUse fires after the tool ran; a skill whose execution
            # later fails still surfaces via the tool_failed pipeline, so this
            # records the invocation outcome the green/red view reads.
            return [("invoke_result", {"text": name, "name": name})]
        return []
```

- [ ] **Step 3: Run, expect PASS, commit**

```bash
python -m pytest tests/test_hookcapture.py -q
git add src/mindhive/hookcapture.py tests/test_hookcapture.py
git commit -m "feat(capture): record Skill tool invocations as invoke_result events"
```

---

### Task 2: Harvest parity (retroactive back-fill)

**Files:**
- Modify: `src/mindhive/harvest.py` (`_items_from_obj`, `_event_for_item`)
- Test: `tests/test_harvest.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_harvest_captures_skill_invocations():
    o = json.loads(_tooluse_line("Skill", {"skill": "mindhive:circle"}))
    assert harvest._items_from_obj(o) == [
        {"kind": "invoke_result", "value": "mindhive:circle"}]


def test_event_for_item_maps_invoke_result():
    etype, payload, tags = harvest._event_for_item(
        {"kind": "invoke_result", "value": "mindhive:circle"})
    assert etype == "invoke_result"
    assert payload == {"text": "mindhive:circle", "name": "mindhive:circle"}
    assert tags == ["harvest", "auto"]
```

Run: `python -m pytest tests/test_harvest.py -q` — expected: FAIL.

- [ ] **Step 2: Implement**

In `_items_from_obj`, add after the Bash/PowerShell branch:

```python
                elif name == "Skill":
                    sk = inp.get("skill")
                    if isinstance(sk, str) and sk.strip():
                        items.append({"kind": "invoke_result",
                                      "value": sk.strip()})
```

In `_event_for_item`, add before the `memo` branch:

```python
    if kind == "invoke_result":
        return "invoke_result", {"text": item["value"],
                                 "name": item["value"]}, ["harvest", "auto"]
```

- [ ] **Step 3: Run, expect PASS, commit**

```bash
python -m pytest tests/test_harvest.py tests/test_plugins.py -q
git add src/mindhive/harvest.py tests/test_harvest.py
git commit -m "feat(harvest): back-fill invoke_result from Skill tool_use in transcripts"
```

After this lands, one `hive sync` / dashboard catch-up back-fills skill call history from every retained transcript — the green/red view goes live with real data.

---

### Task 3: Effectiveness scoring + demotion gate

**Files:**
- Modify: `src/mindhive/plugins.py` (`score`, `evaluate`, new helpers)
- Test: `tests/test_plugins.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_call_counts_tallies_every_occurrence(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    from mindhive import events
    for _ in range(3):
        events.append_event("invoke_result", {"name": "a:x", "text": "a:x"},
                            project="p")
    events.append_event("tool_failed", {"signature": "skill:a:x"}, project="p")
    assert plugins.call_counts(project="p") == {"a:x": {"ok": 3, "fail": 1}}


def test_effectiveness_neutral_without_history():
    plugin = {"skills": [{"name": "a:x", "trigger": "t"}]}
    eff = plugins.effectiveness(plugin, {})
    assert eff["calls"] == 0
    assert eff["success_rate"] == 0.5      # Laplace prior, neutral
    assert eff["demoted"] is False


def test_effectiveness_demotes_repeat_failures():
    plugin = {"skills": [{"name": "a:x", "trigger": "t"}]}
    counts = {"a:x": {"ok": 0, "fail": 4}}
    eff = plugins.effectiveness(plugin, counts)
    assert eff["calls"] == 4
    assert eff["demoted"] is True
    assert "fail" in eff["demote_reason"]


def test_effectiveness_scales_rank_score():
    plugin = {"skills": [{"name": "a:x", "trigger": "t"}]}
    good = plugins.effectiveness(plugin, {"a:x": {"ok": 8, "fail": 0}})
    bad = plugins.effectiveness(plugin, {"a:x": {"ok": 0, "fail": 8}})
    assert good["success_rate"] > 0.8 and bad["success_rate"] < 0.2
```

Run: `python -m pytest tests/test_plugins.py -q` — expected: FAIL on the new tests.

- [ ] **Step 2: Implement in `plugins.py`**

Add near `score()`:

```python
MIN_CALLS_FOR_GATE = 3     # history needed before the demotion gate may fire
DEMOTE_BELOW = 0.4         # success rate under this -> demoted


def call_counts(project=None) -> dict:
    """{skill_name: {"ok": n, "fail": n}} — every occurrence of the same
    ledger events call_status() reads, not just the most recent one."""
    counts: dict = {}
    for e in ev_mod.read_all(project):
        if e.type == "invoke_result":
            name = e.payload.get("name")
            if name:
                counts.setdefault(name, {"ok": 0, "fail": 0})["ok"] += 1
        elif e.type == "tool_failed":
            sig = e.payload.get("signature", "")
            if isinstance(sig, str) and sig.startswith("skill:"):
                counts.setdefault(sig[len("skill:"):],
                                  {"ok": 0, "fail": 0})["fail"] += 1
    return counts


def effectiveness(plugin: dict, counts: dict) -> dict:
    """Aggregate measured effectiveness for one plugin's skills.

    Laplace-smoothed success rate: (ok+1)/(ok+fail+2) — 0.5 (neutral) with no
    history, so unproven plugins are neither boosted nor punished. The gate
    only fires on real evidence: >= MIN_CALLS_FOR_GATE calls with a rate
    under DEMOTE_BELOW."""
    ok = fail = 0
    for s in plugin.get("skills", []):
        c = counts.get(s.get("name"))
        if c:
            ok += c["ok"]
            fail += c["fail"]
    calls = ok + fail
    rate = (ok + 1) / (calls + 2)
    demoted = calls >= MIN_CALLS_FOR_GATE and rate < DEMOTE_BELOW
    return {
        "calls": calls, "ok": ok, "fail": fail,
        "success_rate": round(rate, 3),
        "demoted": demoted,
        "demote_reason": (f"{fail} fail / {calls} calls "
                          f"(rate {rate:.0%} < {DEMOTE_BELOW:.0%})"
                          if demoted else ""),
    }
```

Then in `evaluate()`, after the free/paid split and before ranking, blend effectiveness in and gate the recommendation list:

```python
    counts = call_counts()
    for pub in free_plugins:
        eff = effectiveness(pub, counts)
        pub["score"]["effectiveness"] = eff
        pub["score"]["rank_score"] = round(
            pub["score"]["breadth_score"] * eff["success_rate"], 2)
    ranked = sorted(free_plugins,
                    key=lambda p: p["score"]["rank_score"], reverse=True)
    recommended = [p for p in ranked
                   if not p["score"]["effectiveness"]["demoted"]][:top_n]
    demoted = [{"name": p["name"],
                "reason": p["score"]["effectiveness"]["demote_reason"]}
               for p in ranked if p["score"]["effectiveness"]["demoted"]]
```

and include `"demoted": demoted` in the returned payload alongside the existing keys, using `recommended` where the old code sliced `ranked[:top_n]`. Keep the rest of the payload shape unchanged so the dashboard's existing ranking view keeps painting (it reads names/scores; the `demoted` list is additive).

- [ ] **Step 3: Run, expect PASS, commit**

```bash
python -m pytest tests/test_plugins.py -q
git add src/mindhive/plugins.py tests/test_plugins.py
git commit -m "feat(plugins): effectiveness-weighted ranking with demotion gate"
```

---

### Task 4: Full suite + README

- [ ] **Step 1:** `python -m pytest -q` — expected: PASS.
- [ ] **Step 2:** In `README.md`'s skills/plugins section, add:

```markdown
- **Effectiveness-aware ranking.** Skill invocations are captured live (and
  back-filled from transcripts), so recommendations blend coverage with a
  measured success rate. A skill with ≥3 calls and <40 % success is demoted:
  listed with its failure reason instead of recommended. No history = neutral
  (unproven skills are neither boosted nor punished).
```

- [ ] **Step 3:**

```bash
git add README.md
git commit -m "docs(readme): effectiveness-aware skill ranking"
```

---

## Deferred to Phase 2 (blocked on Phase-1 data)

These need weeks of captured usage before they can be tuned honestly — shipping
them now would rank on noise:

1. **Token efficiency in the ranking** — requires capturing per-invocation
   usage (the PostToolUse payload has no token counts; the transcript's
   assistant-line `usage` block is the source; attach to `invoke_result`
   payloads at harvest time, then add a cost term to `rank_score`).
2. **Outcome buckets (failure / success / no-intervention / oversight)** —
   needs a classifier over what happened *after* the invocation (did the user
   correct it? did it produce tool actions at all?). Design this against real
   Phase-1 histories.
3. **Dynamic model selection for reflection** — `claude_cli.py` pins
   `MODEL="claude-sonnet-4-6"`; per-item complexity routing (haiku for
   shallow verdicts, sonnet for deep ones) should be driven by measured
   verdict-quality differences, which Phase 1's data makes measurable.
4. **Category alignment** — `reflect.py`'s failure categories
   (architecture/syntax/git_merge/engine_logic/dependency/tool_use) stay as
   the *incident* taxonomy; the outcome buckets above are an orthogonal
   *skill-outcome* taxonomy, not a replacement.
