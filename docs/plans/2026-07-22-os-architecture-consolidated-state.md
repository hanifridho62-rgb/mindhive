# MIND HIVE — Consolidated Architecture State (OS Recovery, Phase 1)

**Date:** 2026-07-22
**Branch:** `claude/os-architecture-recovery-d5ae51`
**Status:** Audit recovered + gap-filled. **No implementation code written.**

---

## 0. Provenance — what this document is built from

| Source | Status |
|---|---|
| `mindparallel-multiagent-audit` workflow (26 agents, wf_a14769d9-287) | Result JSON recovered intact from disk |
| 7 of 8 audit dimensions | **Recovered** — 64 findings with file:line evidence |
| `audit:oversee-enrich` (8th dimension) | **Died** on session limit → manually re-derived in §3 below |
| Adversarial verify phase (18 critical findings) | **Never ran** — all 18 verify agents died on the same limit |

Recovered raw artifact: `wv4ln0mr3.output` → dumped to scratchpad `recovered-audit.json` (127 KB).

> ### ⚠ Verification status: `confirmed: 0, refuted: 0` (as recovered)
> The audit's own counters read `{dimensions: 8, total_findings: 64, critical_checked: 18, confirmed: 0, refuted: 0}`.
> **Not one finding survived an adversarial check** — the verify phase never started.
> Everything in §2 is *single-agent assertion with a file:line citation*, not verified fact.
> §3 is the exception: I read those files directly this session, so those findings are first-hand.
>
> ### ✅ Follow-up verification pass, 2026-07-22
> The **6 load-bearing claims** were subsequently verified against source and
> the live ledger. Result: **3 confirmed, 1 refuted, 2 qualified**, plus one
> new measured defect that outranks most recovered findings.
> Full results and the corrected plan:
> [`docs/prds/2026-07-22-003-agent-process-layer-prd.md` §9](../prds/2026-07-22-003-agent-process-layer-prd.md).
>
> **Corrections that apply to §2 below:**
> - §2.7's **high**-severity "skills in multi-agent runs stay grey/unmeasured
>   forever" is **FALSE** — passive hook/harvest capture already records them
>   (ledger-confirmed).
> - The "complete-but-no-output" family (§2.1, §2.2, §2.3, §2.5, §2.8) is
>   **structurally real but never observed**: 0 `"(no text)"` payloads, 0
>   exit-0 misclassifications, 0 orphaned stops across all 29 runs.
> - §2.3's "the autonomous loop already solves this" is a **code template
>   only** — the ledger holds **zero** loop events; it has never run here.
> - **Unranked by every dimension, and the largest measured defect: 13 of 22
>   (59%) persisted multi-agent answers are silently truncated mid-word at
>   exactly 2000 characters** (`workspace.py:674`).
> - **§2.5's central gap (budget interrupts emit no `tool_failed`) survived
>   verification intact** — zero `tool_failed` anywhere in `loop/`.

---

## 1. Your premises vs. what the code actually says

Three of your five stated premises are correct. Two are overstated in a way that changes the design.

| Your claim | Verdict | What the code actually does |
|---|---|---|
| `_reader` only persists on `code == 0 and saw_result`; transcript stuck in `a.lines` | ✅ **Correct** | `workspace.py:671-675`. Transcript lives in `Agent.lines` (`:437`, capped `MAX_LINES=4000`), polled by the dashboard, never appended to the ledger. |
| No budget/token calc before pulling agents | ✅ **Correct** | `spawn()`/`start_mission()` gate only on `MAX_AGENTS=16` slots (`:487-488`, `:705-709`). `workspace.py` imports only `claude_cli, events, paths` (`:34`) — `loop/budget.py` is never in scope. |
| `MANAGER` restart loses everything | ✅ **Correct** | `MANAGER = Manager()` (`:953`); `__init__` (`:467-472`) restores nothing from disk. |
| Agents spawn only via manual dashboard clicks | ✅ **Correct** | No auto-pull loop exists. An unused `auto` execution-mode flag exists but nothing consumes it. |
| **"Disconnect → nothing saved to DB"** | ⚠️ **Overstated** | Four independent dimensions flag this. The ledger *does* get `user_prompt` at send (`:607`) **and** a terminal `tool_failed` marker (`:679-682`). What is lost is **the work content and its cost** — not the run's existence. |
| **"Completed but no output"** | ⚠️ **Two distinct paths** | (a) clean exit with empty result → persisted literally as the string `"(no text)"` (`:674`); (b) exit 0 *without* a `result` event → falls to the `else` branch and is **misclassified as an error**. These need different fixes. |

