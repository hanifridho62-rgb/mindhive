# PRD — The agent process layer (`MindHive OS`)

> Status: **draft v1 — design only, no implementation.**
> Scope: structural persistence, autonomous agent orchestration, adaptive
> policy, and UI componentization for the multi-agent surface
> (MindParallel / MindSwarm).
> Evidence base: `docs/plans/2026-07-22-os-architecture-consolidated-state.md`
> — 64 recovered findings (7 dimensions) + 5 first-hand findings for the
> `oversee-enrich` dimension whose audit agent died on a usage limit.
> **Verification status: the 6 load-bearing claims were verified against
> code and the live ledger on 2026-07-22 — see §9.** Result: **3 confirmed,
> 1 refuted, 2 qualified**, plus one new empirically-measured defect
> (§1.9) that outranks most of the original findings. The other 58
> recovered findings remain unverified single-agent assertions.
> Locked decisions (user, 2026-07-22): budget basis = **user-set session
> ceiling**; orchestrator = **new planner module, `enrich.py` stays pure**.
> Standing constraints (CLAUDE.md + SOPs): imitate the design, don't copy;
> never overwrite existing MindHive code paths; stdlib-only; test-first;
> every `src/`+`tests/` change-set gets `hive log`; land green; never touch
> the live dashboard's process or repoint it via PYTHONPATH.

---

## 0. The idea this PRD continues

The user's ask was to make MindHive "a world-class, production-grade
operating system" that moves "from a static, manual architecture to a
dynamic, autonomous, and persistent one," taking ECC's concept of a mature
OS and **imitating how it works rather than copying it.**

Studying ECC (`C:/Users/hanif/ECC`) shows a system organized as four
separated OS layers. MindHive already has an equivalent for **all four** —
and in several respects a more mature one, because MindHive's are
event-sourced rather than static markdown:

| ECC layer | Purpose | MindHive's existing analogue |
|---|---|---|
| `SOUL.md` / `RULES.md` | Identity + governance kernel | **SOP subsystem** — event-sourced, dynamic fold |
| `WORKING-CONTEXT.md` | Live working state | **STATE.md / MEMORY.md** via `projector` |
| `agents/`, `skills/`, `manifests/` | Capability registry + contracts | **`plugins.py` / `recommend.py`** — health-aware, measured provenance |
| `hooks/` | Deterministic enforcement | **`enforce.py`** — PreToolUse denies minted from observed failures |

**So the thing to imitate is not a file layout — it is the separation of
concerns.** MindHive must not grow a `SOUL.md`; it already has that, better,
as events.

What MindHive lacks is the layer ECC never needed to solve, because ECC does
not run a persistent fleet:

> A real OS has a **process table** (what is running), a **scheduler** (what
> runs next and why), **resource accounting** (what each process consumes
> against a quota), and a **journaling filesystem** (state survives a crash).
>
> MindHive's multi-agent surface has **none of these four.**

| OS primitive | MindHive today | Evidence |
|---|---|---|
| Process table | `Manager.agents` — an in-memory dict that dies with the process | `workspace.py:471`, `:953` |
| Scheduler | A human clicking a button | no auto-pull loop exists |
| Resource accounting | `a.last_cost` assigned, then discarded | `workspace.py:658-659` |
| Journal | Exactly two ledger writes per run | `workspace.py:607`, `:671-682` |

This PRD builds that process layer. The user's five phases map onto it
exactly, and **the dependency order is strict**: you cannot schedule what you
cannot account for, and you cannot learn from what you never recorded.

---

## 1. Current state — evidence

### 1.1 The run lifecycle touches the ledger exactly twice

```
send()      → append user_prompt                          workspace.py:607
  … entire run streams into Agent.lines (memory only)     workspace.py:437,458-461
_reader()   → one terminal event, three-way branch:       workspace.py:666-682
     status=="stopped"  → pass            (NOTHING)              :669-670
     code==0 && saw_result → assistant_response                  :671-675
     else                → tool_failed                           :676-682
```

`Agent.lines` is capped at `MAX_LINES = 4000` and is polled by the dashboard
via `output()`, but is **never appended to the ledger**. A disconnect, a
timeout kill (`RUN_TIMEOUT_S`, `:611-622`), or a budget death therefore loses
every intermediate line.

### 1.2 The user's premises, corrected

