# MindHive Command Catalog

The full `hive` / `python -m mindhive` command surface that the orchestrator draws
on. Every command below is a real subcommand in `src/mindhive/__main__.py`. Reach
for a command only when the phase calls for it — the [SKILL.md](../SKILL.md)
Function-Call Syntax section lists the handful you use most.

**Invocation form (resolve in this order):**

1. `python -m mindhive <command>` (preferred)
2. `hive <command>` (when the console script is on `PATH`)
3. `<checkout>\.venv\Scripts\python.exe -m mindhive <command>` on Windows, `<checkout>/.venv/bin/python -m mindhive <command>` elsewhere (explicit fallback through the venv of the checkout MindHive is cloned into)

If none resolve, MindHive is unavailable on this surface — run ULT-IDEA in
stateless mode (see [compatibility.md](compatibility.md)).

---

## Memory & orientation

| Command | Purpose | Use when |
|---------|---------|----------|
| `state [--project P]` | Print STATE.md — current status, active work, standing constraints | Preflight, first |
| `briefing` | Build and print BRIEFING.md (state + recent decisions + open threads) | Preflight, orientation |
| `recall "<query>" [--semantic] [--limit N] [--project P] [--force]` | Search the ledger for prior decisions, caveats, failures | Preflight; per gap/term/skill |
| `graph related "<query>" [--depth N]` | Connected decisions and files for an entity | Idea touches an existing subsystem |
| `graph build [--semantic]` | Rebuild the knowledge graph | `related` looks stale |
| `graph export [--out F] [--semantic] [--open]` | Write a standalone `graph.html` | Visual inspection of connections |
| `projects` | List namespaces (registered vs unregistered) | Debugging the wrong-project failure |
| `directives` | Print the session operating directives | Orientation |
| `verify` | Verify the tamper-evident hash chain | Integrity check |

## Learning loop (failures → rules → improvements)

| Command | Purpose | Use when |
|---------|---------|----------|
| `rules inject [--project P]` | Print the active prevention-rules block | Preflight (HARD-GATE #1) |
| `rules summaries [--project P] [--json]` | Structured lesson records (dashboard-parity) | Skill selection, learning loop |
| `rules list [--project P]` | Incidents by occurrence | Auditing incident volume |
| `rules show "<signature>" [--project P]` | One incident's full record + lesson | Before invoking a red skill |
| `rules reflect [--project P] [--limit N] [--cooldown-hours H] [--all-projects]` | Bounded reflection over un-reflected incidents | Draining the incident backlog |
| `rules audit [--project P]` | Stamp activations for newly-recurring signatures | Advancing the rule lifecycle |
| `rules promote "<signature>" [--project P]` | Promote a proven rule into canonical guidance | A rule has earned canonical status |
| `rules demote "<signature>" [--project P]` | Manually demote a rule | A rule is no longer warranted |
| `rules reconcile [--project P]` | Record human edits as `rule_revised` events | After hand-editing rules |
| `rules enforce [--suggest] [--enable SIG] [--disable SIG] [--project P]` | Manage the PreToolUse enforcement manifest (nudge→enforce ladder) | Hardening a recurring failure |
| `flag "<text>" [--category C] [--project P]` | Self-flag a judgment error (active at first occurrence) | You misjudged something |
| `failures capture --transcripts DIR [--project P]` | Scan a transcript dir, store `tool_failed` events | Backfilling failures from a run |
| `failures list [--project P]` | List incident signatures by occurrence | Reviewing captured failures |

## Skills (the invoke channel + telemetry)

| Command | Purpose | Use when |
|---------|---------|----------|
| `skills list [--json]` | Live inventory of installed plugins + skills (registry truth) | Skill selection (sourcing) |
| `skills status` | Green/red last-call outcome per skill | Skill selection |
| `skills stats [--json] [--project P]` | Per-skill completion-rate telemetry from the ledger | Skill selection (score anchoring) |
| `skills recommend [--brief] [--top N] [--json] [--project P]` | Deterministic most-needed-skills ranking from measured data | Skill selection (sanity check) |
| `skills invoke <name> [--plugin P] [--project P]` | Run a skill via headless claude, record outcome (exit 0/1) | Headless/dashboard-driven execution |
| `skills record <name> --ok\|--fail [--note "…"] [--plugin P] [--project P]` | Agent-assessed outcome write-through | After an in-agent Skill-tool run |

## Plugins (which plugin suits the work)

| Command | Purpose | Use when |
|---------|---------|----------|
| `plugins recommend` | Top free plugins by breadth score | Architecture (2.5.1), skill selection |
| `plugins scan [--cache DIR] [--top N]` | Scan cache, score, store a catalog snapshot | Catalog is missing |
| `plugins autoscan [--cache DIR] [--top N] [--max-age-hours H] [--force]` | Scan only if the catalog is stale (cheap; safe every session) | Keeping the catalog fresh |

## Recording & housekeeping

| Command | Purpose | Use when |
|---------|---------|----------|
| `append <type> "<text>" [--tags t1,t2] [--session S] [--json] [--stdin]` | Append an event (`decision`, `caveat`, `experiment_result`, …) | Architecture lock, learning loop |
| `sync [--folder F] [--no-briefing]` | Write native MEMORY.md for desktop auto-load | End of run |
| `export [--out F] [--recent N]` | Write `handoff.md` — portable state snapshot | Context handoff |
| `consolidate [--project P] [--limit N] [--cooldown-hours H] [--force]` | Dreaming pass: merge near-duplicate memories | Bounded housekeeping |
| `embed [--force]` | Build the vector index (on-demand) | Enabling semantic recall |
| `gc [target] [--prune] [--orphans] [--graph-blobs] [--force]` | Store hygiene: report sizes; prune junk (opt-in) | Namespace cleanup |
| `project` | Rebuild projections | After manual ledger edits |
| `selftest` | Run the test suite | Verifying the install |

## Execution engines & live monitoring

| Command | Purpose | Use when |
|---------|---------|----------|
| `dashboard [--port N] [--no-browser]` | Open the web control panel for all features | Watching a run live |
| `loop [init\|status] [--dry-run] [--live] [--force] [--no-dashboard]` | Autonomous Claude Code loop until done (the `/circle` engine) | Hands-off execution |
| `harvest [--transcripts DIR] [--project P] [--catch-up] [--include-captured] [--all-projects]` | Capture events from Claude Code transcripts (no hooks needed) | Backfilling memory |
| `autosync [register …]` | Registry-driven harvest + project + briefing + graph + sync | Automating sync for registered projects |
| `hook post_tool_use\|pre_tool_use` | Handle a live Claude Code hook payload (reads stdin) | Wired into hooks, not called by hand |

---

**Guards that always apply:** destructive store operations (`gc --prune`,
`consolidate`, `loop`) honor the `~/.mindhive/LIVE_LOCK` guard and refuse to run
while V17 is live-trading unless `--force` is passed. The orchestrator never
overrides these guards on the user's behalf.
