# TODOS

Deferred items. Each carries enough context to be picked up cold in three months.

Source: `/plan-devex-review` on 2026-08-10 against `master` @ `efa3751`.
Plan: [docs/plans/2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md](docs/plans/2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md)

---

## 1. No LICENSE, while README publishes a public install command

**What:** Add a `LICENSE` (and optionally `CONTRIBUTING`, `CHANGELOG`), or remove the public
install instructions from README.

**Why:** [README.md:59](README.md:59) tells anyone to run
`/plugin marketplace add hanifridho62-rgb/Lintester-project`, and the repo is public on GitHub.
There is no `LICENSE`, `CONTRIBUTING`, or `CHANGELOG` at the root. Without a license the legal
default is all-rights-reserved: nobody may use or modify it, even though the README invites
them to install it.

**Pros:** closes the gap in one file. **Cons:** none technical; picking a license is a real
decision, not a formality.

**Depends on:** deciding whether MIND HIVE is meant to be installable by other people at all.
If not, delete the marketplace instructions instead.

---

## 2. Dashboard nav mixes navigation and mutation in one column

**What:** Separate the 21 nav buttons into navigation (sections) and actions (mutations).

**Why:** [dashboard_page.py:173–203](src/mindhive/dashboard_page.py:173) renders sections
(`showOverview`, `showOversee`, `showMemory`, …) and actions (`doAction('/api/sync')`,
`runReflect()`, `confirmAction('/api/selftest')`) as visually identical buttons in one list.
This is the deeper cause of "I clicked it and nothing happened": the user has no way to know
which buttons navigate and which mutate, so an action that legitimately produces no visible
change is indistinguishable from a broken navigation.

**Context:** out of DX POLISH scope for the 2026-08-10 plan, which fixes the honesty of the
three worst action buttons but not the affordance problem underneath.

**Depends on:** the honest-zero invariant landing first (plan §The honest-zero invariant), so
this becomes a presentation change rather than a behaviour change.

---

## 3. 38 ungrouped top-level CLI subcommands

**What:** Group `hive --help` output by domain, or introduce a short "start here" set.

**Why:** `hive --help` prints 38 subcommands as one flat list. For Claude in a fresh session,
`--help` and the README are how it discovers what is callable; a flat 38 gives no ordering
signal. SOP 11.10 ("CITE ONLY COMMANDS THAT EXIST") exists because a prior SOP cited three
commands that never did.

**Pros:** cheaper discovery for both consumers. **Cons:** argparse grouping is fiddly; risk of
churning help text people have memorised.

**Depends on:** nothing. Independent.

---

## 4. README is 258 lines with no entry point

**What:** Restructure README: Quick Start at the top, the 50 command bullets demoted to a
grouped reference.

**Why:** README goes from a two-line description straight into 50 flat command bullets. The
base install command (`pip install -e .`) appears nowhere — only `pip install -e .[build]` at
[README.md:254](README.md:254), which builds the .exe. [USAGE.md](USAGE.md) is 26 lines with
one heading; [docs/DATA_MODEL.md](docs/DATA_MODEL.md) is 1415 lines. The newcomer-facing doc is
the thinnest and the internals doc is the thickest.

**Context:** the 2026-08-10 plan adds only a Quick Start block (task T16), riding whichever
branch touches README first. The full restructure is this TODO.

**Depends on:** T16 landing first, so the Quick Start already exists to build around.

---

## 5. Split `__main__.py` (4141 lines, and now holding a raised ceiling)

**What:** Split `src/mindhive/__main__.py` into per-domain modules, leaving only argparse wiring.

**Why:** This repo's own ledger caveat already flags it — over 4x the thermo-nuclear threshold,
and the debt lens proposes it every round it runs. It was 3761 lines when that caveat was
written, 3790 when this item was filed, and **4141** as of 2026-08-14. Every other module in
`src/mindhive/` is 21–692 lines; `__main__.py` is the anomaly.

**What changed since this was filed:** the cap is now enforced.
`tests/test_file_size_cap.py` fails the build on any module past 1000 lines that is not in
`ALLOWED`, and `ALLOWED` is a shrink-only ratchet. `__main__.py` entered that list at 4082 and
its ceiling has since been raised **once**, to 4141, with the arithmetic written into the
comment beside it (33 lines from the `hive repo` reporting work, 26 from the five-gaps work).
That raise is a debt entry, not a fix, and the comment says so. The next person who needs to
raise it again should split the file instead — that is the whole point of a ratchet whose
number is expensive to move.