| Claim | Verdict | Reality |
|---|---|---|
| Transcript stuck in `a.lines` | ✅ | `:437`, `:458-461` |
| No budget calc before pulling agents | ✅ | `spawn()`/`start_mission()` gate only on `MAX_AGENTS=16` (`:487-488`, `:705-709`); `workspace.py:34` imports only `claude_cli, events, paths` — `loop/budget.py` is never in scope |
| `MANAGER` restart loses everything | ✅ | `:953`, `__init__` `:467-472` restores nothing |
| Agents spawn only via manual clicks | ✅ | an unused `auto` execution-mode flag exists; nothing consumes it |
| "Disconnect → nothing saved to DB" | ⚠️ **overstated** | `user_prompt` + `tool_failed` **are** durable. What is lost is **the work content and its cost.** |
| "Completed but no output" | ⚠️ **two distinct paths** | (a) empty result → persisted as the literal string `"(no text)"` (`:674`); (b) **exit 0 without a `result` event → falls to `else` and is misclassified as an error** |

**A third silent path the user did not list, and the only true "nothing
saved" case:** a user-initiated stop writes no terminal event at all —
`workspace.py:669-670` is a bare `pass`, orphaning the `user_prompt`.

### 1.3 The budget asymmetry — the root of every claim

Two agent-execution paths with completely different maturity:

| | Single-agent loop (`loop/engine.py`) | Multi-agent (`workspace.py`) |
|---|---|---|
| Budget primitives | ✅ rolling window + `would_exceed` (`:129-136`) | ❌ none — 16-slot count only |
| Cost → ledger | ✅ per-iteration `loop_iteration` (`:159-171`) | ❌ in-memory, discarded |
| 429 / rate-limit detection | ✅ `driver.py:84-88` | ❌ no classification in the reader |
| Interrupt → resume | ✅ snapshot + autoresume (`:177-179`, `:229-230`) | ❌ none |

**The loop's code already solves this exact durability problem for itself** —
`engine.py:219-225` even catches `BaseException` specifically so that "an
unexpected crash must not strand the run as 'running' forever in Oversee,"
which is the same bug §1.6 finds unfixed for chat cards.

> ⚠️ **Qualifier (verified 2026-07-22):** the ledger contains **zero**
> `loop_run_start` / `loop_iteration` / `loop_run_end` / `roadmap` /
> `autoresume` events. **The autonomous loop has never actually run in this
> project.** The pattern is coherent, reviewed code — a sound design
> template — but it is *not* battle-tested infrastructure, and this PRD must
> not present it as proven. Mirror its shape; do not assume its edge cases
> are already discovered.

### 1.4 `loop/budget.py` requires **zero modification**

All five functions are **pure, over a caller-supplied `entries` list** — the
module owns no state:

```python
would_exceed(entries, now, cfg, next_tokens, next_cost) -> bool   # budget.py:39
wait_seconds(entries, now, cfg, next_tokens, next_cost) -> float  # budget.py:49
rolling_avg(entries, n) -> (tokens, cost)                         # budget.py:30
used(entries, now, window_hours) -> (tokens, cost)                # budget.py:24
BudgetConfig(window_hours, token_budget, cost_budget_usd)         # 0 = unlimited
```

`would_exceed` is **already shaped as a pre-flight reservation check** — it
asks "would adding this next thing breach the budget." `loop/engine.py`
merely happens to call it in-run. The gap is therefore *not* in the
primitives; it is exactly two missing pieces: **(a) no entry source** for
workspace runs (costs are discarded rather than persisted), and **(b) no
caller** in the spawn path.

### 1.5 The Improvements loop is structurally blind to this failure class

MindHive has a complete **capture → reflect → inject → prove → promote**
learning loop (`failures.py` → `incidents.py` lifecycle at `THRESHOLD=2` →
`reflect.py` mints prevention rules → `resolve.py` reroutes leakers →
`enforce.py` converts persistent leaks to hard PreToolUse denies).

It cannot learn from budget exhaustion or disconnects, for two reasons:

1. `budget_cap` / `api_cap` interrupts append `loop_run_end` + roadmap +
   autoresume events but **never a `tool_failed`** — there is no signal to
   key on. *(recovered finding, unverified — flagged in §9)*
2. The workspace failure signature is `workspace:<role>` (`workspace.py:681`)
   — **too coarse to learn from**, with no enforce matcher and no
   deterministic fallback.

### 1.6 Oversee actively misreports the fleet (first-hand, this session)

- **Every chat card reports `"running"`, forever.** `interactive_sessions`
  sets `"status": "running"` at card creation (`oversee.py:177`) and never
  assigns `status` again — the identifier appears **exactly once** in the
  file. A card leaves only via `session_end` or 6-hour staleness. **A crashed
  or budget-killed agent displays as "running" for up to six hours.**
