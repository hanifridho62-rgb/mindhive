# Session Legend — the MindHive address map

Where things live and which module owns what. This is the "MAP THE ADDRESSES"
document that SOP `execution-discipline` (§7) asks for: read it to find the
right file fast, not to learn what to work on.

It maps **code**. Two neighbours own the other halves, and this file
deliberately does not duplicate them:

- [`docs/DATA_MODEL.md`](DATA_MODEL.md) — the store contract: tables, event
  schema, payload contracts, invariants, how to add an event type. **When this
  file and the code disagree, the code wins. When this file and DATA_MODEL.md
  disagree about the store, DATA_MODEL.md wins.**
- `../CLAUDE.md` — the standing project rules (dashboard, change-log, Stop
  hook, land-green). Those are binding; this file is a map.

Current state of the work lives in the ledger, not here: `hive state`,
`hive briefing`, `hive sop list`, `hive recall "<topic>"`.

## 60-second orientation

**MindHive** is a deterministic, event-sourced memory system for Claude Code.
Append-only events are the single source of truth; every `.md`/`.json` beside
them is a rebuildable projection. On top of that ledger sit three governing
layers: deterministic **hooks** that fire on every session/prompt/tool call, a
**phase spine** (MindNetwork) that gates how work proceeds, and a local
**dashboard** that renders all of it.

Console entry point: `hive` → `mindhive.__main__:main` (`pyproject.toml`
`[project.scripts]`). Subcommands: `append hook session project state projects
directives context packs verify selftest premise recall consolidate gc export
embed briefing review log replenish sop roadmap mindnet autoresume graph plugins
skills clusters sync dashboard harvest autosync loop flag failures rules triggers
enrich`.

> **Probing the CLI from a worktree gives you the wrong branch.** The venv's
> editable install points at `C:\Users\hanif\mindhive`, so a bare
> `python -m mindhive …` runs the *main checkout's* branch no matter which
> worktree you are in — `hive session` was silently absent from `--help` for
> exactly this reason while writing this file. Scope it explicitly:
> `PYTHONPATH=<worktree>/src`. (pytest is already handled by the root
> `conftest.py`; subprocesses are not.)

## Module ownership

All paths relative to `src/mindhive/`.

### The spine of the CLI

| Module | Owns |
|---|---|
| `__main__.py` | The whole argparse tree **and** the live-hook dispatcher (`cmd_hook`). Past the thermo-nuclear 1000-line threshold by four times over — a known standing caveat, treat additions here with suspicion. It is on the allowlist in `tests/test_file_size_cap.py`, which pins its exact count: the file is allowed to be this long and is not allowed to get longer. Read the count there, not here — a length quoted in prose goes stale silently, and this line has been wrong before. |
| `cliparse.py` | How the parser behaves when given something it does not recognise: `SuggestingParser` (did-you-mean over the live command list, `--help` pointer otherwise, exit 2 unchanged) plus `closest_command` / `subparser_choices`. Apart from `__main__.py` because it is parser *behaviour*, not per-command wiring — `add_subparsers` inherits `parser_class` from `type(self)`, so setting it once at the root reaches all 21 groups. |
| `paths.py` | **Every on-disk location**, and **path equality**. `store_home()`, `detect_project()`, `project_dir()`, `events_dir()`, `runs_dir()`, plus `write_text_atomic`. Never hand-build a store path; call these. It also owns `normalise_path()` and `path_spellings()` — the one answer to "are these two strings the same file?", used by `changelog` (does this entry cover this touched file) and `replenish` (has this caveat been addressed). They live here rather than in either caller because the change-log must not depend on the injector to compare two strings, and because the ledger holds worktree-absolute paths while git reports repo-relative ones: compared literally the two sets never intersect, which is what let the unlogged-changes marker grow without bound. |
| `events.py` | The event record: schema, `VALID_TYPES` registry, `canonical_json`, `append_event`. A new event type starts here (procedure: DATA_MODEL.md §6). |

### Ledger → projections

| Module | Owns |
|---|---|
| `projector.py` | The fold. Events → `STATE.md`, `LEDGER/*.md`, `index.json`. Also where the SOP block gets embedded into STATE (`sop.block(evs)`). `compose_state` is `build_state`'s only caller and serves BOTH projections — the STATE.md rebuild and the digest `sync` embeds in `MEMORY.md` — which is why anything that must reach the auto-loaded surface enters there and not in `cmd_state`, a command that only prints the file. Four notices ride the top of the state body, each appending to one list: review, change-log, roadmap, and the SOP-injection verdict. `_project` refreshes the mandatory-SOP sidecar **before** composing, so the verdict STATE.md reports is the same one the digest reports in the same pass. |
| `chain.py` | Tamper-evident hash chain over **every** event in the ledger — no type filter, telemetry included. Prefix-consistency check against the `chain.json` checkpoint (`hive verify`). "Deterministic" means off the *semantic* layer, not off the chain (DATA_MODEL.md §3.1). |
| `briefing.py` / `sync.py` | `BRIEFING.md`, and the auto-loaded `MEMORY.md` that Claude reads at session start. |
| `recall.py`, `vectors.py` | Search over the LEDGER corpus; `vectors` is the optional `--semantic` index. |
| `dedupe.py`, `consolidate.py` | Meaning-aware dedup; the "dreaming" pass that folds reworded repeats. |
| `graph.py`, `graphview.py` | Knowledge graph build + its rendered view. |
| `harvest.py`, `hookcapture.py`, `failures.py` | Getting events *in*: transcript harvest (cursored), live hook capture, tool-failure capture. |
| `export.py`, `runlog.py`, `hygiene.py`, `migrate.py` | Portable handoff (`hive export`), per-run artifacts under `runs/`, store health (`hive gc` → `hygiene.report`/`prune`), and a one-off import from a legacy memory file / Pensyve (`migrate.py`). **`migrate.py` is not a schema migration and has no CLI subcommand** — its only non-self caller is `tests/test_migrate.py`. The ledger itself is never migrated (`events.py`). |

