---
name: claude-harness-patterns
description: >-
  Claude Code harness wiring as this repo actually uses it: hook stages and
  shims, the deterministic policy tier, skill frontmatter and discovery
  scope, context-budget discipline, worktree lifecycle, and the
  editable-install dashboard rule. Use when configuring hooks, skills, or
  sessions here.
origin: mindhive
pack: claude-harness
role: patterns
kind: patterns
---

# Claude Harness Patterns

How this repo wires the Claude Code harness: deterministic enforcement in
hooks, discovery-scoped skills, budgeted context, and a worktree lifecycle
that never touches the live dashboard.

## When to Activate

- Registering, editing, or debugging a hook in `settings.json`
- Adding a skill (project-scoped or global) or editing SKILL.md frontmatter
- Deciding where an automation rule belongs: prompt prose vs hook vs policy
- Working in a linked worktree, or previewing an unmerged branch's dashboard
- Adopting external skills and weighing their standing context cost
- A hook fires twice, not at all, or on the wrong tool
- Writing a deny reason, an injected directive, or any text a model must act on
- Auditing the permission allowlist or MCP server configuration for scope creep

## The enforcement ladder: prose < skill < hook < policy

A rule that must ALWAYS hold cannot live in prompt text — prompts drift,
sessions compact, models paraphrase. This repo's ladder, weakest to
strongest:

```text
prompt prose      suggestion; survives one session at best
SKILL.md          loads on demand; guidance, not enforcement
hook              deterministic; the harness executes it EVERY matching event
policy tier       deterministic AND versioned in src/: hooks call into
                  enforce.py, where checks like the GateGuard port decide
                  ask/deny before a tool ever runs
```

