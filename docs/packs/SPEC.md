# Stack Content Pack Specification (v2)

> Normative source for MindHive stack content packs and the `hive packs`
> validator. Derived from the ECC "Specific Framework Content" study and the
> adversarially-verified PRD:
> `docs/prds/2026-07-21-001-stack-content-packs-prd.md` (v2).
> The validator (`src/mindhive/packs.py`, WS2) encodes THIS document; its
> thresholds live in `docs/packs/packs.json` — the manifest is the single
> source of truth for every numeric gate (PRD C8). If spec prose and manifest
> values ever disagree, the manifest wins and the prose is a bug.

## 1. Purpose

A **stack content pack** is a named family of skills that gives agent sessions
framework-native guidance for one stack this machine actually runs — the way
ECC's django/laravel/quarkus/springboot quartets do for their frameworks, but
gated by the deterministic checks ECC never built (depth stubs, template
drift, derivation leaks, and copy-paste all shipped invisibly there).

Two lessons from the study govern everything below:

1. **Specificity is proven by internal cross-referencing and native
   mechanics, not by a similarity score.** Line-set similarity is a
   vocabulary-distance proxy — near-guaranteed-low across ecosystems, blind to
   depth and correctness (PRD §1.2, C1). It is a floor, not proof.
2. **Structure may repeat; content must not.** The document skeleton is
   deliberately templated (that is what a spec is); the leaf content — code,
   commands, file references — must be extracted from the real system the
   pack describes.

## 2. Definitions

- **Pack** — a named family declared in the manifest (`mindhive-python`,
  `windows-powershell`, `claude-harness`). A pack owns 1..N skills.
- **Role skill** — a skill filling one of the pack's declared `roles[]`
  (e.g. `patterns`, `security`, `tdd`, `verification`). Role vocabularies are
  **per-pack** — the validator hardcodes no role set (PRD C7: ECC has ≥5
  vocabularies; the quartet is convention, not law).
- **Feature skill** (reserved) — a non-role skill a pack may carry (the ECC
  analog: `django-celery`, `laravel-plugin-discovery`). Declared under a
  pack's optional `features[]`; carries `kind: feature|integration|advisory`
  and a `feature:` slug instead of `role:`. No initial pack ships features;
  the field is reserved so the schema never needs a breaking change.
- **Kind** — a per-skill classifier from the documented vocabulary:
  `patterns | security | tdd | verification | testing | performance | native |
  advisory | integration | feature`. For role skills, `kind` normally equals
  `role`; it exists so QA/specificity checks can route correctly when a
  future pack uses a divergent vocabulary (PRD §1.3).

## 3. Layout and naming