### Governance — the deterministic policy tier

| Module | Owns |
|---|---|
| `sop.py` | The managed SOP registry — `hive sop {add,update,delete,list,show,preflight,injection}`. SOPs reach a *Claude session* by three paths: `projector.py` embeds a rendering into STATE.md and into the `MEMORY.md` digest, `sopgate.py` re-asserts the mandatory tier on **every human prompt**, and `mindnet/advice.py` resolves them per-phase. **Two renderings, one registry:** `block()` carries bodies for STATE.md (budgeted by `BLOCK_FULL_BUDGET`; mandatory bodies exempt), while `digest_block()` — reached via `compose_state(..., digest=True)`, which only `sync.py` passes — carries titles and `hive sop show` pointers bounded by `DIGEST_ITEM_CAP`, and elides a mandatory body only when `sopgate` is verifiably re-asserting it verbatim. The split is because MEMORY.md auto-loads and the native loader cuts it short; at 404a7c0 `block()` was 58.7% of that file and `BLOCK_FULL_BUDGET` sat *above* the delivery channel, so it had never bitten. (`dashboard.py` renders the Sops section; every other module importing `sop` takes only `ID_RE`.) |
| `sopgate.py` | The per-prompt mandatory-SOP pass. `plan()` is the ONE place the per-prompt budget is spent; `render()` renders that plan and `probe()` (`hive sop injection`) counts it, so the report cannot claim "verbatim" while the hook ships pointers. Over `PROMPT_BUDGET` a body degrades to a **pointer, never a truncation** — half a procedure reads like a whole one. Sidecar-only (a ledger fold costs 430 ms on the live store as of 2026-08-14 — 35,202 events; the "0.36 s" this line used to cite is the same measurement taken smaller — and this is the prompt path), fail-open, never a decision. Registers the `sop-injection` surface (`_surface_probe`, named apart from `probe` because it only translates `probe`'s numbers into an outcome). `notice()` is the third consumer of the same measurement: the block `projector.compose_state` puts on **both** STATE.md and the auto-loaded `MEMORY.md` digest when the verdict is degraded, and the empty string when it is `verbatim` — a notice in every healthy digest is a notice every reader learns to skip. It states that the verdict is **global** (this module reads the cross-namespace sidecar by design while STATE.md is per-namespace) and that it is as of the last rebuild. `verdict_sentence()` is shared by the notice and the registry surface so one measurement cannot acquire two phrasings — the same refusal `probe` makes about a second computation. |
| `promptgate.py` | The UserPromptSubmit prompt-quality gate: assess the typed prompt, inject proven-skill directives, train/store trigger rules, record the causal "directed by prompt-gate" trace, beat the faculty cats. |
| `premise.py` | The premise gate — mechanically checks the checkable claims a prompt makes (file paths, roadmap ids, test counts) against the record. |
| `style.py` | The standing reply-style pass (caveman by default, prose when asked to teach). Mode persisted in `style_mode.json`. |
| `enrich.py` | Clarifying questions appended to a fired gate context; also the Oversee enrichment. |
| `enforce.py` + `gateguard.py` | The PreToolUse **deny** rung. `enforce.hook_decision` is the decision function; `gateguard` holds the pure classifiers behind the static policy tier. |
| `incidents.py`, `signature.py`, `reflect.py`, `resolve.py` | The failure loop: `tool_failed` → canonical signature → incident record → prevention rule → resolution. |
| `changelog.py`, `review.py`, `verify.py` | The three blocking gates: mandatory code change-log, thermo-nuclear review queue, mechanical test-run evidence. `changelog.record()` resolves its file list in the order **explicit `--files` -> this session's unlogged sweep -> the pending marker -> git**, and the git leg reads the **working tree** (`git status --porcelain -uall` against an explicitly resolved root), never the branch — diffing branch-vs-master once recorded 61 files for a 14-file change-set, and `git diff` cannot see untracked paths at all. Recording an entry **discharges only the paths that entry named** (`discharge()`, preserving the marker's `created_at`, which `queue()` uses as its coverage floor); the wholesale unlink it replaced silently cleared everyone else's debt. To clear debt no ordinary entry covers — a reverted experiment, an abandoned worktree's file — log it explicitly: `hive log <category> "<why it is not a real change>" --files <paths>`. That is the only sanctioned exit, per CLAUDE.md's "do not clear the notice any other way — write the entry". |
| `directives.py` | The operating directives injected at SessionStart. |
| `roadmap.py`, `autoresume.py` | The ask/plan/progress registry, and post-cooldown delivery of an interrupted plan's marker back into Claude. `roadmap` also owns the **work link** (`work_branch` / `work_pr` / `work_commits`, mergeable onto any action) and the `archive` exit that retires a plan whose last step nothing can verify — reason required, append-only. `retire_landed` is the only place the registry meets `repo.py`. Registers the `roadmap-retirement` surface, which reports degraded for a live plan carrying no work link — `merge_state` can only ever answer `unknown/no_work_link` for one, so no sweep will retire it however often it runs. |
| `repo.py` | Repository plumbing: what git and `gh` can **prove** about a branch or PR. One entry point, `merge_state`, returning `{state, reason, detail, evidence}` — never a boolean, never an exception. `merged` only on proof; otherwise `open`, `closed`, or `unknown` naming which of four things stopped the probe (`no_work_link`, `no_github_remote`, `no_gh_cli`, `probe_failed`). Probes run PR → commits → branch, first definite answer wins, so a machine without `gh` still gets a real answer from recorded commits. The `runner` seam is how the degradation paths are tested on a machine that has all three. Contract and reasoning: `docs/DATA_MODEL.md` §4.15. Also owns `staleness` (`hive repo stale`) — how far this checkout has drifted from its integration ref, which **never fetches** and always pairs the commit count with the age of the snapshot it was measured against, because "0 behind" against an unread remote is the misleading half. Same pairing, second half: `branch` / `upstream` / `upstream_missing` ship beside the counts, because those are measured against the ref a merge lands on (`_integration_ref`, which resolves on any clone) while pulling needs a ref this branch tracks (`_upstream_ref`) — conflating the two is how it once reported `current` for a checkout whose `git pull` was broken. `tracking` is that question on its own — two git calls, so `hive state` can afford to ask it every session and warn when the answer is no. |
| `bench.py` | `hive bench dashboard` — the dashboard's own endpoint timings, plus a serial-vs-concurrent figure. Read-only in the strong sense: no start, no restart, no ledger event, and it reads the dashboard token but never creates one (`dashboard.load_or_create_token` mints and persists; that would both mutate the store and mint a value the running server has never checked against). **Cold/miss and warm/hit are separate rows and nothing is discarded** — the subject *is* a fingerprint-keyed memo layer (`dashboard._STATS_MEMO` and friends), so the first request per endpoint is the only sample comparable to a before-profile taken on unmemoized code, and a discarded warm-up would throw exactly that sample away. Non-200s are excluded from every mean and a 403 is named as a probable token problem, because four fast 403s are what a missing token produces and they otherwise read as a healthy server. `--label` is refused-if-absent for the same reason the before-profile differs 6.7x between two condition sets. The `transport` seam is how unreachable/timeout/403/500 are driven with no server running; its limit, unlike `repo.py`'s runner, is that this module's whole output **is** the time dimension, so the seam proves classification and arithmetic and never latency — which is why the serialization ratio is verified against a real server instead of asserted against instant fakes. The only module that calls `register(subparsers)` on itself rather than being wired inline in `__main__.py`; the reason is that file's size, and it is not a new convention. |
| `plugins.py`, `recommend.py`, `invoker.py`, `playbook.py` | Skill/plugin catalog (backs both `hive plugins` and `hive skills`), deterministic recommendation, the invoke channel, and the per-prompt playbook (MN-15). |
| `replenish.py` | Adaptive work injection — the **decision** half. Seven lenses (`repair`, `evidence-gap`, `tech-debt`, `audit`, `caveat`, `coverage`, and the agentic `innovation`) fold the record for work it proves outstanding; a value gate scores each candidate and refuses anything without evidence, deduping by fingerprint. The scale is integers and every term is named, with `ScoreBreakdown.total` equal to the sum of its parts: base 10, +5 per recurrence (**how many times the fold saw the condition — nothing else**) capped at `RECURRENCE_MAX_STEPS` 20 steps, +20 severity, +5 per magnitude step (`excess_steps` — how far past its own limit the condition sits), +15 for the agentic lens on a record the tuner proves healthy, +50% resonance when several lenses name one target, and a negative decay term as history-derived weight fades with age (full inside a day, half past a week). A veto zeroes it when a prevention rule or pinned caveat already declined the work. `resonate` fuses multi-lens agreement about one `deferral_key` into a single `lens: "resonance"` systemic intervention; `is_spiking` marks a live burst of one failure signature, which is the only signal allowed past the score floor. **Scoring lives in `run_round`, not in the lenses** — one scale has to govern all of them or the threshold means nothing. The `AmbitionProfile` dial (`conservative`/`balanced`/`ambitious`) resolves threshold + lens set + per-run cap together, and `AutoTuningProfile` (folded out of `work_injected` in the same pass as everything else) shifts the threshold on what injected work actually produced. State is the `work_injected` fold, never a sidecar. Knows **nothing** about surfaces: the sink is a parameter, and `tests/test_replenish.py` asserts the module does not import one. A lens receives one `LensContext` (events, project, tree roots, changed paths, clock, model runner) so the tree-reading and model-driven lenses are testable without the real repo or a real model. `explain()` runs the whole pipeline read-only and is what `hive replenish explain` prints — the surface that makes a dead lens (one whose best score cannot clear any position it runs under) visible in one command. **Splitting this module is wanted and DEFERRED** (~2.7k lines, past the thermo-nuclear threshold; the injector proposes it at score 40, and a pinned caveat vetoes that proposal so it is not re-argued every round). Three constraints have to be designed around before anyone tries: (1) the test seam is **module-global rebinding** — `monkeypatch.setattr(replenish, "lenses_for", …)` and the same for `git_modified_paths`, `resolve_trees`, `run_round` — which works only because internal callers reference those names unqualified, so moving them to submodules breaks the seam **silently**, patching a name nothing reads any more; (2) `events.py` hardcodes the module path `".replenish"` for the `work_injected` payload contract, so the file cannot simply move; (3) `loop/`, `mindnet/` and `rotation/` re-export nothing from their `__init__.py`, so the existing split precedent offers no compatibility shim to copy for the ~60 `replenish.<name>` references across `tests/`. |
| `replenish_sinks.py` | The **landing** half, one sink per surface. `roadmap_sink` appends steps to the plan whose `plan_id` is the session id, marked `new`; `roadmap.autoclose_session` then interrupts it and `roadmap.notice` carries it into the next session's auto-loaded STATE.md. `fix_plan_sink` writes `- [ ] (new) <title>: <why>` **above** the first Future/Optional heading (reusing `loop.task._FUTURE_HEADING`, never a second parser) so `loop.task.required_remaining_count` rises and `loop.safeguards.should_exit` refuses. On both surfaces the forcing mechanism is that hop, not prose. |