**Why the correction matters:** you framed Phase 2 as "guarantee zero data loss." The real defect is narrower and sharper — *the run is recorded but its content and cost are not*. And there is a **third silent path you did not list**: a user-initiated stop writes **no terminal event at all** (`:669-670` is a bare `pass`), leaving an orphaned `user_prompt` with no closing record. That is the only genuine "nothing saved" case, and it is a *stop*, not a disconnect.

---

## 2. Recovered findings — 64 across 7 dimensions

Severity as assigned by the (unverified) audit agents.

### 2.1 `workspace-lifecycle` — 9 findings
A run touches the ledger **exactly twice**: `user_prompt` at send, one terminal event at end.

- **high** — Streaming transcript (`Agent.lines`) is memory-only, never reaches the ledger
- **high** — `assistant_response` logged ONLY on `code==0 AND saw_result`; else a one-line `tool_failed` with no work content
- **high** — `MANAGER` singleton state does not survive a dashboard restart
- **high** — No periodic/checkpoint persistence during a run — exactly two ledger writes per run
- **medium** — No pre-flight session budget/token calculation before spawning
- **medium** — User-stop path logs NO terminal event at all; only the earlier `user_prompt` survives
- **medium** — Completed runs with an empty result are persisted literally as `"(no text)"`
- **low** — Disconnect/budget exit *does* write `tool_failed` — "nothing saved" is overstated
- **low** — Agent creation is manual only; the one automation adopts already-spawned agents, never pulls new ones

### 2.2 `budget` — 8 findings
**The core structural asymmetry.** Two agent-execution paths with completely different budget maturity:

| | Single-agent loop (`loop/engine.py`) | Multi-agent (`workspace.py`) |
|---|---|---|
| Budget primitives | ✅ rolling window, `would_exceed`, `wait_seconds` (`:129-136`) | ❌ none — 16-slot count only |
| Cost → ledger | ✅ per-iteration `loop_iteration` events (`:159-171`) | ❌ `a.last_cost` in memory, discarded |
| Rate-limit (429) detect | ✅ `driver.py:84-88` | ❌ no classification in the stream reader |
| Interrupt → resume | ✅ snapshot + autoresume (`:177-179, 229-230`) | ❌ none |

- **high** — No pre-flight budget/token calc before spawning multi-agents
- **high** — On disconnect a generic `tool_failed` IS saved, but the work and its cost are NOT
- **high** — Rate-limit/429 exhaustion is auto-resumed for the loop, a total blind spot for multi-agents
- **medium** ×4 — budget primitives are loop-only and in-run (not pre-spawn); disconnect yields no output; complete-but-`(no text)`; auto-pull absent
- **info** — **Hard constraint for Phase 2:** the only pre-flight signals that actually exist are the account plan (`subscriptionType` via `cli_auth_status`, `:234-239`) and per-run `total_cost_usd` from each result envelope. **There is no account "remaining quota" number available anywhere.**

### 2.3 `ledger` — 7 findings
Append contract any new writer must honor: `append_event(type, payload, session_id, tags, ts, project)` validates against `VALID_TYPES` and **raises `ValueError` on unknown types** (`events.py:113-114`); one `canonical_json` line per append; reads are **total** — `_row_to_event` drops malformed rows rather than raising (`:168-211`).

- **high** ×2 — no pre-flight budget guard on the spawn/send path; work IS lost from the ledger on disconnect
- **medium** ×3 — no final-output event for disconnected agents; complete-without-output paths; creation is user-driven only
- **info** ×2 — the append/read invariants above; **and the autonomous loop already solves this exact durability problem for itself** — a proven in-repo pattern to mirror rather than invent

### 2.4 `oversee-enrich` — **0 findings (agent died)** → re-derived in §3