Two mitigations already in place, so a split is not the only lever:
`bench.register(subparsers)` (`src/mindhive/bench.py`) wires its own group from its own module
and cost this file two lines instead of forty; that is currently the only command that does so,
and its docstring names the deviation. If a second command follows it, the pattern stops being
a deviation and should be written down as the convention — which would also be a cheaper path
out of this item than one large split session.

**Pros:** stops the lens proposing it every round; every later CLI change gets cheaper; restores
the repo's own module-size norm.

**Cons:** a large session of its own; touches nearly every CLI surface at once; high regression
risk in a file with no dedicated test.

**Context for whoever picks this up in 3 months:** `hive` has **39** distinct top-level
subcommands (`append autoresume autosync briefing clusters consolidate context dashboard
directives embed enrich export failures flag gc graph harvest hook log loop mindnet packs
plugins premise project projects recall replenish review roadmap rules selftest session skills
sop state sync triggers verify`), all parsed and dispatched from this one file — 120
`add_parser` calls in total once sub-subcommands are counted. The natural split is one module
per domain, with `__main__.py` assembling parsers and delegating. `pyproject.toml:14` points the
entry point at `mindhive.__main__:main`; that must survive.

**Depends on / blocked by:** the five PRs in
[the 2026-08-10 plan](docs/plans/2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md) landing
first, or the merge conflicts are painful. PR-5 deliberately keeps its `__main__.py` hunks to
argparse wiring only so this rebases cleanly.

---

## 6. Hooks are Windows-only

**What:** POSIX equivalents alongside `hooks/*.ps1`, if MIND HIVE is ever to run elsewhere.

**Why:** `hooks/` contains nine files, all `.ps1`, zero `.sh`: `autosync`, `post_tool_use`,
`post_tool_use_failure`, `pre_compact`, `pre_tool_use`, `session_end`, `session_start`, `stop`,
`user_prompt_submit`. Session rotation, telemetry capture, and SOP injection all ride that
layer, so all of them are Windows-only today.

**Context:** the 2026-08-10 plan states this as an explicit limit (task T17) rather than
building around it. Naming it was the decision; building it is this TODO.

**Depends on:** an actual second machine or platform to justify the work.

---

## 7. `#6b7488` fails WCAG AA contrast

**What:** Raise `#6b7488` until it clears 4.5:1 on the header background, everywhere it is used.

**Why:** Computed, not estimated: `#6b7488` on `#171a23` is **3.87:1**, below the 4.5:1 AA
threshold for normal text. It is used for `#synced` (11px), `nav .group` (11px), and
`#live.paused`. Two of those are also under the 16px body-text floor, which compounds it.
For comparison, `#9aa3b8` on the same background is 7.17:1 and passes comfortably, and the
amber `#e0b341` proposed for the stale chip is 9.24:1.

**Pros:** three surfaces become readable. The sync timestamp is the one thing you look at to
tell whether live refresh is still working, and it is currently the least readable text on the
page.

**Cons:** touches a colour used across the whole file; needs an eye check that nothing else
shifts.

**Context for whoever picks this up in 3 months:** legacy debt, not something the 2026-08-10
plan introduced — that plan's own additions all pass. Cheapest fix is nudging the token's
lightness once, after the colour tokens exist (task T26 in
[the plan](docs/plans/2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md)), so it is one
line instead of a search-and-replace across 123 literals.

**Depends on / blocked by:** T26 (colour token extraction) landing first.

---

## 8. Write `DESIGN.md`

**What:** Document the design system the dashboard already uses.

**Why:** There is no `DESIGN.md`. The system is real and consistent — one colour per meaning, a
stable status glyph vocabulary, utility copy, zero decoration — it is just undocumented. Every
future design review, human or Claude, starts by re-reading 1859 lines to rediscover the rules.
That is exactly what the 2026-08-10 review had to do.

**Pros:** the next review starts from rules instead of archaeology; palette drift (`#9aa3b8`
vs `#8a93a6`, `#e6e8ef` vs `#dfe6f3`) becomes a visible violation rather than an accident;
Claude in a fresh session reads it instead of guessing.

