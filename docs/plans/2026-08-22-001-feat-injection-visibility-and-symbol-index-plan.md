# Context-injection visibility, and the zero-token symbol index

Source: `/plan-devex-review` on 2026-08-22 against `claude/zero-token-static-analysis-51c7d6` @ `191cf1c`
(branch is level with `master`; no plan file existed before this one).

Mode: **DX EXPANSION**. Product type: **CLI Tool + Platform** (`hive`, 38 subcommands) with a
dashboard UI surface and a Claude Code hook integration.

Every number in this plan was measured on this tree during the review. None of it is recalled.

---

## 1. The ask

Two features, one plan (decision D2):

1. **Zero-token structural extraction.** Deterministic local tooling maps symbols and
   dependencies instead of shipping source text to the model. The LLM receives a compact
   symbol index — hundreds of tokens, not thousands — and asks for a function body only when
   it actually needs one.
2. **Context-injection visibility in Oversee.** Every time context is injected into Claude, an
   "injection" mark appears. The mark is a cat. Hover shows the injection's title; click shows
   the whole injected context. Everything that was injected is shown, **including what did not
   get through**. The source of every injection is found and named — the SOP path and the
   force-a-skill path included — so that no form of context injection is invisible.

Feature 2 subsumes feature 1's delivery: once the symbol index exists it becomes the seventh
injector and inherits the cat, the panel, and the verdict for free.

---

## 2. Decisions locked in review

| # | Decision | Chosen |
|---|---|---|
| D1 | Review depth | **DX EXPANSION** |
| D2 | Scope | **One plan, both features** |
| D3 | Where the cats attach in Oversee | **One row per prompt-turn inside the session card** |
| D4 | What counts as a failed injection | **Three states: verbatim / partial / silent** |
| E1 | Token accounting | **Fold per-turn cost into `hive context --check`** |
| E2 | Failed-injection cat | **Its own visual state, never mere absence** |
| E3 | Symbol index selection | **Import-graph ranking (aider-style), not flat + alphabetical truncation** |
| E4 | Replay | **Store and replay the whole composed block, per turn** |
| T1 | Non-Python languages | **Build the pluggable extractor seam now**; tree-sitter itself stays deferred |
| T2 | `injected` naming collision | **Fix in this branch** |
| T3 | Browser-tier probe | **Build alongside the feature** |

Nothing was deferred to TODOS.md by this review.

---

## 3. What already exists (reuse it; do not rebuild it)

The founding risk for this plan was re-implementing a feature that already ships. Most of the
requested UI **already exists and works**.

| Requested | Already implemented at | State |
|---|---|---|
| Pixel-art cat, hover → tooltip, click → panel | [cat_board.py](../../src/mindhive/cat_board.py) — `CAT_GRID:33`, `CAT_PALETTES:53`, `catSVG:195`, `mnPanel:221`, `catPanel:256` | complete |
| Seven faculty cats, active/dim/vanish bands | [ui_cats.py:191](../../src/mindhive/mindnet/ui_cats.py:191) `board`, [telemetry.py:105](../../src/mindhive/mindnet/telemetry.py:105) `FACULTIES` | complete |
| A second cat shape sharing one panel | `cat_board.py` `.mn-sess` + `sessPanel:271` + `PANEL_OWNER:283` | complete |
| Context injection via SOP | [sopgate.py:347](../../src/mindhive/sopgate.py:347) — mandatory SOP bodies verbatim on every human prompt | complete |
| Context injection that forces particular skills | [promptgate.py](../../src/mindhive/promptgate.py) gate + `advice.skills` → the `[mindnet] Skills matched to this task` block | complete |
| A success/failure vocabulary for injection | [sopgate.py:189](../../src/mindhive/sopgate.py:189) `VERDICTS = ("silent","pointers_only","partial","verbatim")` | complete |
| A Tier-1-quality verdict sentence | `sopgate.verdict_sentence` → *"every mandatory body rides in full — 1 SOP(s), 2601 of 3000 chars spent"* | complete |
| Steps in Oversee, click-for-detail | [dashboard_js_oversee.py:159](../../src/mindhive/dashboard_js_oversee.py:159) `.rmstep` | complete |
| A 2.5 s poll that already runs whatever section is open | [dashboard_js_actions.py:147](../../src/mindhive/dashboard_js_actions.py:147) `pollCats` → `/api/mindnet/cats` | complete |
| Python AST parsing, no new dependency | [mindnet/enforcer.py:69](../../src/mindhive/mindnet/enforcer.py:69) `import ast` | complete |

Measured live:

```
$ hive sop injection
Per-prompt SOP injection: verbatim — every mandatory body rides in full
  body     2,601 chars  execution-discipline  (Execution discipline - prepare, understand, loop until the prompt is satisfied)
  bodies 1/1 · body chars 2,601/3,000 · largest 2,601 · injected block 2,943 chars
```

---

## 4. What is missing — eight gaps, all measured

The injection pipeline is six calls in one function body,
[`__main__.py:102-195`](../../src/mindhive/__main__.py:102):

```
promptgate.hook_context(raw)                    :111   conditional (fires only on a vague prompt)
enrich.extend_hook_context(ctx, raw)            :113   conditional (only when promptgate fired)
mn_inject.extend_hook_context(ctx, raw, sc=)    :127   unconditional
premise.extend_hook_context(ctx, raw)           :136   unconditional
sopgate.extend_hook_context(ctx, raw)           :143   unconditional
style.extend_hook_context(ctx, raw)             :150   unconditional, LAST, prepends
print(json.dumps(context))                      :152
```

Measured on this tree, one real prompt, one run:

| injector | delta chars | ~tokens | ms |
|---|---:|---:|---:|
| promptgate | 992 | 248 | 116 |
| enrich | 0 | 0 | 54 |
| mindnet | 1,320 | 330 | 0 |
| premise | 0 | 0 | 0 |
| **sopgate** | **2,945** | **736** | 1 |
| style | 676 | 169 | 43 |
| **TOTAL** | **5,933** | **~1,483** | **~214** |

