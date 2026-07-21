---
name: claude-harness-verification
description: >-
  Verification loop for harness configuration changes: hook registration and
  live-fire audit, skill frontmatter and discovery checks, the
  context-budget gate, served-dashboard feature grep, and worktree/branch
  sanity. Run after touching settings.json hooks, skills, adopted content,
  or session wiring.
origin: mindhive
pack: claude-harness
role: verification
kind: verification
---

# Claude Harness Verification Loop

Configuration changes fail silently: a mis-matched hook never fires, a
malformed skill never surfaces, an over-budget adoption taxes every future
session. This loop makes each of those visible before the change is called
done — first what is *registered*, then what actually *fires*, then what it
all *costs*.

## When to Activate

- After adding, editing, or removing hooks in `settings.json`
- After creating or adopting any skill (project-scoped or global)
- After restarting or redeploying the live dashboard
- After creating, switching, or cleaning up linked worktrees
- After changing permission allowlists or MCP server configuration
- Before reporting any harness/configuration change as complete
- When a hook behaves differently across two sessions with "identical" config
- After an installer or update may have rewritten registrations behind you

## Phase 1: Hook registration audit

Parse the actual registration file — not your memory of it — and confirm
every intended stage, matcher, and shim path:

```bash
py - <<'EOF'
import json, pathlib
cfg = json.loads((pathlib.Path.home() / ".claude" / "settings.json")
                 .read_text(encoding="utf-8"))
seen = {}
for stage, entries in (cfg.get("hooks") or {}).items():
    for entry in entries:
        for h in entry.get("hooks") or []:
            cmd = h.get("command", "?")
            key = (stage, entry.get("matcher", "*"), cmd)
            seen[key] = seen.get(key, 0) + 1
            exists = ("?" if "hive" in cmd else
                      pathlib.Path(cmd.split()[0].strip('"')).exists())
            print(f"{stage:<18} matcher={entry.get('matcher','*'):<24} "
                  f"exists={exists} -> {cmd}")
dupes = {k: n for k, n in seen.items() if n > 1}
print(f"\nregistrations: {len(seen)}  duplicates: {len(dupes)}")
for k in dupes:
    print(f"  DUPE: {k}")
EOF
```

Read the output against four questions:

- Does every shim path point at a file that exists in the checkout it names?
  A stale path fires into a deleted worktree and dies silently — the stage
  looks registered and does nothing.
- Does the matcher cover the tools the rule is for — and no more? The
  policy-tier dispatcher here is scoped `Grep|Bash|PowerShell`; a matcher
  widened to `.*` starts paying spawn cost on every event of the stage.
- Did the change duplicate a registration? Double-fire is the classic
  symptom of an installer stacked on a hand edit, and it doubles both the
  latency and any append the hook performs.
- What is the total spawn budget now? At the measured ~64 ms per process
  start, count registrations per high-frequency stage and multiply — a
  stage carrying ten hooks charges over half a second to every matching
  tool call before any hook logic runs.

If a registered shim path does not exist on disk, stop — every event on
that stage is currently a silent no-op.

## Phase 2: Live-fire probe

Registration proves intent; only a fired hook proves wiring. Trigger one
benign matching event per changed stage and check its observable effect:

```bash
# PreToolUse / policy tier: run a command the policy MUST flag, in a form
# that is safe to refuse — the probe SUCCEEDS when the gate says no:
git commit --no-verify --dry-run 2>&1 || true
# expectation: the deny reason appears, carrying the enforcement marker
# the policy tier stamps on every decision it makes.

# PostToolUse capture: run any matched tool once, then confirm the store
# saw it (the capture stage appends structured events):
py -m mindhive recall "tool" --limit 3
# expectation: a fresh capture row from the probe, with machine-readable
# fields — not a prose blob.

# UserPromptSubmit / promptgate: submit a deliberately underspecified
# prompt in a scratch session and confirm the injected directive block
# appears in the transcript.
```