**Cons:** a document that must be kept true. A stale design doc misleads worse than none.

**Context for whoever picks this up in 3 months:** contents should be the colour tokens from
T26, the five status glyphs and their meanings (✓ did_work, ○ nothing_to_do, ● degraded,
✗ failed, ⊘ abandoned), the loading ladder (skeleton at 150ms, status text past 10s), the
"cards only when the card IS the interaction" rule, and an explicit desktop-only statement.
The gstack `design extract` command needs an approved mockup image and there is none — the
mockup generator has no API key configured on this machine — so this is hand-written from the
code. A hand-built reference board with the real palette and glyphs already exists at
`~/.gstack/projects/hanifridho62-rgb-Lintester-project/designs/status-vocabulary-20260810/board.html`.

**Depends on / blocked by:** T26 (colour tokens), or the document names colours that have no
names.

---

## 9. ~~A section render in flight when the project changes paints the old project's data~~ — DONE

Closed 2026-08-12, not deferred. The fix is one scope check in `apiGet`
([dashboard_page.py](src/mindhive/dashboard_page.py)): capture `PROJECT` before the fetch,
throw a named `StaleScope` error if the picker moved before the body arrived. That is the one
choke point every renderer and both pollers already pass through, so it did not need the
nine-renderer change this item was written to warn about. `apiPost` is deliberately left alone
— a mutation ran against the project named in its own body, so its result is worth showing.

Kept here rather than deleted because the reasoning is the useful part: `shouldPaint` compares
SECTION identity, and section identity is not scope identity. Anything else added later that
fetches outside `apiGet` inherits the bug again.

**Behavioural cover:** `a_render_started_for_another_project_paints_nothing` in the node tier-1
suite, with a mutant half that must fail.

---

## 10. ~~`/api/projects` cannot hit the memo it was given — 60 namespaces, an 8-slot LRU~~ — DONE

Closed 2026-08-15. `_STATS_MEMO` now carries its own bound (`_STATS_MEMO_MAX` = 512) via
`_BoundedMemo`, an `OrderedDict` subclass holding `max_entries`; the two fold-holding memos
keep `_MEMO_MAX` = 8. The trap below was honoured — the shared constant was **not** raised, and
a test asserts it stays at or under 16 so a later change cannot quietly take that shortcut.

The same three passes that cost 1329 / 1214 / 2134 ms holding 8 entries now cost
**1002 / 9.3 / 8.4 ms holding all 60**; end to end over HTTP, warm requests went from
1.76–1.86 s to **35–56 ms**. Memory was measured rather than estimated: 443.5 KiB for a fully
populated 60-entry memo, 7.6 KiB mean, and the size tracks segment *files* rather than entries.
The after-measurement is `T5 RESULT` in the plan, which supersedes `T1 AFTER`'s reading of this
endpoint as unimprovable.

Kept rather than deleted, for the same reason as item 9: the diagnosis is the reusable part. An
LRU whose bound is smaller than a sequential scan over its key set has a **0%** hit rate, not a
reduced one — every pass restarts at the key evicted earliest. Any future memo added in front
of a whole-namespace walk inherits this the moment its bound drops under the namespace count.

**What it was:** give `_STATS_MEMO` a bound sized for the namespace count, separately from the two
memos it then shared `_MEMO_MAX` with.

**Why:** `_get_projects` ([dashboard_get.py:62](src/mindhive/dashboard_get.py:62)) calls
`_project_stats` for **every** namespace, and this machine has 60. `_MEMO_MAX` is 8
([dashboard_memo.py:31](src/mindhive/dashboard_memo.py:31)), so a single request writes 60
entries into eight slots and evicts 52 of them before it finishes. The next request misses on
those 52 — permanently. Measured, three identical passes over all 60 in one process:

```
pass 1 (cold)   1328.8 ms    memo entries after: 8
pass 2 (warm)   1213.6 ms    memo entries after: 8
pass 3 (warm)   2133.5 ms    memo entries after: 8
one namespace ('mindhive'), twice:   1207.0 ms  ->  4.0 ms
```

