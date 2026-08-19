# MIND HIVE

[![CI](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml/badge.svg)](https://github.com/hanifridho62-rgb/Lintester-project/actions/workflows/ci.yml)

Deterministic, event-sourced memory for long-running Claude sessions.
Append events; deterministic projections rebuild everything. Nothing is hand-edited.

## Quick start

    pip install -e .
    hive state                       # what the record says right now
    hive append decision "chose X over Y because Z"
    hive sync                        # refresh the MEMORY.md a session auto-loads
    hive context --check             # is the auto-loaded surface still honest?

Four commands are worth knowing before the rest: `hive state` (the live
projection), `hive append` (the only way anything gets in), `hive sync` (rebuild
what the next session reads), and `hive context --check` (what that costs, and
whether all of it actually arrives). Everything else in this file is a
specialisation of those four.

On Windows use PowerShell 5.1 syntax — no `&&`, no ternaries — see the
**PowerShell 5.1 note** below.

## Commands
- `hive append <type> <payload> [--json] [--stdin] [--tags a,b]` — add an event
- `hive project` — rebuild STATE.md + LEDGER/ + index.json + chain.json
- `hive state` — print the live STATE.md. Also warns (stderr, non-fatal) when this checkout's branch tracks nothing: `git pull` fails there and no drift count can notice it, which is invisible to `git status` and to `hive repo stale` alike
- `hive recall "<query>" [--semantic]` — grep (default) or vector search
- `hive rules summaries [--project P] [--json]` — structured lesson records (root cause, lesson, prevention rule, category, active rule) for every incident — the same data the dashboard **Improvements** view shows, for agents working headless. `--json` emits the raw list
- `hive rules reflect [--limit N] [--cooldown-hours H] [--all-projects]` — bounded reflection pass over active un-reflected incidents via the `claude` CLI (default: 3 per batch, 6h cooldown; `--cooldown-hours 0` disables). `--all-projects` drains the backlog across every registered namespace — a manual drain, never invoked from hooks. The same pass also **reroutes** red leaking rules (below)
- `hive rules resolve [--limit N] [--list]` — active resolution over the **red (unresolved) set**: incidents with no rule are reflected; a leaking **auto** rule is replaced with the best distinct alternative route (model-ranked, deterministic-fallback aware, capped at 3 attempts per signature — then flagged `reroute_exhausted` for a human). The `rule_rerouted` event re-anchors leak counting, so a route that holds turns green (resolved) and auto-promotes to SOUL.md. Human rules are never rerouted. `--list` prints the red set without spending tokens
- `hive flag "<text>" [--category tag]` — self-flag a judgment error (wrong call, not a tool failure); active as a prevention rule from the first occurrence
- `hive graph export [--out path] [--semantic] [--open]` — write a standalone, offline graph.html
- `hive dashboard [--port N] [--no-browser]` — open the web control panel for every feature
- `hive plugins list [--json]` — print the **whole stored catalog**, free and paid, from the latest snapshot; never rescans (that's `scan`/`autoscan`), and says what to run when no snapshot exists yet. Under `--json` stdout is always parseable — `{}` when there is no snapshot, with the hint on stderr
- `hive plugins scan [--cache dir] [--top N]` — catalog free (no API key / no pay) skills & plugins, score by breadth, store a `plugin_catalog` snapshot, print the top N
- `hive plugins autoscan [--max-age-hours H] [--top N] [--force]` — scan **only when the catalog is missing or stale** (default 24h); a near-instant no-op otherwise, so it's safe to run every session
- `hive plugins recommend` — print the top recommendations from the latest snapshot
- `hive skills list [--json]` — list every installed plugin + skill from the live registry (updates the instant one is installed/removed)
- `hive skills invoke <name> [--plugin p]` — call any installed skill through MindHive via a headless `claude`; records the outcome and exits non-zero on failure so callers can gate on it
- `hive skills status` — green/red last-call outcome per skill from the ledger
- `hive skills stats [--json]` — per-skill completion-rate telemetry from the ledger (calls, ok/fail, last outcome, last used)
- `hive skills record <name> --ok|--fail [--note "…"]` — record an agent-assessed skill outcome directly (write-through for skills driven in-session rather than via `invoke`)
- `hive skills recommend [--json|--top N|--brief]` — deterministic most-needed-skills ranking from measured data; `--brief` prints ≤3 one-liners (the same block STATE.md embeds at every projection rebuild)
- `hive review queue [--session id]` — queue the session's touched source files for a thermo-nuclear review (run by `session_end`; writes the pending-review marker)
- `hive review show` — print the pending-review marker, if any
- `hive review clear` — clear the pending-review marker once the review has run
- `hive log <category> "<explanation>"` — record a **mandatory** code change-log entry for the current change-set (categories: `update`, `minor_change`, `deletion`, `reduction`, `major_change`, `replacement`; files auto-detected, `--files` to override). Entries are `code_change` events shown in the dashboard's **Log** section
- `hive log queue [--session id]` — queue the session's unlogged source files (run by `session_end`; writes the pending-changelog marker whose notice blocks STATE.md until the entry is recorded)
- `hive log show` — print change-log entries, newest first
- `hive log pending` — print the pending unlogged-changes marker, if any
- `hive roadmap set <id> --prompt "<ask>" --step "…" [--step "…"|--file steps.txt]` — declare a plan as an event-sourced **roadmap**: the original ask, the ordered steps, per-step progress. Each Oversee card gets a **roadmap** button rendering it as an interactive stepper (ask, progress bar, clickable step nodes)
- `hive roadmap step <id> <index> --status done|active|pending|skipped [--note "…"]` — update one step; marking the active step done auto-advances the pointer to the next pending step
- `hive roadmap interrupt <id> --cause usage_limit` — the **out-of-usage marker**: flags the plan interrupted so STATE.md (and the auto-loaded MEMORY.md) carries the creation context — the ask, steps done, and the next step — until it is resumed. `hive loop` records this automatically when a run ends on `api_cap` (Claude usage limit) or `budget_cap`
- `hive roadmap resume <id>` / `hive roadmap complete <id>` — pick an interrupted plan back up / mark it finished (append-only, like everything else)
- `hive roadmap link <id> [--pr N] [--branch <name>] [--commit <sha>]` — the **durable work link**: point a plan at the code it produced, without changing its status. `set`, `complete` and `archive` accept the same three flags; all three are optional and independent (the branch is known when work starts, the PR only once one is opened), and they merge into the record, so a re-plan never drops the PR number
- `hive roadmap merge-state <id>` — ask git and `gh` whether that work landed. Read-only, and **honest about not knowing**: `merged` only on proof (a merged PR, or a commit git confirms is an ancestor of the integration ref), otherwise `open`, `closed`, or `unknown` with the reason — `no_work_link`, `no_github_remote`, `no_gh_cli`, or `probe_failed`. It never reports `merged` because a probe failed to run
- `hive roadmap archive <id> --reason "<what settled it>"` — retire a plan whose last step nothing can verify ("merge the PR", "watch it for a week"). The honest third exit between *finished* and *still open*: the plan leaves the live views, the reason is recorded append-only, and the progress it actually reached stays on the record, so `archived` never reads as "done"
- `hive roadmap retire` — retire every live plan whose work link **proves** the work is finished: merged → `complete`, closed unmerged → `archive`. Reports every plan it examined, untouched ones included with their reasons, so "nothing was provably landed" cannot be mistaken for a successful sweep
- `hive roadmap list [--all]` / `hive roadmap show <id>` — list plans (retired ones — completed and archived — hidden unless `--all`) / print one plan with step statuses, its work link, and history
- **Session auto-sync** — interactive sessions declare and update their roadmap automatically: the PostToolUse hook mirrors each TodoWrite into a `roadmap set` for the session's id (statuses mapped completed→done, in_progress→active), and `hive roadmap autoclose --session <id>` (run by the SessionEnd hook) completes a finished plan or interrupts an unfinished one so its context rides STATE.md into the next session. Auto-synced plans carry the `auto` tag; human-declared plans are never touched
- `hive consolidate [--limit N] [--cooldown-hours H] [--force]` — one bounded memory-consolidation ("dreaming") pass: merge near-duplicate semantic memories into survivors and drop the absorbed originals from the briefing/MEMORY.md digest. Append-only throughout — the originals stay in the hash chain as the archive; `hive verify` stays green
- `hive export [--out path]` — write a portable handoff.md (zero API tokens)
- `hive verify` — check the hash chain
- `hive embed [--force]` — build the on-demand vector index
- `hive selftest` — run the test suite
- `hive loop init` — scaffold `.hive-loop/` (PROMPT.md, fix_plan.md, AGENT.md, loop.toml) and check the `claude` CLI
- `hive loop` — run an autonomous Claude Code loop in the current project until the task is done; memory-seeded each iteration, with real token/cost budget gating, a context smart-zone reset at half the window, a dual-condition exit gate (the explicit `EXIT_SIGNAL: true` **plus** ≥2 natural-language completion signals in the reply — `[loop] min_completion_heuristics` in loop.toml, `0` restores explicit-only), and a circuit breaker (3 no-progress / 5 same-error iterations). When no `[model].model` is pinned, iterations **route to a model tier** (light/mid/heavy, `[model.tiers]` in loop.toml): small tasks start light, a failed or no-progress iteration escalates one tier, and the starting tier is learned from what completed comparable-size runs in the ledger. A real run also **auto-opens the web dashboard** so you can watch it live — opt out with `--no-dashboard` or `MINDHIVE_LOOP_DASHBOARD=0`
- `hive loop status` — summarize the last loop run
- `hive loop --dry-run` — show prompts + projected budget/context math without calling the CLI (no dashboard is opened)
- **`/mindhive-orchestrator <idea>`** — the **single centralized pipeline**: turns an idea into finished, quality-checked work through a **ten-stage sequential, dynamic** workflow — brainstorm → plan → observation → understand-and-converge → PRD → recommendation matrix → kanban → execution → quality assessment → learning. Its **main execution engine is restricted to the `superpowers` and `compound-engineering` plugins** (either may run, or both combined); every other installed skill is a **complement** that only raises quality. It **recalls before it asks** (from the ledger, or by reading the code), and advances **dynamically** — auto-advancing when intent is clear from recall + codebase, pausing to converge with you only when a scope/architecture/risk fork is genuinely unresolved. It scores execution skills with a **telemetry-anchored Recommendation Matrix** (measured completion rates from `skills stats`, learning-gated `auto`/`suggest` execution modes, green/red `skills status`), chains proactive quality passes, offers a context **Handoff** at ~50%, and appends learnings back to the ledger. At execution it fans work out to **parallel sub-agents** over a **file-complexity cluster database** (`hive clusters`). This is the **only** trigger for the pipeline. Shipped in the plugin (`commands/mindhive-orchestrator.md` + `skills/mindhive-orchestrator/`, with `references/` for the pipeline stages, parallel orchestration, recommendation matrix, cross-platform guide, and handoff protocol).
- **`/circle`** — the loop **primitive** (not the pipeline): type `/circle` and Claude preflights the `claude` CLI, scaffolds/confirms `.hive-loop/`, dry-runs, then drives the autonomous `hive loop` and reports the result. Headless agent runs with `permission_mode = "acceptEdits"` by default. The `/mindhive-orchestrator` pipeline may drive this loop as one of its execution engines. Shipped as a **Claude Code plugin** (`commands/circle.md` slash command + `skills/circle/` skill, declared in `.claude-plugin/`) so it works in any project once installed — see below.
- **`hive clusters build` / `hive clusters plan --lanes N`** — the parallel-orchestration layer the pipeline stands on at execution: clusters the repo by module, classifies each file's complexity, and packs whole clusters into `N` **file-ownership-safe lanes** (no file in two lanes) so parallel sub-agents never collide. Deterministic and dependency-free; the cluster DB is an overwrite-in-place `cluster_db.json` sidecar.
- **`/engineering-loop`** — a **subordinate execution engine** (not a competing pipeline): fuses `/circle`'s bounded headless loop with the compound-engineering `lfg` pipeline into a memory-driven **plan → work → review → fix → verify → learn** loop. MindHive memory sits between every stage (recall before, append after), it drives the chosen skills through the invoke channel with green/red status, and any skill failure feeds the improvement loop as a `skill:<name>` prevention rule. The `/mindhive-orchestrator` pipeline may drive it at its execution stage. Shipped in the same plugin (`skills/engineering-loop/`).

### The always-on surface: what auto-loads, and whether it arrives

Four of these exist because the auto-loaded surface can degrade **silently** —
the block still arrives, so nothing looks broken, and part of what it promised is
missing. `tests/test_docs_parity.py` enumerates the shipped commands off the live
parser and fails when one of them is absent from this file, so this list cannot
quietly fall behind the CLI again.

- `hive sync [--folder P] [--no-briefing]` — write the native `MEMORY.md` digest the desktop app auto-loads. It **warns when the file it just wrote will be cut short by the loader**, naming the sections that will not reach a session and the unit that bit — the digest's caps bound item counts, and no item count bounds lines or bytes, so the tail (lessons, decisions) is what silently stops arriving
- `hive context [--check] [--top N] [--origin hive|claude] [--json]` — measure everything that auto-loads into every session, ranked by estimated token cost, with trim suggestions. `--check` is the gate: exit 1 when over the token budget **or** when `MEMORY.md` is past either loader cut (that one is loss, not cost, and is reported even with no token budget configured)
- `hive context --set-budget N [--set-memory-lines N] [--set-memory-bytes N]` — persist this namespace's token budget and, optionally, the two `MEMORY.md` cuts (defaults: the native loader's own limits). One sidecar holds all three; setting one never discards the others. **The byte budget is the one that usually bites**, and it is measured on LF-normalised content, not the size on disk — `sync` writes in text mode, so on Windows the file on disk is one byte per line larger than what the loader reads
- `hive sop add|update|delete|list|show <id>` — the event-sourced SOP registry. A **mandatory** SOP is re-asserted on every human prompt, verbatim
- `hive sop injection [--json]` — what that per-prompt injection **actually delivers right now**: every mandatory body verbatim, or degraded to `hive sop show <id>` pointers because it outgrew the per-prompt budget. Read-only, and not a gate
- `hive sop preflight [--json]` — pre-execution report: is this session new or a continuation (with the ledger evidence), which SOPs are mandatory, what the improvements record already warns about
- `hive briefing` — build and print `BRIEFING.md` (the full context; `MEMORY.md` is the compact digest of it)
- `hive directives` — print the session operating directives
- `hive repo stale [--max-age-hours H] [--json]` — how far this checkout has drifted from its integration ref. **Never fetches**: it compares against the ref as this checkout last saw it and reports the age of that snapshot alongside the count, because "0 behind" and "0 behind, last fetched in July" are different facts. It also reports whether the branch **tracks anything at all**, because the counts are measured against the ref a merge lands on — which resolves on any clone — while `git pull` needs a ref this branch tracks, and a checkout answering "0 behind" with a broken pull has no way to notice drift in the first place. `unknown` carries its reason (`no_checkout`, `no_integration_ref`, `probe_failed`)
- `hive premise check [--file F]` — check a prompt or handoff document's factual claims against the record: asserted file paths, roadmap ids, and test counts that contradict what is on disk, plus the claims it could not check
- `hive bench dashboard --url <origin> --label "<condition>" [--token T] [--project P] [--reps N] [--timeout S] [--endpoint PATH ...] [--json]` — time the dashboard's own API endpoints. Read-only: it starts nothing, restarts nothing, and writes no events — not even the token, which it reads but will never create. **Cold/miss and warm/hit are separate rows and nothing is discarded**, because the perf work being measured *is* a fingerprint-keyed memo layer: the first request per endpoint pays the fold and the rest are hits, so averaging them yields a figure that moves with the hit/miss ratio. `--label` is required — the same code answers 6.7x apart between two load conditions, so a number without its condition is not evidence. A non-200 is reported with its status and excluded from every mean (a missing token would otherwise time four fast 403s and read as a healthy server). Also reports serial-total against concurrent-wall for the same set, which is how GIL-serialized folds show up

### The Postgres read mirror (`hive pg`) — optional, and off by default

A **one-way copy** of the event ledger in PostgreSQL, so an AI can query it instead of grepping projections or reading 20 MB of JSONL. The **file ledger stays canonical and stays the sole writer**: `MINDHIVE_EVENT_BACKEND` remains `file`, and flipping it to `pg`/`dual` also disables the dashboard's overview memo (a measured 726 ms → 6.6 ms on `/api/overview`), so that is a separate decision with a known price. `psycopg` and the MCP SDK are optional extras (`pip install -e .[pg]`, `.[mcp]`) — with neither installed every command below still answers, structurally, saying what is missing.

**Nothing on the append path touches Postgres, under any backend value.** There is no per-event write seam and there deliberately isn't one: the module the AI path connects with holds `mindhive_reader`, which is `default_transaction_read_only` **at the role level**, so a single-row insert from there could never have succeeded. The mirror follows the ledger by being *refreshed* instead — per segment, from the admin credential, on the `autosync` schedule (or by hand). One loader, one credential, and no second code path to drift.

- `hive pg status [--project P] [--json]` — is the mirror reachable, is it fresh, how many read failures has this process swallowed, and which migrations exist on disk. Exits non-zero when the mirror is unavailable, so a script can gate on it. This is where a degraded mirror becomes **visible on demand** — the read seam is deliberately quiet on the hot path, because it sits under every fold
- `hive pg migrate [--dsn D]` — apply the pending SQL migrations. Needs `MINDHIVE_PG_DSN_ADMIN`, the **owner** credential, which nothing on the AI read path can even name. Refuses loudly when an already-applied migration's bytes have changed; a schema change is a new file, never an edit to an applied one
- `hive pg rebuild [--project P]` — reload **every** segment of one namespace from the file ledger (`DELETE WHERE project` then a bulk load, in one transaction, so a concurrent reader sees the whole old snapshot or the whole new one). Never `TRUNCATE`, which cannot be scoped to a namespace. Admin DSN
- `hive pg refresh [--project P]` — reload **only the segments whose bytes moved** since the recorded fingerprint, then re-run parity. This is the command a stale mirror's own notice names, and the one `hive autosync` runs on a schedule, so an operator typing it by hand does exactly what the machine does. Per-segment reload is safe because row order is not a stored property of the mirror: `ORDER BY COALESCE(ts,''), segment, seg_line` re-establishes it at read time, and `global_seq` is row identity only. Admin DSN
- `hive pg embed [--project P] [--model M] [--batch N] [--rebuild] [--force]` — encode this namespace's **semantic** events (decisions, caveats, experiment results, code changes, next steps, open questions, learnings, SOPs, roadmap rows) into `mindhive.event_embeddings`, which is what `search_events_by_meaning` ranks against. **Incremental and re-runnable**: the row key is the ledger's own `(segment, seg_line, model)` and the stored content hash is compared too, so an already-indexed event is skipped and a *rewritten* one is re-encoded rather than left with a stale vector. Measured on this ledger: first run 664 vectors in 47.9 s, second run 0.98 s with no encoder loaded at all — the probe happens on a short connection that closes before the model would be needed. Vectors are float32 little-endian in `BYTEA`, L2-normalised so cosine similarity is a plain dot product; there is no pgvector on a stock server and `BYTEA` measured the same on disk as `real[]`, 5.5x faster to load and 10x faster to rank via NumPy. Admin DSN, plus `pip install -e .[vectors]`
- `hive pg parity [--project P] [--json]` — compare the file ledger's hash-chain head against the mirror's. **Exits non-zero when it cannot prove parity**, including when it could not look at all: an unchecked gate reported as green is worse than a red one
- `hive pg tool <name> [--arg k=v ...] [--list] [--json]` — run one of the eight AI tools — four that show events (`query_database`, `get_recent_events`, `search_events_by_payload`, `get_session_summary`), three that answer "what IS the state" (`get_latest_event`, `search_events_by_type`, `get_current_state`), and one that searches by **meaning** rather than by string (`search_events_by_meaning`, nearest-neighbour over the vectors `hive pg embed` wrote) — and print the exact envelope a model would receive. `--arg` values are parsed as JSON when they parse and kept as strings when they do not, so `limit=50` is an integer and `session_id=manual` is a string. This is the same dispatch table the MCP server reads, which is the point: the human debug surface cannot drift from the AI one
- `hive pg mcp` — serve those eight tools to an MCP host over **stdio** (no port, no token, reachable only by the process that spawned it). `python -m mindhive.mcp_server` is the same `main()` and is the recommended spelling on Windows, where a console-script shim can go stale against an editable install

### Namespaces, hygiene & automation

- `hive projects` — list namespaces (registered vs unregistered)
- `hive gc [--prune <name>|--prune --orphans|--prune --graph-blobs] [--force]` — store hygiene: report per-namespace sizes, prune junk. Opt-in and guarded — it refuses a bulk prune when the registry is unreadable, rather than treating every namespace as an orphan
- `hive autosync [--max-age-hours H]` — automated `sync` for registered projects (run from hooks; a near-instant no-op when nothing is stale)
- `hive autoresume` — after a usage cooldown, deliver an interrupted plan's marker (the ask, the progress, the next step) into the new session
- `hive replenish [explain]` — propose the work the record proves is outstanding (run by `session_end`); `explain` shows the per-lens scoring behind each proposal
- `hive failures list` — recorded tool failures and incidents, the raw material `hive rules` mints prevention rules from
- `hive triggers` — prompt-quality trigger rules (the UserPromptSubmit gate)
- `hive enrich` — prompt enrichment for Oversee: the default-on toggle and a deterministic preview of the enriched prompt
- `hive packs check [--manifest P]` — stack content-pack quality gates (read-only)
- `hive mindnet` — the MindNetwork task graph and phase spine: declare tasks, walk the six-phase pipeline, record the evidence each phase requires
- `hive session run` — supervise an interactive Claude Code session and rotate it when context fills; see **Smart-zone session rotation** below
- `hive session install-hook <settings.json> [--command C] [--dry-run]` — register the Stop hook that measures occupancy. Idempotent, refuses an unparseable settings file rather than repairing it, and backs up before writing

### Smart-zone session rotation (interactive sessions)

`hive session run` wraps an interactive session: when the Stop hook measures
context past the smart-zone boundary it writes a `ROTATION_PENDING` artifact, and
the supervisor starts a successor seeded from the ledger once the child exits. A
Stop hook cannot end a session, which is why the signal is a file.

Two properties are deliberate, not incidental. The hook is **inert until
switched on** (`MINDHIVE_ROTATION`, which only `hive session run` sets, for its
own children), so registering it changes nothing on its own. And its advice tells
the truth about what happens next: it promises a queued successor only when a
supervisor is actually waiting, and otherwise says plainly that nothing is queued
and names the command that would queue one.

**Rotation is Windows-only today** — every hook shim in `hooks/` is PowerShell
(nine `.ps1`, no POSIX equivalent), so `hive session install-hook` on macOS or
Linux registers a valid entry for a command that cannot run. It reports that as a
`platform_warning` rather than leaving you to discover it as an absence.

## Install the `/circle` plugin (global)
This repo is a self-contained Claude Code plugin marketplace. To get `/circle` in **every** project:

```
/plugin marketplace add hanifridho62-rgb/Lintester-project
/plugin install mindhive@mindhive-dev
```

(or `/plugin marketplace add C:\Users\hanif\mindhive` to install from the local checkout). Then start a new session and type `/circle`. `hive loop` itself still needs the `mindhive` package importable (the plugin shells out to `python -m mindhive loop`) and the `claude` CLI on PATH.

## Event types
Semantic (you/Claude emit): decision, experiment_result, caveat, next_step,
open_question, state_set.
Deterministic (machine-generated, no AI/API cost): file_touched, command_run,
git_diff, session_start, session_end, compaction, plugin_catalog. The first six
are emitted by the session hooks; `plugin_catalog` is emitted on demand by
`plugins scan`/`autoscan` (the `session_start` hook fires a stale-gated
`autoscan` so it refreshes across sessions without you lifting a finger).

## Plugin/skill catalog: auto + manual
The recommendation catalog refreshes three ways, so it's current in any session:
- **Auto (new sessions):** the `session_start` hook runs `plugins autoscan` in
  the background — stale-gated, so it only does real work when the snapshot is
  missing or >24h old.
- **Auto (panel):** opening the dashboard **Skills & plugins** view calls the same
  stale-gated autoscan, so just looking at it keeps it fresh.
- **Manual (force):** `hive plugins scan` or the dashboard **Plugins scan**
  (Heavy) button force a full rescan anytime, in any session.

Because every session shares one event store, a refresh by one session is
immediately visible to all the others.

## Skills & plugins: a live, callable, self-healing view
The dashboard's **Skills & plugins** view is a real-time inventory driven by
Claude's own `installed_plugins.json`, so installing or uninstalling a plugin —
or deleting a local skill — updates the list within seconds (a cheap disk
signature gates the rebuild). Every entry is:
- **Click-through** — opens a detail panel with the skill's **intended use**
  first, then the date it was added, then the full `SKILL.md` body.
- **Callable** — a **▶ call this skill** button (or `hive skills invoke`) runs it
  through MindHive via a headless `claude`. The indicator turns **green** on
  success and **red** on failure.
- **Self-healing** — a failed call is recorded as a `skill:<name>` incident that
  flows into the **Improvements** loop, so a bad call becomes a prevention rule
  the next run can consult. All installed skills are callable this way, not a
  hand-picked subset.
- **Effectiveness-aware ranking.** Skill invocations are captured live (and
  back-filled from transcripts), so recommendations blend coverage with a
  measured success rate. A skill with ≥3 calls and <40 % success is demoted:
  listed with its failure reason instead of recommended. No history = neutral
  (unproven skills are neither boosted nor punished).

## PowerShell 5.1 note
Inline JSON args are mangled by PowerShell 5.1. To set state from PS, pipe via stdin:

```powershell
'{"task":"OFI verdict","next_step":"recapture 24h"}' | hive append state_set --json --stdin
```

(Claude in-session uses the Bash tool, where normal quoting works.)

## V17 safety
`embed` and `recall --semantic` refuse to run while `~/.mindhive/LIVE_LOCK`
exists (V17 is trading). Override with `--force` only when the box is idle.

## Usage-cap fallback
When the Opus window is nearly spent, run `hive export` and paste handoff.md into
any model/client. It is deterministic and costs no API tokens.

## Oversee: live work sessions

The dashboard's **Oversee** view (Work group) shows every kind of live Claude
work for the selected project as a titled card:

- **`hive loop` runs** — titled by a one-line summary of what the run is doing
  (the latest iteration summary, seeded from PROMPT.md).
- **Interactive Claude sessions** — no `/circle` or loop needed: the moment a
  session's hook telemetry flows (prompts, file touches, commands), it appears,
  titled by your latest prompt.

Click a title to expand the progress detail, rendered as a Claude-style task
checklist in the hive palette: green **✓** done lines (past iteration
summaries / checked fix_plan items / decisions and touched files), a pulsing
**◐** for what's happening right now, and gray **○** pending lines (unchecked
required items, next steps; Future/Optional items dimmed). While anything is
live, an **online — Claude is working** strip shows at the top (loop heartbeat
= one iteration timeout; chat heartbeat = tool events within ~3 minutes); it
disappears when nothing is working.

