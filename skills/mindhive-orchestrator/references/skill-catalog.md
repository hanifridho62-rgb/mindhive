# Capability Catalog — When, For What, and Why This Skill Fires

The deep catalog behind the summary in [SKILL.md](../SKILL.md). Its purpose is to
let Claude reason about *when* to select the `mindhive-orchestrator` skill, *which
sub-capability* applies, and *why* — so the skill is chosen deliberately, and so
MindHive's own skill-selection machinery (`skills recommend`, the Recommendation
Matrix) can reason about it too.

---

## The one-line thesis

**Memory-grounded rigor beats cold re-asking.** ULT-IDEA alone forgets and
re-asks; the orchestrator alone under-validates. Fused, the workflow starts from
what the project already knows, refuses to leak into code before the design is
sound, picks execution skills by measured outcomes, and gets smarter every run.

---

## Trigger conditions (when it fires)

| Signal | Example |
|--------|---------|
| Explicit command | `/mindhive [idea]`, `/mindhive-orchestrator [idea]` |
| Orchestration request | "orchestrate this idea", "build X the MindHive way", "take this from idea to implementation-ready" |
| Validation request | "pressure-test this idea", "validate this before we build", "is this idea sound?" |
| Skill-selection request | "pick the right skill for this", "which skill should run this?" |
| Implicit fit | A non-trivial feature idea in a MindHive-enabled project that would benefit from validation *and* memory-grounded execution |

**Priority note:** as a process skill, `mindhive-orchestrator` runs *before*
implementation skills — it decides HOW to approach the work (validate → select →
execute), then invokes the implementation skills it selects.

## Task fit (for what)

| Sub-capability | Phase | What it does |
|----------------|-------|--------------|
| Recall preflight | Prime Directive | Consults `state` / `recall` / `graph related` / `rules inject` before any question |
| Idea validation | Phase 1 | Five-gap product pressure test; durable requirements doc |
| Architecture rigor | Phases 2 / 2.5 | Agent-native review + architecture-decision checkpoint |
| Skill selection | Phase S | Scored Recommendation Matrix over installed skills, anchored on telemetry |
| Execution & chaining | Phase X | Invoke selected skills; chain quality passes; record outcomes |
| Language sharpening | Phase 3 | Eliminate fuzzy terms; define testing strategy; ledger cross-reference |
| Learning | Phase L | Turn mistakes into prevention rules; log durable lessons; `sync` |

## Rationale (why each piece earns its place)

- **Recall preflight** — every question you ask that the ledger could have
  answered is wasted user time and a chance to repeat a solved mistake. The gate
  is the cheapest quality lever in the workflow.
- **Five-gap validation** — building the wrong thing is the most expensive
  failure. Validation is far cheaper than rework.
- **Architecture review** — agent-native mismatches cost code rewrites later;
  catching them at design time is orders of magnitude cheaper.
- **Recommendation Matrix** — selecting the wrong skill wastes an execution cycle;
  scoring on measured `skills stats` / `skills status` makes selection auditable
  instead of a guess.
- **Execution chaining** — a first draft is not finished work; the follow-up chain
  (review → simplify → security → ship) is where quality is actually secured.
- **Learning loop** — without it, the system makes the same mistake twice. With
  it, judgment compounds run over run without a code release.

## When *not* to fire

- A trivial, well-understood edit with no idea to validate and no skill to
  select — do it directly or route to `ce-work`.
- The user wants *only* the autonomous loop → `/circle`.
- The user wants *only* the fused plan→work→review loop → `/engineering-loop`.
- Pure conversation or a factual question — no orchestration needed.

## Sibling skills (how it relates)

| Skill | Relationship |
|-------|--------------|
| `/circle` | The autonomous headless loop engine. `mindhive-orchestrator` may recommend it in Phase X for hands-off execution. |
| `/engineering-loop` | circle × lfg with a memory spine. A heavier, fully-autonomous cousin; this skill is the interactive, validation-first front door. |
| `ce-plan` / `ce-work` | Common Phase-X execution targets, scored in the Recommendation Matrix like any other candidate. |

## Follow-up chain map (Phase X.2)

| Work just done | Recommended follow-up (resolve against available-skills list) |
|----------------|---------------------------------------------------------------|
| New/changed code | a code-review skill, then `ce-simplify-code` |
| Auth / payments / input / external APIs | a security skill (`security-sast`, `ce-security-reviewer`) |
| Bug-shaped / flaky | `ce-debug`, `superpowers:systematic-debugging` |
| UI / frontend | `ce-test-browser`, `ui-design:accessibility-audit` |
| About to ship | `ce-commit-push-pr` |

Give a one-line rationale per follow-up; recommend, let the user choose.