End to end this is why `/api/projects` measured 1781 ms isolated and 12841 ms under load in the
after-profile — essentially unchanged from the 1.76–1.86 s / 11.7–12.4 s before it.

**The trap:** do **not** simply raise the shared constant. `_SESSIONS_MEMO` and `_CATS_MEMO`
entries each hold a full telemetry fold plus every session record for a project, which is what
the cap of 8 was chosen for ([dashboard_memo.py:21-30](src/mindhive/dashboard_memo.py:21)).
`_STATS_MEMO` entries are small count dicts. The fix is a separate bound for the small one, or
an unbounded fingerprinted map with its memory cost measured — not one number for three
different payload sizes.

**The memo is not broken.** On the access pattern its own comment describes — one namespace hot,
the rest cold — it is a 300x win, and that is the picker's pattern and `/api/mindnet/cats`'s.
`/api/projects` is the exact inverse of it inside one request.

**Behavioural cover:** four cases in `tests/test_dashboard_locks.py`, each canary-checked against
mutants. `test_the_picker_caches_every_namespace_it_walks` was the RED-on-old-code one — it seeds
`_MEMO_MAX + 4` namespaces, polls twice and asserts zero ledger reads on the second pass, with its
own guard that the first pass *did* read so it cannot pass for a picker that stopped reading
altogether. `test_the_fold_holding_memos_keep_their_own_small_bound` is the one that makes "just
raise the shared constant" fail. The other two guard the 512 ceiling in both directions and pin
the `getattr` fallback branch for a memo carrying no bound.

**A worked precedent now exists.** `/api/overview` hit the identical wall — one request folds
every namespace — and took the separate-bound route rather than raising `_MEMO_MAX`:
`_OVERVIEW_MEMO` in `dashboard_data.py` with its own `_OVERVIEW_MEMO_MAX` of 128, and
`test_hive_mind_does_not_evict_the_entries_it_just_wrote`
([tests/test_dashboard_overview_memo.py](tests/test_dashboard_overview_memo.py)) is the RED-on-8
test this item asks for, written against 12 namespaces. Neither the constant nor the file this
item names was touched, so the two changes do not collide.

---

## 11. A real-socket test is intermittent, and nobody has caught it in the act

**Source:** observed across full-suite runs, 2026-08-12 → 2026-08-14.

**What:** stabilise
`tests/test_dashboard_http.py::test_a_refused_body_never_becomes_the_next_request`.

**Why:** it has gone green / red / green on one unchanged tree under a full-suite run, and green
every time in isolation. It passed in both full local runs and in CI on 2026-08-14, which is
exactly what makes it dangerous — an intermittent that is currently passing reads as a fixed
one. It is a real-socket test, so the failure is a scheduling race rather than a logic error.

**The technique that already worked once:** the job-state race in this repo was fixed by parking
the work instead of trusting the scheduler. The same shape applies here.

**Cons of leaving it:** the next unrelated red run costs someone an afternoon deciding whether
their change broke it.

---

## 12. The job polling flow has no browser-tier probe

**Source:** deferred from the five-surfaces plan, restated 2026-08-14.

**What:** a `-m browser` test that drives a long operation to completion and asserts the DOM
updates *during* the poll, not only after it.

**Why:** the 202-accepted job flow is the mechanism behind six long operations, and the tier
that would prove the browser actually repaints mid-poll is the one layer never written. Server
tests prove the job endpoint answers; node tests prove the render function behaves. Neither
proves the page changes while a job is running, which is the entire user-visible promise.

**Context:** the browser tier exists and is cheap to extend —
`tests/test_dashboard_browser.py` starts a `ThreadingHTTPServer` on an ephemeral port
(`("127.0.0.1", 0)`) and drives real Chromium; 14 tests run in ~30 s.

---

## 13. `origin` points at a repository name that only resolves by redirect

**Source:** noticed while pushing `feat/close-five-gaps`, 2026-08-14.

**What:** `git remote set-url origin https://github.com/hanifridho62-rgb/mindhive.git`.

**Why:** `origin` is still
`https://github.com/hanifridho62-rgb/Lintester-project.git`, and every push reaches
`hanifridho62-rgb/mindhive` only because GitHub redirects renamed repositories. It works today
and is verifiable (`gh repo view` reports `hanifridho62-rgb/mindhive`; the pushed ref is there),
but the redirect is a courtesy, not a contract — and `README.md:59` still publishes the old name
in a public install command, which ties this to item 1.