The wiring: the harness invokes a thin PowerShell shim (`hooks/*.ps1`), the
shim calls `hive hook <stage>`, and the stage dispatches into Python. Logic
lives in Python — testable by the ordinary suite; the shim stays too small
to be wrong. When a behavior must be guaranteed ("never bypass commit
hooks", "flag destructive commands"), it goes in the policy tier, where
`hook_decision` consults `POLICY_CHECKS` before any learned matcher.

## Hook stages in this repo

```text
PreToolUse         the gate. Matcher-scoped (Grep|Bash|PowerShell here);
                   the policy tier answers ask (escalate to the human) or
                   deny (hard block) — fail-open on internal error, and
                   every reason carries the ENFORCED_MARK so transcripts
                   show why a call was stopped.
PostToolUse        the recorder. hookcapture turns outcomes into structured
                   store events with machine-readable payloads
                   ({tool, error, category, retryable}) — 260 legacy rows
                   with no parseable tool field taught that lesson.
UserPromptSubmit   the enricher. promptgate injects skill directives when a
                   prompt is underspecified; treat an injection as "the
                   prompt needed rewriting", not as a safety net.
SessionStart/End   the bookends. Start auto-loads MEMORY.md; end queues the
                   review and change-log gates into the next session's STATE.
```

Registration lives in `settings.json` under `hooks.<Stage>` as
matcher+command arrays. Three rules keep it sane:

```text
1. One command per concern — a 47 KB hooks blob of inline one-liners (the
   ECC anti-pattern) is unreviewable and undebuggable; shims point at
   versioned code instead.
2. Count your spawns: every registered hook is a process start per event.
   Measured here at ~64 ms per spawn — 18 hooks on one event class costs a
   second of latency on EVERY tool call before any hook does work.
3. A stage that appends to the store must be idempotent against re-capture:
   live events pre-seed the same (session, signature, action) key the
   transcript back-harvester dedups on.
```

## Skill anatomy and the flat-frontmatter contract

A skill is a directory with one `SKILL.md`. The harness reads only the
frontmatter at discovery time; the body loads when invoked:

```markdown
---
name: claude-harness-patterns        # MUST equal the directory slug
description: >-                      # the ONLY text resident in context —
  ...folded block scalar...          # this line is the discovery surface
origin: mindhive                     # flat provenance keys; the shared
pack: claude-harness                 # parser FLATTENS any nested block,
role: patterns                       # so nested metadata cannot be
kind: patterns                       # validated and is rejected by gates
---
```

Two hard-learned parsing facts: the dependency-free parser folds block
scalars (`>-`) — without that, an ECC-style folded description parses to the
literal `>-` marker and zeroes every description-derived signal downstream —
and it flattens indentation, which is WHY the pack spec mandates flat keys.
Write descriptions as triggers ("Use when...") within the manifest's length
budget: the description is charged to every session; the body is free until
invoked.

## Discovery scope: project skills vs global skills

Where a skill lives decides who pays for it:

```text
<repo>/.claude/skills/<name>/     project-scoped. Served only to sessions
                                  inside this repo. Costs OTHER projects
                                  nothing. Where the stack content packs
                                  live, and why they can be deep.

~/.claude/skills/<name>/          global. The description is resident in
                                  EVERY session on this machine — measured
                                  here at ~44,000 est. tokens across 282
                                  skills before any work begins.
```

Adoption discipline follows: external skills get a provenance prefix
(`ecc-*`) to keep telemetry keys collision-free, land globally ONLY behind
the context budget gate, and default to project scope when their subject is
project-shaped. The budget mechanism itself:

```bash
py -m mindhive context --set-budget 48000      # operator sets the ceiling
py -m mindhive context --check                 # exit 1 over budget — the
                                               # gate adoption flows script
```

The sibling verification file wires this check as its budget phase, and its
registration-audit phase parses the `settings.json` layout described above.

## settings.json anatomy

The registration file, reduced to the shapes this repo actually uses:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Grep|Bash|PowerShell",
        "hooks": [
          { "type": "command",
            "command": "powershell -NoProfile -File hooks/pre_tool_use.ps1",
            "timeout": 10 }
        ]
      }
    ],
    "PostToolUse":      [ { "matcher": "...", "hooks": [ ... ] } ],
    "UserPromptSubmit": [ { "hooks": [ ... ] } ],
    "SessionStart":     [ { "hooks": [ ... ] } ]
  },
  "permissions": {
    "allow": [
      "Bash(git status:*)",
      "Bash(git log:*)",
      "Bash(ls:*)"
    ]
  },
  "env": { "MINDHIVE_AUTORESUME_TIMER": "off" }
}
```

Reading rules: the `matcher` is a regex over tool names — omit it and the
entry fires on every event of that stage; `timeout` bounds the shim (a hook
that hangs, hangs the session's tool call with it); user-level
(`~/.claude/settings.json`) and project-level (`.claude/settings.json`)
files merge, which is exactly how duplicate registrations sneak in when an
installer writes one level and a hand edit wrote the other.

## The hook I/O contract

A hook process receives the event as JSON on stdin and answers with its
exit code — the harness reads both:

```text
stdin    {"tool_name": "Bash", "tool_input": {"command": "..."}, ...}
         (stage-specific fields; PostToolUse adds the tool's result)

exit 0   proceed; stdout may add context (UserPromptSubmit stdout is
         injected into the prompt — that is how directive injection works)
exit 2   BLOCK the action; stderr is fed back to the model as the reason —
         this is the channel the deny tier answers through
other    non-blocking error; the harness logs and proceeds (fail-open)
```

Consequences worth designing around: a deny reason must be written for the
MODEL to read (actionable, specific — it will try again with a fix), and a
capture hook that prints to stdout on the wrong stage can accidentally
inject noise into context. Keep shims silent except for their one channel.

## Permission allowlist discipline

Pre-approving safe commands is configuration, and it follows one rule:
allowlist READ-ONLY, prompt for everything else.

```text
Belongs in permissions.allow        Never belongs there
--------------------------------    --------------------------------
git status / log / diff (read)      git push, git reset, rm
ls, pwd, which/Get-Command          anything with -Force/-Recurse
py -m pytest (runs the suite)       registry/service mutation
hive recall / state (read-only)     hive gc --prune, store writes
```

The measured motivation: permission denials were the single largest
recorded tool-failure class here (50+), and each prompt interrupts the
operator. The fix is a *narrow* allowlist expanded deliberately — glob
each entry (`git log:*`) rather than blanket-allowing a binary, and audit
the list whenever it grows: an allowlist is attack surface exactly as much
as it is convenience.

## MCP server scope

Same principle as skills, one level down the stack:

```text
.mcp.json (repo root, checked in)   project-shared servers — the next
                                    developer inherits the same toolset
                                    from the clone alone
~/.claude.json (user global)        personal servers only
```

A project-only integration configured at user scope is a portability bug:
the repo silently depends on tooling it does not declare. When a session
needs an MCP tool that shows as deferred, schemas load on demand — batch
every tool you expect to need into ONE lookup rather than paying a
round-trip per tool.

## Skill invocation is exact-name, deliberate

Discovery is automatic; invocation is not. The rules this repo's sessions
run under:

```text
- Skills are invoked through the Skill tool by their EXACT listed name,
  including any plugin prefix (compound-engineering:ce-plan) — a bare or
  guessed name errors.
- A user-typed /name maps to the matching listed skill, slash stripped.
- Built-in CLI commands (/help, /config, /model) are NOT skills — never
  route them through the Skill tool.
- A skill whose trigger matches the task is invoked BEFORE substantive
  work, not after; the body loads at invocation, which is why deep bodies
  are free and long descriptions are not.
```

## CLAUDE.md hierarchy and the instruction ladder

Instruction files stack, and the resolution order is part of the wiring:

```text
~/.claude/CLAUDE.md          personal, cross-project rules (shell dialect,
                             edit discipline) — travels with the user
<repo>/CLAUDE.md             project law, checked in — dashboard rules,
                             change-log mandate, land-green policy
                             ON CONFLICT: the project file wins
@imports                     keep the root file thin; hierarchical files
                             beat one monolith for maintenance and for
                             the context they cost
```

The division of labor with the enforcement ladder above: CLAUDE.md states
policy a human maintains; hooks enforce the subset that must never depend
on a model remembering it. When the same rule appears in both, the hook is
the enforcement and the prose is the documentation — removing the prose
does not remove the rule.

## Worktree lifecycle

Parallel work here happens in linked worktrees under `.claude/worktrees/`,
one branch per worktree. The rules that keep them safe:

```text
- cwd does NOT persist between tool invocations in a session; every command
  uses absolute paths rather than relying on an earlier cd.
- Imports resolve to the MAIN checkout via the editable install unless a
  subprocess is pointed at the worktree's src explicitly — the single most
  common "my change has no effect" failure in this repo's history.
- Namespaces deliberately collapse: hive commands run in a worktree land in
  the MAIN repo's store namespace, so memory stays unified across branches.
- A worktree is disposable; the branch is not. Commit on the branch before
  leaving a worktree idle — unchanged worktrees may be cleaned up.
```

## The editable-install dashboard rule

The live dashboard on port 54461 serves whatever branch the MAIN checkout
has checked out — that is the entire mechanism, and it is correct by
default. Two prohibitions with an incident behind them:

```text
NEVER repoint the live process at another worktree via PYTHONPATH/sys.path:
it silently serves older or differently-scoped code with NO error — this
once made the entire Oversee section vanish and looked exactly like data
loss (it was a served-code regression; the store was never touched).

NEVER call a dashboard restart done on HTTP 200 alone: grep the served page
for the feature hooks users rely on (showOversee, renderOversee). The
sibling verification file owns that check as a numbered phase.
```

Previewing an unmerged branch's dashboard is fine — on a DIFFERENT port,
from that branch's own worktree, as a separate process.

## Choosing the tier: worked examples

Four real rules from this repo, mapped onto the ladder — the reasoning is
the reusable part:

```text
"Never bypass commit hooks (--no-verify)"
  -> policy tier, DENY. Deterministic to detect, never legitimate in this
     workflow, and the cost of a miss is unverified history. Prose would
     drift; a deny with a marked reason cannot.

"Destructive commands need a human"
  -> policy tier, ASK. Detection is deterministic but legitimacy is
     situational — rm -rf on a scratch dir is fine. Escalation preserves
     the human call while guaranteeing the pause happens.

"Every src/tests change-set gets a change-log entry"
  -> hook (session end) + STATE notice, not a block at write time.
     Enforcement at the WRONG moment would fight iteration; queueing a
     blocking notice enforces it at the moment that matters (next
     session's start) without slowing this one.

"Prefer f-strings; keep docstrings in this repo's voice"
  -> CLAUDE.md prose. Style has judgment in it; a hook would false-fire
     endlessly. The cost of a miss is a review comment, which is the
     correct price.
```

The pattern behind all four: pick the tier by *detection determinism* and
*cost of a miss*, never by how strongly someone feels about the rule.

## Session bookkeeping surfaces

```text
MEMORY.md      auto-loaded at session start; rebuilt by `hive sync` — run it
               at session end so the next session inherits current state.
STATE.md       carries blocking notices (unlogged changes, pending reviews);
               clear them by DOING the thing, never by editing the file.
session_id     the store's join key; inherit MINDHIVE_SESSION when set,
               "manual" otherwise — never fabricate one.
```

## Quick Reference

| Mechanic | Use |
|---|---|
| prose < skill < hook < policy | put each rule at the weakest tier that still guarantees it |
| shim → `hive hook <stage>` → Python | logic stays versioned and testable; shims stay trivial |
| `hook_decision` + `POLICY_CHECKS` | ask/deny before learned matchers; fail-open with a marked reason |
| structured failure payloads | `{tool, error, category, retryable}` — machine-readable or it never existed |
| flat frontmatter + folded `>-` description | the parser flattens and folds; description = the only resident cost |
| `<repo>/.claude/skills/` first | project scope costs other projects nothing; global scope needs the budget gate |
| `context --set-budget` / `--check` | the adoption gate; exit 1 over ceiling |
| absolute paths + worktree PYTHONPATH | cwd resets between calls; editable install points at main |
| port 54461 untouched + hook grep | live dashboard serves main; presence of showOversee proves the restart |
| `hive sync` at session end | MEMORY.md reflects reality for the next session |
| exit-2 + stderr | the deny channel: reasons written for the model to act on |
| stdout on UserPromptSubmit | the injection channel — keep every other shim silent |
| `permissions.allow` globs | read-only pre-approvals only; audit the list as it grows |
| `.mcp.json` vs `~/.claude.json` | project-shared servers checked in; personal ones global |
| detection determinism × miss cost | the tier-selection formula behind every worked example |
| merged settings levels | user + project files combine; the classic double-registration source |
| bounded shim timeouts | a hanging hook hangs the tool call it gates |
| provenance prefixes on adopted skills | collision-free telemetry keys (`ecc-*` precedent) |