- Role skills live at `.claude/skills/<pack>-<role>/SKILL.md`
  (project-scoped: served to sessions in this repo, invisible to
  `hive context`'s global `~/.claude` scan — zero always-on context tax).
- The directory slug, the frontmatter `name`, and `<pack>-<role>` MUST all be
  identical. This is the slug grammar ECC lacks (`dotnet-patterns` vs
  `csharp-testing` — PRD §1.3).
- Feature skills (when used): `.claude/skills/<pack>-<feature-slug>/SKILL.md`.

## 4. Frontmatter contract — FLAT keys only (PRD C6)

`plugins._parse_frontmatter` (plugins.py) matches `key: value` at any
indentation and flattens results into one lowercased dict; a nested
`metadata:` block collapses (`metadata` itself parses to `""`). Therefore
frontmatter uses **flat top-level keys only** — nested metadata is a spec
violation the structure gate rejects.

Required keys (all flat):

| Key | Rule |
|---|---|
| `name` | == directory slug == `<pack>-<role>` |
| `description` | ≤ `max_description_chars` (manifest; 300) after block-scalar folding; states the pack's stack + the role's job |
| `origin` | `mindhive` for authored packs (provenance marker, mirrors ECC's `origin: ECC` but flat) |
| `pack` | the owning pack's manifest name |
| `role` | one of the pack's declared `roles[]` |
| `kind` | from the §2 vocabulary |

Block scalars (`>`, `>-`, `\|`, `\|-`) are permitted for `description` — the
parser folds them (this repo added that support in the ECC wave).

## 5. Canonical document template

One template, ending ECC's three-way drift (PRD §1.1, Appendix B). The
fill-in skeleton is `docs/packs/TEMPLATE-SKILL.md`; the normative rules:

1. Frontmatter (§4).
2. `# <Title>` + one-sentence scope line.
3. `## When to Activate` — 5–10 concrete trigger bullets. This exact heading;
   never `When to Use` (the drift variant).
4. Role body — the pack's real content (§7 authoring rules).
5. `## Quick Reference` — a closing table of the file's key
   mechanics/commands.

**Role additions for `verification`:** numbered `## Phase N: <name>` sections
(at least `min_phase_sections_by_role.verification` of them), at least one
explicit stop condition ("if X fails, stop and fix"), and an
`## Output Template` block showing the report shape a verification run
produces. For `verification`, the manifest's `required_sections_by_role`
REPLACES the default required list (Quick Reference becomes optional there —
the Output Template is its closing artifact).

Section checks are exact stripped-line equality against the manifest's
`required_sections` / `required_sections_by_role` entries; phase sections are
counted by the pattern `^## Phase \d+`.

## 6. Depth rules (PRD C3)

Measured on the body (frontmatter stripped); "non-empty" = any
non-whitespace character.

- **Line floor (the anti-stub gate):** each role file ≥ `min_lines` (150)
  non-empty lines. This — not the ratio — is what catches an
  ECC-`springboot-tdd`-style stub (125 non-empty lines, uniformly shallow
  family ratio 1.98×).
- **Code-block floor:** ≥ `min_code_blocks` (6) fenced blocks;
  `min_code_blocks_by_role` overrides per role (verification: 4 — its blocks
  are longer).
- **Within-pack parity ratio (secondary discriminator):**
  `max(nonempty) / min(nonempty)` across a pack's role files ≤
  `depth_ratio_max` (2.5). Calibrated from the measured good ECC families
  (django 1.56×, quarkus 1.74×); laravel's 5.27× spread is the failure mode
  it exists to flag. Trivially satisfied by 1-role packs; 2-role packs
  compare two files.
- **Quartet-depth target (report-only):** files below
  `quartet_depth_target_lines` (300) non-empty are flagged "below quartet
  depth" as a **warning**. WS3 acceptance additionally requires the median
  file ≥ the target — the floor prevents stubs; the target enforces the
  actual depth ambition (django/quarkus exemplars run 400–812 lines).

## 7. Authoring rules (the specificity bar)

These are the rules the similarity metric cannot check (PRD C1) — they are
enforced editorially at authoring and review time:

- **Extract from the real system.** Write each file the way ECC's
  `quarkus-patterns` was written — from a running system (this repo, this
  machine), with runnable commands and real `path:line` references. Never
  invent a hypothetical demo domain.
- **Internal cross-referencing (mandatory).** Each pack's role files must
  share ONE example subsystem, and each role file must reference at least
  **two** artifacts (functions, files, fixtures, commands) that a sibling
  role file defines or discusses. This is the Django-wiring authenticity
  signal (`ProductFactory` → `Product` → `ProductQuerySet.active()` →
  `IsOwnerOrReadOnly`) that a template-fill pipeline cannot fake.
- **Portability: every command must run on THIS machine.** Windows 11,
  PowerShell 5.1 + Git Bash. `py` (or the full interpreter path), never bare
  `python` (Store stub). No `&&`/`||`/ternary in PowerShell examples; judge
  native commands by `$LASTEXITCODE`; absolute paths. The ECC `python3`
  dashboard break is the cautionary tale.
- **No ECC text.** The packs copy the *design* (role families, phase-gated
  verification, quick-reference closers), never the prose or code. The
  internal cross-pack gate (§8) is the honesty check that matters; the
  external vs-ECC comparison is corroboration only.
- **Pack-specific requirements for the three initial packs:**
  - `mindhive-python` — all four roles built around one shared subsystem
    (the event-ledger/recall pipeline), so tdd tests the exact modules
    patterns describes.
  - `windows-powershell` — automation and CLI workflows must be robust in
    local **administrative** environments: standard modules/cmdlets only (no
    third-party module dependencies), explicit elevation detection
    (`WindowsPrincipal`/`IsInRole` pattern) before admin-only operations,
    execution-policy awareness, `Test-Path` guards before destructive or
    creating operations, `-Confirm:$false`/`-Force` used deliberately and
    documented, and every snippet valid in Windows PowerShell **5.1**
    (user requirement, 2026-07-21).
  - `claude-harness` — grounded in this repo's actual hook wiring
    (`hive hook` stages, PreToolUse policy tier), skill frontmatter contract,
    worktree lifecycle, and context-budget discipline.

## 8. Similarity rules (PRD C4)

**Metric** (the study's metric, exactly): per file, take the body, split into
lines, `strip` → collapse internal whitespace to single spaces → lowercase →
drop lines shorter than 10 characters → form a set. For a pair,
`similarity = |A ∩ B| / min(|A|, |B|) × 100`.

**Comparisons and thresholds:**

| Comparison | Pairs | Threshold |
|---|---|---|
| **Internal** (the real risk) | same-role files across different packs | > `similarity_internal_same_role_max_pct` (18) → **error** |
| **External** (corroboration) | pack file vs same-role files in a declared external corpus | > `similarity_external_max_pct` (10) → **error**; > `similarity_external_warn_pct` (6.5) → **warn** |

**Shared-preamble exclusion (internal pairs only):** before comparing two
internal files, drop from both sets any normalized line containing one of the
manifest's `shared_preamble_markers` (case-insensitive substring). All three
packs describe one machine and one repo; honest shared truth ("run pytest →
`$LASTEXITCODE` → `hive log` → `PYTHONPATH=<worktree>/src` → CI green") must
not trip the gate. The exclusion does NOT apply to external comparisons — the
external gate keeps its full strength. Per-pair overrides may be added to the
manifest if a legitimate pair still trips (PRD §7).

**External corpus resolution:** `external_corpora.<name>.path_env` names an
environment variable holding the corpus root (e.g. `MINDHIVE_ECC_SKILLS`).
No filesystem path is committed to the repo (PRD C10). Env var unset or path
missing → a **warning** finding ("corpus skipped"), never an error. Corpus
files are role-tagged from the trailing directory token
(`django-tdd` → `tdd`) and restricted to the declared `families` allowlist.

**CI vs local:** CI cannot see the ECC clone, so by the missing-corpus rule
CI silently skips the external comparison — therefore CI's anti-copy gate is
the hermetic fixture-corpus unit test in `tests/test_packs.py`, and the real
vs-ECC run is a **local manual acceptance step**:

```powershell
# PowerShell 5.1
$env:MINDHIVE_ECC_SKILLS = 'C:/Users/hanif/ECC/skills'; hive packs report
```

```bash
# Git Bash
MINDHIVE_ECC_SKILLS=/c/Users/hanif/ECC/skills hive packs report
```

## 9. Domain-marker / leak rules (PRD C5)

Each pack declares `domain_markers`: short, **pack-exclusive** raw-text tokens
from its example domain (the ECC analog is `/api/markets`, not "spring").
Markers must NOT be vocabulary the sibling packs legitimately share — `hive `
and `namespace` are the documented counter-examples that would false-positive
across all three packs.

The leak gate scans each pack's raw bodies (case-sensitive substring) for
**other** packs' markers; a pack's own markers in its own files are ignored.
A hit is a finding at the **marker-owning pack's** `leak_severity` (`warn`
for all three initial packs — single-author, single-session authoring makes
real derivation leaks unlikely, while same-machine packs share vocabulary by
nature). This is a lightweight guard, not a headline gate.

## 10. Manifest schema (`docs/packs/packs.json`)

Valid JSON (no comments). Top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `version` | int | schema version; validator warns on unknown versions |
| `similarity_external_max_pct` | number | §8 external error threshold |
| `similarity_external_warn_pct` | number | §8 external warn threshold |
| `similarity_internal_same_role_max_pct` | number | §8 internal error threshold |
| `shared_preamble_markers` | list[str] | §8 internal exclusion list |
| `depth_ratio_max` | number | §6 within-pack parity ceiling |
| `min_lines` | int | §6 non-empty line floor |
| `min_code_blocks` | int | §6 fenced-block floor |
| `min_code_blocks_by_role` | object | per-role override of the block floor |
| `quartet_depth_target_lines` | int | §6 report-only depth target |
| `max_description_chars` | int | §4 description ceiling |
| `required_sections` | list[str] | §5 default required headings (exact match) |
| `required_sections_by_role` | object | per-role REPLACEMENT of the default list |
| `min_phase_sections_by_role` | object | §5 minimum `## Phase N` count per role |
| `packs` | list | pack declarations (below) |
| `external_corpora` | object | named corpora: `{path_env, families}` |

Per-pack fields:

| Field | Type | Meaning |
|---|---|---|
| `name` | str | pack name; prefixes every skill slug |
| `roles` | list[str] | declared role vocabulary (any tokens; never assumed) |
| `features` | list[str] | reserved; non-role skill slugs (default `[]`) |
| `domain_markers` | list[str] | §9 pack-exclusive leak tokens |
| `leak_severity` | `"warn"`\|`"error"` | severity of leaks of THIS pack's markers elsewhere |
| `compare_external` | list[str] | corpus names to compare against (may be empty) |
| `related` | list[str] | reserved; cross-pack edges for future ledger emission (PRD §8 Q5) |

## 11. Validation contract (`hive packs`)

A subcommand **group** (registered like `review`/`sop`/`roadmap` — PRD C9):

- `hive packs check [--manifest PATH] [--json]` — runs every gate; exit **1**
  if any **error**-severity finding exists (warnings alone → exit 0). The
  pre-commit / adoption gate, same contract as `hive context --check`.
- `hive packs report [--manifest PATH] [--json] [--pack NAME]` — full stats
  (per-file non-empty lines, code blocks, below-target flags, internal and
  external similarity matrices, findings). No exit-code semantics (always 0).

`--manifest` defaults to `<git root>/docs/packs/packs.json`, resolved via
`git rev-parse --show-toplevel` (never bare `Path.cwd()` — cwd does not
persist between tool calls). Findings reuse the `context.Finding` shape
`(rule, severity, subject, message)`; reads are **total** — malformed or
unreadable files become findings, never exceptions (SOP 7.5). `--json`
output is stable-keyed: `{ok, findings: [...], stats: {...}}`.

## 12. Versioning and change process

The spec, template, and manifest version together (`version: 2` matches PRD
v2). Threshold tuning is a manifest edit + a matching update to the WS2
tests that encode it (drift breaks tests, not users — PRD §7). Content
changes to pack skills follow the repo's standard discipline: full pytest
suite green, `hive packs check` exit 0 inspected, `hive log` for any
`src/`+`tests/` change-set (pack SKILL.md content under `.claude/skills/` is
content, not source — log it when a change-set also touches code, and at
minimum record a `major_change` entry when a pack first lands, per PRD WS3
acceptance).