### `mindnet/` — the phase spine (MindNetwork)

Units are MN-numbered; the number in each docstring is the address.

| Module | Owns |
|---|---|
| `controller.py` | MN-1 — the phase state machine: where a task is, what evidence cleared each exit. |
| `phases.py` | MN-1 — per-phase evidence descriptors, the `mindnet_phase` contract. |
| `tasks.py` | MN-2 — the dynamic hierarchical task graph. |
| `sidecar.py` | MN-1 U5 — the phase projection sidecar at `<project>/mindnet/state.json`, and its `rebuild`. |
| `advice.py` | MN-3 U2 — the advice resolver; makes injection mechanical. |
| `enforcer.py` | MN-4 — the teeth; where the spine refuses instead of describing. |
| `inject.py` | MN-1 U9 — phase-aware injection into the prompt context. |
| `optin.py` | MN-6 — the blast-radius gate. Enforcement is **opt-in, per checkout**. |
| `orchestrator.py` | MN-7 — the parallel orchestrator that walks the graph in waves. |
| `agent.py` | MN-8 — what a spawned agent actually runs for a task. |
| `worktree.py` | MN-9 — one isolated checkout per orchestrated task. |
| `decompose.py` | MN-11 — a task proposing its own subtasks. |
| `executor.py` | MN-1 U6 — the validation executor (holds the denylist, DATA_MODEL.md §4.2). |
| `autodeclare.py` | MN-18 — decide whether a prompt should mint a task at all. |
| `telemetry.py`, `ui_cats.py`, `ui_sessions.py` | MN-1 U10 / MN-5 U8 / MN-19 — the faculty activity feed (`mindnet_activity`) and the two cat boards. **MN-10 is a different slice**: the pr-green completion gate in `orchestrator.py` (DATA_MODEL.md §4.9). |

