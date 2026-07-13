# MindHive Function Catalog — everything the orchestrator can lean on

This is the full menu of MindHive capabilities the orchestrator draws on to make
idea → spec → execution **more effective and more efficient**. The main `SKILL.md`
uses a handful of these constantly (the recall preflight, the plugin/skill DB, the
learning loop); this file is the exhaustive reference so nothing is invented and
every command is used with its real flags.

## Invocation form (read this first)

Every command below is one MindHive subcommand. Invoke it in whichever of these
forms resolves on the current machine, in priority order:

1. `python -m mindhive <cmd>` — the canonical form the user specified. Works when a
   real Python that has MindHive installed is first on `PATH`.
2. `C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive <cmd>` — the
   project venv. **Prefer this on this machine:** the bare `python` on Windows is
   often the Microsoft Store *app-execution-alias* shim, which prints "Python was
   not found" and exits 0 without running anything. The venv interpreter is the
   reliable path here.
3. `hive <cmd>` — the console-script shortcut, when that interpreter is active.

Pick one, confirm it actually returns MindHive output (not the Store shim
message), then use it consistently for the rest of the run. See
`cross-platform.md` for detection and graceful degradation when no shell exists.

---

## 1. Memory & ledger — *recall before you ask; append after you learn*

The tamper-evident event ledger is the spine. These feed Phase 1 (recall before
asking) and Phase 4 (log learnings).

| Command | When to use / for what | Why |
|---------|------------------------|-----|
| `state [--project P]` | Start of every run, before any question. Prints `STATE.md`: active task, last verified, next step, open questions, deferred, last decision. | Orients the whole pipeline in the project's current reality so you never re-ask what's already settled. |
| `recall "<query>" [--semantic] [--limit N] [--project P]` | Before each interview gap and before scoring each candidate skill. Literal search over the ledger; add `--semantic` when literal recall is thin (meaning-based). | Pre-answers or skips questions; surfaces prior decisions, caveats, and failures on the topic. |
| `graph related "<entity>" [--depth N]` | When the idea touches an existing subsystem/file/entity. Returns connected decisions and files. | Reveals the blast radius and the decisions already attached to what you're about to change. |
| `graph build [--semantic]` | Once, if `graph related` returns nothing and you suspect the graph is stale. | Rebuilds `graph.json` so relationship queries work. |
| `graph export [--out] [--semantic] [--open]` | When the user wants to *see* the decision/file web. | Produces a standalone `graph.html`. |
| `briefing` | When you want the full auto-loaded briefing, not just `STATE.md`. | The richer context digest. |
| `append <type> "<one line>" [--tags a,b] [--json] [--stdin]` | Phase 4 only, when something memorable happened. Types used by the orchestrator: `decision`, `caveat`, `experiment_result`. | Writes durable lessons so the next `/mindhive` is smarter. Do **not** append on smooth, unremarkable runs — that's noise. |
| `sync [--folder F] [--no-briefing]` | End of run, after appends. | Refreshes native `MEMORY.md` so the next session auto-loads the new lessons. |
| `export [--recent N]` | At a context handoff. | Writes portable `handoff.md`. |
| `verify` | If a ledger read errors or you suspect tampering. | Verifies the hash chain. |
| `projects` / `project` / `directives` | Diagnostics: list namespaces, rebuild projections, print operating directives. | Debugging the memory layer. |

---

## 2. Plugin & skill database — *which skill/plugin actually fits this command*

This is the catalog the Recommendation Matrix is grounded in, and the live
registry of what is callable. It answers "which plugin is suitable for this
command" from data, not memory.

