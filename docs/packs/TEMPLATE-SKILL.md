# Stack Content Pack — canonical SKILL.md template (v2)

> Fill-in skeleton for every pack role skill. Normative rules live in
> `docs/packs/SPEC.md`; numbers live in `docs/packs/packs.json`. Copy the
> skeleton below into `.claude/skills/<pack>-<role>/SKILL.md` and replace
> every `<angle-bracket>` token. Lines starting with `[guidance]` are
> instructions to the author — delete them from the real file.

---

## Skeleton (all roles)

```markdown
---
name: <pack>-<role>
description: >-
  <One folded sentence, ≤300 chars total: the stack this pack covers + what
  THIS role file gives an activating session. Front-load the trigger nouns.>
origin: mindhive
pack: <pack>
role: <role>
kind: <role>
---

# <Human Title: Stack + Role>

<One sentence: what this file makes a session good at, and for which system.>

## When to Activate

- <trigger 1 — a concrete task/situation, not a topic>
- <trigger 2>
- <trigger 3>
- <trigger 4>
- <trigger 5>
[guidance] 5-10 bullets. Write situations ("adding a CLI subcommand to
[guidance] __main__.py"), never subjects ("CLI design"). These bullets are the
[guidance] discovery surface — they decide when the skill fires.

## <Topic Section 1>

<Prose stating the rule/pattern and WHY it holds in this system.>

```<lang>
<real code or commands, extracted from the live system — cite path:line in
the surrounding prose. Runnable on THIS machine (py not python; PS 5.1
dialect; absolute paths).>
```

[guidance] Repeat topic sections to depth: floor 150 non-empty lines and >=6
[guidance] fenced blocks per file (verification: >=4); TARGET >=300 non-empty
[guidance] lines (quartet depth - django/quarkus exemplars run 400-812).
[guidance] MANDATORY internal cross-referencing: this file must reference at
[guidance] least TWO artifacts (functions, files, fixtures, commands) that a
[guidance] sibling role file in the same pack defines or discusses, and all
[guidance] the pack's roles must share ONE example subsystem. This is the
[guidance] authenticity signal a template cannot fake (SPEC §7).
[guidance] Do NOT copy ECC text. Do NOT use sibling packs' domain_markers.

## Quick Reference

| Mechanic | Use |
|---|---|
| `<command or API>` | <when/why> |
| `<command or API>` | <when/why> |

[guidance] Closing table of the file's key mechanics. Required for every role
[guidance] except verification (see below).
```

---

## Additional skeleton for `role: verification`

[guidance] Verification files are phase-gated pipelines (the strongest shape
[guidance] in the studied ECC corpus). Replace the topic sections with
[guidance] numbered phases (>=3, pattern `## Phase N:`); include at least one
[guidance] explicit stop condition; close with `## Output Template` instead of
[guidance] Quick Reference (which becomes optional).

```markdown
## Phase 1: <Gate name>

```<lang>
<the real commands, in execution order>
```

If <failure condition>, stop and fix before continuing.

## Phase 2: <Gate name>

<...>

## Phase 3: <Gate name>

<...>

## Output Template

```
<STACK> VERIFICATION REPORT
===========================
Phase 1: <name>   [PASS/FAIL] <key numbers>
Phase 2: <name>   [PASS/FAIL] <key numbers>
Phase 3: <name>   [PASS/FAIL] <key numbers>

Overall: [READY / NOT READY]

Issues to fix:
1. ...
```
```

---

## Author checklist (delete before shipping — the validator re-checks most of it)

- [ ] Directory slug == frontmatter `name` == `<pack>-<role>`
- [ ] Frontmatter keys FLAT (`origin:`/`pack:`/`role:`/`kind:` top-level — never nested under `metadata:`)
- [ ] `description` ≤ 300 chars after folding
- [ ] `## When to Activate` exact heading, 5–10 situation bullets
- [ ] ≥150 non-empty lines (target ≥300); ≥6 code blocks (verification ≥4)
- [ ] verification: ≥3 `## Phase N:` sections, ≥1 stop condition, `## Output Template`
- [ ] other roles: closing `## Quick Reference` table
- [ ] ≥2 references to sibling-role artifacts; one shared example subsystem across the pack
- [ ] Every command runs on this machine (`py`, PS 5.1 dialect, no `&&`/`||`, `$LASTEXITCODE` not `$?` for natives)
- [ ] No ECC prose/code; no sibling pack's `domain_markers`
- [ ] `hive packs check` exit 0; `hive packs report` inspected
