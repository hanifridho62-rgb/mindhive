# hive loop — Autonomous Claude Code loop with MindHive memory

**Date:** 2026-06-24
**Status:** Design (pending implementation plan)
**Author:** Hanif + Claude

## 1. Summary

Port the [Ralph](https://github.com/frankbria/ralph-claude-code) autonomous-loop
technique into MindHive as a native Python subsystem, exposed as `hive loop`.
The loop drives the **headless Claude Code CLI** (`claude -p`) iteratively until a
task is complete, with MindHive serving as its **long-term memory**: every
iteration is seeded from MindHive's deterministic briefing and writes its
outcome back to the event ledger. Safeguards prevent infinite loops and API
overuse using **real token/cost accounting and context-window awareness**, not
hardcoded call limits.

### Decisions locked during brainstorming

1. **Port into MindHive (Python, native Windows)** — one codebase, reuse the
   existing event store, recall, briefing, and hooks. Ralph's Bash does not run
   cleanly on the target Windows/PowerShell environment.
2. **Single autonomous loop first** — `hive loop` runs one task to completion in
   the current project. Queue/parallel is explicitly deferred (YAGNI).
3. **Headless Claude Code CLI** — each iteration shells out to `claude -p`, so
   Claude keeps its full tool harness (Edit/Write/Bash/etc.). A preflight check
   verifies the CLI is on PATH.
4. **Fully bidirectional memory** — context flows *in* (briefing + recall seed
   each iteration) and *out* (loop events appended to the ledger), so learning
   compounds across iterations, runs, and sessions.

### Non-goals (deferred)

- Task queue / GitHub-issue batch processing (Ralph's `ralph-queue`).
- Multiple parallel loops + multi-loop dashboard.
- Docker / E2B sandboxes.
- A direct Anthropic-API driver (the driver interface leaves room, but only the
  CLI driver ships).

## 2. Architecture

A new `loop` package under `src/mindhive/`, plus two accounting modules. Each
module has one responsibility and a clean interface; `engine.py` is the only
component that wires them together, so every other module is unit-testable in
isolation with a fake driver and canned usage numbers.

```
src/mindhive/
  loop/
    __init__.py
    engine.py      # orchestrates one run; owns the iteration cycle
    driver.py      # headless Claude Code invocation + JSON parsing
    contract.py    # parse LOOP_STATUS block: completion signals + EXIT_SIGNAL
    prompt.py      # build each iteration's prompt + appended loop context
    task.py        # load/parse .hive-loop/ task files
    config.py      # loop.toml loading + defaults
    state.py       # in-run state: iteration count, progress, breaker, session id
    safeguards.py  # circuit breaker + dual-condition exit gate
    budget.py      # rolling-window token/cost accounting (pure functions)
    context.py     # context-window fill tracking + smart-zone reset decision
```

Integration points with existing MindHive modules (reused, not duplicated):

- `events.py` — append loop events (`loop_run_start`, `loop_iteration`,
  `loop_run_end`) and reuse `decision` / `experiment_result` / `file_touched` /
  `compaction`.
- `briefing.py` / `projector.py` — produce the compact context seeded into each
  iteration; re-projected after each iteration so the next cycle sees the latest.
- `recall.py` — task-scoped recall (grep + optional semantic) added to prompts.
- `guard.py` — honor the existing `~/.mindhive/LIVE_LOCK` (V17 trading guard):
  refuse to start a loop while locked.

### The one rejected alternative

Making live run-state itself event-sourced (every tick reconstructed from
events) was rejected: it couples the hot loop to projection rebuilds each
iteration. Instead, live state stays in memory (`state.py`) and **milestone
events are appended**; state can be reconstructed from events after the fact for
`hive loop status`, but the running loop does not pay that cost per tick.

## 3. Loop lifecycle

```
preflight → ( build → drive → parse → assess progress → record →
              exit gate → safeguard check → context check )* → finalize
```

### 3.1 Preflight (no API cost)

1. Verify `claude` is on PATH; if missing, fail fast with an install hint.
2. Check `~/.mindhive/LIVE_LOCK`; refuse if present (override `--force`).
3. Load `loop.toml` + task files from `.hive-loop/`.
4. Append `loop_run_start` event (config snapshot, goal, timestamp).
5. Initialize `state.py` (iteration 0, breaker CLOSED, fresh session id slot).

### 3.2 Each iteration

1. **Build prompt** (`prompt.py`):
   - **`-p <prompt>`** — task-focused: the current `fix_plan.md` priorities +
     `PROMPT.md` goal + a `recall.py` query on the active task.
   - **`--append-system-prompt <loop_context>`** — MindHive briefing + STATE
     summary + the standing **contract instruction** (§3.4). Keeping loop
     context in the system prompt (not the task prompt) matches Ralph and keeps
     the task prompt clean.
2. **Drive** (`driver.py`): build argv and run:
   ```
   claude [--model M] [--effort E] --output-format json \
          --allowedTools <list> \
          [--resume <session_id>] \
          --append-system-prompt <loop_context> \
          -p <prompt>
   ```
   - Uses **`--resume <session_id>`**, never `--continue` (which can hijack the
     most-recent session in the directory). Session id is captured from the
     first iteration's JSON result and reused while in the smart zone.
   - `--live` swaps `--output-format json` → `stream-json` for real-time output.
   - Wrapped in a timeout (`iteration_timeout_minutes`).
3. **Parse** (`driver.py`): read the final JSON `result` object —
   `result` text, `usage` (`input_tokens`, `output_tokens`,
   `cache_creation_input_tokens`, `cache_read_input_tokens`),
   `total_cost_usd`, `num_turns`, `session_id`. Also scan for structural
   `"rate_limit_event"` with `status:"rejected"`.
4. **Assess progress**: did files change since last iteration? (git diff if a
   repo, else mtime scan over the project). Record a boolean `progress`.
5. **Record** (`events.py`): append a `loop_iteration` event:
   `{iteration, summary, progress, files_touched[], usage{}, cost_usd,
   context_fill_ratio, breaker_state, session_id}`. Re-project briefing.
6. **Exit gate** (§3.4 / §4.1): decide complete vs continue.
7. **Safeguard check** (§4): breaker + budget. Continue / pause / halt.
8. **Context check** (§4.3): if fill ≥ smart-zone ratio, reset session.

### 3.3 Finalize

Append `loop_run_end` with a reason:
`completed | breaker_open | budget_cap | api_cap | max_iterations | max_hours
| user_abort`. Print a summary (iterations, total cost, files changed, reason).
Refresh briefing. On `api_cap` / `budget_cap`, suggest `hive export` for a
zero-token handoff.

### 3.4 The completion contract

Every iteration's appended system prompt ends with a fixed instruction asking
Claude to emit a trailing block:

```
LOOP_STATUS:
  summary: <one line of what I did this iteration>
  progress: true|false
  STATUS: IN_PROGRESS|COMPLETE
  EXIT_SIGNAL: true|false   # true ONLY when the ENTIRE task is finished
```

Parsing precedence (`contract.py`), ported faithfully from Ralph's
`response_analyzer.sh`:

1. **Explicit `EXIT_SIGNAL` is authoritative.** If present, its value wins over
   any keyword/heuristic. `EXIT_SIGNAL: false` explicitly means "I think I'm
   basically done but keep working" → the loop continues.
2. **Fallback only when no explicit signal:** a `STATUS: COMPLETE` field infers
   completion; absent that, a confidence score from completion keywords.
3. **JSON mode suppresses keyword-only exit.** When `--output-format json`,
   keyword heuristics alone never trigger exit — only an explicit `EXIT_SIGNAL`
   (or `STATUS: COMPLETE`) can. This prevents false positives from Claude merely
   *discussing* completion.

## 4. Safeguards (real-math, smart-zone)

All thresholds are config-driven. Decisions are driven by measured tokens,
measured cost, projected burn, and real context fill — never a hardcoded number.

### 4.1 Dual-condition exit gate

The loop stops on "completed" only when **both** hold:

- **Explicit:** `EXIT_SIGNAL: true` (per §3.4 precedence), **and**
- **Semantic:** all non-optional items in `fix_plan.md` are checked off, OR ≥2
  completion signals are present. Items under a `## Future / Optional` heading
  never block exit.

Mismatch handling (matches Ralph's intent):
- `EXIT_SIGNAL: true` but required fix_plan items remain → log the mismatch,
  **continue** (Claude may be over-eager).
- High completion confidence but `EXIT_SIGNAL: false` → **continue** (respect
  the explicit "keep working").

### 4.2 Circuit breaker (`safeguards.py`)

State machine: `CLOSED → OPEN → HALF_OPEN → CLOSED|OPEN`.

- Opens on either:
  - `no_progress_threshold` consecutive iterations with no file changes
    (default 3), or
  - `same_error_threshold` consecutive identical errors (default 5).
- OPEN halts the loop and starts a `cooldown_minutes` timer (default 30).
- After cooldown → HALF_OPEN: run exactly one trial iteration. Progress →
  CLOSED; no progress → OPEN again.
- Every state transition is recorded on the `loop_iteration` event so
  `hive state` / the dashboard show breaker history.

### 4.3 Context-window smart zone (`context.py`)

Keeps every iteration in the high-attention first half of the model's context
window instead of letting the transcript bloat into the "dumb zone."

```
context_window = <per-model max>          # Opus 4.8 = 200_000; 1_000_000 if 1M beta
occupancy      = last_turn.input_tokens + last_turn.cache_read_input_tokens
fill_ratio     = occupancy / context_window
```

- `fill_ratio < smart_zone_ratio` (default 0.5) → **smart zone**: keep the
  session warm (`--resume <session_id>`), iterate normally.
- `fill_ratio ≥ smart_zone_ratio` → **context reset**: end the current Claude
  session, append a `compaction` event (reusing MindHive's compaction machinery
  and the `pre_compact` hook), drop the stored `session_id`. The next iteration
  starts a **fresh session** seeded *only* by MindHive's compact briefing/STATE
  + a task recall — not the bloated transcript.

This is the core synergy: MindHive's deterministic briefing *is* the compaction
target, so the loop can run indefinitely while each iteration stays in the
high-quality zone. The reset is observable as a `compaction` event in the graph.

### 4.4 API / usage budget (`budget.py`)

Rolling-window accounting over Claude's usage window (default 5h). `budget.py`
exposes pure functions over an in-memory time-ordered ledger of
`(timestamp, total_tokens, cost_usd)` per iteration:

```
used_tokens = Σ tokens within window      used_cost = Σ cost within window
avg_iter    = rolling mean of recent N iterations (tokens & cost)
remaining_t = window_token_budget    − used_tokens
remaining_c = window_cost_budget_usd − used_cost
```

Before each iteration the loop **projects** the next iteration's burn from
`avg_iter`:

- If `used + avg_iter` would exceed a budget → do **not** fire blindly. Compute
  the exact time until enough old entries age out of the rolling window to free
  the needed budget, then either:
  - `on_cap = "pause"` → sleep for that computed duration (with a countdown),
    then retry, or
  - `on_cap = "stop"` → finalize with `reason=budget_cap`.
- A budget of `0` means "unlimited" (no projection gating on that dimension).

**Hard API-cap backstop:** if the CLI nonetheless returns a `rate_limit_event`
`status:"rejected"` (or the 5-hour usage-cap signal), halt immediately with
`reason=api_cap`. With the projection math, this should rarely fire — it's the
last line of defense, not the primary control.

### 4.5 Hard ceilings

`max_iterations` (default 50) and `max_hours` (default 8) always bound a run
regardless of every other signal.

## 5. Config & task files

Per-project files live in `.hive-loop/` (kept separate from `.claude/` and the
MindHive store to avoid collisions).

```
.hive-loop/
  PROMPT.md        # standing goal / instructions for the loop
  fix_plan.md      # prioritized checklist; required items gate exit
  AGENT.md         # build / test / run commands Claude should use
  loop.toml        # configuration (below)
  logs/            # per-run human-readable logs (events remain source of truth)
```

`fix_plan.md` convention: top-level checklist items are required and gate exit;
anything under a `## Future / Optional` heading is non-blocking.

### loop.toml

```toml
[model]
context_window   = 200000     # 1000000 if the 1M-token beta is enabled
smart_zone_ratio = 0.5        # reset the session above this context fill
# model  = "claude-opus-4-8" # optional --model override
# effort = "high"            # optional --effort override

[budget]
window_hours           = 5
window_token_budget    = 0    # 0 = unlimited; set to your plan's window cap
window_cost_budget_usd = 0    # 0 = unlimited
on_cap                 = "pause"   # "pause" | "stop"

[loop]
max_iterations          = 50
max_hours               = 8
allowed_tools           = "Write,Read,Edit,Bash(git *),Bash(npm *)"
iteration_timeout_minutes = 15

[breaker]
no_progress_threshold = 3
same_error_threshold  = 5
cooldown_minutes      = 30
```

TOML is chosen over Ralph's shell `.ralphrc` to match MindHive's existing
`pyproject.toml` convention and avoid shell evaluation.

## 6. CLI surface

Extends the existing `hive` dispatcher (`src/mindhive/__main__.py`):

- `hive loop` — run in the current project until the task is complete.
- `hive loop init` — scaffold `.hive-loop/` with template files; run preflight
  (verify `claude` on PATH, print next steps).
- `hive loop status` — current/last run state, reconstructed from loop events.
- `hive loop --dry-run` — build the prompts and show projected budget/context
  math for the next iteration **without** calling the CLI (mirrors Ralph's
  `--dry-run`; zero API cost).
- `hive loop --live` — stream Claude's output (`stream-json`).
- `hive loop --force` — override the `LIVE_LOCK` guard (idle box only).

## 7. Events

New event type registered in `events.py`:

| Event | When | Key payload |
|-------|------|-------------|
| `loop_run_start` | preflight | goal, config snapshot |
| `loop_iteration` | end of each cycle | iteration #, summary, progress, files_touched, usage, cost_usd, context_fill_ratio, breaker_state, session_id |
| `loop_run_end` | finalize | reason, totals (iterations, cost, files) |

Reused existing types: `decision`, `experiment_result`, `file_touched`,
`compaction`. Because everything routes through the existing ledger,
`hive state`, `hive recall`, `hive graph`, and the dashboard surface loop
history with no extra work.

## 8. Testing

- **Fake driver.** `driver.py` is the sole boundary to the real CLI. Tests
  inject a fake driver returning canned JSON results (text + usage), exercising
  the engine, contract parser, budget math, breaker, and context logic at zero
  API cost — mirrors Ralph's `tests/helpers/mocks.bash`.
- **Port exit-detection cases.** Translate the scenarios in Ralph's
  `test_exit_detection.bats` (explicit `EXIT_SIGNAL` precedence, JSON-mode
  heuristic suppression, `STATUS: COMPLETE` inference) into Python unit tests.
- **Pure-function math.** `budget.py` and `context.py` are pure over usage
  numbers → table-driven unit tests (fill ratios, window aging, projection
  gating, pause-duration computation).
- **Integration.** A full fake-driven run that walks CLOSED→OPEN→HALF_OPEN, a
  smart-zone reset (verify a `compaction` event + new session id), and a clean
  dual-condition completion.
- Wire into the existing `hive selftest` / `pytest` setup. No new external deps
  beyond what MindHive already uses (stdlib `tomllib` for config on 3.11+).

## 9. Risks & mitigations

- **CLI not installed / not on PATH** (already observed on this machine) →
  preflight fails fast with an install hint; `hive loop init` checks it too.
- **Claude Code JSON schema drift** (field names in `usage`) → isolate all
  schema knowledge in `driver.py`; the rest of the system consumes a normalized
  internal usage dataclass.
- **Context-occupancy proxy imperfect** (`input_tokens + cache_read` is an
  approximation of true context size) → conservative default ratio (0.5) leaves
  wide margin; ratio is configurable; reset is cheap and idempotent.
- **Windows path / process handling** → driver uses `subprocess` with explicit
  arg lists (no shell string interpolation) and a portable timeout.

## 10. Implementation order (for the plan)

1. `config.py` + `task.py` + `hive loop init` scaffold (no API cost; testable).
2. `driver.py` + normalized usage dataclass + fake driver.
3. `contract.py` (port exit-detection precedence + tests).
4. `budget.py` + `context.py` (pure math + tests).
5. `safeguards.py` (breaker + exit gate).
6. `prompt.py` (briefing/recall/STATE assembly).
7. `engine.py` (wire the cycle; `loop_*` events).
8. `state.py` + `hive loop status`.
9. `--live`, `--dry-run`, `--force` flags.
10. Integration tests + `hive selftest` wiring + docs.