Sessions are **removed automatically** when a loop run ends through the dual-
condition exit gate (graceful exit) or the circuit breaker trips, when an
interactive session ends, or when a chat session goes idle past ~6 hours. Any
card can be deleted manually with its **delete** button. Deletion is an
append-only `loop_run_dismissed` tombstone — the events stay in the hash chain
(the archive), so `hive verify` never breaks — and a session that was actually
still alive resurfaces on its next event instead of burning tokens out of
sight.

## Desktop control panel

`hive dashboard` starts a localhost web app (bound to 127.0.0.1) and opens your
browser to a control panel for every feature — view state/briefing/graph, run
recall, append memories, project, sync, verify, export, embed, and the live
**Skills & plugins** view (click-through detail, one-click skill calls, green/red
status). The graph is one of its views.

**Improvements view.** The Memory group also has an **Improvements** view:
lessons learned from past session logs — recurring tool failures projected into
incident lessons (root cause, lesson, prevention rule, category) plus manually
recorded caveat notes, with search and status/category filters. An efficiency
strip compares the tool-failure rate (per 100 tool actions) against the
previous day / week / month / 3 months; lessons sort by newest, most critical,
or most frequent (with a direction toggle), carry first/last-seen timestamps,
and lead with a resolution glyph — green ✓ (rule holding), red ✗ (unresolved
or recurring), amber ● (watching / not yet tested), gray ○ (dismissed via the
per-card dismiss button). The **Reflect (learn from failures)** button (Heavy)
turns recurring unreflected incidents into lessons via the `claude` CLI, a
small bounded batch per click.