A probe with no observable effect while Phase 1 shows the registration is
the worst state in this loop: wired-looking and dead. Bisect by running the
shim directly (`powershell -File hooks/<stage>.ps1` with a sample stdin
payload) to separate "harness never called it" from "shim swallowed it".

## Phase 3: Skill frontmatter and discovery audit

Every new or edited skill must parse through the real parser and surface in
discovery. Exercise both:

```bash
py - <<'EOF'
import pathlib, sys
sys.path.insert(0, "src")
from mindhive.plugins import _parse_frontmatter
for skill in sorted(pathlib.Path(".claude/skills").glob("*/SKILL.md")):
    fm = _parse_frontmatter(skill.read_text(encoding="utf-8"))
    problems = []
    if fm.get("name") != skill.parent.name:
        problems.append(f"name={fm.get('name')!r} != dir")
    if "metadata" in fm:
        problems.append("nested metadata block (must be flat keys)")
    if not fm.get("description", "").strip():
        problems.append("empty description")
    if fm.get("description", "").strip() in (">", ">-", "|", "|-"):
        problems.append("description parsed to a literal scalar marker")
    print(f"{skill.parent.name:<34} "
          f"{'OK' if not problems else '; '.join(problems)}")
EOF
```

Then confirm discovery end-to-end: a project skill should appear in the
session's available-skills listing by name — this repo's pack skills
demonstrably surface within the same session that writes them, so absence
after a write is a real finding, not eventual consistency. A skill that
parses but never surfaces usually sits at the wrong scope for where you are
testing from (project skill, session outside the repo), or its description
collapsed under an older parser. For pack skills,
`py -m mindhive packs check` subsumes this phase with stricter gates — run
it instead and read its findings.

## Phase 4: Context budget gate

Any change that adds resident context — new global skills, longer
descriptions, adopted content — must clear the persisted budget:

```bash
py -m mindhive context --check
```

The exit-code contract, and what each demands:

- **0** — under the ceiling; record the measured total in the report.
- **1** — over; the output ranks the heaviest contributors. Trim or
  de-adopt before proceeding; do NOT raise the budget to make the number
  fit — the budget encodes a decision, and revising it is the operator's
  call, made explicitly with `--set-budget`, never a side effect.
- **2** — no budget set. For an adoption flow this is itself a finding:
  set the ceiling first (baseline + agreed margin), then re-run.

Two scope facts sharpen the reading: project-scoped skills do not move this
number (the analyzer measures the global `~/.claude` surface), so if this
phase regresses after a project-skill-only change, something landed at the
wrong scope — that IS the finding. And description length is the entire
per-skill cost: a 90-token description across hundreds of sessions dwarfs
the one-time cost of a deep body.

## Phase 5: Served-dashboard feature grep

After any dashboard restart or change that could affect what the live
process serves, prove the sections users rely on are still present — an
HTTP 200 proves only that a process answered:

```bash
curl -s http://127.0.0.1:54461/ | grep -c "showOversee\|renderOversee"
```

A zero count on hooks that were present yesterday means the process is
serving the wrong code — the exact signature of the vanished-section
incident (a worktree branch predating the feature, wired in via an import
override; it looked like data loss and was a served-code regression).
Recovery is never a data operation: restart the process plainly from the
main checkout and re-grep. Extend the same proof to whatever feature the
change itself added:

```bash
# The generalized form — grep for YOUR feature's rendering hook:
curl -s http://127.0.0.1:54461/ | grep -c "<newFeatureHookName>"
```

When previewing an unmerged branch, run the same grep against the PREVIEW
port and leave 54461 alone entirely — the preview's count proves the
branch; 54461's count proves you did not touch what users are looking at.

## Phase 6: Worktree, branch, and namespace sanity

Close the loop on session wiring before handing the branch over:

```bash
git worktree list                  # every listed path should still exist
git rev-parse --abbrev-ref HEAD    # the branch this work actually sits on
git log --oneline -3               # the commits you believe you made
git status --short                 # nothing uncommitted left behind
```

An uncommitted worktree is a liability (idle worktrees may be cleaned);
work belongs on its branch before the session ends. Then the namespace
check — if `hive` commands ran during the change, confirm they landed in
the MAIN namespace rather than minting a fresh one:

```bash
py -m mindhive projects
```

A freshly-created namespace in the listing is a misfire to investigate, not
ignore: it means some command ran with a cwd or env that escaped the
worktree-collapse rule, and its events are stranded in an empty store the
briefing will never read. Record which namespace received this session's
events in the report.

## Interpreting probe failures

When Phase 2 finds a dead stage, the bisect runs outside the harness — feed
the shim the same payload the harness would:

```powershell
# A minimal stage payload, piped to the shim directly (PowerShell 5.1):
$payload = '{"tool_name":"Bash","tool_input":{"command":"git status"}}'
$payload | powershell -NoProfile -File hooks/pre_tool_use.ps1
"shim exit: $LASTEXITCODE"
```

The decision table the bisect feeds:

```text
shim run direct: works    harness probe: dead
  -> registration problem: wrong matcher, wrong stage, wrong settings
     level (user vs project), or the harness never restarted after the
     edit. Re-read Phase 1's output with fresh eyes.

shim run direct: fails    with a Python traceback
  -> the stage handler is broken in code; fix it under the ordinary suite
     (the handler is versioned Python precisely so this is testable).

shim run direct: fails    silently, exit 0, no output
  -> the shim is swallowing errors — the worst configuration state,
     because every future failure will be invisible. Add the error path
     to the shim before anything else.

shim run direct: fails    with "file not found" on the hive command
  -> environment: the shim's process cannot resolve the interpreter or
     the checkout (the classic editable-install/worktree mismatch).
```

Record which branch of the table the finding landed in — "hook was dead"
without the mechanism is not a resolvable report.

## Stop conditions

- Phase 1 finds a registered shim pointing at a missing file, or a
  duplicate registration → stop; fix the registry before probing.
- Phase 2 probe produces no observable effect on a registered stage →
  stop; wired-looking-but-dead is the highest-priority finding this loop
  can produce.
- Phase 3 reports a nested-metadata or name-mismatch skill → stop; it will
  never validate, and may never surface.
- Phase 4 exits 1 after a supposedly project-scoped change → stop; the
  content landed at the wrong scope.
- Phase 5 greps zero feature hooks on 54461 → stop everything else and
  restore the live dashboard first; users are looking at missing sections.
- Phase 6 shows a freshly-minted namespace → stop and re-home the events
  before they orphan.

## Output Template

```
HARNESS CONFIGURATION VERIFICATION
==================================
Phase 1  hook registry    [PASS/FAIL]  <n> registrations, stale=<n>, dupes=<n>, spawn budget ~<ms>/event
Phase 2  live fire        [PASS/FAIL]  <stage>: <observed effect / DEAD>
Phase 3  skill audit      [PASS/FAIL]  <n> skills parsed, problems: <list or none>; discovery: <seen/missing>
Phase 4  context budget   [PASS/FAIL/UNSET]  exit <code>, total ~<tokens> vs budget <tokens>
Phase 5  served features  [PASS/FAIL/N-A]  hook count <n> on 54461 (preview port: <n>/N-A)
Phase 6  worktree state   [PASS/WARN]  branch <name>, uncommitted <n>, namespace <name>

Overall: [READY / NOT READY]

Notes:
- <accepted warnings, preview ports used, budget headroom, probes run>
```

## Worked example: landing a project skill pack

The reference shape of a passing run after adding project-scoped skills:

```text
Phase 1  N-A (no hook change) — registry re-read anyway: 0 stale, 0 dupes
Phase 2  N-A (no hook change)
Phase 3  8 skills parsed OK (flat keys, names match dirs);
         packs check run instead: exit 0, findings read
         discovery: all 8 surfaced in-session after write
Phase 4  exit 0 — total unchanged (project scope, as designed)
Phase 5  N-A (no dashboard change) — 54461 spot-grep: hooks present
Phase 6  branch claude/..., 0 uncommitted after commit, namespace: main
Overall  READY
```

The two details that make this a verification and not a ceremony: Phase 4's
*unchanged* total is affirmative evidence the content landed project-scoped
(the cheap way to prove a scope claim), and Phase 3's discovery check used
the live session listing, not the file tree — files exist the moment you
write them; surfacing is the thing that can silently fail.

## Evidence rules for configuration findings

Configuration evidence has a property code evidence lacks: it goes stale
the moment anyone else touches the machine. Three rules compensate:

```text
Timestamp the registry read. The Phase 1 output is a snapshot of
  settings.json AT a moment; pair it with the file's mtime so a later
  "but it shows X now" has a reference point.

Probe effects are quoted, not summarized. The deny reason's actual text,
  the capture row's actual fields, the injected directive's first line —
  paraphrased probe results are indistinguishable from assumed ones.

Scope claims get their negative space. "Landed project-scoped" is proven
  by the budget total NOT moving AND the skill surfacing in-repo; either
  half alone is consistent with a mistake. Record both numbers.
```

## Pairing with the code-change loop

Harness changes rarely travel alone — a new stage handler is Python code
AND a registration. The two loops divide the work; run both, dedupe
nothing by assumption:

```text
The code half (handler logic, capture modules, gate functions)
  -> the mindhive-python verification loop owns it: suite, diff review,
     change-log. A hook handler is ordinary tested Python there.

The wiring half (settings.json, shims, matchers, scopes)
  -> THIS loop owns it: registration audit, live-fire probe, budget,
     namespace. No pytest run proves a matcher regex fires.

The overlap trap: a green suite plus a correct-looking settings edit still
proves NOTHING about the pair — the suite never reads settings.json, and
the registry audit never executes handler logic. Only Phase 2's live-fire
probe exercises the actual seam, which is why it exists.
```

Ordering when both run in one session: code loop first (a broken handler
makes every probe result ambiguous), then this loop, then one combined
report — the harness change is not done until BOTH Output Templates read
READY, and the change-log entry names the settings/shim files alongside
the Python ones.

## Quick Reference

| Mechanic | Use |
|---|---|
| settings.json parse loop + dupe count | what is REGISTERED, deduped, with per-event spawn budget |
| stale-shim-path check | a dead path means a silently dead stage |
| live-fire probes per stage | deny-marker, capture row, injected directive — wiring proven, not assumed |
| shim-direct bisect | separates "never called" from "swallowed" |
| `_parse_frontmatter` audit loop | name==dir, flat keys, non-empty non-marker description |
| `packs check` supersedes skill audit | pack skills get the stricter deterministic gates |
| `context --check` exit codes | 0 under / 1 over (trim, never quietly raise) / 2 unset (set, then re-run) |
| unchanged-total scope proof | project-scoped claims verified by the number NOT moving |
| `curl \| grep <featureHook>` | feature presence, not HTTP 200, proves the dashboard serve |
| preview-port discipline | unmerged branches grep their own port; 54461 stays untouched |
| `git worktree list` + `projects` listing | no orphaned trees, no freshly-minted namespaces |
| shim-direct payload replay | the four-way bisect: registration, code, swallow, environment |
| mtime-stamped registry reads | configuration evidence dated against later edits |
| quoted probe effects | deny text, capture fields, injected lines — verbatim or worthless |
| both-loops READY rule | harness work ships only when code AND wiring templates close green |
| spawn-budget arithmetic | registrations × ~64 ms = the latency tax per matching event |
| post-installer re-audit | updates rewrite registrations; re-read, never assume |