- **Workspace runs land as anonymous, zero-cost cards.** `workspace._log`
  writes `session_id=a.session_id or f"ws-{a.id}"` with tags
  `["workspace", "parallel"|"swarm", role]` (`:691`). Those events are not
  `loop_*`-prefixed and the sid is not `"manual"`, so `oversee.py:171` admits
  them as `kind:"chat"`. But `origin_of` (`events.py:79-90`) recognizes
  **only** `self-reliant`, so `origin` is `None`; with no title they render
  as `f"session {sid[:12]}"` (`:243`); and `total_cost_usd: 0.0` /
  `total_tokens: 0` are set at creation (`:178-179`) and **never updated**,
  despite `a.last_cost` holding the real figure in memory.
- **`interactive_sessions` re-folds the entire ledger every refresh.**
  `ev_mod.read_all(project)` (`:157`) against a ~19,000-event ledger, on a
  2.5 s dashboard tick. O(all-history) per tick, degrading monotonically.

### 1.7 ~~Skills used inside multi-agent runs never become measurable~~ — **REFUTED**

The recovered audit rated this **high**. It is **false**, and the PRD would
have built a fix for a non-problem.

The mechanism it describes is real: `workspace.py:650-651` collects `Skill`
invocations into `a.skills_used` **in memory only**, and `workspace.py`
emits neither `invoke_result` nor `skill_used` (verified: zero occurrences).
But the conclusion does not follow, because skill telemetry has **three**
sources, not two — `plugins._skill_calls` normalizes `skill_used`
(**passive hook capture**), `invoke_result`, and `tool_failed`/`skill:*`.

Multi-agent runs reach that first source by two independent paths:

1. **Hooks.** `_profile_env` returns `dict(os.environ)` and no
   `--no-settings` flag is passed, so spawned agents load the normal hook
   chain; `hookcapture.py:153` emits `skill_used`.
2. **Transcripts.** Headless `claude -p` writes to
   `~/.claude/projects/<encoded-cwd>/*.jsonl` — exactly the directory
   `harvest.py` reads (`claude_projects_root`, `:442-444`), including a
   whole-transcript `backfill_skills` pass.

**Ledger evidence:** session `58a4554c…` is a Self-Reliant workspace agent
(26 `workspace`-tagged `assistant_response` events) carrying **2
`skill_used` events** — `systematic-debugging`, `test-driven-development` —
under the same session id, which flow into `skill_stats` with
`provenance: "passive"`.

**Residual (small) gap:** when no `claude` session id ever arrives, `_log`
falls back to `session_id=f"ws-{a.id}"` (`:691`), which has no transcript to
harvest. That is a narrow edge case, not the systemic blindness claimed.

### 1.9 The largest *measured* defect: 59% of persisted answers are truncated

Measured directly against the live ledger (22 `workspace`-tagged
`assistant_response` events, decoded as UTF-8):

```
lengths: 489, 531, 650, 673, 681, 802, 1514, 1618, 1872,
         2000 ×13          ← exactly at the cap
```

**13 of 22 (59%) sit at exactly 2000 characters**, and their tails are cut
mid-word (`"…if mos"`, `"…so I'll ai"`, `"…Tamper-before"`). The cause is
`workspace.py:674` — `(result_text or "").strip()[:2000]`.

This is the **highest-frequency real data loss in the multi-agent surface**,
it is silent, it has been happening since 2026-07-10, and it is a one-line
fix. No recovered audit dimension identified it.

*(Method note: a first measurement using PowerShell's default decoding
showed 2004–2030 and looked like a cap violation. That was mojibake —
UTF-8 read as Latin-1. Re-decoding as UTF-8 gives a clean constant 2000.)*

### 1.8 The two assets Phase 3 and Phase 5 can build on