**Cons of fixing:** none, beyond remembering that older clones and any script with the old URL
hard-coded rely on the same redirect.

---

## 14. `/autoplan` cannot aggregate a phase on this machine — no `jq`

**Source:** the five-gaps plan run, 2026-08-14.

**What:** install `jq`, or teach the aggregation step a fallback that does not need it.

**Why:** the planning skill writes a JSONL artifact per phase and forbids hand-rolling it.
`jq` is not installed here (`command -v jq` is empty), so that artifact was not written and the
phase cannot be aggregated. Nothing was lost — the plan document itself carries the decisions —
but the machine-readable half of two planning runs is simply absent, and it will keep being
absent silently.

---

## 15. A merged PR whose code never reached master — the whole `fabricate` feature

**Source:** branch audit, 2026-08-14.

**What:** decide whether to land `feat(fabricate): the PRD assembly line` (`3a45aea`) on master,
and until that decision is made, **do not delete**
`claude/prd-assembly-line-40e466` or `claude/session-resumption-marker-40e466`.

**Why this is not an ordinary stale branch:** PR org/repo#71 is `MERGED` — green, closed,
2026-07-18. Its base was `claude/session-resumption-marker-40e466`, **not** `master`. So the
merge succeeded into a branch and the trunk never received a line of it. Verified against
`origin/master` today:

```
src/mindhive/fabricate.py        ABSENT
tests/test_fabricate.py          ABSENT
"fabrication" in events.py       0 occurrences
```

That is 1242 insertions across 7 files, 41 tests the commit message reports as passing, and a
live-verified Windows Task Scheduler dispatch — `hive fabricate compile | dispatch | run | list`,
the `fabrication` event type, and loop-mode autoresume. None of it is reachable from master.

**Cost to rescue, measured (`git merge-tree`, non-destructive):** three conflicts, all in
append-style registries — `docs/DATA_MODEL.md`, `src/mindhive/__main__.py`,
`src/mindhive/events.py`. `src/mindhive/autoresume.py` and `src/mindhive/loop/engine.py`
auto-merge. So the shape is "take both sides in three registries", not a rewrite — but the 41
tests must be re-run against today's master, which has moved a long way since 2026-07-19.

**The generalisable lesson, which outlives this item:** a PR being `MERGED` says only that its
head reached its **base**. It says nothing about master. Any audit that trusts the merged flag
instead of `git merge-base --is-ancestor <sha> origin/master` will report this feature as
shipped.

**Depends on:** nothing. It is independent of every other item here.

---

## 16. ~~`claude/gifted-mirzakhani-d842b5` — graph pinwheel from real nodes~~ — SUPERSEDED

Closed 2026-08-14 by audit, not deferred. The branch carries one unmerged commit `a5c739d`
(2026-06-24), "pinwheel view is now built from the real nodes". Master reached the same idea
independently one day later in `c3ef3e0`, "node-based living pinwheel — symmetric spacing, star
drift, twinkle, buzz", and went further. Master's `graphview.py` is 856 lines against the
branch's 657.

Kept here rather than deleted because the branch still exists and someone auditing it later will
ask the same question: **the branch is safe to delete, nothing unique is lost.** That is the
answer, with the commit that supersedes it named.

---

## 17. `/api/oversee` is still a full read per poll, and it is the harder half

**Source:** the `/api/overview` memo, 2026-08-15 — this is what that change deliberately did
**not** do, recorded rather than dropped.

**What:** put a gate in front of `_get_oversee`
([dashboard_get.py:515](src/mindhive/dashboard_get.py:515)) the way `overview_stats`
([dashboard_data.py](src/mindhive/dashboard_data.py)) now gates `/api/overview`.

**Why it did not ride along — and it is not the fingerprint.** Both producers are pure folds
over `events.read_all` (`list_runs` at
[loop/state.py:111](src/mindhive/loop/state.py:111), `interactive_sessions` at
[oversee.py:234](src/mindhive/oversee.py:234)), so `segment_stats` would key this endpoint
correctly. The obstacle is that the **wall clock decides three separate things inside the fold**,
and a fold cached as-is freezes a board rather than a number:

1. **A loop run's `online`** — `status == "running"` and last event within that run's own
   `heartbeat_seconds` (default 15 min + 60 s slack),
   [loop/state.py:175](src/mindhive/loop/state.py:175).
2. **A chat session's `online`** — last event within `ONLINE_WINDOW_SECONDS` (180 s),
   [oversee.py:314](src/mindhive/oversee.py:314). This is the only one
   `dashboard_memo._with_online` already implements.
3. **Whether a chat session is on the board at all** — idle past `ACTIVE_WINDOW_SECONDS` (6 h)
   and the fold drops it, [oversee.py:308](src/mindhive/oversee.py:308). Cache the fold and an
   aged-out session lingers until the ledger next moves.

All three are re-derivable at serve time from fields the output rows already carry
(`last_event_ts`, plus `status` / `heartbeat_seconds` on loop rows), so the shape of the fix is a
seam like `_with_online` that dispatches on `kind` — **not** a second copy of each window's rule,
which is what that function's docstring exists to forbid.

**Two genuine off-ledger inputs**, both verified by reading, not assumed:

* `list_runs` calls `_fix_plan_items(r["goal_dir"])`
  ([loop/state.py:180](src/mindhive/loop/state.py:180)), which reads
  `<goal_dir>/fix_plan.md` from disk. A run's plan can change with no event appended. In practice
  a progressing run appends `loop_iteration` rows anyway, but "in practice" is not a cache
  invariant — the goal_dirs held by the cached entry would have to join the fingerprint, the way
  `chain.json` joined `/api/overview`'s. New goal_dirs need no special handling: a run cannot
  appear without a `loop_run_start` event, which moves the ledger stamp on its own.
* `oversee.intended_use_map()` ([oversee.py:51](src/mindhive/oversee.py:51)) comes from the
  **plugin inventory on disk** and is TTL-memoized for 60 s. It feeds `skill_trails` inside the
  fold, so a fold cached against a ledger stamp can outlive the map it was built from.

**A free win to take while in there:** `_get_oversee` reads the ledger **twice** per namespace —
once itself and once inside `list_runs`, which has no `evs=` parameter the way
`interactive_sessions` does. Adding one halves the cost of every miss, which matters more here
than on `/api/overview` because misses stay common on an actively-written namespace.

**Why it is a session of its own, not a fourth line in `_overview_fingerprint`:** four inputs
(ledger, two off-ledger sources, and the clock) against the panel's highest-visibility surface.
Getting it wrong does not return a stale number — it freezes a board that reports whether work is
alive.

**Measured cost:** 1114 / 1448 ms isolated and 4740 / 6843 ms under load in the after-profile
([T1 AFTER](docs/plans/2026-08-10-001-fix-feedback-loop-five-surfaces-plan.md)) — the same order
as `/api/overview` before its gate, and it is polled by the Oversee section.

**Behavioural cover needed, beyond the overview file's:** three tests that hold the ledger still
and move only the clock — a loop card must go offline past its own `heartbeat_seconds`, a chat
card past 180 s, and a chat card must **leave the board** past 6 h. Each is RED against a fold
cached without a re-derivation seam, which is the point, and why this is a separate piece of work
rather than a fourth line in `_overview_fingerprint`.

## 18. ~~`pgtools.py` is 1253 lines and now holds a twice-raised ceiling~~ — DONE

`tests/test_file_size_cap.py` caps a module at 1000 lines. `pgtools.py` went past it
with the state-shaped trio (`get_latest_event`, `search_events_by_type`,
`get_current_state`) and was added to `ALLOWED` at 1180 — a debt entry, which the cap's
own message says is not a fix. It has been raised twice since: 1233, then **1253** when
`_stale_notice` grew the repair command and the actor who can run it. Neither raise added
surface area, and neither is a reason not to split.

**The split that fits:** the module is two things wearing one name. The **result shaper**
(`summarise_payload`, `stream_rows`, `shape_rows`, `envelope`, `error_envelope`,
`notice_for`, `_fit_response` — roughly 350 lines) is pure Python over rows and has no
opinion about which tool called it. The **seven tools** are argument validation plus one
SQL statement each. `pgshape.py` + `pgtools.py` is the obvious cut.

