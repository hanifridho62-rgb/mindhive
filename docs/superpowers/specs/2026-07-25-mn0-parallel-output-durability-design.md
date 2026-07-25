# MN-0 — Parallel output durability

> Status: **specced; awaiting operator review before a plan is written.**
> Slice 0 of 6 in `2026-07-25-mindnetwork-architecture-decomposition.md`.
> Sequenced first by operator decision: it is the precondition for R14
> (forced parallelism), and it pays for itself even if nothing else ships.
> Every defect below was **read directly in the code on 2026-07-25**, not
> inferred from a report. Line numbers are against
> `C:/Users/hanif/mindhive` at that date.
> Standing constraints: stdlib-only; test-first; `hive log` the change-set;
> land green; append-only ledger.

---

## 1. The problem, stated honestly

The operator reports that parallel work in this environment "stops midway, or
even finishes without showing the output that was worked on."

**The `docs/superpowers/specs/2026-07-09-parallel-orchestration-brief.md` does
not document these failure modes.** It names different sharp edges (the Windows
`.cmd`-shim spawn bug, cross-process write locks, budget/breaker interplay) and
leaves failure semantics explicitly undecided. So the claim is *not* corroborated
by the design docs.

It **is** corroborated by the code. Reading the two paths that actually run
parallel work found four independent mechanisms that discard produced output,
and each maps onto one of the reported symptoms. The operator's account is
accurate; the documentation simply never caught up with it.

This matters beyond tidiness. Every discarded output is tokens already paid for,
and a run whose evidence vanished cannot be audited, resumed, or learned from —
which silently starves the failure data that MN-3's method memory and the
prevention-rule loop both depend on.

---

## 2. Verified defects

### D1 — Loop path: only a single regex-captured line survives

`loop/engine.py:152` parses the reply, `:159-171` persists the iteration:

```python
status = parse_status(result.text)
...
events.append_event("loop_iteration", {..., "summary": status.summary or "", ...})
```

`DriverResult.text` (`loop/driver.py:28`) holds the agent's **entire** reply.
`parse_status` reduces it to one line via
`_SUMMARY_RE = re.compile(r"summary:\s*(.+)", re.IGNORECASE)`
(`loop/contract.py:10`), taking the *last* match (`_last`, `:50-58`) and
returning `None` when absent (`:77`).

So the persisted payload is `status.summary or ""`. **If the model never emits a
`summary:` line, the ledger records an empty string** and the full worked output
is written nowhere — it survives only inside the Claude CLI transcript, which
nothing in MindHive reads for loop runs.

This is the exact shape of "finished but the output isn't shown."

### D2 — Swarm path, success: truncated to 2000 characters, and volatile

`workspace.py:671-675`:

```python
elif code == 0 and saw_result:
    a.status = "done"
    self._log(a, "assistant_response",
              {"text": (result_text or "").strip()[:2000] or "(no text)", ...})
```

The durable record is capped at 2000 chars. The *live* view the dashboard polls
is `a.lines` — a plain in-memory list trimmed from the front at
`MAX_LINES = 4000` (`workspace.py:43`, `emit` at `:458-461`):

```python
def emit(self, line: str) -> None:
    self.lines.append(line)
    if len(self.lines) > MAX_LINES:
        del self.lines[:len(self.lines) - MAX_LINES]
```

Two consequences. A long run loses its **beginning** from the live view while
the operator is watching. And because the buffer is in-memory, a dashboard
restart erases the full output of even a perfectly successful run, leaving only
the 2000-char excerpt.

`output()` additionally clamps a stale cursor — `since = min(since, len(a.lines))`
(`workspace.py:543-545`). The comment says "clamp so a stale cursor still
works," and it does keep polling alive, but a poller that falls behind by more
than 4000 lines silently resumes at the wrong offset. The gap is never reported.

### D3 — Swarm path, failure: the output is discarded entirely *(the worst one)*

`workspace.py:676-682`:

```python
else:
    a.status = "error"
    a.emit(f"✖ run failed (exit {code})")
    self._log(a, "tool_failed",
              {"text": f"workspace run failed (exit {code})",
               "signature": f"workspace:{a.role}",
               "action": f"workspace agent {a.name} run"})
```

On a non-zero exit **or** a missing `result` event, the accumulated
`result_text` is never persisted. Only a bare `tool_failed` naming the exit code
is written. Worse, `result_text` and `a.last_result` are assigned **only** inside
the `if obj.get("type") == "result"` branch (`workspace.py:652-655`), so an agent
killed mid-run has produced streamed assistant text that was never captured into
`result_text` at all.

A run that did real work and then died loses **100% of that work's record**.
This is "stops midway" — and it explains why the output vanishes rather than
appearing partially.

### D4 — Timeout orphans the agent, and the orphan kills the next turn

`research.py:742-754` documents the cascade in its own comment: a turn crossing
its wall-clock budget leaves the underlying run in flight; abandoning the worker
thread orphans the workspace agent with `status` stuck at `"running"`; the next
turn's `send()` then rejects with "… is busy", which the loop treats as terminal,
"and the whole run dies."