**Workspace: MindParallel & MindSwarm.** The **Workspace** group runs real
Claude Code agents from the dashboard, all feeding the same hive memory.
**MindParallel** is a grid of up to 16 panes — each pane is a headless `claude`
session (follow-ups resume the same session), with named **profiles** mapping
panes to different `CLAUDE_CONFIG_DIR`s so multiple accounts run side by side,
and a kanban board whose cards dispatch their prompt to a free pane and move
backlog → running → done with the run. **MindSwarm** launches a mission crew —
a coordinator plus builders, scouts, and reviewers — where every agent's system
prompt tells it to read and write the shared Mind Hive ledger (`hive recall` /
`hive append`), so what one agent learns the next one starts with; a command
bar directs any single agent or the whole swarm. Every exchange lands in the
ledger as `user_prompt` / `assistant_response` events (failures as
`tool_failed` with signature `workspace:<role>`), so workspace activity flows
into Memory, the graph, and the Improvements loop automatically.

**Self-healing capture.** The server runs a background loop that periodically
**auto-harvests** any session the live hooks missed — so the store stays current
even in environments where the hooks never fire (e.g. the Agent SDK harness).
It's safe alongside the hooks: it skips sessions already captured live (no
double-counting) and only touches projects you already track (no surprise
namespaces). Disable with `MINDHIVE_AUTOHARVEST=0`; tune cadence with
`MINDHIVE_AUTOHARVEST_SECS` (default 180). Trigger it by hand with the **Catch
up** button, the `POST /api/harvest` endpoint, or `hive harvest --catch-up`.