| Command | When to use / for what | Why |
|---------|------------------------|-----|
| `skills list [--json]` | Phase 2 (Recommendation Matrix), before naming any candidate. Live inventory of installed plugins + skills, straight from Claude's own `installed_plugins.json`. | The **authoritative** runtime registry — never recommend a skill name not present here. Updates the instant a plugin is installed/removed. |
| `plugins recommend` | When choosing among *free* (no-API-key, no-pay) plugins for breadth. Prints the latest catalog's top picks by breadth score, with curated notes. | Data-driven "which plugin is suitable" — breadth-scored so a broadly-useful skill outranks a narrow one. |
| `plugins autoscan [--max-age-hours H] [--top N] [--force]` | Safe to run every session; a near-instant no-op unless the catalog is missing/stale (default 24h). | Keeps the recommendation catalog fresh without cost. The `session_start` hook and the dashboard's Skills & plugins view both call this. |
| `plugins scan [--cache DIR] [--top N]` | Force a full rescan (new plugins just installed). | Rebuilds the `plugin_catalog` snapshot: counts free/paid/skills, scores by breadth, stores it. |

**Breadth scoring (how "suitability" is grounded):** a skill "covers" a topic if a
curated keyword (test, security, frontend, backend, data, deploy, debug, api,
review, git, database, refactor, design, auth, …) appears in its description.
Spread = distinct topics hit. Paid/keyed plugins (deps like `openai`/`anthropic`,
or `*_API_KEY`/`*_TOKEN` config) are flagged so free-first recommendations stay
free. The engine of record is `claude-opus-4-8`; the scan/score pipeline is
deterministic (zero API tokens at runtime).

---

## 3. Learning loop — *learn from mistakes: what works, what fails, effective syntax*

This is how the orchestrator gets **more effective over time**: failed tool/skill
calls become prevention rules; each skill carries a green/red track record. Consult
before executing; feed after executing.

| Command | When to use / for what | Why |
|---------|------------------------|-----|
| `skills status` | Phase 2/3, before invoking a candidate skill. Green/red *last-call* outcome per skill, from the ledger. | Prefer green skills; treat red as "fix or avoid, with a reason." The binary track record you can gate on. |
| `skills stats [--json]` | Phase 2, to fill the Recommendation Matrix. Per-skill **completion-rate telemetry**: calls, ok/fail, completion %, last outcome, last used. | The *measured* rate behind the green/red dot — a low rate or open incident argues substitute; a strong rate argues retry-on-blip. |
| `skills recommend [--json] [--top N] [--brief]` | Phase 2, to rank candidates from data. Deterministic most-needed ranking with per-entry `execution_mode` (`auto`/`suggest`) and a `discontinued` list. `--brief` = ≤3 one-liners (the block STATE.md embeds). | Grounds selection in measured need, not intuition; tells you which skills may auto-apply vs must be proposed. |
| `skills record <name> --ok\|--fail [--note "…"]` | Immediately after an *in-session* skill finishes (not via `invoke`). Write-through of an `agent-assessed` outcome. | Keeps the measured completion rates honest for skills you drove in-session; skipping one starves the next matrix. |
| `skills invoke <name> [--plugin P]` | Phase 3 execution *through* MindHive (headless `claude`). Exit `0` = ok, `1` = fail. | Lets you gate the next step on real success/failure, and every call is recorded — so the system learns which invocations work. |
| `flag "<text>" [--category T]` | When *you* (not a tool) made a judgment error — a wrong call, a missed constraint. Active as a prevention rule from the **first** occurrence (unlike tool failures, which need recurrence). | Lets the orchestrator record its own mistakes so the next run is warned — self-correction, not just tool-failure capture. |
| `rules summaries [--project P] [--json]` | Before executing in a known-risky area, or to review lessons. Structured lesson records: root cause, lesson, prevention rule, category, active rule — the same data the dashboard **Improvements** view shows. | Turns accumulated failures into "here's the effective way / the syntax that works" guidance, headless-readable. |
| `rules show "<signature>"` | When a specific skill/tool shows `fail` in `skills status`. Prints that one incident record + its prevention rule. | **Before invoking a skill that shows red, read its lesson and apply the prevention rule** — this is the learning loop closing. |
| `rules list` / `failures list` | To see incident signatures ranked by occurrence (`candidate` vs `active`). | Situational awareness of what keeps breaking. |
| `rules reflect [--limit N] [--cooldown-hours H] [--all-projects] [--project P]` | Manually turn recurring unreflected incidents into lessons (needs `claude` CLI auth). Bounded: default 3 per batch, 6h cooldown (`--cooldown-hours 0` disables); `--all-projects` drains every registered namespace. | The reflection stage — recurring failure → durable prevention rule. |
| `rules inject [--project P]` | To print the active prevention-rules block for injection. | Puts learned rules in front of the next run. |
| `failures capture --transcripts DIR [--project P]` | After a session, to scan a transcript dir and store `tool_failed` events. | Feeds the incidents → reflect → Improvements pipeline. |
| `rules promote/demote "<sig>"` · `rules audit` · `rules reconcile` | Rule lifecycle management (promote into canonical `SOUL.md`, stamp activations, record human edits). | Curating the rule set over time. |