**Why it was not taken in the same change as the tools:** this repo has already been bitten
by exactly that refactor — "a mechanical module split silently breaks monkeypatch
addresses" (splitting `dashboard.py` moved three patch targets). `tests/test_pgtools.py`
patches `pgtools._run`, `pgtools._session` and `pgstore.mirror_health` by module path, and
`tests/test_mcp_server.py` patches through the registry. Mixing a refactor into the feature
that adds the tools would leave neither verified.

**What makes it safe when taken:** do it as its own change, with the 141 tests in
`tests/test_pgtools.py` green before and after, and re-run `pytest -m pg` (32 tests, needs a
server — and now a CI job, see item 20) because the shaper is what every one of those
envelopes goes through.

**Done, 2026-08-19.** `pgshape.py` is 460 lines, `pgtools.py` is 877, and the entry is
**deleted** from `tests/test_file_size_cap.py` rather than lowered — both are under the
1000-line cap. The block was moved by anchor-located line range rather than retyped, so its
docstrings are byte-identical to what they were. The monkeypatch hazard was answered rather
than hoped away: `pgtools` re-exports every moved name and `tests/test_pgshape.py` asserts
each one is the SAME OBJECT as `pgshape`'s, so an import that silently forked into a copy
fails a test instead of a production path. The names the tests actually patch never moved.

## 19. ~~The mirror does not follow the ledger — `_insert_one` still raises~~ — DONE

**Resolved by deleting the seam, not by implementing it.** `pgstore.mirror_one` and
`_insert_one` are gone; `events.append_event` now writes one line to one file and returns,
under **every** value of `MINDHIVE_EVENT_BACKEND`. The mirror follows the ledger from a
schedule instead: `pgmirror.refresh_changed` reloads only the segments whose bytes moved,
called by `hive pg refresh` and by every `hive autosync` run that finds an admin DSN.

The three reasons deletion beat implementation, all of them already in the entry above and
none of them softened by a fix:

* **The credential.** `pgstore` holds the reader DSN and `mindhive_reader` carries
  `default_transaction_read_only` set on the ROLE, so the INSERT was refused by the server
  on every call. Repairing the seam meant putting the owner DSN on the append path, which is
  the one thing the module split exists to prevent.
* **`seg_line`.** A second write path would have had to compute it, and it is
  `scan_segments`' `enumerate` index over every *physical* line — blank and torn rows
  included. Two loaders agree until the first segment gains an empty line.
* **The cheaper alternative, priced.** It was the right one. A per-segment reload writes the
  rows of one session file, not 37k; the write is incremental, the verification fold
  deliberately is not, because `parity` is what re-stamps `ledger_fingerprint` and a refresh
  that skipped it would report the mirror stale forever.

**Kept as a known limit:** the fingerprint is per-segment byte size, so a same-length
in-place edit is invisible to it. That is corruption rather than an update on an append-only
hash-chained ledger, and `parity` — the chain comparison — is what catches it;
`tests/test_pg_live.py::test_a_same_length_in_place_edit_is_invisible_to_the_fingerprint`
states the limit and proves parity still bites. If the fingerprint ever gains a field,
`pgstore._ledger_fingerprint` has to gain it in the same change or every namespace reports
itself permanently stale.

## 20. ~~The `pg` tier does not run on CI~~ — DONE

A sixth job, `pg-tests`, runs `pytest -m pg` against a `postgres:17` service container on
`ubuntu-latest` (the service-container feature is Linux-runner only, so this job cannot be
folded into the Windows matrix). It applies the migrations with the owner DSN, then mints a
random password for `mindhive_reader` with `openssl rand`, masks it with `::add-mask::`, and
passes it to the tests through `$GITHUB_ENV` — so no credential is written down anywhere,
which is the same rule `tests/test_pg_repo_gates.py` enforces on the repo.

**Non-vacuity, proven:** two mutants to `003_roles_and_views.sql`, each applied to a
throwaway database — deleting the reader's `GRANT SELECT` on the read-only view, and handing
the reader the base table the split exists to hide — each turn the tier red on the test that
names the property. So the assertions bite. **Not proven:** that the workflow YAML itself
runs them, which needs a push and a real run; the job is unexercised until this branch is
pushed and its checks come back.