`inject.engaged_faculties(spine)` on the same turn: **`['sop']`** — one cat.

### G1 — The injected text is never stored

`print`ed to stdout, then gone. The cat records a *faculty name*, a status, a task_id and a
phase ([telemetry.py:131](../../src/mindhive/mindnet/telemetry.py:131) `Activity`). Nothing
anywhere holds the text. **"Click to show the whole injected context" has no data source today.**

### G2 — Four of six injectors light zero cats, and the one cat that lights is mislabelled

[`inject.py:128`](../../src/mindhive/mindnet/inject.py:128):
`_ADVICE_FACULTY = {"sops": "sop", "constraints": "improvements"}`. The `skill-plugin` and
`history` cats come from `promptgate.record_direction`. That is the whole producer set for the
prompt path.

So `premise`, `style`, `enrich` and **`sopgate`** light nothing. And the cat labelled **"SOPs"**
on the board is lit by the mindnet advice sidecar's `sops` rows — **not** by `sopgate`, the pass
that actually delivers the mandatory SOP and which is **50% of the measured payload**. Two
different subsystems, one label, and the bigger one is dark.

`FACULTIES` is a seven-member tuple pinned by `tests/test_mindnet_telemetry.py` against
`events.py` and the architecture decomposition. **Do not extend it** to hold injector names; that
pin exists precisely to stop a synonym from silently rendering no cat.

### G3 — Failure is invisible by design, and the design is correct

All six injectors end in `except Exception: return context`. That fail-open property is
deliberate and load-bearing — a raise here does not degrade a feature, it breaks the user's
prompt — and `CLAUDE.md` protects the same property for the rotation hook. The consequence is
that **"what did not get injected" is zero rows, everywhere.**

Recording must therefore ride a **separate path** that cannot itself throw into the prompt.

**Reproduced live on 2026-08-23, by accident, while re-measuring for this review.** The
measurement harness passed the hook payload as a `dict` where a JSON *string* was expected.
`json.loads` raised `TypeError` inside three injectors; all three `except Exception` blocks
absorbed it:

```
payload as JSON string : promptgate=1464  sopgate=2943  style=674
payload as dict (wrong): promptgate=0     sopgate=0     style=0
stderr captured: ''
```

Nothing raised. Nothing logged. `stderr` was **empty**. The result still looked entirely
plausible — a well-formed context dict carrying 1,319 chars of `mindnet` content — and the first
version of this re-measurement reported those numbers as a finding before the error was caught.

That is the whole feature in one paragraph: **the author of this plan, measuring this exact
pipeline, on purpose, could not tell that 4,082 of 4,940 characters had silently failed to
inject.** A user who is not measuring has strictly less chance. G3 is no longer a predicted
failure mode — it is a recorded one.

### G4 — Oversee has no cats at all

The strip mounts in the dashboard header and on the swarm page
([dashboard_page.py:204-206](../../src/mindhive/dashboard_page.py:204)). Oversee renders session
cards, skill trails and roadmap steps. Zero cats, zero injections.

### G5 — The one budget number you trust is wrong

[`context.py:2`](../../src/mindhive/context.py:2) says it plainly: *"Context-budget analyzer for
the **always-on session surface**."*

```
$ hive context --check
Auto-loaded context: ~48,004 est. tokens (193,214 bytes across 353 contributors)
  claude  ~ 33,752 est. tokens    70%  (349 item(s))
  hive    ~ 14,252 est. tokens    30%  (4 item(s))
  budget  51,000 est. tokens -> under by ~2,996
```

The per-turn hook injection is **not in that number**. Measured on 2026-08-23 across five prompt
shapes, all six injectors, same session, same tree:

| prompt shape | promptgate | enrich | mindnet | premise | sopgate | style | total | ~tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| the real `/plan-devex-review` prompt | 0 | 0 | 1319 | 0 | 2945 | 676 | 4,940 | ~1,235 |
| a short slash command | 0 | 0 | 1319 | 0 | 2945 | 676 | 4,940 | ~1,235 |
| a vague one-liner (`fix the thing`) | 1464 | 367 | 1320 | 0 | 2945 | 676 | 6,772 | ~1,693 |
| a clear code request | 970 | 0 | 1320 | 0 | 2945 | 676 | 5,911 | ~1,477 |
| a bare greeting (`halo`) | 0 | 0 | 1319 | 0 | 2945 | 676 | 4,940 | ~1,235 |

Three things this table settles that the earlier ~1,483 estimate only implied:

1. **There is a constant floor of 4,940 chars / ~1,235 tokens on every prompt** — `mindnet` +
   `sopgate` + `style` are near-invariant. Typing `halo` costs the same ~1,235 tokens as the real
   review request. "Under by 2,996" is spent in **two greetings**.
2. **The variable part swings 0 → 1,464 chars**, entirely inside `promptgate`, driven by
   `assess(prompt)["skip"]` at [promptgate.py:899](../../src/mindhive/promptgate.py:899) and
   `block_for` at [sopgate.py:367](../../src/mindhive/sopgate.py:367). Payload size is a function
   of **prompt text**, so no two turns are reliably comparable — which is precisely why a
   recorded per-turn row, not a static budget figure, is the thing that answers "what did I get?"
3. `enrich` fired exactly **once** in five shapes, and only alongside `promptgate`'s gate branch.

Adding a second mandatory SOP would read as safe right up until the block silently degrades to
pointers.

### G6 — `injected` already means something else in this exact card

[`dashboard_js_oversee.py:212`](../../src/mindhive/dashboard_js_oversee.py:212) renders a pill
`"N injected"` whose tooltip reads *"work the injector added to this run's plan from the
record"* — the **replenishment** injector, a different subsystem entirely. Putting a per-turn
context-injection row in the same card gives one word two meanings. (T2: fixed here.)