### `rotation/` — smart-zone session rotation

Interactive sessions only, and **opt-in**: inert unless `MINDHIVE_ROTATION` is
set, which only `hive session run` does for its children. **Windows-only today** —
every shim in `hooks/` is PowerShell (nine `.ps1`, no POSIX equivalent), so
`install` reports a `platform_warning` elsewhere rather than letting rotation
silently never fire.

| Module | Owns |
|---|---|
| `occupancy.py` | True context occupancy read from the Claude Code transcript (tail-read — these reach 20MB+). |
| `window.py` | Model id → context window → rotation threshold. |
| `hook.py` | The Stop-hook decision. **Never emits `{"decision":"block"}`** — for a Stop hook that means *keep working*, the exact opposite of rotating. Two advice texts, chosen by `MINDHIVE_ROTATION_SUPERVISED` (set only by `supervisor._child_env`): a successor is promised only when one is actually waiting. Registers the `rotation-advice` surface — silence with rotation off is reported as `nothing_to_do`, because opt-in is the design and "off" must not read as "broken"; the two advice texts having collapsed into one is the degraded state it exists to catch. |
| `artifact.py` | `ROTATION_PENDING` at `project_dir()/ROTATION_PENDING` — the only channel between hook and supervisor. |
| `supervisor.py` | `hive session run` — the outer loop around an interactive session. Nothing inside an interactive session can clear its own context, so the loop lives outside it. |
| `install.py` | Idempotent registration of the Stop hook in a `settings.json` (`hive session install-hook`). |

### `loop/` — the autonomous headless loop (`hive loop`, `/circle`)

`engine.py` (the loop), `driver.py`, `config.py` (`.hive-loop/`), `context.py`
(the smart zone), `router.py` (model-tier routing), `budget.py`,
`safeguards.py`, `contract.py`, `state.py`, `prompt.py`, `scaffold.py`,
`task.py`, `progress.py`. Termination is **gated, not free-form** — but read
the code before quoting SOP `agentic-loops` §1 at it: the hard stops (rate-limit
→ `api_cap`, token/cost budget, `max_iterations`/`max_hours`) and the circuit
breaker are structural, while *graceful completion* runs through the
dual-condition exit gate, which does read the reply text — `contract.parse_status`
scrapes `EXIT_SIGNAL`/`STATUS` out of the reply and `safeguards.should_exit`
additionally requires ≥`min_completion_heuristics` natural-language completion
signals plus zero remaining required work. Prose alone cannot end a run; prose is
one of two conditions.