- **`recommend.py` already does full-registry, health-aware, provenance-ranked
  skill selection.** `enrich.skill_suggestions` (`enrich.py:282-322`) calls
  `recommend.flatten_inventory(plugins.live_inventory()["inventory"])` and
  ranks measured-beats-unmeasured → completion rate → match strength,
  truncating at `SKILL_CAP = 3` (`:39`). *(The user's premise is exact.)* It
  is simply **not wired into the multi-agent feature.**
- **The separate-page-module pattern already exists.** MindParallel,
  MindSwarm and Self-Reliant are full-page modules (`workspace_page.py` 761,
  `swarm_page.py` 369, `research_page.py`) served into the SPA's iframe —
  the established precedent for new UI, and the decomposition target for the
  1780-line `dashboard_page.py`.

---

## 2. Problem statement

MindHive runs a fleet of headless `claude` subprocesses with no process
table, no scheduler, no accounting, and no journal. Concretely:

1. **Work is not durable.** A disconnect, timeout, or budget death preserves
   the *fact* of a run but discards its content and cost.
2. **Spend is unbounded and invisible.** Nothing checks cost before spawning
   up to 16 subprocesses; the cost each one reports is thrown away; and the
   dashboard view meant to show cost hardcodes it to zero.
3. **The fleet cannot survive a restart.** An in-memory singleton means a
   dashboard restart wipes every agent, transcript, session id and cost.
4. **Nothing is autonomous.** Agents spawn only on a human click, and the
   ranking engine that could choose them is not connected.
5. **The system cannot learn from any of the above**, because the failure
   class emits no signal the Improvements loop can key on.
6. **The UI misreports state** — dead agents read as "running" for six hours
   — and its largest module is 1780 lines, past the 1000-line cap.

---

## 3. Goals

- **G1 — Journal.** No run may end in a state where its produced work exists
  only in memory. Every run leaves a durable, ordered record sufficient to
  reconstruct what it did.
- **G2 — Accounting.** Every run's real cost reaches the ledger and is
  attributable to an agent, role, and mission; spend is checked against a
  user-set ceiling **before** subprocesses are launched.
- **G3 — Process table.** A dashboard restart rehydrates the fleet's state
  from the ledger rather than presenting a blank singleton.
- **G4 — Scheduler.** A task can be decomposed and dispatched to an
  appropriate set of agents without a human clicking per-agent, under the G2
  budget gate.
- **G5 — Adaptive policy.** Budget exhaustion and disconnects emit learnable
  signals that flow into the existing reflect → SOP/rule machinery.
- **G6 — Honest, modular UI.** The dashboard reports true agent state, shows
  live spend, survives refresh, and no module exceeds the 1000-line cap.

**Non-negotiable invariants** (SOP 7): the ledger stays append-only; new
event types are registered with why-comments; reads stay total (malformed
rows skipped, never raised); corrections are new events, never edits.

---

## 4. Non-goals

- **Not** rewriting `loop/engine.py` or the single-agent path — it is the
  reference implementation, not the problem.
- **Not** modifying `loop/budget.py` (§1.4 — the primitives are already
  correct and pure).
- **Not** turning `enrich.py` into an orchestrator (locked decision — it
  stays a pure composition module).
- **Not** inventing an account "remaining quota" number. None exists (§5.2);
  the ceiling is user-set and honest about being a local policy, not a
  provider fact.
- **Not** a Rust rewrite, a second control plane, or an ECC file-layout clone.
- **Not** autonomous, unreviewed mutation of governance. Phase 4 *proposes*;
  promotion follows the existing threshold lifecycle (§6.4).

---

## 5. Target architecture

### 5.1 The four layers, and where each lands

```
   ┌──────────────────────────────────────────────────────────┐
   │  SHELL          dashboard_page/* modules, Oversee,        │  WS5
   │                 burn-rate gauge, state rehydration        │
   ├──────────────────────────────────────────────────────────┤
   │  SCHEDULER      planner.py — decompose → select → gate    │  WS3
   │                 → dispatch    (consumes recommend/enrich) │
   ├──────────────────────────────────────────────────────────┤
   │  ACCOUNTING     ceiling sidecar + entry source            │  WS2
   │                 → loop/budget.would_exceed (unchanged)    │
   ├──────────────────────────────────────────────────────────┤
   │  JOURNAL        agent_checkpoint / agent_disconnect       │  WS1
   │                 → rehydration fold → process table        │
   └──────────────────────────────────────────────────────────┘
        ▲                                                  │
        │  learnable failure signals                       ▼
   ┌──────────────────────────────────────────────────────────┐
   │  POLICY KERNEL (exists)  failures → incidents → reflect   │  WS4
   │                          → sop.record / enforce           │
   └──────────────────────────────────────────────────────────┘
```

### 5.2 Budget model — a locked decision, and why the honest one

**Basis: a user-set session ceiling.** The audit's most important constraint
is that **no account remaining-quota number is available anywhere.** The only
real signals are the plan tier (`subscriptionType` via `cli_auth_status`,
`workspace.py:234-239`) and per-run `total_cost_usd` from each result
envelope (`:658`).

A "strict reservation check" cannot be strict against an unknown denominator.
So the ceiling is a **local policy the user sets**, and the system is honest
that it is exactly that:

```
BudgetConfig(window_hours, token_budget, cost_budget_usd)   # already exists
   ▲ from a ceiling sidecar (enrich.json / triggers.json precedent:
     config stays out of the append-only chain; each change appends an audit event)

entries: [BudgetEntry(ts, tokens, cost_usd), …]
   ▲ NEW: folded from persisted per-run workspace cost (WS2 fixes the discard)

next_cost estimate for a spawn:
   rolling_avg(entries_for_role, n)      # budget.py:30, already exists
   ▲ no history for this role → estimate unavailable → FAIL OPEN, record, never block
```

**Fail-open is deliberate.** A budget gate that blocks work because it has no
prior for a novel role would be worse than the status quo. The gate refuses
only when it has evidence that it would breach a ceiling the user set.

### 5.3 Process table & rehydration

`Manager.__init__` (`workspace.py:467-472`) gains a bounded rehydration fold
over recent `agent_*` events, reconstructing agent identity, status, session
id, accumulated cost, and a transcript pointer. **Subprocesses are not
re-attached** — a killed process is gone; rehydration restores the *record*
of the fleet so the UI and the ledger agree, and so a dead agent is shown as
dead rather than as a blank slate or a phantom "running" card (§1.6).

---

## 6. Technical plan

Five workstreams. **WS0 is separable and should land first** — it is small,
high-value, and independent of the rest.

### WS0 — Correctness fixes (no new architecture)

Three defects that mislead the user *today* and cost little to fix.

Ordered by **measured** impact, not by assumed severity.

| # | Defect | Location | Evidence | Fix |
|---|---|---|---|---|
| ~~**0.1**~~ ✅ **LANDED** | **59% of persisted answers truncated mid-word at 2000 chars** | `workspace.py:674` | **13 of 22 measured** (§1.9) | **Done 2026-07-22:** `_cap_result()` bounded by `RESULT_TEXT_CAP = 30000` (same order as `harvest._MAX_WORDS`) + a visible `…[truncated]` marker so truncation can never be silent again. 3 new regression tests; full suite 1675 passed. Verified no leak into STATE.md/MEMORY.md — every consumer bounds it itself (`headline()` at 140, `graph.py:121` excludes it). |
| ~~0.2~~ ⚠️ **LANDED, impact masked** | Every Oversee chat card reports `"running"` forever | `oversee.py:177` | Structural — `status` assigned once, never updated | **Done 2026-07-22:** status derived from workspace-tagged terminal events, keyed on the `workspace` tag (4 other producers write `tool_failed`); `ovStatus` surfaces it. 4 tests; suite 1679 green; live-ledger replay shows **0 ordinary sessions** wrongly re-statused. **But see 0.5/0.6 — two upstream defects hide the result on historical data.** |
| **0.5** | **A fleet run is split across two ledger sessions** | `workspace.py:691` | **14 of 14** `ws-<id>` sessions hold *only* `user_prompt`; all 22 responses + 6 failures live under real claude uuids | `_log` resolves `a.session_id or f"ws-{a.id}"`, but `a.session_id` is still `None` at `send()` — it is set later when `_reader` sees the stream's `system` init event. The prompt is orphaned under one id, the outcome lands under another |
| **0.6** | **Completed fleet runs vanish from Oversee entirely** | `oversee.py` ended-branch | **13 of 13** terminal-bearing sessions carry a harvested `session_end` | Harvest imports the subprocess's own transcript into the *same* session id; any `session_end` removes the card. The run that carries the actual outcome is deleted from the board |
| 0.3 | Workspace runs render as anonymous zero-cost sessions | `events.py:79-90`, `oversee.py:178-179` | Structural; cost discarded 29/29 runs | Teach `origin_of` the `workspace` tag; populate cost once WS2 persists it |
| 0.4 | Exit-0-without-`result` misclassified as error | `workspace.py:671-682` | **0 occurrences** in ledger history — all 6 failures were exit 1 | Harden opportunistically; **not** first-tranche work |

**Priority correction.** 0.1 was found only by measuring, and it outranks
every defect the 26-agent audit surfaced. 0.4 was ranked as a live bug in
the audit and turns out to be theoretical. This inversion is the single
strongest argument for the verification pass having been worth running.

**What a user actually sees for one fleet run today** (0.5 + 0.6 together —
both found while verifying 0.2, neither in any audit dimension):

```
 send()  ──► user_prompt      logged under  ws-3d601cf9      (a.session_id is None)
              │                                  └─► card lingers "running" 6h, no outcome
              ▼
 _reader ──► system init      a.session_id = 5c963d55-…
         ──► assistant_response / tool_failed  logged under  5c963d55-…
              │                                  └─► card REMOVED once harvest
              ▼                                      imports that session's session_end
        (harvest imports the subprocess transcript into the SAME uuid)
```

The prompt and its outcome are **two different cards**, and the one holding
the outcome is the one that gets deleted. This is a large part of why the
multi-agent surface *feels* like it loses work even though §1.2 proves the
ledger accounting is complete — **the data is durable, the view is not.**

0.5 is the root cause and should be fixed before 0.2's status work can pay
off. It is also a prerequisite for WS1's rehydration (§6, WS1): a process
table cannot be folded from a ledger where one run has two identities.

**On 0.3's sibling — the `"(no text)"` path.** The user proposed forcing the
agent to generate a synthetic summary. **Recommend against it:** that is a
second LLM call on the failure path — it costs money, adds latency, and can
itself fail or hit the very limit that caused the problem. The reader already
walks every `assistant` message's content blocks (`workspace.py:640-651`).
**Capture the last text block during the stream and use it as the fallback.**
Deterministic, free, no extra call, no new failure mode.

### WS1 — Journal (Phase 2a): incremental checkpointing

**New event types**, registered in `DETERMINISTIC_TYPES` with why-comments
per SOP 7 §6 (telemetry, following the `autoresume` / `prompt_enriched`
precedent — *not* semantic; these must stay out of the hash-chained intent
layer and the knowledge graph):

- `agent_checkpoint` — `{agent, role, mission?, seq, lines_delta, cost_so_far, turns}`
- `agent_disconnect` — `{agent, role, reason: timeout|stopped|budget|exit|stream, exit_code, cost, seq_last}`

`agent_disconnect` is what makes the failure class **learnable** (WS4): it is
machine-readable and distinguishes a budget death from a genuine tool failure,
which the current coarse `workspace:<role>` signature cannot (§1.5).

**Cadence — the design constraint the user's "every 10 s" must respect.**
A 4000-line buffer checkpointed every 10 s against a ledger that Oversee
re-folds every 2.5 s (§1.6) would degrade every projection in the system.
Therefore:

- checkpoints carry a **delta since the previous checkpoint**, not the buffer
- with a **per-checkpoint byte cap** and a monotonic `seq`
- the terminal event **supersedes** the checkpoint chain for display
- checkpoints are a **merge/compaction candidate** (SOP 7 §3 requires this
  decision explicitly) — they are recoverable scratch, not history

**Rehydration** (G3): bounded fold in `Manager.__init__`, newest-first, with
a horizon so startup cost is constant rather than O(all-history).

**The user-stop path** (`:669-670`) starts writing `agent_disconnect`
(`reason: "stopped"`), closing the only structurally-orphaning case (§1.2).

> **Verified scope check:** across all 29 historical runs the accounting is
> already complete — **22 `assistant_response` + 7 `tool_failed` = 29
> `user_prompt`**. Zero orphans, zero `"(no text)"` payloads. The user-stop
> orphan is therefore **structurally real but never observed**. Fix it for
> correctness; do not justify WS1 on it. **WS1's real justification is that
> the intermediate transcript and the cost are lost on 29/29 runs.**

### WS2 — Accounting (Phase 2b): pre-flight reservation

`loop/budget.py` is **not modified** (§1.4). Three additions:

1. **Entry source.** Persist per-run cost (currently discarded at
   `workspace.py:658-659`) so `entries` can be folded from the ledger.
2. **Ceiling sidecar.** `BudgetConfig` from a per-project settings file
   (`enrich.json` precedent); every change appends an audit event.
3. **Caller.** `Manager.spawn` / `start_mission` call `would_exceed` with a
   `rolling_avg`-derived estimate before launching, and surface
   `wait_seconds` when a window is the binding constraint. Fail-open when no
   prior exists (§5.2).

**Scope correction:** an earlier draft justified this workstream partly by
"multi-agent skill usage is invisible to `recommend.py`." **That premise was
refuted** (§1.7) — passive capture already covers it. WS2 is justified by
cost alone, which is genuinely lost on **29 of 29 runs**. Recording
`skills_used` alongside cost remains a cheap nicety (it would close the
narrow `ws-<id>`-fallback gap) but is **not** a driver and must not be used
to size this workstream.

### WS3 — Scheduler (Phase 3): the planner module

**Locked decision: a new module; `enrich.py` stays pure.** `enrich()`
(`:416-433`) is text-in/markdown-out with no task graph, agent roster, or
dependency ordering. Extending it in place would either bloat a clean pure
module past its purpose or produce a shallow imitation of orchestration.

New `planner.py`:

```
prompt → decompose into units {title, role, skills, deps}
       → select skills per unit   (recommend.py — the engine already exists)
       → cost-estimate the whole plan  (WS2 rolling_avg per role)
       → GATE: would_exceed(plan_cost)?  → refuse with a reason, or
       → dispatch via Manager.spawn, respecting deps and MAX_AGENTS
       → every stage leaves ledger evidence (SOP 1.4)
```

`enrich.skill_suggestions` is consumed as the **selection input**, unchanged.

**Decomposition is the genuinely novel piece** and the main execution risk —
nothing in the repo does it today. It must be bounded (a cap on units), and
must degrade to a single agent when decomposition is low-confidence.

### WS4 — Adaptive policy (Phase 4): the reflection loop

The machinery exists; it needs a signal and a proposer.

1. **Signal** — WS1's `agent_disconnect` gives `failures.py`/`incidents.py` a
   learnable, machine-readable signature keyed on `reason`, replacing the
   coarse `workspace:<role>`.
2. **Proposer** — a background observer reads a completed mission's
   checkpoint/disconnect trail and proposes an SOP or prevention rule.
   `reflect.py` is the working template; `sop.record()` is the single guarded
   append surface; SOPs are a deterministic fold regenerated every sync
   (`sop.py:88-141`), so a runtime proposal is *architecturally native* — no
   new storage, no static file to edit.
3. **Governance — proposals are not auto-adopted.** Autonomous mutation of
   the governance kernel is the highest-blast-radius idea in this PRD. It
   follows the existing incident lifecycle instead: **candidate at
   `THRESHOLD=2` → active → promoted/demoted/expired**. One bad run cannot
   rewrite a standing procedure.

### WS5 — Shell (Phase 5): componentization + honest UI

1. **Decompose `dashboard_page.py` (1780 → modules)** using the established
   separate-page-module precedent (§1.8). Mechanical, high-risk-of-regression
   → land it as its own change-set with the section-hook greps CLAUDE.md
   mandates (`showOversee`, `renderOversee`, …) as the acceptance check.
2. **State rehydration** — a refresh reads mid-run state from the ledger
   (WS1) rather than wiping the view.
3. **Address the O(all-history) fold** (§1.6) before adding any polling UI —
   Phase 5 will hit this wall head-on.
4. **Animation, bound to real state, never to volatile browser memory:**
   typing indicator driven by checkpoint arrival; **burn-rate bar — the
   roadmap progress bar is a ready-made component** (recovered finding);
   swarm node-graph pulse driven by agent status.

> **CLAUDE.md gate:** the live dashboard on port 54461 is never touched for
> preview. Any UI verification runs on a separate port from this worktree via
> `.claude/launch.json`, and "done" requires grepping the served page for the
> feature hook — never an HTTP 200.

---

## 7. Suggested execution order

| Order | WS | Why here |
|---|---|---|
| 1 | **WS0** | Independent, small, fixes active misreporting today |
| 2 | **WS1** | Load-bearing for everything; nothing else is safe without a journal |
| 3 | **WS2** | Needs WS1's persistence surface; unblocks the gate |
| 4 | **WS4 (signal half)** | Cheap once WS1 emits `agent_disconnect`; closes the learning gap early |
| 5 | **WS3** | Must not dispatch autonomously before the gate (WS2) exists |
| 6 | **WS5** | Consumes WS1/WS2 state; decomposition lands as its own change-set |
| 7 | **WS4 (proposer half)** | Needs real mission trails to learn from |

Each workstream: test-first, full suite green, one `hive log <category>`
change-set entry, no `src/` change called done before the real flow's own
output is read (SOP 6.5).

---

## 8. Risks & mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| ~~Findings are unverified~~ — **retired**: the 6 load-bearing claims were verified (§9) | — | 3 confirmed, 1 refuted, 2 qualified; the plan was corrected accordingly |
| **The other 58 findings remain unverified** and one of the 6 checked was outright false (§1.7) — base rate suggests more are wrong | **Medium** | Do not let an unverified recovered finding *justify a workstream* on its own; measure before sizing, as §1.9 and §9 did |
| Checkpoint volume degrades every projection | High | Delta-only + byte cap + supersession + compaction policy (WS1); measure against the live 19k-event ledger before shipping |
| Budget gate wrongly refuses novel work | Medium | Fail-open without a prior (§5.2); refuse only against evidence |
| `dashboard_page.py` split regresses a section invisibly | High | Own change-set; served-page hook greps as acceptance (CLAUDE.md precedent — this exact class of regression already made Oversee vanish once) |
| Decomposition (WS3) produces bad plans | Medium | Cap units; degrade to single-agent on low confidence; the WS2 gate bounds the blast radius |
| Autonomous SOP mutation corrupts governance | **High** | Propose-only + `THRESHOLD=2` candidate lifecycle (§6.4 WS4.3) |
| New event types rejected at append | Low | `append_event` raises on unknown types (`events.py:113-114`) — registration is step one of WS1, with why-comments |
| Rehydration resurrects phantom agents | Medium | Restore the *record*, never re-attach subprocesses (§5.3) |

---

## 9. Verification results (2026-07-22) — 3 confirmed, 1 refuted, 2 qualified

Each of the six load-bearing claims was checked against source and, where
the claim was about behavior, against the live ledger (`~/.mindhive/mindhive/
events`, 327 session files).

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Budget/api interrupts emit no `tool_failed` — **WS4's premise** | ✅ **Confirmed** | **Zero** `tool_failed` anywhere in `loop/`. `engine.py` appends only `roadmap`, `loop_run_start`, `loop_iteration`, `compaction`, `loop_run_end`, `loop_run_dismissed` |
| 2 | `workspace.py` has no budget path — **WS2's premise** | ✅ **Confirmed** | `:34` imports only `claude_cli, events, paths`; **zero** occurrences of "budget" in 1034 lines |
| 3 | `MANAGER` restores nothing — **WS1/G3's premise** | ✅ **Confirmed** | `MANAGER = Manager()` at `:953`; `__init__` sets `agents = {}`; zero `rehydrate`/`restore`/`read_all` in the file |
| 4 | Multi-agent skills never reach the registry | ❌ **REFUTED** | `_skill_calls` has 3 sources incl. passive `skill_used`; agents inherit hooks + write harvested transcripts; ledger shows a workspace agent with 2 `skill_used` events (§1.7) |
| 5 | Exit-0-without-`result` reachable in practice | ⚠️ **Unsupported** | Structurally reachable, but **0 occurrences** — all 6 historical failures were `exit 1`; also 0 `"(no text)"`, 0 orphaned stops |
| 6 | The loop's interruption pattern is reusable | ⚠️ **Qualified** | Code is present and coherent (`engine.py:219-233`), **but the ledger holds zero loop events — the loop has never run here.** Template, not proven infrastructure |

**Measured baseline established while verifying** (all first-hand):

| Metric | Value |
|---|---|
| Workspace runs in ledger history | **29** (`user_prompt`, workspace-tagged) |
| Terminal-event accounting | **22 done + 7 failed = 29** — complete, no orphans |
| Responses truncated at exactly 2000 chars | **13 of 22 (59%)** ← §1.9, largest measured defect |
| Runs whose cost reached the ledger | **0 of 29 (100% loss)** ← WS2's real justification |
| `"(no text)"` payloads | **0** |
| Loop runs ever executed | **0** |

**Net effect on the plan:** one workstream justification removed (WS2's
skills rationale), two "live bugs" demoted to theoretical (0.4, user-stop),
one unranked defect promoted to first priority (0.1, the 2000-char cap), and
one architectural assumption downgraded from "proven" to "template" (§1.3).
**WS4's premise — the highest-severity recovered finding — survived intact.**

### Design questions (defaults chosen so execution never blocks)

| # | Question | Default |
|---|---|---|
| Q1 | Checkpoint cadence — turns, seconds, or bytes? | Bytes-and-seq with a seconds ceiling; measured against the live ledger before locking |
| Q2 | Do checkpoints carry content or pointers? | Content, delta-only and capped — pointers into a volatile buffer are worthless after a crash |
| Q3 | ~~Route multi-agent skill usage through the invoke channel?~~ | **Moot** — passive capture already covers it (§1.7 refuted). Do nothing beyond the narrow `ws-<id>` fallback gap |
| Q7 | What should the new response cap be (§1.9)? | Store the full result; truncate at display time. If a cap is required, size it from the measured distribution, not a round number |
| Q4 | Is the ceiling per-session, per-mission, or both? | Per-session, with a per-mission override |
| Q5 | Does the planner ask before dispatching? | Yes on first use; a persisted opt-out mirrors `enrich.py`'s default-on toggle |
| Q6 | Rehydration horizon? | Bounded by event count, not wall-clock, so startup cost is constant |

---

## Appendix A — recovered-audit provenance

| Item | Value |
|---|---|
| Workflow | `wf_a14769d9-287`, 26 agents |
| Raw artifact | `…/tasks/wv4ln0mr3.output` (137 KB) → scratchpad `recovered-audit.json` |
| Counters | `{dimensions: 8, total_findings: 64, critical_checked: 18, confirmed: 0, refuted: 0}` |
| Died | `audit:oversee-enrich` + all 18 verify agents — `"You've hit your session limit · resets 6:30pm (Asia/Jakarta)"` |
| Gap-filled | §1.6, §1.8 first-hand from `oversee.py` (253) and `enrich.py` (484) this session |

## Appendix B — file inventory

| File | Lines | Role in this PRD |
|---|---|---|
| `workspace.py` | 1034 | WS0/WS1/WS2 primary target |
| `dashboard_page.py` | **1780** | WS5 decomposition target (cap: 1000) |
| `dashboard.py` | 1335 | over cap; not in scope this round |
| `workspace_page.py` | 761 | WS5 animation surface |
| `enrich.py` | 484 | **unchanged** — consumed by WS3 |
| `swarm_page.py` | 369 | WS5 node-graph surface |
| `oversee.py` | 253 | WS0.1/WS0.2 target |
| `events.py` | 245 | WS1 type registration; WS0.2 `origin_of` |
| `sop.py` | 249 | WS4 proposal surface |
| `loop/budget.py` | 68 | **unchanged** — called by WS2 |