### G7 — Nothing documents the pipeline as a pipeline

[docs/session-legend.md](../../docs/session-legend.md) names all six modules — rows 75-79, 103 —
but as six unrelated table entries. **The order lives only in `cmd_hook`'s body**, and the order
is load-bearing: `style` runs last precisely so its prepend survives truncation of anything
earlier.

### G8 — `premise` contributed zero bytes in every shape tested

Across all five prompt shapes above, `premise.extend_hook_context` added **0 characters** — not a
small block, not a pointer, nothing. G2 counted it among the injectors that light no cat; the
sweep shows it may also be an injector that, for ordinary prompts, produces no output at all.

This is a **finding, not a defect claim**: `premise` is designed to fire on prompts that *assert*
checkable facts (paths, roadmap ids, test counts), and none of the five shapes asserted any. That
is very likely correct behaviour. But it cannot be distinguished from a broken injector by any
signal available today, which is the same blindness G1 and G3 describe arriving at a third door.

The visibility feature answers it directly: a `silent` verdict with the reason *"no checkable
claims in prompt"* is a different row from no row at all. **Do not "fix" `premise` on the strength
of this table** — instrument it first, then look.

### Zero-token extraction: nothing exists yet

`grep -rn "tree_sitter|tree-sitter|ctags"` over `src/` → nothing. Measured source mix:

```
py:   303 files, 125,241 lines      src/mindhive: 135 modules, 55,950 lines
js:   0 files    ts: 0 files    html: 0 files
```

[`pyproject.toml:6`](../../pyproject.toml:6) — `dependencies = []`, deliberately, with the
comment *"Runtime `dependencies` stays EMPTY — `hive`, the dashboard, the hooks and the whole
default test tier must keep working with neither installed."*

**tree-sitter would be the first runtime dependency ever admitted, and it would buy zero
coverage** — there is no non-Python source file to parse. `ast` covers 100% of it and is already
imported in this tree.

---

## 5. Developer Persona Card

```
TARGET DEVELOPER PERSONA
========================
Who:       Owner-operator of MIND HIVE — one person, one machine, debugging their own agent
Context:   Claude just ignored a standing procedure. "Did the context actually reach it?"
Tolerance: ~2 minutes. If the answer needs six modules read, they stop and guess instead
Expects:   The dashboard is already open on 127.0.0.1:54461; the answer must be there,
           not behind a CLI command they have to learn
Secondary: Claude itself is a consumer of this API. The injected block IS the DX surface
           for an LLM, and a block it cannot parse is a DX failure with no bug report
```

## 6. Developer Empathy Narrative

> Claude just broke `execution-discipline`. I open `http://127.0.0.1:54461`. There is a cat strip
> in the header — three lit: Skills, SOPs, Improvements. I hover: *"SOPs — active, last seen 12s
> ago"*. I click: task, phase, skill, state, last seen. **Not one of them tells me what was
> sent.** I know the SOP faculty "lit", but not which SOP, not how many characters, not whether
> the body rode in full or degraded to a pointer. I switch to Oversee — session cards, skill
> trail, roadmap steps. No cats. No injections. I type `hive sop injection` and it answers one
> injector of six, the mandatory-SOP one. For the other five there is no command, no event, no
> row anywhere. I end up reading `__main__.py` to remember who injects at all. Six minutes. What
> I needed was one sentence.

## 7. Competitive DX Benchmark

