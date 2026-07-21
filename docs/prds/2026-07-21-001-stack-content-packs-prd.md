# PRD — Stack Content Packs (from the ECC "Specific Framework Content" study)

> Status: **ready for execution** (next session, maximum output depth).
> Version: **2 (adversarially verified).** v1 was a single-context draft; v2
> folds in an 11-agent deep-study-and-verify workflow (8 parallel framework/
> integration study agents writing durable dossiers to
> `C:/Users/hanif/ECC/.ecc-review/study/prd-*.md`, + 3 adversarial reviewers).
> Every study finding carries file:line provenance; every plan change traces to
> a named adversarial recommendation. Workflow run `wf_cae37272-a5a`,
> ~1.07M subagent tokens, 11/11 agents, 0 errors.
> Source study: ECC clone at `C:/Users/hanif/ECC` (skills corpus), re-measured
> live 2026-07-21 — not quoted from the earlier PDF snapshot.
> Standing constraints (CLAUDE.md + SOPs): **build from scratch, copy the design
> not the code; never overwrite existing MindHive code paths; stdlib-only;
> test-first; every `src/`+`tests/` change-set gets `hive log`; land green.**

---

## 0. What changed from v1 (the adversarial corrections)

The deep-study workflow confirmed v1's strategy but refuted several of its
supporting claims and found gate-calibration defects that would have inverted
the validator's intent. v2 corrects all of them:

| # | v1 claim | Verdict | v2 correction |
|---|---|---|---|
| C1 | "0–6.5% similarity proves the content isn't boilerplate" | Overclaimed; bound is wrong (JVM pair hits 16%) | Split the claim (§1.2); demote similarity to a corroborating vocabulary proxy; make internal cross-referencing the primary evidence |
| C2 | The packs are "genuinely specific / not templated" | The container **is** templated (uniform frontmatter + 5-slot spine) | Scope "not boilerplate" to the code-example altitude only (§1.1) |
| C3 | depth-parity ratio ≤3.0, "springboot spread fails at 5.1×" | 5.1× is a **cross-family** pair; within-pack ratios are django 1.56 / quarkus 1.74 / **springboot 1.98 (passes!)** / laravel 5.27 | Tighten ratio to ≤2.5; the **line floor**, not the ratio, catches stubs (§5 WS1) |
| C4 | similarity gate ≤10% vs ecc "makes copy measurable" | vs-ecc is near-vacuous (orthogonal domains → <5%); the real risk is cross-pack same-role among the 3 new packs | Split thresholds: external ≤10% error / internal same-role ~18% + shared-preamble exemption (§5 WS2) |
| C5 | leak gate is a headline gate; `/api/markets` is unique quarkus contamination | `/api/markets` is an ECC-wide demo convention (7 files); example markers `hive `/`namespace` would false-positive across MindHive packs | Right-size to a warn-level guard; markers must be pack-exclusive tokens (§5 WS2) |
| C6 | frontmatter `metadata.origin/pack/role` (nested) | `plugins._parse_frontmatter` **flattens** all keys to top-level (verified `plugins.py:89`) | Template uses **flat** `origin:`/`pack:`/`role:` keys (§5 WS1) |
| C7 | quartet = the canonical 4-role shape | Not enforced anywhere: ≥5 role vocabularies in ECC (react quartet-B, perl trio, go/rust/py duo, singletons) | Manifest declares **per-pack** `roles[]`; validator hardcodes no role set (§3, §5 WS1) |
| C8 | thresholds "come from the manifest, not code constants" | manifest example omitted the floors | Add `min_lines`/`min_code_blocks`/`required_sections` to the manifest schema — single source of truth (§5 WS1) |
| C9 | `hive packs` registers "exactly like cmd_context" | cmd_context is a flat leaf; check/report is a **group** | Register like `review`/`sop`/`roadmap` (add_subparsers + per-leaf set_defaults); only lazy-import + `int|None` match cmd_context (§5 WS2) |
| C10 | external_corpora hardcodes `C:/Users/hanif/ECC/skills` | machine-specific path committed to the repo | Env var / git-ignored local override; missing corpus = warning (§5 WS1) |

One reviewer claim was itself **rejected** on verification: machine-reality
reported "`hive context --check` doesn't exist." It does on **this** branch
(`--check`, `--set-budget`, persisted budget all present — the ECC wave landed
here). The reviewer tested the live `hive` install, which resolves to the main
checkout, not this worktree (the documented editable-install/PYTHONPATH trap).
So WS4 **can** rely on the budget gate on this branch.

---

## 1. What was studied — the ECC framework-content architecture

ECC ships four *framework families*, each a quartet of skills with identical
**roles** but framework-native **content**:

| Family | patterns | security | tdd | verification | total lines |
|---|---|---|---|---|---|
| django | 735 | 644 | 730 | 470 | **2,579** |
| laravel | 416 | 948 | 675 | 180 | **2,219** |
| quarkus | 723 | 468 | 812 | 480 | **2,483** |
| springboot | 315 | 273 | 159 | 232 | **979** |

### 1.1 The repeating structure — templated at the outline level (C2)

Every quartet file follows the same five-slot spine, and this spine **is**
uniform scaffolding — "not boilerplate" is true only of the leaf code, not the
document:

1. **Frontmatter** — the *only fully uniform slot across all 16 files*:
   `name` (== directory slug), `description`, `metadata.origin: ECC`.
   `origin: ecc` is literally the single most-shared normalized line in the
   corpus.
2. H1 + one-sentence scope.
3. **Trigger** — `## When to Activate` (12/16) **or** `## When to Use` (4/16:
   laravel-patterns, laravel-verification, quarkus-tdd, springboot-tdd).
4. **Role body** (the framework-native part — see §1.2).
5. **Closing** — a `Remember:` maxim (7/16), a `## Quick Reference` table
   (4/16), `## Related Skills` (2/16, both Laravel). Inconsistent — this is
   the template drift the spec ends (§5 WS1).

Three coexisting templates were confirmed: Template A (`When to Activate` +
flat topic H2s, 12 files), Template B (`When to Use` + How-It-Works + Examples
wrapper, laravel-patterns/verification only — so the Laravel quartet is itself
split), and a JVM-TDD hybrid (`When to Use` + Workflow, quarkus-tdd/springboot-tdd).

### 1.2 The specific content — the corrected specificity claim (C1)

**The claim, split into what the evidence supports and what it doesn't:**

- **SUPPORTED (descriptive):** the framework-specific *code* is genuinely
  framework-native — not another framework's API, not generic prose with
  keywords swapped in. This is the real finding.
- **NOT SUPPORTED (inferential):** "the 0–6.5% similarity *proves* it." The
  metric is a **vocabulary-distance proxy** (line-set overlap on ≥10-char
  lines). Different languages use different tokens (`models.Model` vs
  `$fillable` vs `@Entity`), so low cross-language overlap is near-tautological
  and is blind to depth, correctness, and templated origin.

**The primary evidence is internal cross-referencing, which no metric produces
and no template-fill pipeline can fake:**

- **Django** — all four skills compose ONE coherent e-commerce domain
  (`Product`/`Category`/`Tag`/`User(AbstractUser)`/`Order` under
  `config.settings.*` split settings, `apps/products`, `apps/users`). The TDD
  `ProductFactory` builds the exact `Product` model patterns declares; its test
  exercises patterns' custom `ProductQuerySet.active()`; the ViewSet imports
  security's `IsOwnerOrReadOnly`. That semantic wiring is the opposite of
  leaked boilerplate.
- **Quarkus** — all four orbit ONE real invoice/document-processing system
  (Camel + `spring-rabbitmq` + AWS S3, `InvoiceFormat.UBL`, `CREDIT_NOTE`
  routing, bespoke `CustomLog`/`LogContext`/`SafeAutoCloseable`) — real
  production extraction.

**Load-bearing mechanics are framework-ONLY (not noun-swaps)** — the quotable
evidence inventory is in Appendix C. Highlights: Camel `AdviceWith`/`MockEndpoint`
route testing (exists in NO other family), Panache positional-param DSL,
Sanctum `tokenCan`/abilities, the six Laravel fakes
(`Http`/`Mail`/`Queue`/`Event`/`Notification`/`Storage::fake`), DRF
`APIClient.force_authenticate`, the `DisableMigrations` trick,
`MD5PasswordHasher`-for-test-speed.

**Corrected similarity range (C1)** — cross-ecosystem pairs sit **0.2%–5.0%**
(confirming the "genuinely distinct" reading), but the same-ecosystem JVM pair
(Quarkus↔Spring Boot, shared Maven/JaCoCo/OWASP/Bean-Validation) breaks the
"0–6.5%" bound: **security 16.0%, verification 14.9%, tdd 14.4%**, patterns 6.0%.
The `/api/markets` leak (§1.4) proves ECC uses a **template-and-redomain**
workflow; because that leak is only *visible* intra-JVM (shared vocabulary),
the low cross-ecosystem numbers **cannot rule out** that Django/Laravel skills
were also template-instantiated. Absence of a signal the metric can't produce
is not evidence of independent authorship.

**Per-role specificity gradient:** highest in **patterns** (dense native
idioms), lowest in **verification** (portable mypy/ruff/black/Checkstyle/PMD/
SonarQube/OWASP/k6/CI tooling) and **JVM tdd** (a Spring-Boot-origin template,
un-re-domained). If MindHive ever adopts selectively, flag verification as the
most portable / highest derivation-risk slice.

### 1.3 The quartet is NOT enforced (C7) — a first-class spec constraint

The 4-role quartet is one of **≥5 role vocabularies** in the ECC corpus:

- **quartet-A** (patterns/security/tdd/verification): only django, laravel,
  springboot, quarkus.