`ResearchController` works around this for itself. The workaround lives in the
controller, **not** in `Manager`, so the swarm and MindParallel paths — the ones
the operator uses — do not have it.

Partial mitigations that do exist and must not be duplicated: `_timeout_kill` at
`RUN_TIMEOUT_S` (default 1800 s, env-overridable via `MINDHIVE_WS_TIMEOUT`,
`workspace.py:44`, `616-622`) prevents an indefinitely hung child;
`engine.py`'s `except BaseException` writes `loop_run_end` with
`reason="interrupted"` so a crash cannot strand a run as `running` forever in
Oversee; `board_dispatch` rolls a card back to backlog on a failed send.

---

## 3. Design

One principle: **output durability is a persistence problem, not a display
problem.** Make produced text survive every exit path first; showing it is then
trivial and belongs to MN-5.

### 3.1 A run-artifact store (new)

Full agent output goes to a per-run text artifact, not into the ledger:

```
~/.mindhive/<project>/runs/<run_key>.txt
```

`run_key` is `ws-<agent_id>-<n>` for workspace runs and `loop-<run_id>-<iter>`
for loop iterations — deterministic, collision-free, and greppable. `<n>` is a
monotonic per-agent run counter (an `Agent` may be sent many prompts over its
life, and each `send()` is one run and one artifact); `<iter>` is the loop's
existing iteration number.

**Why not the ledger.** Events are canonical JSON on a hash chain, feeding
projections, briefings, recall, and the graph. Multi-kilobyte transcripts there
would violate SOP 8.2 ("never paste raw tool output into decision/caveat text")
and blow the context budgets `context.py` exists to police. The event keeps a
bounded excerpt plus the artifact path; the artifact keeps everything.

**Artifacts are evidence, not projections.** They are explicitly *not*
rebuildable from the ledger — the same status `enrich.json` and
`context_budget.json` already hold ("config, not a projection"). Losing an
artifact loses evidence and breaks nothing: every consumer degrades to the
excerpt, the way `taskview.detail()` degrades to
`{"error": "transcript_unavailable"}` rather than raising.

Written via `paths.write_text_atomic` (tmp sibling + `os.replace`), so a
concurrent reader never sees a half-written artifact — the invariant that
matters most when 16 agents finish at once.