**A `completed` exit is now provisional.** `engine._replenish_before_exit` runs a
replenishment round on `surface="loop"` at that exact moment; anything it lands
raises `required_remaining_count`, so the next `should_exit` refuses on its own —
the count does the forcing, not the branch that called the injector. Four
structural stops bound it, all counters folded from `work_injected` rather than
held in memory (so a resumed run inherits its spent budget): a **dry streak**
scoped to `run_id` (2), a per-run **injection budget** from the dial, a per-run
**round ceiling** (`replenish_max_rounds`, 5), and a candidate **depth** ceiling
(2, enforced in the gate). Every failure path returns 0 = honour the exit, because
an injector able to pin a run open is worse than one that misses a round — but the
fault is recorded as `tool_failed` under `replenish:round` or `lens:<name>`.

`CircuitBreaker.reset()` is public and the engine calls it when work lands:
finding evidence-backed work IS progress, and carrying a no-progress streak across
it would trip the breaker for succeeding.

On `api_cap`, `budget_cap` **or `max_hours`**, `_snapshot_interrupted_plan` hands
over a baton — steps with their `new` flags, a `relay_context`, and a
spent-fingerprint **cache**. `autoresume.rehydrate_spent` unions that cache with a
fresh fold and **the fold wins**: the cache may only add to the spent set, so a
stale one costs a candidate the ledger would also have skipped, while one able to
remove an entry would silently re-propose finished work.

### Dashboard (port 54461)