### 2.5 `improvements` — 11 findings
MindHive already has a complete **capture → reflect → inject → prove → promote** learning loop (`failures.py` → `incidents.py` lifecycle at `THRESHOLD=2` → `reflect.py` mints prevention rules → `resolve.py` reroutes leakers → `enforce.py` converts persistent leaks into hard PreToolUse denies).

- **high** — **CENTRAL GAP:** `budget_cap` / `api_cap` interrupts append `loop_run_end` + roadmap + autoresume events but **never a `tool_failed`** — so the Improvements loop is *structurally blind* to budget exhaustion and disconnects. It cannot learn from the exact failure class you want fixed.
- **medium** ×4 — no multi-agent pre-flight; complete-yet-`(no text)`; manual-only; workspace failure signature `workspace:<role>` is **too coarse to learn from**, has no enforce matcher and no deterministic fallback
- **low** ×3, **info** ×3 — incl. the note that the existing `hive budget` CLI is *context-injection sizing*, **not** runtime spend (easy to conflate) and that the reflect→reroute→enforce machinery already exists and merely needs a budget signal to key on

### 2.6 `sop` — 9 findings
SOPs are an event-sourced registry: each add/update/delete is one `sop` event, the live set is a deterministic fold in timestamp order (`sop.py:88-141`), delete is a tombstone, re-add revives while preserving `created`. Delivery is passive via `projector.compose_state` → STATE.md → MEMORY.md, budgeted at `BLOCK_FULL_BUDGET=16000`.

- **key enabler for Phase 4** — nothing in the SOP layer is static: the block is regenerated from events every sync, so **a feature can propose a new SOP at runtime simply by appending a `sop` "add" event**, exactly mirroring how `reflect.py` mints prevention rules. `reflect.py` is the working template.
- **medium** ×2 — no pre-flight budget; disconnect saves only a failure marker
- **low** ×2, **info** ×5

### 2.7 `skills-plugins` — 8 findings
Two parallel registries: (1) `plugins.evaluate()` — a **top-5 plugin-level breadth ranking** with a hand-curated 8-entry notes table; (2) `plugins.live_inventory()` + `recommend.recommend()` — the **full-registry, skill-level, health-aware** ranking with measured-vs-estimated provenance and incident penalties. Per-skill health is derived from exactly two event types the invoke channel writes: `invoke_result` and `tool_failed` with signature `skill:<name>`.

- **high** — **Skills used inside multi-agent runs do NOT feed the registry.** `workspace.py` captures `skills_used` in memory only (`:650-651`) and logs coarse `workspace:<role>` outcomes. Every skill a swarm agent uses stays **grey/unmeasured forever.**
- **high** — on disconnect the multi-agent path saves only the prompt; the invoke channel by contrast *always* records an outcome
- **medium** ×4 — incl. "only a few plugins are recommended" is explained by the top-5 breadth ranking, not a bug
- **info** — an unused `auto` execution-mode flag exists; nothing consumes it
- **Key enabler for Phase 3:** the full-registry, health-aware selection you want **already exists in `recommend.py`** — it is simply not wired into the multi-agent feature.

### 2.8 `dashboard-ui` — 12 findings
SPA served by `dashboard.py` (route tables `:1055-1117`). `render_page()` in `dashboard_page.py` is **one 1780-line function** emitting all HTML/CSS/JS as a string. MindParallel/MindSwarm/Self-Reliant are **not** in that page — they are separate full-page modules (`workspace_page.py` 761, `swarm_page.py` 369, `research_page.py`) served into an iframe.

- **high** — `dashboard_page.py` is **1780 lines** (your figure is exact), past the 1000-line thermo-nuclear cap; `dashboard.py` (1335) is too
- **medium** ×5 — no pre-flight budget display; disconnect loses only the final text; complete-yet-empty; no auto-dispatch; existing checkpoint machinery is roadmap/loop-level, not agent-level
- **low** ×2, **info** ×4 — incl. two ready-made assets: **the separate-page-module pattern is the established precedent for new UI**, and **the roadmap progress bar is a ready-made budget-gauge component**

---

## 3. `oversee-enrich` — manually re-derived (first-hand, this session)

I read `oversee.py` (253 lines) and `enrich.py` (484 lines) directly. These are **not** recovered assertions; each is cited to a line I read.

### 3.1 Your premise, confirmed
> *"`enrich.py` already ranks the full skill inventory via `recommend`, caps at 3."*