- **quartet-B** (patterns/**performance**/**testing**/**native**): the react
  family — no security, "testing" not "tdd", "native" not "verification", and
  a whole `performance` role dimension quartet-A lacks.
- **trio** (patterns/security/testing): perl.
- **duo** (patterns/testing): golang, rust, python.
- **topic-sharded, no role suffix:** kotlin (`kotlin-coroutines-flows`, …),
  swift (`swiftui-patterns`, `swift-concurrency-6-2`, …).
- **singletons:** fastapi-patterns, nestjs-patterns, vue-patterns,
  nextjs-turbopack, angular-developer.
- Quartet families also carry a **non-role 5th skill** (a *feature*):
  `django-celery`, `laravel-plugin-discovery` (an MCP integration doc).

Naming and frontmatter placement are also unenforced (`dotnet-patterns` vs
`csharp-testing`; `react-native-patterns` puts `origin` at top-level vs
everyone else's `metadata.origin`; cross-links are hand-written prose that
already drifts — `react-patterns` calls `react-native-patterns` "not present
yet" though it exists at 326 lines).

**Implication (drives §3, §5 WS1):** MindHive must let each pack declare its own
`roles[]` and separate `role_skills` from `feature_skills`, carry a per-skill
`kind:` (patterns|security|tdd|verification|testing|performance|native|advisory|
integration|feature), support cardinality 1..N (singletons are first-class),
and emit cross-links as manifest-declared graph edges — never hardcode the
quartet.

### 1.4 The weaknesses (what "make it better" means)

1. **Uneven depth** — springboot totals 979 lines vs django's 2,579;
   `springboot-tdd` (159 lines, 125 non-empty, 6 blocks) is a stub whose
   unit-test and `@WebMvcTest` content is *duplicated and out-depthed* by
   `springboot-verification` Phase 3. Within-pack line ratios: django 1.56×,
   quarkus 1.74×, **springboot 1.98×**, laravel 5.27×. (Note: the stub is
   caught by a *line floor*, not by a within-pack ratio — see C3.)
2. **Template drift** — three templates, inconsistent Quick Reference /
   Related Skills / coverage-target tables (per-component table in
   django/laravel-tdd; degraded to a bare "80%+" prose phrase in the JVM two).
3. **No content gates** — ECC's CI "Validated 278 skill directories" means
   *frontmatter parses*, nothing else. The 16% JVM overlap, the `/api/markets`
   leak, and example-drift bugs (`APIClient.user` is not a real attribute;
   undefined `TagFactory`/`category` fixtures in django-tdd; Market-vs-User
   domain inconsistency in springboot) all shipped invisibly.
4. **Not execution-verified** — the drift bugs + the `/api/markets` k6 script
   would 404 on a real Quarkus app + the machine reality below mean quartet
   content is **reference material, not runnable or correctness-verified**. The
   PRD must not claim the code works.
5. **`/api/markets` is a convention, not unique contamination (C5)** — it
   appears in **7 ECC files** including non-quartet skills (`backend-patterns`,
   `coding-standards`, `frontend-patterns`, `tdd-workflow`); it's an ECC-wide
   demo domain, evidence of shared lineage but not a targeted quarkus defect.
6. **Zero relevance to this machine** — verified live: `php`, `composer`,
   `java`, `mvn`, `gradle` are **ABSENT** (bare `python` is the Store stub;
   only `py`/`node`/`dotnet` are real). 12/16 quartet files are unexecutable
   here; the 4 Django files have no project to run against. Adopting them adds
   ~16 always-in-context skill descriptions (the live always-on payload is
   **44,067 est. tokens / 282 skills**) for content that cannot be exercised.

---

## 2. Problem statement

MindHive has proven the *mechanism* for skill content (in-repo
`.claude/skills/`, `plugins.py` frontmatter parsing incl. block scalars,
`hive skills recommend`, `hive context` budget enforcement incl. `--check`/
`--set-budget` on this branch) — but has **no stack-specific content packs**
for the stacks actually used on this machine (Python-stdlib event-sourced CLI,
pytest, PowerShell 5.1 / Windows, the Claude Code harness itself), and **no
deterministic quality gate** that keeps such content specific, deep, and
leak-free. ECC demonstrates the content pattern works at scale and
simultaneously demonstrates the failure modes of shipping it ungated.

## 3. Goals

- **G1 — Pack spec.** A written, versioned spec: **per-pack** `roles[]`
  declaration (not a fixed quartet — C7), `role_skills` vs `feature_skills`,
  per-skill `kind:`, flat frontmatter contract (C6), required sections, depth
  floors + a quartet-depth target, domain-marker declaration. One canonical
  template ends the drift (§1.1).
- **G2 — Deterministic validator.** `hive packs` (new stdlib-only
  `src/mindhive/packs.py` + a `check`/`report` subcommand **group**): structure,
  depth (floor + within-pack ratio + below-target flag), similarity (split
  external/internal thresholds), and domain-leak gates, machine-readable, with
  a non-zero exit on error-severity findings — the gate ECC never built.
- **G3 — Three authored packs, from scratch,** for the stacks this machine runs,
  each passing G2 and grounded in *this repo's* real conventions the way
  quarkus-patterns is grounded in its source system — at **django/quarkus depth**
  (median file ≥ ~300 non-empty lines), proven by `hive packs report`.
- **G4 — Budget-safe integration.** Packs live in-repo (project-scoped, zero
  global context tax); optional adoption to `~/.claude/skills/` only behind
  `hive context --check` staying green (the gate exists on this branch).

## 4. Non-goals

- **Not** adopting/porting the 16 ECC quartet files (unexecutable toolchains
  here; §1.4.6). If Django/Laravel/JVM work ever starts, adopt the relevant
  quartet *then*, `ecc-`-prefixed, from that framework's own project
  `.claude/skills/` (Route A), never `~/.claude/skills/`.
- **Not** copying ECC text into the new packs. The **internal** cross-pack
  similarity gate (not the near-vacuous vs-ecc one — C4) is what keeps the three
  new packs honest against *each other*.
- **Not** adding dependencies (similarity = stdlib set arithmetic; frontmatter
  = the existing `plugins._parse_frontmatter`; token weight =
  `context.estimate_tokens`).
- **Not** dashboard UI (dashboard_page.py is already past the thermo-nuclear
  size threshold). Optional future follow-up only.
- **Not** new ledger event types (the validator is read-only, like `context`;
  `hive log` is the audit trail). Revisit only if a gate needs a persisted trail.

---

## 5. Technical plan

### WS1 — Pack spec + template + manifest (docs)

**Files:** `docs/packs/SPEC.md`, `docs/packs/TEMPLATE-SKILL.md`,
`docs/packs/packs.json`.

**Frontmatter contract (C6) — FLAT keys only.** `plugins._parse_frontmatter`
(`plugins.py:89`) matches `key: value` at any indentation and flattens to a
flat lowercased dict (nested `metadata.origin` collapses to top-level `origin`;
`metadata` itself becomes `""`). So the template declares flat keys the proven
parser can read:

```markdown
---
name: mindhive-python-patterns
description: >-
  Event-sourced ledger discipline, stdlib-only module patterns, and CLI
  conventions for the MindHive codebase.
origin: mindhive
pack: mindhive-python
role: patterns
kind: patterns
---
```

**Manifest — single source of truth for ALL thresholds (C8), per-pack roles
(C7), no hardcoded machine path (C10):**

```json
{
  "version": 2,
  "similarity_external_max_pct": 10.0,
  "similarity_internal_same_role_max_pct": 18.0,
  "shared_preamble_markers": ["run the full pytest suite", "hive log", "PYTHONPATH=<worktree>/src", "CI green", "$LASTEXITCODE"],
  "depth_ratio_max": 2.5,
  "min_lines": 150,
  "min_code_blocks": 6,
  "min_code_blocks_by_role": { "verification": 4 },
  "quartet_depth_target_lines": 300,
  "required_sections": ["## When to Activate", "## Quick Reference"],
  "required_sections_by_role": {
    "verification": ["## When to Activate", "## Output Template"]
  },
  "packs": [
    {
      "name": "mindhive-python",
      "roles": ["patterns", "security", "tdd", "verification"],
      "domain_markers": ["append-only", "event ledger", "fold(", "projection rebuild"],
      "leak_severity": "warn",
      "compare_external": ["ecc-quartets"]
    },
    {
      "name": "windows-powershell",
      "roles": ["patterns", "verification"],
      "domain_markers": ["$LASTEXITCODE", "New-Item -Force", "Store stub"],
      "leak_severity": "warn"
    },
    {
      "name": "claude-harness",
      "roles": ["patterns", "verification"],
      "domain_markers": ["PreToolUse", "hook_decision", "settings.json"],
      "leak_severity": "warn"
    }
  ],
  "external_corpora": {
    "ecc-quartets": {
      "path_env": "MINDHIVE_ECC_SKILLS",
      "families": ["django", "laravel", "quarkus", "springboot"]
    }
  }
}
```

- `path_env` resolves the ECC clone from an env var (default probes
  `C:/Users/hanif/ECC/skills` if unset), so no personal path is committed
  (C10). A missing corpus is a **warning** finding, never an error.
- `families` restricts the external comparison to the four quartet families
  (not all 278 role-suffixed dirs); corpus files are role-tagged by the
  trailing dir token (`django-tdd` → `tdd`).
- `domain_markers` are **pack-exclusive** tokens (C5) — the ECC analog is
  `/api/markets`, not `spring`. Note `hive `/`namespace` are deliberately
  **absent**: they legitimately appear across all three MindHive packs.

**Canonical template (ends drift).** frontmatter (flat) → H1 + one-line scope →
`## When to Activate` (5–10 bullets) → role body → `## Quick Reference` table.
**verification** role adds numbered `## Phase N:` sections, ≥1 stop condition,
and an `## Output Template` block (the strongest part of the studied corpus).

**Depth rules (C3).** Line floor is the anti-stub gate: each role file ≥ **150
non-empty lines** and ≥ **6 fenced code blocks** (≥ 4 for verification). The
within-pack ratio `max(lines)/min(lines) ≤ 2.5` is a *secondary* discriminator
(good ECC families sit ≤ 1.74×); the rationale explicitly does **not** cite
springboot, which passes at 1.98× and is caught by the floor instead. A
separate **quartet-depth target** flags any file < 300 non-empty lines as
"below target" (report-only), and WS3 acceptance requires the median file ≥ 300.

**Portability rule.** Every command shown must run **on this machine** (Windows,
PowerShell 5.1 + Git Bash, `py` not bare `python`) — the compatibility lesson
from the ECC `python3` dashboard break.

**Acceptance:** spec + template + manifest committed; manifest validates against
WS2's loader.

### WS2 — Validator: `src/mindhive/packs.py` + `hive packs` group (code, test-first)

Design mirrors `context.py` (PR #72): frozen dataclasses, pure
collector/analyzer functions, injectable paths, total reads (malformed files →
findings, never raised), rich module docstring.

```python
# final signatures decided at execution; declare Report so WS2 doesn't invent it mid-flight (feasibility C: (c))
@dataclass(frozen=True)
class PackFile:        # pack, role, path, lines, nonempty_lines, code_blocks, norm_lines(frozenset[str])
@dataclass(frozen=True)
class Finding:         # reuse context.Finding shape: (rule, severity["error"|"warn"], subject, message)
@dataclass(frozen=True)
class Report:          # ok: bool, findings: list[Finding], stats: dict   (or return a plain dict, like context.analyze)

load_manifest(path) -> Manifest                       # missing/invalid -> error Finding, not raise
collect_pack(root, pack_decl) -> list[PackFile]       # via plugins._skills_for(root/".claude") + _parse_frontmatter
norm_lines(text) -> frozenset[str]                    # strip, collapse ws, lower, drop <10 chars (this study's metric)
gate_structure(pf, spec) -> list[Finding]             # flat frontmatter keys, required sections, description<=300
gate_depth(files, spec) -> list[Finding]              # line/block floors + within-pack ratio + below-target flag
gate_similarity(all_files, corpora, spec) -> list[Finding]   # same-role; external<=10 err; internal same-role<=18; shared-preamble lines excluded from numerator
gate_leak(all_files, manifest) -> list[Finding]       # foreign pack-exclusive markers; severity per pack.leak_severity
analyze(manifest, root) -> Report
render_text(report) / to_json(report)
```

**Similarity gate (C4).** Two thresholds, because the three MindHive packs are
the *same-ecosystem* analog of the JVM pair and will legitimately share
environment lines:
- **external** (vs `ecc-quartets`, genuinely cross-ecosystem): ≤ 10% → error.
- **internal** (cross-pack same-role among the 3 new packs — the real risk):
  ≤ 18% → error, with `shared_preamble_markers` lines removed from the
  numerator so honest shared truth ("run pytest → `$LASTEXITCODE` → `hive log`
  → `PYTHONPATH=<worktree>/src` → CI green") doesn't trip the gate. Per-pair
  manifest overrides allowed.

**Leak gate (C5).** Foreign pack-exclusive markers only; severity is
per-pack `leak_severity` (`warn` for all three MindHive packs — one-author,
one-session authoring makes real copy-leaks unlikely while same-machine packs
share vocabulary by nature). Demoted from a headline gate to a lightweight
guard.

**CLI — a subcommand GROUP (C9).** Registers like `review`/`sop`/`roadmap`
(`add_subparsers` + per-leaf `set_defaults(func=…)`), **not** flat like
cmd_context. Only the lazy-import (`from . import packs`) and `cmd_x(args) ->
int | None` return contract match cmd_context.
- `hive packs check [--manifest PATH] [--json]` — exit **1** on any
  error-severity finding (same contract as `hive context --check`).
- `hive packs report [--manifest PATH] [--json] [--pack NAME]` — full stats:
  per-file lines/nonempty/blocks, below-target flags, the split similarity
  matrix (internal + external), no exit-code semantics.

**Root/manifest resolution (feasibility C: root-path).** `--manifest` defaults
to a git-root-relative `docs/packs/packs.json`, resolved via the
`git rev-parse --show-toplevel`/`--git-common-dir` pattern already in
`paths.detect_project`/`context._memory_folder` — never bare `Path.cwd()` (cwd
does not persist between tool calls).

**Tests** (`tests/test_packs.py`, autouse `isolate_store` per `conftest.py`;
fixture packs + a hermetic fixture-`ecc` dir in `tmp_path`): manifest load
happy path; missing/corrupt manifest → finding not raise; `norm_lines` edge
cases (unicode, code fences, short lines); structure gate catches missing flat
key / wrong section / over-long description; depth gate catches stub (line
floor) AND a >2.5 within-pack ratio AND below-target; similarity gate: identical
→ ~100%, disjoint → 0%, shared-preamble exclusion works, external vs internal
thresholds distinct; external-corpus comparison against the fixture-ecc dir with
role-tagging; leak gate flags a planted foreign marker at the pack's configured
severity and ignores self-markers; `--json` is `json.loads`-able and
stable-keyed; exit codes 0/1; **real-flow test** runs `analyze` against the
real repo manifest — **skips cleanly if the packs' SKILL.md dirs are absent**
(not if the manifest is absent; the manifest is a WS1 deliverable present during
WS2/WS3).

**CI vs local (feasibility C: CI).** The vs-ecc comparison needs
`C:/Users/hanif/ECC`, absent on GitHub CI → by the missing-corpus rule it's a
skipped warning, so CI must **not** be the anti-copy gate. CI gate = the
hermetic fixture-ecc unit test; the real-clone vs-ECC similarity run is an
explicit **local manual acceptance** step (§6).

**Acceptance:** all new tests green; full suite green; `hive packs check` and
`report` exercised against the real repo with actual output inspected (SOP 6.5);
`hive log minor_change`/`major_change` recorded per change-set.

### WS3 — Author three packs (content, from scratch, gated by WS2)

Grounding rule (the study's core lesson, §1.2): write each file the way
`quarkus-patterns` was written — extracted from a *real running system* (this
repo, this machine), with runnable commands, real `file:line` references, and
**internal cross-referencing across a pack's own roles** (the Django-style
`ProductFactory`→`Product`→`ProductQuerySet` wiring is the strongest
authenticity signal a template can't fake). No text copied from ECC.

1. **`mindhive-python`** — full quartet (4 files), sharing ONE example subsystem
   across all four roles (e.g. build every role around the event-ledger/recall
   pipeline so tdd tests the exact modules patterns describes):
   - *patterns*: append-only canonical-JSON events, folds/projections, total
     reads (SOP 7.5), the collector-registry pattern (`context.py` exemplar),
     stdlib-only constraint, paths/namespace hygiene, CLI subcommand
     registration, docstring house style.
   - *security*: dashboard auth-token handling, subprocess hygiene, worktree
     path traversal, secrets-never-in-ledger, fail-open vs fail-closed
     (GateGuard precedent), Windows file-permission caveats.
   - *tdd*: autouse isolated-store fixture, `tmp_path` packs,
     `PYTHONPATH=<worktree>/src` for subprocesses, discriminating-test design
     (the even-length-median lesson), canary tests vs vacuous negatives (the
     `/tmp`-prefix CI lesson), coverage expectations per module type.
   - *verification*: the phase loop for THIS repo — full pytest → `hive context
     --check` → served-page hook grep (`showOversee`) for dashboard changes →
     `hive log` recorded → CI green; `## Output Template` mirroring the ECC
     verification-report shape.
2. **`windows-powershell`** — patterns + verification (2 roles): PS 5.1 dialect
   (no `&&`/`||`/ternary; `$LASTEXITCODE` vs `$?`; native-stderr redirect trap;
   UTF-8/BOM traps; `New-Item -Force` truncation), Git-Bash path rules, `py` vs
   Store stub, absolute-path discipline; verification = the pre-"done" checklist
   for Windows-touching work.
3. **`claude-harness`** — patterns + verification (2 roles): hook lifecycle
   (`hive hook` stages, shims, PreToolUse policy tier), skill frontmatter +
   block scalars, worktree lifecycle + the editable-install dashboard mechanism
   (never repoint port 54461), context-budget discipline, deferred-tool loading.

Each file ≥ 150 non-empty lines / ≥ 6 blocks (≥ 4 verification),
template-conformant, **median file ≥ 300 non-empty lines** (quartet depth).

**Acceptance:** `hive packs check` exits 0 over the real manifest;
`hive packs report` shows **every file at/above the 300-line quartet-depth
target** (or an explicit justification per exception) AND the **internal
same-role** similarity (patterns×3, verification×3) inspected and under the 18%
gate after shared-preamble exclusion — not just the near-vacuous vs-ecc row;
suite green; `hive log major_change` for the content change-set.

### WS4 — Budget-safe adoption (optional, last)

Only if the user wants the packs global rather than project-scoped. The budget
gate **exists on this branch** (`hive context --set-budget N`,
`hive context --check` → exit 1 over budget). Flow: measure `hive context`
baseline → set budget = baseline + margin → adopt copies into
`~/.claude/skills/` → `hive context --check` must stay green → else revert to
project-scoped. **Default: stay project-scoped** — in-repo `.claude/skills/`
already auto-serves this project with zero global context tax (verified:
`collect_claude` reads `~/.claude`, not repo-root `.claude`). Note: the live
`hive` command (main checkout, port 54461) only gains the budget gate after this
branch's ECC wave is merged there.

---

## 6. Execution protocol for the next session (maximum output depth)

Order: **WS1 → WS2 → WS3 → (WS4 if asked)** — spec before validator (the
validator encodes the spec), validator before content (content lands already
gated, mirroring how ECC's own verification skills gate its apps).

Per change-set discipline (non-negotiable):

1. Tests first for WS2 (red → green → refactor).
2. Full pytest suite green before each commit; worktree subprocesses get
   `PYTHONPATH=<worktree>/src` (the editable-install trap).
3. `hive log <category> "<what & why>"` for every `src/`+`tests/` change-set
   **before** calling it done. (Docs-only WS1 needs no `hive log`.)
4. **Verify with real output:** run `hive packs check` / `report` against the
   real manifest and read the output — plus the **local manual acceptance**:
   run the vs-ECC similarity comparison with `MINDHIVE_ECC_SKILLS` pointed at
   the clone (not CI-enforceable).
5. No PYTHONPATH/sys.path games against the live dashboard (port 54461).
6. Commit per workstream on this feature branch; PR when the user asks.

Depth expectation: WS3 files are the "maximum output" surface — write at
django/quarkus depth (Appendix C shows what that means concretely), with
cross-role internal references, then let `hive packs report` prove depth and
same-role internal distinctness numerically.

## 7. Risks & mitigations

- **Similarity metric is a vocabulary proxy, blind across languages and to
  depth/correctness (C1).** It false-negatives across ecosystems and
  false-positives within one. Mitigation: it is a *floor*, not proof; the
  authenticity bar is internal cross-referencing + native-API presence (WS3
  acceptance), and the leak gate adds a re-domain check.
- **Multi-pack shared-environment overlap (C4, NEW).** patterns×3 and
  verification×3 all describe one machine/repo; honest shared lines can hit
  ~15%+ on 150–250-line files. Mitigation: internal same-role threshold 18% +
  `shared_preamble_markers` exclusion + per-pair overrides. The executing
  session should draft the verification×3 files **early** and measure them
  against each other before committing to the threshold (it's a projection, not
  yet a measurement).
- **Gates too strict for 2-role packs** (small denominators): depth ratio is
  within-pack; 2-role packs compare 2 files. Manifest per-pack overrides cover
  legitimate shapes.
- **Frontmatter parser is shallow (C6):** flat keys only; do not rely on nested
  `metadata.*`. WS2 tests encode this.
- **Validator vs spec drift**: spec lives beside the manifest; WS2 tests encode
  the manifest rules, so drift breaks tests, not users.
- **ECC clone moves/deleted**: external corpus path is env-var data; missing
  corpus = warning (comparison skipped), never error.
- **Scope creep** into dashboard/event types: declared non-goals.

## 8. Open questions (defaults chosen so execution never blocks)

1. Global adoption (WS4)? **Default: no — project-scoped only.**
2. Similarity thresholds: external 10% error / 6.5% warn; internal same-role
   18% error. **Default: as stated; revisit after the early verification×3
   measurement.**
3. Should `hive selftest` run `packs check`? **Default: no in v1** — opt-in
   until the packs stabilize.
4. Pack for the Node/dashboard-web surface? **Default: no** — no Node product
   source in this repo.
5. Emit pack cross-links as ledger graph edges (the ECC "Related Skills" fix)?
   **Default: defer** — manifest declares them; ledger emission is a follow-up.

---

## Appendix A — measured similarity matrix (2026-07-21, this machine)

```
              dj-la  dj-qk  dj-sb  la-qk  la-sb  qk-sb
patterns        1.0    0.5    1.2    0.5    0.6    6.0
security        4.5    2.4    4.5    2.0    3.8   16.0   <- JVM outlier
tdd             3.7    0.2    1.1    0.3    1.1   14.4   <- JVM outlier
verification    2.9    4.5    5.0    4.3    4.3   14.9   <- JVM outlier
```
(dj=django la=laravel qk=quarkus sb=springboot; % = shared normalized lines /
smaller file. Metric: strip → collapse whitespace → lowercase → drop lines
<10 chars → set intersection ÷ min set size.) **Cross-ecosystem envelope:
0.2–5.0% (all 20 non-JVM cells). JVM pair: 6.0–16.0% — 3 of 4 roles are
2.2–2.5× over the "0–6.5%" ceiling.** Within-pack line ratios (depth): django
1.56×, quarkus 1.74×, springboot 1.98×, laravel 5.27×.

## Appendix B — structural drift evidence

Trigger heading: `When to Activate` 12/16 vs `When to Use` 4/16
(laravel-patterns, laravel-verification, quarkus-tdd, springboot-tdd).
`Quick Reference` 4/16; `Related Skills` 2/16 (both Laravel); closing
`Remember:` maxim 7/16; per-component coverage table only in
django-tdd/django-verification/laravel-tdd (JVM files use bare "80%+" prose).
Frontmatter is the only fully-uniform slot (`name`==slug + `description` +
`origin: ECC`). Code-block counts range 6 (springboot-tdd) to 32
(laravel-security).

## Appendix C — native-mechanics inventory (the quotable specificity evidence)

**django** (verdict SPECIFIC; primary evidence = one e-commerce domain wired
across all four roles):
- tdd: `pytest.ini addopts = --reuse-db --nomigrations --cov=apps`;
  `class DisableMigrations` (`__contains__`→True) as `MIGRATION_MODULES`;
  `PASSWORD_HASHERS=['…MD5PasswordHasher']` for test speed; factory_boy
  `SubFactory`, `PostGenerationMethodCall('set_password',…)`,
  `@factory.post_generation def tags`; DRF `APIClient.force_authenticate`,
  `override_settings(EMAIL_BACKEND='…locmem…')` + `len(mail.outbox)==1`.
- security: `User.objects.raw('… WHERE username = %s', [q])` safe vs
  `raw(f'… = {q}')` VULNERABLE; `mark_safe(escape(x))`,
  `format_html('<span>{}</span>', escape(username))`; hashers Argon2 > PBKDF2 >
  BCryptSHA256; validators `min_length 12`.
- patterns: `ProductQuerySet.as_manager()` chainable
  `.active().with_category().in_stock()`; `select_related` (FK) vs
  `prefetch_related` (M2M); ViewSet `@action(detail=False,…)`,
  `get_serializer_class` on `self.action`; `models.CheckConstraint(Q(price__gte=0))`.
- verification: `manage.py check --deploy`, `makemigrations --check`,
  `migrate --plan`, `generateschema`; `pip-audit`, `safety`, `bandit`,
  `gitleaks` (the portable slice).
- drift bugs: `APIClient.user` (not a real attribute); undefined
  `TagFactory`/`category` fixtures — hand-authored, un-linted.

**laravel** (SPECIFIC):
- security: `abort_unless(Auth::user()->tokenCan('read'),403)` +
  `->middleware('abilities:read')`; "`NEVER $guarded=[]`" vs `$fillable`;
  Blade `{{ }}` vs `{!! !!}`; `'metadata'=>'encrypted:array'` (L11+),
  `'password'=>'hashed'` (L10+); `Password::min(12)->…->uncompromised()`.
- tdd: all six fakes (`Http`/`Mail`/`Queue`/`Event`/`Notification`/`Storage`)
  with `assertSent`/`assertPushed`/`assertDispatched`; `RefreshDatabase`;
  `createToken('test')->plainTextToken` + `$this->withToken($token)`.
- verification: `vendor/bin/pint --test`; `phpstan analyse`; `composer audit`;
  `migrate --pretend` + `Y_m_d_His_*` filename check; staging
  `dispatch((new QueueHealthcheck())->onQueue('healthcheck'))` + `queue:work --once`.
- patterns: `Route::scopeBindings()->group(…)` anti-cross-tenant; anonymous-class
  migrations.

**quarkus** (SPECIFIC; primary evidence = one invoice/document system):
- tdd (corpus-unique): Camel route testing via
  `AdviceWith.adviceWith(…, advice -> advice.weaveByToString(".*spring-rabbitmq.*").replace().to("mock:rabbitmq"))`
  + `MockEndpoint.assertIsSatisfied`; `@QuarkusTest`/`@InjectMock`; REST Assured.
- patterns: CDI `@ApplicationScoped` + Lombok `@RequiredArgsConstructor`;
  Panache `find("status = ?1 order by createdAt desc", status).page(…)`; Camel
  `RouteBuilder` content-based `.choice().when(header(…))`; `%dev`/`%test`/`%prod`
  YAML; `CompletableFuture.supplyAsync(…, executorService)` with `LogContext`
  propagation via `SafeAutoCloseable`.
- security: `@RolesAllowed`, `SecurityIdentity`, `BcryptUtil.bcryptHash`,
  SmallRye JWT/OIDC config keys; one leaked Spring `@Query(nativeQuery=true)`
  example.
- verification: JaCoCo `LINE 0.80`/`BRANCH 0.70`; GraalVM `mvn package -Dnative`;
  `@RegisterForReflection`; `/q/health/live` — plus the portable tail
  (Checkstyle/PMD/SonarQube/OWASP/k6) and the `/api/markets` leak.

**springboot** (SPECIFIC but shallowest; real defect = depth, not boilerplate):
- patterns: `ForwardedHeaderFilter` + `server.forward-headers-strategy=NATIVE`
  + the careful "X-Forwarded-For is spoofable" treatment; Bucket4j
  `Bandwidth.classic(100, Refill.greedy(…))`; `@ControllerAdvice`
  `@ExceptionHandler(MethodArgumentNotValidException.class)`; RFC 7807
  `spring.mvc.problemdetails.enabled=true` + HikariCP (one-line bullets only).
- security: `@EnableMethodSecurity` + `@PreAuthorize("@authz.isOwner(#id,
  authentication)")`; `@Bean PasswordEncoder = new BCryptPasswordEncoder(12)`;
  security-headers DSL; Spring Cloud Vault.
- tdd (stub, 159 lines): `@WebMvcTest+MockMvc+@MockBean`; `@DataJpaTest`;
  JaCoCo `0.8.14` prepare-agent+report; Testcontainers is a **2-line bullet**
  (full `@Container`/`@DynamicPropertySource` lives in verification instead).

## Appendix D — adversarial verdicts (workflow `wf_cae37272-a5a`)

- **Thesis — PARTIALLY HOLDS.** Descriptive (framework-native code) survives on
  independent evidence; inferential (0–6.5% *proves* it) is overclaimed and the
  bound is wrong (up to 16%). Metric = vocabulary distance, blind to
  depth/correctness/templating. Primary evidence = internal cross-referencing.
  → folded into §1.2, C1/C2.
- **Plan — SOUND ON STRATEGY (option D correct), WS2 gate calibration needed
  revision.** Depth-ratio rationale was factually wrong (5.1× is cross-family;
  within-pack springboot 1.98× passes, laravel 5.27× fails); similarity gate
  mis-weighted toward the vacuous vs-ecc comparison; leak gate over-billed;
  machine path hardcoded. → folded into C3/C4/C5/C10, §5 WS1/WS2.
- **Feasibility — FEASIBLE WITH FIXES.** Shallow frontmatter parser (flat keys —
  C6); leak markers `hive `/`namespace` would false-positive (C5); 10% ceiling
  contradicts own data (C4); manifest incomplete (C8); no root-path helper;
  `hive packs` is a group not flat (C9); `Report` undeclared; CI can't verify
  vs-ecc. One reviewer claim (budget gate absent) **rejected** — it exists on
  this branch. → folded into §5 WS1/WS2, §6.

## Appendix E — deliverable inventory for the executing session

| # | Path | Kind |
|---|---|---|
| 1 | `docs/packs/SPEC.md` | doc |
| 2 | `docs/packs/TEMPLATE-SKILL.md` | doc |
| 3 | `docs/packs/packs.json` | manifest (v2 schema, §5 WS1) |
| 4 | `src/mindhive/packs.py` | code (stdlib-only) |
| 5 | `tests/test_packs.py` | tests |
| 6 | `src/mindhive/__main__.py` | edit (additive: `hive packs` group) |
| 7–10 | `.claude/skills/mindhive-python-{patterns,security,tdd,verification}/SKILL.md` | content |
| 11–12 | `.claude/skills/windows-powershell-{patterns,verification}/SKILL.md` | content |
| 13–14 | `.claude/skills/claude-harness-{patterns,verification}/SKILL.md` | content |

Durable study dossiers backing this PRD (survive context resets):
`C:/Users/hanif/ECC/.ecc-review/study/prd-{django,laravel,quarkus,springboot,cross-structure,adjacent,mindhive-surface,machine-reality}.md`.

**Definition of done:** all 14 artifacts landed; `hive packs check` exit 0 with
output inspected; `hive packs report` shows median file ≥ 300 non-empty lines
and internal same-role similarity under gate after shared-preamble exclusion;
full suite green; `hive log` per change-set; local vs-ECC similarity run
performed with `MINDHIVE_ECC_SKILLS` set.
