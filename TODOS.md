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

## 5. Split `__main__.py` (3790 lines)

**What:** Split `src/mindhive/__main__.py` into per-domain modules, leaving only argparse wiring.

**Why:** This repo's own ledger caveat already flags it — nearly 4x the thermo-nuclear threshold,
and the debt lens proposes it every round it runs. It was 3761 lines when that caveat was
written and is **3790** now. Every other module in `src/mindhive/` is 21–692 lines;
`__main__.py` is the anomaly.

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