✅ **Exact.** `skill_suggestions` (`enrich.py:282-322`) calls `recommend.flatten_inventory(plugins.live_inventory()["inventory"])` — the full live inventory — ranks by the provenance chain (measured beats unmeasured → higher completion rate → stronger match) and truncates at `SKILL_CAP = 3` (`:39`).

### 3.2 New finding — Oversee reports **every** chat card as `"running"`, forever

`interactive_sessions` sets `"status": "running"` at card creation (`oversee.py:177`) and **never assigns `status` again** — the string `status` appears exactly once in the whole file. A card leaves the board only via `session_end` (removed) or 6-hour staleness (`ACTIVE_WINDOW_SECONDS`).

**Consequence:** a multi-agent run that crashed, timed out, or was budget-killed writes its `tool_failed` and then **continues to display as "running" in Oversee for up to six hours.** The dashboard actively misreports dead work as live. This is the single most user-visible defect found in either the recovered audit or this pass, and no recovered dimension caught it.

### 3.3 New finding — workspace runs land in Oversee as anonymous sessions with **zero cost**

`workspace._log` writes with `session_id=a.session_id or f"ws-{a.id}"` (`workspace.py:691`) and tags `["workspace", "parallel"|"swarm", role]`. Those events are **not** `loop_*`-prefixed and the sid is not `"manual"`, so `interactive_sessions` (`oversee.py:171`) **admits them as `kind: "chat"` cards.** But:

- `origin_of` (`events.py:82-90`) recognizes **only** `self-reliant` — the `workspace`/`parallel`/`swarm` tags are unknown, so `origin` is `None` and the card is indistinguishable from a human interactive session.
- With no `user_prompt`-derived title it falls back to `f"session {sid[:12]}"` (`oversee.py:243`).
- `total_cost_usd: 0.0` and `total_tokens: 0` are set at creation (`:178-179`) and **never updated in the chat fold** — even though `a.last_cost` holds the real figure in memory.

**Consequence:** Oversee shows multi-agent work as unnamed, unattributed, zero-cost chat sessions. Cost observability for the entire multi-agent surface is **structurally absent from the one view meant to provide it.**

### 3.4 New finding — `interactive_sessions` re-folds the entire ledger on every refresh
`ev_mod.read_all(project)` (`oversee.py:157`) reads **all** events; the dashboard refreshes every 2.5 s. The ledger is currently ~19,000 events. This is an O(all-history) scan per tick and will degrade monotonically — a scaling wall Phase 5's state-rehydration will hit head-on.

### 3.5 New finding — `enrich` fails open **silently**, with no telemetry
Every context source swallows exceptions and returns `[]` (`:296-297`, `:256-257`, `:166-167`, `:483-484`). A broken inventory yields a prompt with no skills and **no event, no log, no signal**. `record()` (`:436`) fires only when a user explicitly copies/records an enrichment, so there is no measurement of whether enrichment ever helps.

### 3.6 Structural verdict on your Phase 3 plan
> *"Upgrade `enrich.py` to dynamically parse the prompt, select skills, and autonomously spawn agents."*

`enrich()` (`:416-433`) is a **pure prompt-composition function** — text in, markdown out. It has **no concept of decomposition**: no task graph, no agent roster, no dependency ordering, no spawn surface. It answers *"which skills suit this one prompt?"*, never *"what are the N units of work and who runs each?"*

**This is not an upgrade to `enrich.py`.** It is a new orchestrator module that *consumes* `enrich`'s ranking as its skill-selection input. Framing it as an edit to `enrich.py` would either bloat a clean 484-line pure module past its purpose or produce a shallow imitation of orchestration. Recommend: keep `enrich.py` pure, add a separate planner that calls it.

---

## 4. The OS framing — what to imitate from ECC, and what MindHive is actually missing

ECC (`C:/Users/hanif/ECC`) organizes itself as a mature OS with four separated layers:

| ECC layer | Purpose | MindHive's existing analogue |
|---|---|---|
| `SOUL.md` / `RULES.md` | Identity + governance kernel | **SOP subsystem** (event-sourced, dynamic) ✅ |
| `WORKING-CONTEXT.md` | Live working state | **STATE.md / MEMORY.md via projector** ✅ |
| `agents/`, `skills/`, `manifests/` | Capability registry + contracts | **`plugins.py` / `recommend.py`** (health-aware) ✅ |
| `hooks/` | Deterministic enforcement | **`enforce.py` / PreToolUse denies** ✅ |