| Module | Owns |
|---|---|
| `dashboard.py` | The server and the assembly point. It was 2043 lines and is now seven modules; this one keeps `make_handler` — which composes `GetRoutes` + `PostRoutes` + `BaseHTTPRequestHandler` and stamps the session token on the class as `_token` — plus `do_GET`/`do_POST` (dispatch, auth, and the HIVE MIND read-only guard), `load_or_create_token`, and the launch/serve lifecycle. It also **re-exports** every lock, memo and data helper the split moved out, so `dashboard._HARVEST_LOCK`, `dashboard._STATS_MEMO` and `dashboard.default_project` still resolve — to the same objects, which is the property the lock and memo tests depend on. The connection is **keep-alive** (`protocol_version = "HTTP/1.1"`, `timeout = 30`), which makes three things load-bearing rather than optional: every response carries an exact Content-Length; a request whose body is refused closes the connection instead of reusing it (`_body`); and a route that raises *after* writing must not frame a second response (`_fail`, `_responded`). Reads are cached per namespace behind a **stat-only** ledger fingerprint (all of it in `dashboard_memo.py`, below). The six long operations (harvest, embed, plugin scan, reflect, resolve, selftest) answer **202** with a job id and are polled at `GET /api/job?id=&offset=`; validation that is instant (a bad `limit`, an active LIVE_LOCK) still answers synchronously. `serve()` runs the boot seal before `serve_forever`. |
| `dashboard_get.py` + `dashboard_post.py` | The two halves of the route surface, as **mixins** on the handler — `GetRoutes` and `PostRoutes`, each holding its handlers and its half of the `_GET_ROUTES`/`_POST_ROUTES` table. They declare no `__init__` and every `self._json` / `self.path` in them resolves only on the class `make_handler` composes, so neither file shows you the whole handler; that is the trade for reading 600 lines of routes without 900 lines of everything else. Nesting them inside `make_handler` had looked load-bearing and was not: exactly two handlers needed the closure, and they read `self._token`. Four handlers sit in the "wrong" file — `_get_enrich_settings` and `_get_job` are GETs beside their POST siblings in the source, `_post_workspace`/`_post_research` the reverse — because a class body cannot name a method defined on another class. |
| `dashboard_jobs.py` | The six `_job_*` bodies registered with `jobs.register_kind`, and `run_catch_up`, which lives here rather than in `dashboard` because both of its callers do: `_job_harvest` beside it, and the background harvest loop, which reaches it through this module. That also makes this the address a test patches. |
| `dashboard_data.py` | What a project IS and the folds the panel reads: `_project_stats` (memoized face of `_project_stats_uncached`), `_all_projects`, `default_project`, the Improvements efficiency strip with its low-sample dispositions, and the HIVE MIND constants + `is_all`/`all_event_pairs`. Pure functions over the store — no HTTP, no locks, no request state. Also `overview_stats`, which serves **both** `/api/overview` branches: the single-project route returns it directly and HIVE MIND sums one per namespace (distinct `(project, session)` pairs IS the sum of each project's distinct sessions, which is what makes the two identical). Its memo is **not** `dashboard_memo`'s — `_OVERVIEW_MEMO` keeps its own `_OVERVIEW_MEMO_MAX` of **128**, above the namespace count, because one HIVE MIND request folds every namespace and an 8-slot LRU would evict what that same request just wrote; and its fingerprint is **compound** (`segment_stats` **and** `chain.json`'s stat), because `chain_ok` comes out of a file `hive project` rewrites with no event appended. `/api/oversee` is deliberately excluded — `online` is wall-clock derived from two producers and `loop_state.list_runs` reads loop state rather than the segments; the reason is written at `_get_oversee`. |
| `dashboard_memo.py` | The caching layer, and the bottom of this cluster's dependency graph. `_ledger_fingerprint` (`mindnet.sidecar.segment_stats`, no read, `None` when it cannot be trusted) plus `_fingerprinted`; `_CATS_MEMO`, `_SESSIONS_MEMO`, `_STATS_MEMO`, each a `_BoundedMemo` carrying its OWN LRU bound. The two fold-holding memos stay at `_MEMO_MAX` (8) because one entry is a whole telemetry fold; the picker's `_STATS_MEMO` gets `_STATS_MEMO_MAX` (512). **One shared bound cost that memo its entire hit rate, not merely some of it**: 8 slots against this machine's 60 namespaces, and both of its callers scan every namespace in sorted order on every poll, so one pass evicted 52 of the 60 entries it had just written and the next restarted at the name evicted earliest — a sequential scan longer than the bound is the one access pattern an LRU degrades to exactly 0% on. Measured 2026-08-14: three identical passes cost 1329/1214/2134 ms holding 8 entries after each, one hot namespace 1207 ms → 4.0 ms. With the per-memo bound the same three passes are 1002/9.3/8.4 ms and hold all 60. See `T5 RESULT` in `docs/plans/2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md`, which supersedes that file's `T1 AFTER` reading of this endpoint as unimprovable. **A bound per memo is now the pattern here, not an exception** — `dashboard_data`'s `_OVERVIEW_MEMO` (128) reached the same conclusion independently from the same cause, so a new memo placed in front of a whole-namespace walk sizes its own bound above the namespace count rather than borrowing `_MEMO_MAX`. `_with_online` is the rule that makes caching safe at all: the FOLD is memoized, never the response, because `online` is a function of the wall clock rather than of the events. |
| `dashboard_locks.py` | One lock per mutating operation and `_one_at_a_time`, the decorator that answers `ok:False` with a 200 instead of running a second concurrent call. Below the route and job modules deliberately — all three take locks from here, and a lock reached through an import cycle is a lock you cannot reason about. |
| `dashboard_page.py` | The page **shell**: the `<head>`, header, nav, panel, and the splice chain that assembles everything else into them. It was 2396 lines; the stylesheet and the ~2050 lines of page JS now live in the eight sibling modules below, spliced in before the `theme`/`cat_board`/`taskview` placeholders because those markers live inside the fragments' own text. The page is still **one `<script>`** — the fragments share one scope and function declarations hoist across the seams, so the file boundaries are for readers and for the cap, not module boundaries. Sections and behaviour, wherever they now live: Section render/show pairs: `Overview`, `State`, `Memory`, `Log`, `Sops`, `Improvements`, `Oversee`, `Sessions`. The **Skill & Plugin** section breaks the pattern — `renderPlugins`/`paintPlugins`, entered via `showSkillPanel()` (there is no `showPlugins`). Plus `showBriefing`/`showRecall`/`showGraph`/`showAppend`/`showWorkspace`. Colours come from `theme` (see below), never as literals. A section load passes its skeleton SHAPE to `run()` — `cards` (Overview), `rows` (every `ul.hits` list, and Sessions, whose `.tcard`s render as stacked rows), `block` (State, Briefing, which paint into `pre#out`); Graph and the workspace iframes pass none. The six long clicks go through `runJob`/`confirmJob`/`runEmbed` → `awaitJob`, which follows a 202 by polling `apiJob` and threads the returned `offset` back so each tick carries only new output; `doAction` still runs the short ones inline. `awaitJob` deliberately bypasses `apiGet`'s scope guard — a job id names one namespace already, so switching the picker mid-run must not turn a running suite into an error. |
| `dashboard_css.py` | The shell's own stylesheet, as one constant, following the pattern `cat_board.CSS` / `taskview.CARDS_CSS` / `theme.PALETTE_CSS` already set. It ships the **whole** sheet including the reduced-motion guard, and that is a constraint: `tests/test_dashboard.py` scans each module for an animation switched on with no `@media (prefers-reduced-motion: reduce)` block in the SAME file, so splitting this sheet in two would separate the animations from their guard. |
| `dashboard_js_core.py` | Element handles, the real-time engine, the render/paint choke points, project scoping, Overview. Everything else calls into it (`esc`, `u`, `apiGet`/`apiPost`, `run`, `showHTML`, `liveFn`); it calls back out only to `showOverview`. Concatenated first, and the file to open first. |
| `dashboard_js_improve.py` | Memory and Improvements. |
| `dashboard_js_oversee.py` | Sessions and Oversee, plus the prompt-enrichment panel that rides Oversee's controls. |
| `dashboard_js_views.py` | The thin sections — Log, SOP, State, Briefing, Recall, Graph, Workspace, Append — each a few lines of controls over `run()` and one fetch, which is why they share a module instead of getting eight. |
| `dashboard_js_skills.py` | The Skill & Plugin panel. |
| `dashboard_js_actions.py` | The six long clicks as polled jobs, the cat board's poll, and the boot sequence. Concatenated **last**, and it has to be: `setInterval(pollCats, …)` and the boot IIFE are the only statements on the page that execute at parse time rather than declaring a function. |
| `workspace.py` + `workspace_page.py` | MindParallel — the fleet of headless `claude` subprocess agents. |
| `swarm_page.py` | MindSwarm — the mission view. |
| `research.py` + `research_page.py` | Self-Reliant autoresearch. |
| `oversee.py`, `taskview.py`, `cat_board.py` | Oversee projection, the Sessions notification center, the faculty cat board fragment. |
| `theme.py` | The colour palette, as `:root` custom properties — one Python source of truth for four documents. `PALETTE_CSS` goes into the shell (injected at the `/*__PALETTE__*/` marker, and `taskview.CARDS_CSS` renders into that same document, so it is written against these tokens too); `STATUS_CSS` — the three colours every document agrees on — goes into the `workspace`/`swarm`/`research` iframe pages, which are separate DOCUMENTS and cannot inherit a parent's `:root`. **`cat_board` is deliberately outside it**: it renders into two documents with different palettes, and its colours become SVG `fill=` presentation attributes, which do not substitute `var()`. `tests/test_dashboard_theme.py` holds the rules (no colour written twice, no token value left as a literal, every document defines exactly the tokens it uses). |
| `outcomes.py` | One vocabulary for "did that actually do anything?" — `did_work`, `nothing_to_do`, `degraded`, `failed`, `abandoned` — plus the **opt-in registry** of the surfaces that answer it. The distinction that earns the module is `did_work` vs `nothing_to_do`: both are successes, and a bare `ok: True` read identically for either. `abandoned` is in `OUTCOMES` but not in `SELF_REPORTABLE`, because nothing witnesses its own death. Registration is opt-in so the parametrized test grew a case per surface instead of staying red until the last of five landed. **All five are now registered**, and this is the list: `dashboard-jobs` (`jobs._probe`) · `roadmap-retirement` (`roadmap._probe`) · `rotation-advice` (`rotation/hook._probe`) · `sop-injection` (`sopgate._surface_probe`) · `memory-budget` (`context._probe`). Each probe is read-only and cheap by contract — the suite runs every one of them twice — and each lives in the module that owns its surface, never in a central prober that would drift from what the surface actually does. `tests/test_outcomes.py::EXPECTED_SURFACES` is where a sixth one gets named. |
| `jobs.py` | The dashboard's long operations, off the request thread: `register_kind` (opt-in), `start` (returns `None` when one is already running), `OutputRing` (capped, read by absolute byte offset so a poll carries only what is new), and the `job_started`/`job_finished`/`job_abandoned` rows. **The finish row is written BEFORE the job reads as finished** — the reverse order leaves a window where a dying process makes a completed run look abandoned. `seal_orphans` is the boot seal and the only writer of `job_abandoned`; read the module docstring for the single-writer assumption it rests on. The kind's `lock` is the pre-existing per-operation lock, held for the life of the job, because the same work is reachable through doors this module cannot see. |
| `claude_cli.py` | The shared **one-shot** headless `claude` call (`-p --output-format json`), plus exe resolution and envelope parsing — used by `invoker.py`, `reflect.py`, `autoresume.py`, `mindnet/agent.py`. `loop/driver.py` and `workspace.py` deliberately build their own argv (stream-json, `--resume`, `--permission-mode`) and borrow only `resolve_exe`. |

**Not dashboard-wired** despite sitting near it: `clusters.py`, `packs.py` and
`context.py` are CLI-only analyzers (`hive clusters`, `hive packs`,
`hive context`) — no route in `dashboard.py`, no import in `dashboard_page.py`.
`context.py` additionally registers the `memory-budget` surface: it resolves the
digest through `_memory_folder` (the **main repo**, not `cwd` — from a linked
worktree a cwd-keyed lookup misses the real MEMORY.md and would report "not
written yet" about a file that is in fact being truncated), and keeps three states
apart that `memory_pressure` reports as one `readable: False` — absent
(`nothing_to_do`, nothing delivered so nothing lost), present-but-unreadable
(`degraded`), and over the loader's budget (`degraded`, naming the lost sections).

## Where the data lives

Store home `~/.mindhive/` (override `MINDHIVE_HOME`), one directory per project
namespace. **All git worktrees of a repo collapse onto the main repo's
namespace** — `paths.detect_project()` resolves `git rev-parse
--git-common-dir` on purpose (SOP `session-lifecycle` §3). `paths.worktree_root()`
is the deliberately *different* function for per-checkout evidence paths;
conflating the two is a real bug.

Under `paths.project_dir()`:

- **Source of truth:** `events/<session_id>.jsonl` — append-only, one canonical
  JSON event per line. Never edit; corrections are new events.
- **Projections** (rebuildable, but *not all by one command*): `STATE.md`,
  `LEDGER/*.md`, `index.json`, `chain.json` ← `hive project`; `BRIEFING.md` ←
  `hive briefing`; `graph.json` ← `hive graph build`; `graph.html` ←
  `hive graph export`; `handoff.md` ← `hive export`.
- **Sidecars & cursors** (stateful, *not* rebuildable): `mindnet/state.json`
  (phase spine), `rules/` (prevention rules), `SOUL.md` (promoted rules),
  `runs/` (run artifacts), `pending_review.json`, `pending_changelog.json`,
  `harvest_state.json`, `style_mode.json`, `plugin_catalog.json`,
  `context_budget.json`, `enrich.json`, `faculty_beat-<session>.json`,
  `ROTATION_PENDING`, `chroma/`.

The auto-loaded memory file lives **outside** the store, at
`~/.claude/projects/<encoded-folder>/memory/MEMORY.md`, composed by
`sync.sync()`. Full table: DATA_MODEL.md §1.

## Hook wiring (`hooks/*.ps1`)

Every hook is a thin PowerShell pipe; the parsing and the decision live in
Python so they are unit-testable.

| Script | Claude Code event | Python it calls |
|---|---|---|
| `session_start.ps1` | SessionStart | `append session_start`, `state` (injects STATE.md), `directives`; detached `plugins autoscan` + trigger refresh |
| `user_prompt_submit.ps1` | UserPromptSubmit | `hook user_prompt_submit` |
| `pre_tool_use.ps1` | PreToolUse | `hook pre_tool_use` → `enforce.hook_decision` |
| `post_tool_use.ps1` | PostToolUse | `hook post_tool_use` → `hookcapture.record_post_tool_use` (+ faculty beat) |
| `post_tool_use_failure.ps1` | **none — built but registered nowhere.** No Claude Code event fires it; `~/.claude/settings.json` registers seven hooks and this is not one of them. | `hook post_tool_use_failure` → `hookcapture.record_post_tool_use_failure` |
| `pre_compact.ps1` | PreCompact | `append compaction` |
| `session_end.ps1` | SessionEnd | `append session_end`, `replenish run`, `roadmap autoclose`, `review queue`, `log queue`, `project`, `briefing`, `graph build`, `sync`; detached auto-reflection. **The order of the first three is load-bearing and silent when wrong:** injected steps added *after* the autoclose still exist, but the plan is already interrupted, so `notice()` never renders them and the next session never sees the work. `tests/test_replenish_cli.py` asserts the ordering against this file. |
| `stop.ps1` | Stop | `hook stop` → `rotation.hook.hook_decision` |
| `autosync.ps1` | *not a hook* — and **not actually on the schedule either**: the registered `\MindHive-AutoSync` task runs `pythonw.exe -m mindhive autosync` directly, bypassing this wrapper. Editing it changes nothing. | (wrapper would call `autosync` via `pythonw.exe`) |

Properties that hold for all of them — do not weaken:

- **Headless guard.** Every script exits 0 immediately when
  `MINDHIVE_HEADLESS=1`, so spawned reflection sessions cannot re-fire hooks or
  mint phantom events. (`stop.ps1` included — relevant if you expect rotation
  during a headless run.)
- **stdout is the decision/context channel — for the gate hooks only.** In
  `pre_tool_use.ps1`, `user_prompt_submit.ps1` and `stop.ps1` nothing may print
  there except the Python decision/context JSON, and silence = allow / no
  injection. `session_start.ps1` (`directives` + `state`) and `pre_compact.ps1`
  deliberately emit plain markdown/text to stdout — that *is* their injection
  mechanism. Do not "fix" them to be silent.
- **Fail-open and hang-proof.** Any error allows the tool and injects nothing.
  Parse/record failures on the `post_tool_use` and `post_tool_use_failure` paths
  land in `hooks_errors.log` (`hookcapture.log_hook_error`, the only writer);
  the other seven scripts fail open via a bare `catch {}` and leave no trace.
- **Absolute interpreter path.** All use
  `C:\Users\hanif\mindhive\.venv\Scripts\python.exe`.

### What composes a prompt's injected context

`__main__.cmd_hook`, stage `user_prompt_submit`, in this order:

1. `promptgate.hook_context(raw)` — fires only on an underspecified prompt.
2. `enrich.extend_hook_context` — **only if** the gate above fired.
3. `mindnet.inject.extend_hook_context` — unconditional (the spine steers every
   turn); sidecar is read **once** here and threaded in.
4. `premise.extend_hook_context` — unconditional.
5. `style.extend_hook_context` — **last**, so it prepends to whatever the
   earlier passes produced. Like `mindnet.inject` and `premise`, it can build a
   context from a `None`/empty one when every earlier pass stayed silent.
6. `print(json.dumps(context))` — then, strictly *after* the print, the
   side-effect layer: `record_direction`, `announce_faculties`, autodeclare.

That ordering is load-bearing. New passes go before step 5, and anything that
writes to the ledger goes after step 6.

## Hazards (learned the hard way — do not repeat)

- The **live dashboard** at http://127.0.0.1:54461 belongs to the human. Never
  kill or restart it, and **never repoint it via `PYTHONPATH`/`sys.path`** — it
  serves whatever branch `C:\Users\hanif\mindhive` has checked out, by way of
  the editable install, and that is the mechanism working correctly. To preview
  an unmerged branch, serve it on another port from its own worktree. After any
  restart, grep the served page for a known hook (`showOversee`,
  `renderOversee`) — HTTP 200 is not evidence.
- Work in a **git worktree**, never directly on master.
- **Read every file before you Edit it** in the same session. A Grep/Glob hit
  does not satisfy the read-gate.
- **`python` is not on PATH** on this machine — use
  `C:/Users/hanif/mindhive/.venv/Scripts/python.exe`. From a worktree,
  subprocesses need `PYTHONPATH=<worktree>/src` explicitly.
- **Never run `scripts/self_reliant/bring_up.sh`** — it downloads gigabytes and
  is gated on explicit human intent.
- The Stop hook is **global**: once registered it fires in every Claude Code
  session on this machine. Keep it fail-open and opt-in (`../CLAUDE.md`).
- Every change to `src/mindhive/` or `tests/` needs `hive log <category> "…"`
  before the work is done. The suite must be green before "done".

---

*Predecessor: [`docs/archive/2026-07-12-session-legend-pr-49.md`](archive/2026-07-12-session-legend-pr-49.md)
— a mission playbook for a PR that merged 2026-07-12, archived unmaintained.*