**The self-healing arc:** a failed `skills invoke` writes a `tool_failed` event with
a `skill:<name>` signature → recurrence crosses a threshold → `rules reflect`
distills a prevention rule → that skill's dashboard indicator turns red until it
succeeds again → the next run reads the rule via `rules show` and avoids the repeat.
A skill that fails **teaches the system**.

---

## 4. Dashboard — *the one-window control panel for all of the above*

| Command | When to use / for what | Why |
|---------|------------------------|-----|
| `dashboard [--port N] [--no-browser]` | When the user wants to watch/drive visually. Localhost web app bound to `127.0.0.1` (fixed port `54461` for a stable URL; `--port 0` for ephemeral). | Surfaces every subsystem in one window. |

Dashboard views the orchestrator points users to:

- **Skills & plugins** — live registry inventory; click-through detail (intended-use
  first); a **▶ call this skill** button = `skills invoke`; **green/red** last-call
  status per skill. Opening this view auto-runs the stale-gated `plugins autoscan`.
- **Memory → Improvements** — recurring failures turned into lessons. The **Reflect
  (learn from failures)** button (Heavy) turns unreflected incidents into lessons via
  the `claude` CLI. This is the visual face of §3.
- **Plugins scan** — force a catalog refresh from the panel.

A real `loop`/`/circle` run auto-opens this dashboard so the autonomous loop can be
watched live.

---

## 5. Autonomous & maintenance commands (adjacent, not core to a single `/mindhive` run)

| Command | For what |
|---------|----------|
| `loop [init\|status] [--dry-run] [--live] [--force] [--no-dashboard]` | Run an autonomous Claude Code loop until done (the engine behind `/circle`); memory-seeded each iteration, budget/context gated. |
| `hook post_tool_use` (reads stdin) | The **live** capture path: a Claude Code PostToolUse hook pipes its JSON payload here and MindHive records the tool call/failure into the ledger in real time. This is what silently populates the memory the orchestrator recalls from — you rarely call it by hand, but it's why the ledger is fresh. |
| `harvest [--transcripts DIR --project P \| --catch-up]` | The **backfill** capture path (complement to `hook`): scan Claude Code transcripts and record sessions the live hook missed — no hooks needed. |
| `autosync [register …]` / `autosync` | Registry-driven harvest + project + briefing + graph + sync for registered projects. |
| `review queue [--session id] / clear / show` | The thermo-nuclear review queue for touched source files (run by `session_end`). |
| `consolidate [--limit N] [--cooldown-hours H] [--force]` | One bounded memory-consolidation ("dreaming") pass: merge near-duplicate semantic memories into survivors and drop the absorbed originals from the briefing/`MEMORY.md` digest. Append-only — originals stay in the hash chain; `verify` stays green. |
| `embed [--force]` · `gc [--prune …]` · `selftest` | Build the vector index; store hygiene; run the test suite. |

---

## Safety guardrails that always apply

- Destructive store ops (`gc --prune`) and `loop` runs are refused while the V17
  live-trading `~/.mindhive/LIVE_LOCK` guard is set. Never pass `--force` to defeat
  that guard on the user's behalf.
- Per-skill effectiveness comes from the **measured telemetry backend** — `skills stats`
  (completion rate) and `skills recommend` (ranking + `execution_mode`), with `skills
  status` as the binary last-call view. A percentage is a **model estimate** (`estimated`)
  only for a never-called skill; never present an estimate as a measured rate, and never
  present an `agent-assessed` `skills record` grade as measured task effectiveness.