**Bounded, with explicit numbers.** Per-artifact cap **1 MiB**; on hitting it the
artifact keeps the head, and records the loss as a literal marker line
(`--- truncated: N bytes dropped ---`) inside the file rather than silently
stopping. The in-event excerpt stays at **2000 chars**, matching the cap
`workspace.py:674` already uses, so no existing consumer changes shape. Total
retention is pruned by the existing opt-in `hive gc` (never automatically —
deleting the operator's only evidence must be a deliberate act).

### 3.2 Capture output as it streams, not only at the result event

The root cause of D3 is that `result_text` is populated exclusively from the
terminal `result` event. The fix inverts it: accumulate assistant text blocks
into the artifact **as they arrive** in `_reader`'s stream loop, so whatever was
produced before a death is already on disk.

This also fixes D2's volatility for free: the artifact is complete regardless of
the 4000-line in-memory ring, and survives a dashboard restart.

### 3.3 Persist on every exit path

`_reader`'s three branches become symmetric — each finalises the artifact and
records a durable pointer:

| exit path | today | after MN-0 |
|---|---|---|
| clean + `saw_result` | `assistant_response`, text ≤2000 | unchanged event shape + `artifact` pointer; artifact holds full text |
| non-zero exit / no `result` | `tool_failed` only, **output dropped** | `tool_failed` **plus** a partial `assistant_response` carrying the excerpt + `artifact` pointer |
| user stop (`status == "stopped"`) | nothing recorded | partial `assistant_response` + `artifact` pointer |

### 3.4 No new event types

Deliberate. MN-0 extends two existing payloads instead:

- `assistant_response` gains `artifact` (str path) and `partial` (bool).
  Semantically correct — a partial reply is still an assistant response — and
  the type already sits in `MEMORY_VIEW_TYPES` while staying out of the LEDGER,
  hash-chained intent layer, and knowledge graph, which is exactly right for
  bulky run output.
- `loop_iteration` gains `artifact` (str path).

Adding a field is materially safer than adding a type: no
`events.VALID_TYPES` change, no `consolidate`/`dedupe` exemption decision, no
projection wiring, no new `validate_payload` surface. Readers already use
`.get()`, and the totality invariant means historical events lacking the field
must keep folding — which the tests will assert directly rather than assume.

### 3.5 Settle-before-send, generalised (D4)

The orphan guard moves from `ResearchController` into `Manager`, so every caller
inherits it: on timeout or abandonment, actively stop the in-flight run and mark
the agent settled before any subsequent `send()`. `send()` additionally reports a
busy agent as a **structured, retryable** condition rather than a bare string, so
callers can reroute instead of treating it as terminal (SOP 2.1: failures return
`isError`/`errorCategory`/`isRetryable`, never an unstructured string).

### 3.6 Reporting the gap instead of hiding it

`output()`'s cursor clamp keeps working, but when it clamps it also returns a
`dropped` count so a falling-behind poller can say "N lines lost" instead of
silently renumbering. This is the same principle as the architecture doc's
invariant 7 and the workflow rule against silent caps: a bounded view must
announce its bound.

---

## 4. Module boundaries

- `workspace.py` — `_reader`, `output`, `send`, and the new settle guard. Already
  1034 lines; the artifact mechanics do **not** go here.
- **`runlog.py` (new, small)** — the artifact store, sole owner of the `runs/`
  directory: key construction, atomic append/finalise, byte cap and truncation
  marker, excerpt extraction, read-back, and GC enumeration. One purpose,
  testable with no subprocess, no threads, and no dashboard.
- `loop/engine.py` — one call to finalise the iteration artifact and one added
  payload field. No behavioural change to the exit gate, breaker, or budget.
- `paths.py` — one helper for the `runs/` directory, matching the existing
  `events_dir` shape.

Nothing is added to `dashboard_page.py` (1780 lines, past the thermo-nuclear
threshold). Surfacing artifacts in the UI is MN-5.

---

## 5. Testing

New `tests/test_runlog.py`, plus additions to `tests/test_workspace.py` and the
loop engine tests. The discriminating cases — each must fail before the fix and
pass after:

1. **D3 regression.** A stream that emits assistant text and then exits non-zero
   with no `result` event: the artifact contains the produced text and a partial
   `assistant_response` carries the pointer. Today this asserts against total
   loss.
2. **D1 regression.** A loop reply with **no** `summary:` line: `loop_iteration`
   still records `summary: ""` (unchanged, deliberately) *and* the artifact holds
   the full reply text.
3. **D2 truncation.** Output exceeding both the 2000-char excerpt and the
   4000-line ring: the artifact is complete; the excerpt is bounded; the
   truncation marker is present when the byte cap is hit.
4. **Restart durability.** Artifact readable after the `Manager` instance is
   discarded — the property the in-memory ring cannot provide.
5. **Fold totality (canary).** A synthetic historical `loop_iteration` /
   `assistant_response` **without** `artifact` or `partial` still folds, projects,
   and renders. Guards the payload extension.
6. **Atomicity under concurrency.** Concurrent finalises never yield a partial
   read — mirrors the `_BOARD_LOCK` rationale that two runs finishing at once
   must not drop each other's write.
7. **D4 settle guard.** After a simulated timeout the agent is settled and a
   subsequent `send()` succeeds; a genuinely busy agent returns a *structured
   retryable* error, not a bare string.
8. **Cursor honesty.** A cursor stale by more than `MAX_LINES` reports a nonzero
   `dropped` count.

Worktree note: subprocess-based tests need `PYTHONPATH=<worktree>/src`
explicitly — the editable install otherwise resolves `import mindhive` to the
main checkout (recorded lesson; the root `conftest.py` only fixes in-process
imports).

---

## 6. Non-goals

- **No new parallelism.** MN-0 makes existing parallel output trustworthy; it
  does not add spawning, fan-out, or lane planning. That is MN-2, and it stays
  bound by KTD6 ("Python plans; the agent executes").
- **No UI.** No cats, no new panel, no `dashboard_page.py` edits. MN-5.
- **No new event types**, no ledger schema change (§3.4).
- **No change to the loop exit gate, circuit breaker, or budget logic** — that
  machinery is sound and MN-2 reuses it as-is.
- **No retroactive recovery** of output already lost to past runs.

---

## 7. Risks

1. **Disk growth.** 16 agents × long runs × uncapped artifacts would grow
   without bound. Mitigated by the per-artifact byte cap and `hive gc`
   enumeration; deliberately *not* mitigated by automatic deletion.
2. **Write amplification in the stream loop.** Appending per streamed block adds
   I/O to a hot path. Mitigated by buffered writes with a single atomic finalise,
   and measured before merge — an unmeasured latency claim would violate
   invariant 7.
3. **Payload extension drift.** If a future reader assumes `artifact` is always
   present, historical events break. Mitigated by test 5 as a standing canary.
4. **`MINDHIVE_WS_TIMEOUT` interaction.** The settle guard must not fight
   `_timeout_kill`; ownership of the kill path must be single and explicit.

---

## 8. Acceptance criteria

MN-0 is done when all of the following hold, each verified by reading the real
output rather than a proxy:

1. Every one of D1–D4 has a test that fails before the fix and passes after.
2. No agent output is lost on any exit path: clean, non-zero, missing `result`,
   user stop, or timeout.
3. Output produced by a run survives a dashboard restart and is retrievable
   from disk.
4. The full `pytest` suite is green (78 files today; no regressions).
5. The stream-loop latency cost is **measured** and stated, not estimated.
6. `hive log <category> "<what changed and why>"` is recorded — mandatory for any
   `src/mindhive/` or `tests/` change-set.
7. No new dependency; `pyproject.toml` `dependencies` stays `[]`.