**MindHive already has four of the four.** In several respects (event-sourced SOPs, measured skill provenance, learned prevention rules) it is *more* mature than a static markdown kernel.

What MindHive lacks is the layer ECC also doesn't have to solve, because ECC doesn't run a fleet: **the process layer.**

> A real OS has a **process table** (what is running), a **scheduler** (what runs next and why), **resource accounting** (what each process is consuming against a quota), and a **journaling filesystem** (state survives a crash).
>
> MindHive's multi-agent surface has **none of these four**. `Manager.agents` is an in-memory dict that dies with the process (no process table). Spawning is a human clicking a button (no scheduler). `a.last_cost` is discarded (no accounting). Two ledger writes per run (no journal).

**This is the correct frame for the whole initiative, and it re-orders your phases.** Your Phases 2–5 are, in OS terms:

- Phase 2 = **journaling + resource accounting** (checkpointing, pre-flight reservation, rehydration)
- Phase 3 = **the scheduler** (auto-orchestration)
- Phase 4 = **adaptive policy** (the reflection loop feeding the governance kernel)
- Phase 5 = **the shell / display server** (componentization + state-backed UI)

The dependency order is strict and matches your numbering: **you cannot schedule what you cannot account for, and you cannot learn from what you never recorded.** Phase 2 is load-bearing for 3 and 4; §2.5's "central gap" (budget interrupts emit no `tool_failed`) is precisely the seam where Phase 2's telemetry feeds Phase 4's learning.

*Imitate, don't copy:* ECC's contribution here is the **separation of concerns**, not its file layout. MindHive should not grow a `SOUL.md` — it already has that, better, as events.

---

## 5. Open questions Phase 2 must answer before any code

1. **What is a "budget" when no remaining-quota API exists?** (§2.2) The only real signals are plan tier and post-hoc per-run cost. A "strict token/budget reservation check" cannot be strict against an unknown denominator. Options: a user-set spend ceiling per session; a rolling-window projection mirroring `loop/budget.py`; or a learned per-role cost prior from historical `result` envelopes.
2. **Checkpoint cadence vs. ledger bloat.** Your "every 10 s or 5 turns" against a 4000-line buffer and a 19k-event ledger that Oversee re-folds every 2.5 s (§3.4). Needs a size cap and a merge/compaction policy, or it degrades every projection in the system.
3. **New event types must be registered or `append_event` raises** (`events.py:113-114`) — and per SOP 7 §6, adding a type requires a why-comment, validation at append surfaces, a merge-exemption decision, and deliberate projection choices.
4. **Does rehydration replay the transcript or only the run record?** Determines whether checkpoints carry content (expensive, bloats ledger) or pointers.
5. **Should the user-stop path (§1) start writing a terminal event?** It is the only true "nothing saved" case, and it is a deliberate `pass`.

---

## 6. What is NOT yet established

**Resolved by the 2026-07-22 verification pass:**
- ~~Zero adversarial verification~~ → the 6 load-bearing claims are now checked (§0).
- ~~No read of `loop/budget.py` / `loop/engine.py`~~ → both read; `budget.py`'s
  five functions are pure over a caller-supplied list and need **no
  modification**, and `engine.py`'s event set was enumerated directly.

**Still open:**
- **58 of the 64 recovered findings remain unverified.** One of the six
  checked was outright false, so treat the remainder as leads, not facts —
  never size a workstream on an unverified finding alone.
- No measurement of actual checkpoint cost against the live ledger (the
  binding constraint for WS1's cadence).
- `swarm_page.py` (369) and `workspace_page.py` (761) characterized but not
  read line-by-line.
- The `improvements`/`rules` modules were never located as files this
  session — §2.5's lifecycle description is still a recovered assertion,
  though its central claim (no `tool_failed` on budget interrupts) is
  independently confirmed from the `loop/` side.
- The 29-run sample is small and dominated by one agent type
  ("Self-Reliant researcher"). The 59% truncation rate is solid for that
  population; it may not generalize to swarm roles.