| Tool | Approach | Number | Source |
|---|---|---|---|
| **aider repo map** | tree-sitter AST + PageRank over the import graph, SQLite cache | **1,024-token default budget**, `--map-tokens` | [aider.chat/docs/repomap](https://aider.chat/docs/repomap.html) |
| **Langfuse** | end-to-end tracing, prompt management, retrieved-context per span | MIT core | [qaskills.sh](https://qaskills.sh/blog/langfuse-vs-arize-phoenix) |
| **Arize Phoenix** | OpenTelemetry + OpenInference; retriever/tool/LLM spans carry typed attributes | standard conventions | [qaskills.sh](https://qaskills.sh/blog/langfuse-vs-arize-phoenix) |
| **MIND HIVE today** | 1 of 6 injectors reported; text not stored; failures unrecordable | **TTA ≈ 6 min** | measured, §6 |
| **MIND HIVE target** | 6 (then 7) injectors, three verdicts, replay, per-turn cost | **TTA < 30 s** | this plan |

aider settles the feasibility of feature 1: a symbol index for a repo this size is a
**~1,024-token** artifact, exactly the "hundreds of tokens" the ask claims. Phoenix and Langfuse
settle the shape of feature 2: one span per injection, full text as an attribute.

**Metric honesty.** Classic TTHW does not apply — nothing is installed. Two honest metrics
replace it: **TTA-inject** ("did my context arrive?" → answered) and **TTHW-symbols**
(`hive symbols` from zero to useful output, target < 2 min, Champion tier).

## 8. Magical Moment Specification

**The moment:** the operator clicks a cat on a turn row and sees **the exact text Claude read on
that turn, byte-identical**, with each injector's contribution labelled and coloured by verdict.
Not six fragments to reassemble mentally — the composed block, in composition order, with
`style` at the top where it actually lands.

**Delivery vehicle:** the panel that already exists. `mnPanel` (`cat_board.py:221`) is already
the one renderer both the faculty cat and the session cat share; the injection panel becomes the
third caller. No new component, no hosted sandbox, no install step. The dashboard **is** the
playground, and it is already open.

**Requirements for the moment to land:**
- The composed block is stored (E4), capped, and content-deduped against the previous turn.
- The panel renders it with `textContent`, never `innerHTML` — every value here is ledger-supplied
  prose, and `cat_board.py` already holds that rule.
- Verdict colour is on each row, so "verbatim" and "partial" are distinguishable at a glance
  before any text is read.

## 9. Developer Journey Map

| Stage | Developer does | Friction found | Resolution |
|---|---|---|---|
| 1. Discover | Sees cats in the header | Cats say a faculty is awake, never what it said (G1) | fixed — turn row + panel |
| 2. Ask | "Was the SOP injected?" | 4 of 6 injectors have no cat; the lit one is mislabelled (G2) | fixed — per-injector rows, `mandatory-sop` named apart from `sop` |
| 3. Check failure | "What did *not* get through?" | Zero rows exist anywhere (G3) | fixed — three-state verdict on a separate recording path |
| 4. Locate | Opens Oversee | No cats in Oversee at all (G4) | fixed — row per prompt-turn in the session card |
| 5. Budget | Runs `hive context --check` | Number omits ~1,483 tok/turn (G5) | fixed — `hook` origin row (E1) |
| 6. Read | Reads the card's pills | `injected` means two things (G6) | fixed — pill relabelled `N added to plan` (T2) |
| 7. Learn | Looks for docs | Pipeline order lives only in a function body (G7) | fixed — generated pipeline table in session-legend + README |
| 8. Extend | Adds a 7th injector | Must edit `cmd_hook`'s body; panel cannot know | fixed — `INJECTORS` registry |

## 10. First-Time Developer Confusion Report

```
FIRST-TIME DEVELOPER REPORT
============================
Persona: owner-operator, MIND HIVE
Attempting: find out whether a standing procedure reached Claude this turn

T+0:00  Opens 127.0.0.1:54461. Header strip shows three cats. Hovers "SOPs".
        Tooltip: "SOPs — active, last seen 12s ago". No content.            [G1]
T+0:30  Clicks. Panel: task, phase, skill, state, last seen. Still no text.  [G1]
T+1:00  Switches to Oversee expecting per-step detail. Zero cats there.      [G4]
T+1:30  Sees a pill reading "3 injected", opens it — it is roadmap work
        added by the replenishment injector. Wrong "injected".              [G6]
T+2:30  Runs `hive sop injection`. Correct, useful, and answers 1 of 6.
        Nothing says it answers only 1 of 6.                                 [G2]
T+4:00  Greps for other injectors. Finds six calls in __main__.py:102-195.
        No doc lists them as a pipeline.                                     [G7]
T+6:00  Reads the source to reconstruct what was sent. Gives up on
        confirming whether anything was truncated.                           [G3]
```

Every item above is addressed by this plan. None are deferred.

---

## 11. Architecture

### 11.1 `src/mindhive/injectors.py` — NEW. The registry and the one legend.

Follows the committed `FACULTIES` / `_PRODUCER_PROBES` idiom: one home for the name set, membership
checked rather than restated, and a test that fails loudly on drift.

```python
VERDICTS = ("verbatim", "partial", "silent")   # D4. sopgate's finer
                                               # "pointers_only" survives in `reason`.

class Injection(NamedTuple):
    name: str        # member of INJECTOR_NAMES
    verdict: str     # member of VERDICTS
    chars: int
    tokens: int
    ms: int
    reason: str      # the Tier-1 sentence: what + why + fix + the numbers
    text: str        # this injector's own contribution, capped

INJECTOR_NAMES = ("promptgate", "enrich", "mindnet", "premise",
                  "sopgate", "style", "symbols")
EVENT_TYPE = "context_injected"
```

`INJECTOR_NAMES` is **not** `telemetry.FACULTIES` and must never be merged into it. `FACULTIES` is
pinned to the MN-5 decomposition by `tests/test_mindnet_telemetry.py`; these are a different axis
(who injected) from that one (which faculty was consulted). A test pins them as *distinct* sets.

### 11.2 Each injector grows a `probe()`, the shape `sopgate` already has

`sopgate.probe()` ([sopgate.py:192](../../src/mindhive/sopgate.py:192)) is the existing proof that
a pass can report what it will deliver without re-deriving it — *"the report cannot claim
'verbatim' while the hook ships pointers"*. Generalise that, do not invent something new.

Why a `probe()` per injector rather than measuring the delta from outside: an external delta of 0
cannot distinguish **"nothing to say this turn"** from **"raised and fell open"**. Those are two of
the three states D4 asked for. Only the injector knows which.

The recorder wraps the six calls **outside** their own `try`, so the fail-open contract in G3 is
untouched: the injectors keep swallowing everything, and the recorder's own write is separately
guarded and best-effort — the same contract `telemetry.emit` and `promptgate.record_direction`
already hold.

### 11.3 Storage: ONE event per turn, not six

`events.py` already warns that emission rate on a UI feed lands on `hive verify`'s runtime for the
life of the store. Six rows per prompt × ~200 prompts/day against a **41,488-event** ledger whose
`read_all` measures **430 ms** is a real capacity problem, not a hypothetical one.

- **One `context_injected` row per turn**, carrying all seven verdicts.
- Registered in `DETERMINISTIC_TYPES`, **never** `SEMANTIC_TYPES` — machine telemetry carrying no
  human intent must not land a LEDGER markdown file, a Memory-view row and a recall-corpus entry
  per prompt. Same reasoning `mindnet_activity`, `policy_denied` and `work_injected` already carry.
- **Content dedup for the replay text (E4).** `sopgate` + `style` are ~3,621 of 5,933 chars and are
  near-constant turn to turn. Store the composed block's hash always; store the block itself only
  when the hash changed, otherwise `{"same_as": "<hash>"}`. Measured saving on the current mix:
  ~60%.
- Hard size cap on every stored string, reusing the `MAX_BLOCK_CHARS` idiom from
  [inject.py:90](../../src/mindhive/mindnet/inject.py:90).

### 11.4 The Oversee turn row (D3)

Chosen over attaching to `.rmstep` because the two are different axes: a roadmap step can span 40
turns, and a turn can touch no roadmap at all — so `.rmstep` would drop the **most common** case
silently, which is the exact failure `ui_cats.py` was written to prevent.

- New container `#mn-inject`, rendered inside `ovCard`'s body, one row per turn, newest first.
- Reuses `catSVG(idCat(name), 18)`, `mnPanel`, `mnOpen`/`mnClosePanel` with a third
  `PANEL_OWNER` value `'inj'` — the ownership rule already in `cat_board.py:283` is what stops one
  strip's repaint from closing another's open panel.
- Hover → `el.title` = the injection's title + verdict + chars. Click → `mnPanel` with the injector's
  own text, plus a **"whole turn"** control that renders the composed replay block (E4).
- A repaint-signature guard like `SESS_SIG` (`cat_board.py:395`), so a 2.5 s poll cannot restart a
  1.6 s animation or close a panel being read.

### 11.5 Three visual states (E2)

Absence cannot express failure. `cat_board.py` already pays for this lesson twice — the amber
`.blind` class exists because *"'nothing is happening' and 'I cannot know' rendered identically"*.

| verdict | class | look | meaning |
|---|---|---|---|
| `verbatim` | `.mn-inj.verbatim` | green, bobbing | everything this injector had, delivered whole |
| `partial` | `.mn-inj.partial` | amber (`#ff9e64`, the existing `.blind` colour) | it spoke, and something was dropped or degraded to a pointer |
| `silent` | `.mn-inj.silent` | grey, `grayscale(100%)` | nothing to say this turn, or it fell open |

`reason` distinguishes the two `silent` cases in the panel; the colour does not, because the
operator's next action is the same either way.
`prefers-reduced-motion` is inherited for free — the guard keys on `.mn-cat-sprite`, which
`catSVG` stamps on every cat it draws (`cat_board.py:204`).

### 11.6 `src/mindhive/symbols.py` — NEW. Zero-token structural extraction.

Stdlib `ast` only. No runtime dependency. `pyproject.toml`'s `dependencies = []` is untouched.

```
extract(path)       -> {"file", "imports", "classes": {name: {"methods", "bases"}}, "functions"}
graph(files)        -> {module: {imported_module, ...}}     # falls straight out of extract()
rank(graph)         -> {module: score}                      # PageRank, pure Python, ~20 lines
index(root, budget) -> ranked, budget-capped symbol index   # default budget 1,024 tokens
```

**Ranking (E3), not alphabetical truncation.** At 303 files / 125,241 lines a flat listing cannot
fit any budget, so something chooses. Without ranking the chooser is `sorted()`: `autoresume.py`
lands and `__main__.py` (4,237 lines, the centre of everything) is cut. The import graph is already
in hand from `extract()`, so ranking costs one function, not one dependency.

**Pluggable extractor seam (T1).** `EXTRACTORS = {".py": _ast_extract}` with the lookup by
suffix. A future language adds a module and an entry; tree-sitter, if ever needed, arrives as an
**optional extra** beside the existing `pg` / `mcp` / `browser` extras, never as a runtime
dependency. Recorded limit: the dashboard's JavaScript lives inside Python string literals
(`cat_board._JS`, `dashboard_js_*`), so `ast` cannot see `paintCats` or `catPanel`. The seam is
where that gets fixed; this plan does not fix it.

**Surfaces:** `hive symbols [PATH] [--budget N] [--json]`, and
`symbols.extend_hook_context(...)` as the **seventh injector** — which means it arrives with a cat,
a verdict, a panel and a cost line already built by feature 2.

### 11.7 Token accounting (E1)

`context.py` grows a third origin alongside `claude` and `hive`: **`hook`**, fed from the last
`context_injected` row. `hive context --check` then reports the true total instead of a
session-start-only total that reads 2,996 under budget while a single turn spends half of that.

### 11.8 Naming (T2, G2, G6)

- `dashboard_js_oversee.py:212`: pill label `N injected` → **`N added to plan`**. The tooltip text
  is already correct; only the label collides.
- The header cat keeps the label **`SOPs`** for `advice.sops`. The injector row uses
  **`mandatory-sop`** for `sopgate`. Two subsystems, two names, and a test that asserts they are
  spelled differently.

### 11.9 Documentation (G7)

`docs/session-legend.md` gains one **pipeline table** — the six (then seven) injectors in
composition order with their conditionality — generated from `INJECTORS` so it cannot go stale, plus
a README section. The load-bearing fact that `style` runs last so its prepend survives truncation
gets written down where someone will find it.

---

## 12. NOT in scope

- **tree-sitter itself.** The seam ships (T1); the parser does not. Zero non-Python source files
  exist to justify the first runtime dependency this project has ever taken.
- **Extracting the dashboard JS out of Python string literals.** Named as the symbol index's known
  blind spot; a separate refactor.
- **LICENSE / CONTRIBUTING / CHANGELOG.** Still absent while README.md:59 publishes a public install
  command. Already TODOS #1 since 2026-08-10; this review adds no new information and does not
  reopen it.
- **Splitting `__main__.py` (4,237 lines) or `replenish.py` (2,781 lines).** Both are past the
  thermo-nuclear threshold and both are separately tracked.
- **Retro-filling injection history.** The ledger has no rows for turns already taken; the panel
  starts empty and fills forward. Saying so is cheaper than a fold that invents them.

---

## 13. DX Scorecard

```
+====================================================================+
|              DX PLAN REVIEW — SCORECARD                             |
+====================================================================+
| Dimension            | Before | After  | Trend  |
|----------------------|--------|--------|--------|
| Getting Started      |  3/10  |  9/10  |  +6 ^  |
| API/CLI/SDK          |  4/10  |  9/10  |  +5 ^  |
| Error Messages       |  2/10  |  9/10  |  +7 ^  |
| Documentation        |  5/10  |  9/10  |  +4 ^  |
| Upgrade Path         |  6/10  |  8/10  |  +2 ^  |
| Dev Environment      |  6/10  |  9/10  |  +3 ^  |
| Community            |  4/10  |  4/10  |   0 -  |
| DX Measurement       |  3/10  |  9/10  |  +6 ^  |
+--------------------------------------------------------------------+
| TTA-inject           | ~6 min | <30 s  |  ^     |
| TTHW-symbols         |  n/a   | <2 min |  new   |
| Competitive Rank     | Champion (target)                            |
| Magical Moment       | designed - byte-identical replay in mnPanel  |
| Product Type         | CLI Tool + Platform (dashboard + CC hooks)   |
| Mode                 | EXPANSION                                    |
| Overall DX           |  4/10  |  8/10  |  +4 ^  |
+====================================================================+
| DX PRINCIPLE COVERAGE                                               |
| Zero Friction      | covered - the dashboard is already open        |
| Learn by Doing     | covered - click a cat, read the real block     |
| Fight Uncertainty  | covered - three verdicts + Tier-1 sentences    |
| Opinionated + Escape Hatches | covered - registry defaults on,      |
|                              | extractor seam is the escape hatch    |
| Code in Context    | covered - replay is the real turn, not a demo  |
| Magical Moments    | covered - byte-identical replay                |
+====================================================================+
```

Community stays 4/10: no LICENSE while a public install command ships. Out of scope here, tracked
as TODOS #1. It is the one dimension this plan does not move.

---

## 14. DX Implementation Checklist

```
[x] Time to answer < 30 s target set and instrumented
[x] One command answers the whole question (`hive inject show`)
[x] First run produces meaningful output (six rows, real numbers)
[x] Magical moment delivered via the existing mnPanel
[x] Every verdict carries: what + why + how to fix + the actual numbers
[x] CLI naming guessable (`hive inject show`, `hive symbols`)
[x] Every parameter has a default (`--budget 1024`, `--turn last`)
[x] Docs pipeline table generated from the registry, cannot go stale
[x] Examples show the real turn, not a synthetic one
[x] Additive ledger type; every fold total-by-contract
[x] No breaking change; no runtime dependency added
[x] Works in CI (default tier) and non-interactively (`--json`)
[x] Cross-platform: pure stdlib, PowerShell 5.1 safe
[ ] Changelog exists and is maintained          <- TODOS #1, out of scope
[ ] License                                      <- TODOS #1, out of scope
[x] Browser-tier probe for hover/click/three states (T3)
```

---

## 15. Implementation Tasks

Synthesized from this review's findings. Each task derives from a specific finding above.

- [x] **T1 (P1, human: ~1d / CC: ~45min)** — `injectors.py` — Add the injector registry, `Injection`, and three-state `VERDICTS`
  - Surfaced by: §4 G2 — the injector list lives only in `cmd_hook`'s body; nothing can enumerate it
  - Files: `src/mindhive/injectors.py`, `tests/test_injectors.py`
  - Verify: a test pins `INJECTOR_NAMES` distinct from `telemetry.FACULTIES`, and pins `VERDICTS` against `sopgate.VERDICTS`
  - **Landed** 2026-08-23 (change-log `major_change @ 2026-08-23T14:29:43Z`, 10 files); the checkbox was simply never ticked, which left the plan reading as though its four foundational tasks were outstanding. Verified against the code before ticking, not from the record: `VERDICTS`, `class Injection` and `INJECTOR_NAMES` are all in `injectors.py`
- [x] **T2 (P1, human: ~1d / CC: ~45min)** — six injectors — Give each a `probe()` returning `Injection`, modelled on `sopgate.probe`
  - Surfaced by: §4 G3 — an external delta of 0 cannot separate "nothing to say" from "fell open"
  - Files: `promptgate.py`, `enrich.py`, `mindnet/inject.py`, `premise.py`, `sopgate.py`, `style.py`
  - Verify: each `probe()` returns `silent` on a forced fault and the prompt context is unchanged
  - **Landed** 2026-08-23 (change-log `major_change @ 2026-08-23T14:29:43Z`, 10 files); the checkbox was simply never ticked, which left the plan reading as though its four foundational tasks were outstanding. Verified against the code before ticking, not from the record: all seven registered modules define a module-level `probe()` (`promptgate`, `enrich`, `mindnet.inject`, `premise`, `sopgate`, `style`, `symbols`) — six when this line was written, seven since T8
- [x] **T3 (P1, human: ~4h / CC: ~20min)** — `events.py` + `injectors.py` — Register `context_injected` in `DETERMINISTIC_TYPES`, one row per turn, hash-deduped replay text
  - Surfaced by: §11.3 — 6 rows/prompt against a 41,488-event ledger whose `read_all` is 430 ms
  - Files: `src/mindhive/events.py`, `src/mindhive/injectors.py`, `tests/test_events.py`
  - Verify: one row per turn asserted; a canary asserts the type is NOT in `SEMANTIC_TYPES`
  - **Landed** 2026-08-23 (change-log `major_change @ 2026-08-23T14:29:43Z`, 10 files); the checkbox was simply never ticked, which left the plan reading as though its four foundational tasks were outstanding. Verified against the code before ticking, not from the record: `"context_injected"` is a member of `events.DETERMINISTIC_TYPES`, and `record_turn` appends one row per CHANGED turn rather than per turn
- [x] **T4 (P1, human: ~2h / CC: ~15min)** — `__main__.cmd_hook` — Route the six calls through the recorder, outside their own try
  - Surfaced by: §4 G3 — fail-open must survive exactly as it is
  - Files: `src/mindhive/__main__.py:102-195`, `tests/test_hook_injection.py`
  - Verify: a raising injector still returns the context untouched and still prints; the recorder writes `silent`
  - **Landed** 2026-08-23 (change-log `major_change @ 2026-08-23T14:29:43Z`, 10 files); the checkbox was simply never ticked, which left the plan reading as though its four foundational tasks were outstanding. Verified against the code before ticking, not from the record: `__main__.cmd_hook` calls `injectors.run_pipeline` and then `injectors.record_turn`, the latter strictly after the print
- [x] **T5 (P1, human: ~1d / CC: ~1h)** — `cat_board.py` + `dashboard_js_oversee.py` — The per-turn injection row with three visual states
  - Surfaced by: §4 G4 + E2 — Oversee has no cats; absence cannot express failure
  - Files: `src/mindhive/cat_board.py`, `src/mindhive/dashboard_js_oversee.py`, `src/mindhive/dashboard_get.py`
  - Verify: node tier asserts three classes render distinctly; `PANEL_OWNER` gains `'inj'` without breaking the two existing owners
  - **Landed.** `#mn-inject` renders one cat per TURN (not per pass — the ring is turn-keyed). Three verdict classes proven distinct twice: by CSS rule body in the default tier, and EXECUTED under node — `tests/test_injection_cats_node.py`, 14 cases, 8 of 8 mutations caught. The repaint guard is keyed on `box.dataset.sig`, not a module variable, because `showHTML` recreates the container. `PANEL_OWNER` gained `'inj'` with both existing owners still asserted.

- [x] **T6 (P2, human: ~4h / CC: ~20min)** — `cat_board.py` — Composed-block replay in `mnPanel`, `textContent` only
  - Surfaced by: E4 — the question is what Claude read, which is the composed order
  - Files: `src/mindhive/cat_board.py`
  - Verify: replayed block is byte-identical to the recorded composed text
  - **Landed.** Click replays the composed block via `mnPanel`'s new `body` parameter, `textContent` only — pinned by a node case that feeds `<script>alert('pwned')</script>` through and asserts `innerHTML` stays empty. An older turn says *why* it has no block rather than showing a neighbour's.

- [x] **T7 (P1, human: ~2d / CC: ~1.5h)** — `symbols.py` — `ast` extractor, import graph, PageRank, budget cap, pluggable seam
  - Surfaced by: §1 feature 1, E3, T1 — 303 Python files, 0 non-Python; aider's 1,024-token benchmark
  - Files: `src/mindhive/symbols.py`, `tests/test_symbols.py`
  - Verify: `index()` on this repo fits 1,024 tokens and ranks `__main__.py` above `autoresume.py`
  - **Landed.** `symbols.py`, stdlib `ast` only, `dependencies = []` untouched. Measured on this tree: 137 modules → 15 included, 122 omitted, 993/1024 tokens, 3,975 chars, 598 ms cold. Ranking is hub+authority PageRank (1.0/0.5) because authority ALONE was measured putting `__main__` at #131 of 136 — the verify line is unreachable without a hub term. Final: `__main__` #0, `paths` #1, `events` #3, `autoresume` #46.

- [x] **T8 (P2, human: ~4h / CC: ~20min)** — `symbols.py` — Wire as the seventh injector with its own cat and verdict
  - Surfaced by: §1 — feature 2 subsumes feature 1's delivery
  - Files: `src/mindhive/symbols.py`, `src/mindhive/__main__.py`, `src/mindhive/injectors.py`
  - Verify: the symbols row appears in `hive inject show` with a real char/token count
  - **Landed**, with one deliberate change: the pass is registered and wired but stays SILENT unless `MINDHIVE_SYMBOLS` is set — it spends ~993 tokens on *every* prompt, so registering it and switching it on are two steps, exactly as `MINDHIVE_ROTATION` is. Switched off it still produces a row, because "did not inject" is the answer the row exists to show. Hook cost when on: ~9–12 ms warm (cached), 620 ms cold. Surface: `hive symbols [PATH] [--budget N] [--json]`, whose behaviour lives in `symbols.cli` because `__main__.py` had 19 lines under its size ratchet.

- [x] **T9 (P2, human: ~3h / CC: ~20min)** — `context.py` — Add the `hook` origin so `--check` stops under-reporting
  - Surfaced by: §4 G5 — 48,004/51,000 omits ~1,483 tokens/turn
  - Files: `src/mindhive/context.py`, `tests/test_context.py`
  - Verify: `hive context --check` shows a `hook` row and a total larger than the session-only figure
  - **Landed.** `context.collect_hook` reads the injection sidecar, never the ledger. `hive context --origin hook` lists all seven passes including the ones at zero; `--origin all` now prints a `hook` line beside `hive` and `claude`. Verified through the real CLI against a real hook run.

- [x] **T10 (P2, human: ~2h / CC: ~10min)** — `__main__.py` — `hive inject show [--turn last] [--json]`
  - Surfaced by: §5 Pass 1 — no entry point exists; `hive sop injection` answers 1 of 6
  - Files: `src/mindhive/__main__.py`, `tests/test_cli_inject.py`
  - Verify: exit code 0 and six rows on a real turn; `--json` parseable on stdout even when empty
  - **Landed**, as `hive inject show [--turn last|N] [--project P] [--json]`. SEVEN rows, not six — `symbols` landed after this line was written, and the report is built from the REGISTRY rather than from the record precisely so that a pass added later cannot go missing from it; a pass with no row this turn is reported as such. Behaviour lives in `injectors.cli` and the group self-registers (`injectors.register`, the fourth module to), because `__main__.py` had six lines of headroom under its ratchet and inline wiring needed ten. `--turn N` indexes the recent ring and says `index only: no text on the ring` rather than showing an empty block. Exercised against the real sidecar: exit 0, seven rows, `--json` parses, and the empty case puts `{}` on stdout with the hint on stderr

- [x] **T11 (P3, human: ~30min / CC: ~5min)** — `dashboard_js_oversee.py:212` — Relabel the replenish pill to `N added to plan`
  - Surfaced by: §4 G6 (T2) — one word, two meanings, same card
  - Files: `src/mindhive/dashboard_js_oversee.py`, whichever test pins the string
  - Verify: grep finds no remaining `injected` label on the replenish pill
  - **Landed**, and the relabel itself was already done — what was missing was the other half of the pin. `test_the_replenishment_pill_no_longer_says_plain_injected` asserted only that the word is GONE, which a card that had lost the pill entirely satisfies just as well. `test_the_replenishment_pill_still_says_what_it_counts` is the canary, and it was proven red by cutting the pill

- [x] **T12 (P2, human: ~3h / CC: ~20min)** — `docs/session-legend.md` + `README.md` — Generated pipeline table
  - Surfaced by: §4 G7 — the composition order lives only in a function body
  - Files: `docs/session-legend.md`, `README.md`
  - Verify: the table is generated from `INJECTORS`; a test fails if a registry member is missing from it
  - **Landed.** `injectors.PIPELINE_NOTES` + `pipeline_table()`, surfaced as `hive inject table [--markers]`, carried by both documents between generated markers, and compared by `tests/test_cli_inject.py` in both directions (registered-but-undocumented AND documented-but-retired). The gap was worse than G7 recorded: `docs/session-legend.md` described a FIVE-pass pipeline. `sopgate` and `symbols` had each been added exactly where its numbered list said to add them, and the list was never touched either time — it also still credited `__main__.cmd_hook` with an order that had moved into `run_pipeline`

- [x] **T13 (P2, human: ~4h / CC: ~30min)** — `tests/test_dashboard_browser.py` — Browser-tier probe: hover, click, three states
  - Surfaced by: T3 — hover and click are the ask, and only a browser can prove them
  - Files: `tests/test_dashboard_browser.py`
  - Verify: `pytest -m browser` green; panel stays inside the viewport near the right edge
  - **Landed:** four probes (three states, hover, click open/close, viewport clamp) seeded through the REAL sidecar and served by the real `/api/injection`, with seven canary mutants, all proven red. Two corrections came out of building it. The states are asserted on COMPUTED STYLE, not class names — a class nothing answers is a state nobody can see. And the clamp probe does not bite at 1500px: the strip's rightmost cat sits at x=790 and a ~210px panel ends at 1000, so the guard could be cut with the probe still green. It runs at 900px, clicks the rightmost cat by measurement rather than the last in the DOM (the row wraps), and asserts FIRST that an unclamped panel would have overflowed

---

## 16. Unresolved Decisions

- **Outside voice did not run.** Codex is not installed (`CODEX_MODE: not_installed`), and this
  session's operating instructions forbid dispatching a subagent unasked, so the fallback path was
  not taken either. No second model has challenged this plan. Fix:
  `npm install -g @openai/codex`, then `/codex review`.
- **Per-turn ledger growth is estimated, not measured.** The ~200 prompts/day figure behind §11.3 is
  an assumption. The hash-dedup design holds regardless, but the real growth rate should be measured
  after T3 lands rather than argued now.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | NOT RUN | codex not installed |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | NOT CLEARED | required before implementation |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 2 | issues_open | score: 4/10 → 8/10, TTA: ~6 min → <30 s, 8 gaps found, 11 decisions taken, 0 deferred |

- **VERDICT:** DX CLEARED — eng review required. Eight gaps found (G1–G8), all addressed in scope,
  none deferred to TODOS.md. The capacity design in §11.3 and the `probe()` seam in §11.2 are the
  two items eng review should attack hardest.

**Run 2 (2026-08-23) — what the re-run changed.** No source file under `src/` or `tests/` had
changed since run 1, so the design was not re-litigated. Three things are new, all from
measurement rather than reasoning:

1. **G3 stopped being a prediction.** The re-measurement harness passed a `dict` where a JSON
   string was expected; three injectors returned zero and `stderr` stayed empty. The first draft
   of this re-run reported those numbers as a finding before the error surfaced. The fail-open
   contract hid a 4,082-character injection failure from the person deliberately measuring it.
2. **G5's `~1,483 tokens/turn` is now a measured range**, not an estimate: a constant floor of
   4,940 chars (~1,235 tokens) on *every* prompt including `halo`, rising to 6,772 (~1,693) on a
   vague one-liner. The variable part lives entirely in `promptgate`.
3. **G8 is new** — `premise` produced zero bytes across all five prompt shapes. Recorded as a
   finding to instrument, explicitly *not* as a defect to fix.

The score is unchanged at 8/10: the re-run strengthened the evidence for the same design, and
found nothing that alters the architecture in §11.

**UNRESOLVED DECISIONS:**
- Outside voice never ran: Codex not installed, and subagent dispatch is forbidden in this session. No second model has challenged this plan.
- Per-turn ledger growth rate (~200 prompts/day) is assumed, not measured; verify after T3 lands.
- ~~G8: whether `premise` is correctly silent or silently broken cannot be settled without the instrumentation this plan proposes; do not act on it before T3.~~ **SETTLED 2026-08-24, after T3 landed: `premise` is correctly silent.** The instrumentation this plan asked for answered it in one run. Six prompt shapes against the real store:

  | prompt shape | claims extracted | chars injected | ms |
  |---|---|---|---|
  | `halo` | 0 | 0 | 2 |
  | a vague one-liner | 0 | 0 | 0 |
  | a **false** file-path claim | 1 | **611** | 1 |
  | a **true** file-path claim | 1 | 0 | 0 |
  | a roadmap-id claim (`R-9999`) | 0 | 0 | 0 |
  | a test-count claim | 1 | **618** | **462** |

  The gate fires on a false claim and stays quiet on a true one, which is the
  contract. The five shapes in run 2's G5 table simply contained no false
  checkable claim — the zero was the right answer to the question asked, not a
  broken pass. `probe()` says the same thing in words: *"fires only on prompts
  asserting checkable facts (paths, roadmap ids, test counts)"*.

  One finding worth carrying: a **test-count** claim costs **462 ms**, because
  that is the one claim kind that reads the ledger. Path claims — the common
  case — cost ~1 ms. That is the per-prompt tail `promptgate`'s docstring warns
  about, now measured on this path too.