**The six long operations are jobs, not requests.** Catch up, Embed, Plugins
scan, Reflect, Resolve red and Self-test answer **`202 Accepted`** with
`{"ok": true, "job": {"id": …, "state": "running"}}` and do the work on their own
thread; poll `GET /api/job?id=<id>&offset=<bytes>` for state and for output since
`offset`. They used to hold one fetch open for the whole run, so anything longer
than the browser's timeout was reported as a failure while it was still going and
still about to succeed. A second click while one runs is still refused the same
way as before — `{"ok": false, "error": "… already running"}` with a 200.

A finished job names **what it did**, not just that it ran: `did_work`,
`nothing_to_do`, `degraded`, or `failed`. "Nothing to do" is a success — a
harvest that found no missed sessions is working correctly, and it used to be
indistinguishable from one that ingested forty. Each run leaves `job_started` and
`job_finished` rows in the ledger; a job whose process died is closed out as
`job_abandoned` by the next dashboard start-up, so an orphan no longer reads as
still-running forever.

That vocabulary is not the dashboard's alone. **Five surfaces in MIND HIVE report
progress to a human, and all five now answer in the same words** — the rule being
that every path which can produce zero work, a degraded result, or an abandoned
job has to say *which* of the three it is, and *why*:

| Surface | The loop it closes |
|---|---|
| `dashboard-jobs` | a long click reports what it did, not just that it started |
| `roadmap-retirement` | a plan card retires because something checkable said so, or names which of four things stopped it |
| `rotation-advice` | the end-of-turn advisory claims a queued successor only when one is really queued |
| `sop-injection` | a prompt is told when a mandatory procedure arrived as a pointer instead of its body |
| `memory-budget` | the always-on digest reports the sections that fall past the loader's cut instead of losing them quietly |

Each surface registers itself in `outcomes.py` and carries its own read-only
probe, which the suite runs on every pass. The point of the registry is what it
makes impossible to do quietly: a surface can no longer report "fine" for a run
that did nothing, and `nothing_to_do` — rotation switched off, no live plan, no
mandatory SOP, no digest written yet — is a **success with a reason attached**,
not an absence of news.

**The URL is stable across launches.** It binds a fixed port (**54461**) and a
token persisted to `~/.mindhive/dashboard.token`, so every launch (CLI or
`MindHive.exe`) serves the same `http://127.0.0.1:54461/?token=…` — bookmark it
once. Override the port with `--port N`, or use `--port 0` for an ephemeral one;
override the token with the `MINDHIVE_DASHBOARD_TOKEN` env var. If the port is
already bound (the dashboard is already running), a second launch just reopens
that same URL instead of failing.

The panel updates in **real time**: the Overview, Memory, Sessions and State
views auto-refresh every few seconds and the moment you append a memory, so new
events — including ones written by another session or hook — appear without a
manual reload. A `● live` toggle in the header pauses/resumes it; polling also
pauses while the tab is hidden.

**Project → Session → Memory.** A **project** picker in the header switches which
store namespace the page shows (it defaults to the one with the most events, so
a fresh launch lands on real data rather than an empty cwd-derived namespace).
Within a project the panel keeps three layers distinct so intent and context
stay legible:
- **Memory** — the semantic events you/Claude author (decision, caveat,
  experiment_result, next_step, open_question, state_set): the intent layer.
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
- **session / auto** — deterministic telemetry (file_touched, command_run,
  session_start/end, …), counted separately on the Overview so it never drowns
  out the memory.

Every endpoint takes a `?project=` (GET) / `"project"` (POST) parameter, so the
same running server serves any namespace; the picker just drives that.

**HIVE MIND — one unified view.** The first entry in the project picker
(`⨁ HIVE MIND (all projects)`, internally `project=*`) aggregates **every**
project into a single stream: all sessions, logs, memory and context unified.
Overview sums events/memory/sessions across the whole store (plus a project
count); Memory, Sessions, Recall and the Graph span all namespaces and label
each item with its project; Verify reports per-project chain status. It's a
read-only lens — mutating actions (append, sync, embed, scans) still need a
concrete target project, so select one first.

`hive graph export` still writes a single self-contained `graph.html` (data +
renderer inlined, no network, no keys) you can open in any browser on any PC.
Nodes are labelled and coloured by meaning — decisions, caveats (amber),
experiment results, open questions, etc. — with a colour legend, click-to-inspect
side panel, and a **Fit** button that frames the whole graph.

To build a double-click binary (Windows, Python 3.12):

    pip install -e .[build]
    python build/build_app.py        # -> dist/MindHive.exe

The exe runs a small console window showing the (stable) URL; close it to stop
the server. The window/exe use `build/icon.ico`.
