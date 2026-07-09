---
name: mindhive-orchestrator
description: >-
  Turn a raw idea into a rigorously validated, implementation-ready design and
  then into executed, quality-checked work — on a MindHive memory spine. This is
  the ULT-IDEA workflow (five-gap product pressure test, agent-native
  architecture review, architecture-decision checkpoint, fuzzy-language
  elimination, testing strategy, anti-leak gating, ruthless YAGNI) fused with the
  MindHive orchestrator (recall-before-ask preflight, scored skill Recommendation
  Matrix, invoke-channel execution with green/red status, and a learning loop
  that turns every mistake into a prevention rule). Use when the user types
  /mindhive [idea] or /mindhive-orchestrator [idea], or asks to "orchestrate this
  idea", "pressure-test and build X the MindHive way", "take this from idea to
  implementation-ready", "validate this idea before we build", or "pick the right
  skill for this". Before asking the user anything, it recalls from the MindHive
  ledger and explores the codebase; it scores candidate skills with an explicit
  matrix; it chains follow-up quality passes; and it logs what it learned back
  into MindHive. Works in Claude Code, Claude Cowork, and Claude NewTask, and
  degrades gracefully to stateless ULT-IDEA rigor when the MindHive CLI is absent.
---

# MindHive Orchestrator — ULT-IDEA rigor on a memory spine

**Transform rough ideas into rigorously validated designs — and then into
executed, quality-checked work — with built-in guardrails against implementation
leaks, vague language, unexamined assumptions, and architectural misalignment.
Every stage is seeded by MindHive memory: the ledger is consulted before the user
is ever asked a question, candidate skills are scored against measured telemetry,
and every run teaches the system a durable lesson.**

This skill is the fusion of two disciplines:

- **ULT-IDEA** — the idea → design → implementation-ready spec workflow. It
  supplies the *thinking rigor*: validate the idea against five gaps, review
  agent-native architecture, lock architecture decisions, sharpen every term,
  define a testing strategy, and refuse to leak into implementation before the
  design is approved.
- **The MindHive Orchestrator** — the *memory and execution substrate*. It
  supplies recall-before-ask preflight, a scored skill Recommendation Matrix,
  the invoke channel that drives any installed skill with green/red status, and
  a learning loop (failures → prevention rules → improvements) that makes the
  next run smarter than this one.

> **What this does:** it recalls what the project already decided, interviews you
> only about what the ledger can't answer, pressure-tests the idea, reviews the
> architecture, recommends the best skills with a scored matrix, executes through
> them, chains quality passes, and writes lessons back to the MindHive ledger.
> ULT-IDEA is the canvas; MindHive is the memory the canvas is painted on.

---

## Table of Contents

1. [Prime Directive: Recall Before You Ask](#prime-directive)
2. [MindHive Function-Call Syntax](#syntax)
3. [What This Skill Is — Capability Catalog](#catalog)
4. [Overview & North Star](#overview)
5. [Anti-Pattern Doctrine](#anti-pattern)
6. [High-Level Process Flow](#process-flow)
7. [Phase 1: Explore (Validate Idea)](#phase-1)
8. [Phase 2: Agent-Native Architecture Review (If System)](#phase-2)
9. [Phase 2.5: Architecture Decision Checkpoint](#phase-2-5)
10. [Phase 3: Interrogate & Sharpen](#phase-3)
11. [Phase S: Skill Selection & Recommendation Matrix](#phase-s)
12. [Phase X: Execute & Chain](#phase-x)
13. [Phase L: Learning Loop & Log-Back](#phase-l)
14. [Communication Rules](#communication-rules)
15. [Anti-Leak Protection](#anti-leak)
16. [Ruthless YAGNI](#yagni)
17. [Completion Criteria](#completion)
18. [Troubleshooting & Edge Cases](#edge-cases)
19. [Context Handoff at ~50%](#handoff)
20. [Compatibility & Portability](#compat)
21. [Integration Map](#integration-map)

---

## <a name="prime-directive"></a>Prime Directive: Recall Before You Ask — HARD-GATE #1

**Before you ask the user a single question — before Phase 1's first word — you
must consult the MindHive ledger and the project's own code.** This is not a
suggestion; it is a gate with the same force as the Anti-Leak HARD-GATE that
forbids implementation before design approval. ULT-IDEA without memory re-asks
questions the project already answered and repeats mistakes the project already
learned from. MindHive exists so that never happens again.

**The recall preflight (run first, always):**

1. `python -m mindhive state` — current project status, active work, and standing
   constraints. This tells you what the project is *currently doing* so your
   questions don't relitigate settled scope.
2. `python -m mindhive recall "<key terms from the idea>"` — prior decisions,
   caveats, and failures on this topic. Add `--semantic` for meaning-based
   matches when literal recall is thin.
3. `python -m mindhive graph related "<main entity>"` — connected decisions and
   files, when the idea touches an existing subsystem. The graph surfaces the
   blast radius: which other decisions and files your idea will bump into.
4. `python -m mindhive rules inject` — the active prevention rules for this
   project. These are lessons the system already paid for; honor them from the
   first keystroke instead of rediscovering them the hard way.

**The crucial rule:** *if a question can be answered by exploring the codebase or
the ledger, explore instead of asking.* Only genuinely user-owned decisions —
scope, priorities, taste, product intent — reach the user. When the ledger
already settled something, say so and move on:

> "The ledger shows you settled on single-school scope here (decision from
> 2026-06-30), so I'll assume that unless you tell me otherwise."

**Why this is a gate, not a nicety.** The five-gap pressure test, the
architecture review, and the skill Recommendation Matrix are all *sharper* when
they start from what the project already knows. Recall is not a preamble you can
skip when you're in a hurry — skipping it is how you waste the user's time asking
what they already told you, and how you re-run a skill that the ledger shows fails
on this exact task. Run the preflight. Let its results pre-answer or skip
questions. Then begin ULT-IDEA.

**If MindHive is unavailable** (a Claude surface without the CLI installed — see
[Compatibility & Portability](#compat)), announce it once —
"MindHive memory unavailable — running ULT-IDEA in stateless mode" — and proceed
with full ULT-IDEA rigor, substituting in-conversation reasoning and direct
codebase exploration for ledger recall. The gate still holds in spirit: explore
the code before asking what the code can answer.

---

## <a name="syntax"></a>MindHive Function-Call Syntax

Every MindHive capability is reached through one command surface. Learn the
invocation form once; it is used at every phase below.

### Invocation resolution (three portable forms)

MindHive commands run through the project's Python module. Resolve the
invocation in this order and use the first that works:

1. **Preferred:** `python -m mindhive <command>` — works whenever the active
   interpreter has MindHive installed.
2. **Alias:** `hive <command>` — works when the console-script entry point is on
   `PATH`.
3. **Explicit venv fallback (this machine):**
   `C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive <command>` — use
   when neither of the above resolves but the project venv exists.

Throughout this document commands are written as `python -m mindhive …` for
brevity; substitute whichever form resolves in your environment.

**Degradation contract:** if none of the three resolve, MindHive is not available
on this surface. Announce stateless mode once and continue with ULT-IDEA rigor.
Never hard-fail the workflow because a memory command was missing — the idea
still deserves to be validated.

### The three commands you run before asking anything

These are the preflight from the Prime Directive — the ones you will run most:

| Command | Returns | Use when |
|---------|---------|----------|
| `python -m mindhive state` | Current project status, active work, standing constraints (STATE.md) | Always, first — before Phase 1 |
| `python -m mindhive recall "<term>"` | Prior decisions, caveats, failures on a topic (add `--semantic` for meaning-based) | Always, and again per gap/term/skill |
| `python -m mindhive graph related "<entity>"` | Connected decisions and files (add `--depth N`) | When the idea touches an existing subsystem |

### The broader surface that makes execution effective and efficient

You do not need every command every run. Reach for these when the phase calls
for them (the full catalog with flags lives in
[references/mindhive-command-catalog.md](references/mindhive-command-catalog.md)):

| Command | What it gives you | Phase it serves |
|---------|-------------------|-----------------|
| `python -m mindhive briefing` | The auto-loaded session briefing (STATE + recent decisions + open threads) | Preflight, orientation |
| `python -m mindhive rules inject` | The active prevention-rules block for this project | Preflight (HARD-GATE #1) |
| `python -m mindhive rules summaries` | Structured lesson records (what was learned, per signature) | Skill selection, learning loop |
| `python -m mindhive rules show "skill:<name>"` | The prevention lesson for a specific skill that has failed before | Before invoking a red skill |
| `python -m mindhive skills list` | Live inventory of installed plugins + skills (registry truth) | Skill selection |
| `python -m mindhive skills status` | Green/red last-call outcome per skill | Skill selection |
| `python -m mindhive skills stats` | Per-skill completion-rate telemetry from the ledger | Skill selection (score anchoring) |
| `python -m mindhive skills invoke <name>` | Runs a skill via headless claude, records the outcome (exit 0 ok / 1 fail) | Execution (the invoke channel) |
| `python -m mindhive skills record <name> --ok\|--fail --note "…"` | Writes an agent-assessed outcome into the ledger | Execution, learning loop |
| `python -m mindhive skills recommend` | Deterministic most-needed-skills ranking from measured data | Skill selection |
| `python -m mindhive plugins recommend` | Top free plugins by breadth score (which plugin suits the work) | Architecture, skill selection |
| `python -m mindhive plugins scan` | Refreshes the plugin catalog snapshot | When the catalog is stale |
| `python -m mindhive append <type> "<text>" --tags …` | Appends a `decision`, `caveat`, or `experiment_result` event | Architecture lock, learning loop |
| `python -m mindhive flag "<text>" --category …` | Self-flags a judgment error (active at first occurrence) | Learning loop |
| `python -m mindhive graph build` | Rebuilds the knowledge graph (`--semantic` for meaning links) | When related is stale |
| `python -m mindhive dashboard` | Opens the web control panel for all features | Live monitoring of a run |
| `python -m mindhive export --recent N` | Writes `handoff.md` — the portable state snapshot | Context handoff |
| `python -m mindhive sync` | Refreshes the native MEMORY.md briefing for the next session | End of run |
| `python -m mindhive consolidate` | Dreaming pass: merges near-duplicate memories | Housekeeping (bounded) |
| `python -m mindhive verify` | Verifies the tamper-evident hash chain | Integrity check |

**Honesty about telemetry.** `skills stats` and `skills status` are *measured*
from the ledger — use them as hard signal. Anything you estimate that has no
telemetry backing (e.g., a first-time skill's probability of success) must be
labeled `(est.)`. Never present a model estimate as a measured rate. This mirrors
ULT-IDEA's demand for evidence over vibes, applied to skill selection.

---

## <a name="catalog"></a>What This Skill Is — Capability Catalog

Claude should understand *when* to fire this skill, *for what* it fires, and
*why* — so it selects the skill deliberately, not by accident. The deep catalog
lives in [references/skill-catalog.md](references/skill-catalog.md); the summary:

**Under what conditions it fires**

- The user types `/mindhive [idea]` or `/mindhive-orchestrator [idea]`.
- The user asks to "orchestrate this idea", "pressure-test / validate this idea
  before building", "take this from idea to implementation-ready", "build X the
  MindHive way", or "pick the right skill for this".
- A rough idea arrives that would benefit from validation *and* memory-grounded
  execution — i.e., most non-trivial feature ideas in a project that runs
  MindHive.

**For what tasks**

- **Idea validation** — the five-gap product pressure test (Phase 1).
- **Architecture rigor** — agent-native review + architecture-decision
  checkpoint (Phases 2 / 2.5).
- **Skill selection** — scored Recommendation Matrix over installed skills,
  anchored on measured telemetry (Phase S).
- **Execution & chaining** — invoke the selected skills, chain quality passes
  (Phase X).
- **Language sharpening** — eliminate fuzzy terms, define the testing strategy
  (Phase 3).
- **Learning** — turn every mistake into a prevention rule, log lessons back
  (Phase L).

**Why (the thesis)**

Memory-grounded rigor beats cold re-asking. ULT-IDEA alone forgets; the
orchestrator alone under-validates. Fused, the workflow starts from what the
project already knows, refuses to leak into code before the design is sound,
picks skills by measured outcomes, and gets smarter every run.

**When *not* to fire it**

- The task is a trivial, well-understood edit with no idea to validate and no
  skill to select — just do it (or route to `ce-work` / a direct edit).
- The user explicitly wants only the autonomous loop (`/circle`) or only the
  fused plan→work→review loop (`/engineering-loop`) — those are siblings, not
  this skill.

---

## <a name="overview"></a>Overview & North Star

**ULT-IDEA** is a skill that embodies rigorous thinking at every stage of idea
development. It exists to eliminate waste caused by:
- **Unvalidated assumptions** — "We think there's a problem, but is there real proof?"
- **Fuzzy language** — "Account" means three different things; which one is correct?
- **Premature implementation** — Starting to code before the spec is clear
- **Unexamined complexity** — Adding features "just in case" for a future that never arrives
- **Misaligned architecture** — Building systems that require code changes to add new features
- **Architectural mismatch** — Tool design that locks in behavior instead of enabling it

MindHive adds a sixth waste this fusion eliminates:
- **Amnesia** — Re-deciding what was already decided, re-asking what was already
  answered, and repeating mistakes the project already learned from. The recall
  preflight and the learning loop close this gap.

**Four core movements drive the skill** (ULT-IDEA's three, plus memory):

### Movement 1: Validate the Idea Itself (Phase 1)
**Question:** Does this problem exist? Is the user real? Can you build the simplest version?

Before proposing any design, rigorously test the idea against five critical gaps:
- **Evidence Gap** — Is there concrete proof this problem exists? (Time wasted? Money spent? Workflow blocked?)
- **Specificity Gap** — Who is the specific user? Can you name them? How does their life improve?
- **Counterfactual Gap** — What do they do *right now* without this solution? What workaround exists?
- **Attachment Gap** — Are you married to one solution form? What's the minimal version that proves success?
- **Durability Gap** — (For large products) Will this survive trend changes in 12–24 months?

If gaps exist, fill them. If evidence is weak, reject the idea or demand proof first.

**▸ MindHive weave:** each gap is checked against the ledger before it is asked.
Recall may already hold the evidence, the specific user, or a prior durability
decision — surface that instead of re-asking.

**See also:** [product-implications.md](product-implications.md) for progressive disclosure, latent demand discovery, and approval frameworks that emerge from validated ideas.

### Movement 2: Ensure Smart Architecture (Phase 2 + Phase 2.5)
**Question:** If building a system, does it follow agent-native principles? Are tool designs sound?

For software systems, validate that:
- **Parity** — Agent can do everything the user can do (no capabilities locked behind code)
- **Granularity** — Tools are atomic CRUD primitives, not bloated procedures
- **Composability** — New features emerge from prompts, not architecture rewrites
- **Emergent Capability** — The system can solve problems you never explicitly designed for
- **Improvement Over Time** — Better behavior comes from context accumulation, not code releases

And in Phase 2.5, make explicit decisions about:
- **Shared workspace** (user and agent work in same data space) or separate sandboxes
- **Primitives vs domain tools** (start with primitives, graduate to domain tools deliberately)
- **Approval gates** (what operations need explicit user approval)
- **Architecture pattern** (event-driven, unified orchestrator, etc.)

For non-software projects (curricula, sermon series, organizational processes), Phases 2 and 2.5 are skipped, but the guardrails about clarity and durability still apply.

**▸ MindHive weave:** consult `plugins recommend` to see which installed plugin
fits the emerging system, and `append decision` to log each locked architecture
choice so the next run inherits it.

**See also:**
- [mcp-tool-design.md](mcp-tool-design.md) for tool primitives vs workflows, CRUD completeness, dynamic capability discovery
- [action-parity-discipline.md](action-parity-discipline.md) for maintaining agent-user parity
- [architecture-patterns.md](architecture-patterns.md) for event-driven, unified orchestrator, approval gates, model tier selection
- [files-universal-interface.md](files-universal-interface.md) for file organization and shared workspace
- [shared-workspace-architecture.md](shared-workspace-architecture.md) for agent-user collaboration patterns

### Movement 3: Eliminate Fuzzy Language & Plan Implementation (Phase 3)
**Question:** Does every term mean exactly one thing? Are we ready to implement?

Interrogate the design by:
- **Sharpening terminology** — "Account" becomes "Customer Login" vs. "Billing Entity" (precise definition)
- **Testing edge cases** — Invent concrete scenarios that force precision about boundaries
- **Cross-referencing reality** — Does the code actually support what you claim? If not, fix it before designing new code
- **Recording decisions** — CONTEXT.md (glossary) and ADRs (hard reversals) capture what was decided and why
- **Planning testing** — Define what tests prove this works (capability tests, parity tests, integration tests)

**▸ MindHive weave:** cross-reference the *ledger* as well as the code —
`recall` and `graph related` catch idea-vs-decision contradictions, not just
idea-vs-code. Log canonical terms and hard reversals with `append decision` so
sharpened language persists.

**See also:**
- [CONTEXT-FORMAT.md](references/context-format.md) and [ADR-FORMAT.md](references/adr-format.md) for templates
- [agent-native-testing.md](agent-native-testing.md) for testing strategy and capability verification
- [IMPLEMENTATION-READINESS.md](IMPLEMENTATION-READINESS.md) for the handoff from design to implementation

### Movement 4: Remember, Select, and Learn (Prime Directive + Phases S / X / L)
**Question:** What does the project already know, which skills execute this best, and what did this run teach us?

- **Remember** — the recall preflight (`state` / `recall` / `graph related` /
  `rules inject`) runs before any question, so the workflow starts informed.
- **Select** — the Recommendation Matrix (Phase S) scores candidate skills on
  seven parameters, anchored on measured `skills stats` and `skills status`.
- **Execute & chain** — Phase X invokes the chosen skills and chains quality
  passes.
- **Learn** — Phase L turns every mistake into a prevention rule and logs
  durable lessons back, so the next `/mindhive` is smarter.

This is the movement ULT-IDEA lacks and MindHive supplies. It wraps the other
three: memory informs the front, learning captures the back.

---

## <a name="anti-pattern"></a>Anti-Pattern Doctrine: "This Is Too Simple To Need Design"

**Every project—no matter how small—gets rigor.**

A one-page todo feature. A config change. A single sermon outline. All of them.

**Why?** Because "simple" projects are where unexamined assumptions cause the most wasted work.

### The Anti-Pattern in Action

❌ **Bad:**
> "We need a way to let teachers mark attendance. It's straightforward—checkboxes next to student names, click save. Let's just build it."

✅ **Good:**
> "We need attendance marking. Before we design, let's validate:
> - Evidence: Is attendance tracking actually a bottleneck for teachers? (How much time do they waste currently?)
> - Specificity: Who uses this—teachers? Administrators? Both? In what context (classroom, office, home)?
> - Counterfactual: What do they do now? (Paper checklist? Verbal count? Digital spreadsheet?)
> - Attachment: Are we locked into checkboxes? Could a scan-QR approach work? Voice? Mobile app?
> - Durability: Will this survive if hybrid learning becomes standard? If class sizes change?
>
> Once we answer these, we can design smartly. And for a system that agents might use, we need to ensure parity—the agent can mark attendance, not just the UI."

**▸ MindHive weave:** before you even ask the five questions above, run
`recall "attendance tracking"` and `graph related "attendance"`. If the ledger
already holds the evidence or the prior decision, the "simple" project just got
simpler — you skip straight to what's genuinely unresolved.

### The Rule

**No implementation begins until:**
1. The idea has been tested against the five gaps
2. A design has been presented and approved by the user
3. A durable requirements document exists
4. (If applicable) Architecture review is complete, with explicit decisions documented
5. (If applicable) Fuzzy language has been eliminated and terms are locked in CONTEXT.md
6. (If applicable) Testing strategy has been approved

---

## <a name="process-flow"></a>High-Level Process Flow

```
START: User presents an idea  (/mindhive [idea])
  ↓
0. RECALL PREFLIGHT (HARD-GATE #1)   ◄── run BEFORE any question
   ├─ python -m mindhive state
   ├─ python -m mindhive recall "<key terms>"  (--semantic if thin)
   ├─ python -m mindhive graph related "<entity>"  (if touches a subsystem)
   └─ python -m mindhive rules inject
       └─ pre-answer / skip questions the ledger already settled
  ↓
1. DIAGNOSE PROJECT TYPE
   └─ Ask: Lightweight / Standard / Deep scope?
   └─ If multiple independent subsystems → FORCE DECOMPOSITION
  ↓
2. PHASE 1: EXPLORE (Always)
   ├─ Understand the idea (purpose, constraints, success criteria)
   ├─ Test against 5 gaps (Evidence, Specificity, Counterfactual, Attachment, Durability)
   │    └─ each gap checks the ledger first
   ├─ Offer Visual Companion (if UI/spatial design involved)
   ├─ Propose 2-3 approaches with trade-offs
   ├─ Present design & get approval (section by section)
   ├─ Write durable Requirements Document
   └─ USER REVIEWS & APPROVES spec
  ↓
3. ROUTE BASED ON PROJECT TYPE
   ├─ Non-system (curriculum, sermon, org process) → Skip Phases 2/2.5, go to Phase 3
   ├─ Lightweight system → Skip Phase 2, brief 2.5, then Phase 3
   ├─ Standard system → Phase 2 (full review) + Phase 2.5 (architecture checkpoint) + Phase 3
   └─ Deep system → Phase 2 (full review) + Phase 2.5 (comprehensive architecture) + Phase 3
  ↓
4. PHASE 2: AGENT-NATIVE ARCHITECTURE REVIEW (if applicable)
   ├─ Validate 5 principles (Parity, Granularity, Composability, Emergent, Improvement)
   ├─ Detect missing tool parity, bloated tools, locked-in behavior
   ├─ Detect CRUD completeness gaps (can agent create AND update AND delete?)
   ├─ Consult plugins recommend (which plugin suits the system)
   ├─ Propose fixes
   └─ Update design if scope changes
  ↓
5. PHASE 2.5: ARCHITECTURE DECISION CHECKPOINT (if system)
   ├─ Workspace / tools / approval gates / pattern / storage / context / model tier / mobile
   ├─ append decision for each locked choice (memory)
   └─ LOCK ARCHITECTURE DECISIONS
  ↓
6. PHASE 3: INTERROGATE & SHARPEN (Always)   ◄── completes the design; clears Anti-Leak HARD-GATE #2
   ├─ Eliminate fuzzy language → canonical terms
   ├─ Test with 3-5 concrete scenarios + edge cases
   ├─ Cross-reference with code AND the ledger
   ├─ Update CONTEXT.md (inline, real-time)
   ├─ Write ADRs (only if 3-part test passes)
   ├─ Define testing strategy (what proves this works)
   └─ USER FINAL APPROVAL of complete spec
  ↓
7. PHASE S: SKILL SELECTION & RECOMMENDATION MATRIX   ◄── orchestrator core (design is now locked)
   ├─ skills list / status / stats  (live inventory + measured telemetry)
   ├─ score 2-4 candidates on 7 parameters
   ├─ before a red skill: rules show "skill:<name>", apply the lesson
   └─ present matrix, get selection approval
  ↓
8. PHASE X: EXECUTE & CHAIN   ◄── implementation begins only after the gate cleared in Phase 3
   ├─ invoke selected skill(s) (Skill tool, or skills invoke for headless)
   ├─ record outcome (skills record --ok/--fail)
   └─ chain follow-up quality passes with a rationale each
  ↓
9. PHASE L: LEARNING LOOP & LOG-BACK
   ├─ record mistakes/inefficiencies/preferences (append caveat/experiment_result/decision)
   ├─ failures → rules → improvements (the loop closes)
   └─ python -m mindhive sync  (next session inherits the lessons)
  ↓
END: Spec + CONTEXT.md + ADRs + Testing Strategy + executed work + logged lessons
```

**Context handoff** runs continuously in the background: at every phase boundary,
estimate context consumption and offer a clean handoff at roughly 50% (see
[Context Handoff at ~50%](#handoff)).

---

## <a name="phase-1"></a>Phase 1: EXPLORE (Mandatory for All Projects)

Phase 1 answers the question: **"Is this idea valid, and what is the simplest design that solves it?"**

**▸ MindHive preflight for this idea (run before 1.0).** You already ran the
recall preflight from the Prime Directive. Now translate its results into a
question-suppression list: which of the questions below has the ledger already
answered? Note them explicitly ("state shows active work on X; recall shows a
prior decision on Y"). Every question you can answer from `state`, `recall`,
`graph related`, or direct codebase exploration is a question you do **not** ask
the user. Only scope, priorities, taste, and product intent survive to the
interview.

### 1.0 Project Diagnosis: Classify Scope

**Objective:** Determine how much rigor is needed. Do not skip this step—it shapes everything downstream.

**Ask one diagnostic question:**

> "What type of project is this?"
>
> **A) Lightweight** — Small, bounded, obvious scope. Examples:
> - New page/screen in existing app
> - Configuration change
> - Single sermon outline
> - Simple UI tweak
> - New field in a form
>
> **B) Standard** — Normal feature or component. Some decisions to make. Examples:
> - New workflow for existing system
> - Curriculum module (3–5 weeks)
> - Sermon series (4–8 weeks)
> - Authentication feature
> - API endpoint with business logic
>
> **C) Deep** — Cross-cutting, strategic, highly ambiguous. Examples:
> - New system or platform from scratch
> - Major refactor affecting multiple teams
> - Product pivot
> - Organization-wide curriculum redesign
> - Multi-agent system with complex interactions

**▸ MindHive weave:** `state` often answers this before you ask. If active work
and constraints indicate the scope, lead with your inference: "Based on the
ledger this looks Standard — a new workflow on the existing system. Correct?"

**Special Rule: Subsystem Decomposition**

If the user describes multiple independent subsystems:

> "I notice this includes chat, billing, and analytics—three independent systems. We can't design them together; they'll bloat the spec and cause scope creep. Let's pick one and design it thoroughly. Which should we start with? (We'll iterate through each separately afterward.)"

Do not proceed until scope is single and clear.

### 1.1 Understand the Idea: Three Dimensions

Ask questions **one at a time**. Focus on three dimensions:

**1.1.1 Purpose**
- Why does this matter?
- What problem does it solve?
- For whom?
- What business or personal goal does it advance?

**Example questions:**
- "Tell me about the pain point you're trying to solve. What happens right now that frustrates people?"
- "Who benefits from this? Be specific—describe one person."
- "How will you know this succeeded? What changes?"

**▸ MindHive weave:** run `recall "<purpose keywords>"` first. If the ledger holds
a prior problem statement or a stated goal, present it: "You previously framed
this as X — still the pain point?" instead of asking cold.

**1.1.2 Constraints**
- What are the boundaries? What's off-limits?
- Budget, timeline, audience, technical limits?
- Regulatory or organizational constraints?
- Existing systems or workflows that must be respected?

**Example questions:**
- "Are there any hard constraints—budget, timeline, platform, people—we need to work within?"
- "What can't we change, even if we wanted to?"
- "Who else needs to approve this, and what are their constraints?"

**▸ MindHive weave:** `state` lists standing constraints and `graph related`
surfaces which existing systems the idea touches. Read those before asking — many
constraints are already recorded.

**1.1.3 Success**
- How will you measure success?
- What behavior changes, for whom, and by how much?
- What metrics matter (engagement, time saved, accuracy, adoption)?

**Example questions:**
- "What does success look like? Be concrete: what metric tells you this worked?"
- "If we built this, how would teachers' workflows change?"
- "What's the minimum win that would justify building this?"

**Preference:** Use multiple-choice questions when possible. Keep cognitive load low.

**Guiding principle:** Ask about **WHAT** is needed, never **HOW** to build it. Implementation decisions (database schema, programming language, library choice, folder structure, tool design) are forbidden here.

### 1.2 Test Against the Five Gaps: Product Pressure Test

**Objective:** Rigorously validate the idea before investing in design.

Once you believe you understand the idea, interrogate it systematically. For each gap, ask a clarifying question and listen for weakness. **For each gap, check the ledger before you ask** — the evidence, the specific user, or a prior decision may already be recorded.

#### Gap 1: Evidence Gap
**Question:** Is there real, concrete proof the problem exists?

Not "we think there's a problem." Real evidence: time wasted, money spent, workflow blocked, people abandoning a tool.

**How to test:**
- "What's the most concrete thing someone's already done about this—paid for a workaround, built a DIY solution, spent extra time manually?"
- "Can you show me the bottleneck? What does a frustrated teacher do right now?"
- "How much time does this actually waste per week/month?"

**Red flag:** Vague answers like "people struggle with this" or "I feel like this would be useful." Demand proof.

**▸ MindHive weave:** `recall "<problem> evidence"` — a prior run may already have
captured the proof. If it did, cite it and move on.

#### Gap 2: Specificity Gap
**Question:** Who is the specific user? Can you name them? How does their life change?

Not "teachers" or "students." Specific: "Mrs. Rodriguez, a 9th-grade math teacher with 30 students, teaches three sections." Specific: "She spends 10 minutes per class taking attendance manually."

**How to test:**
- "Describe one specific person who would use this. What's their role? Where do they work? What's their day like?"
- "How many people fit this description? 1? 10? 1000?"
- "If we built this, how would their 9am Tuesday look different?"

**Red flag:** Abstract personas or "anyone who needs to" answers. Push for concreteness.

**▸ MindHive weave:** `recall "<user / persona>"` — the specific user may already
be defined in the ledger from prior product work.

#### Gap 3: Counterfactual Gap
**Question:** What do they do *right now* without this solution? What's their workaround?

Understanding the current state clarifies what's actually changing and prevents designing solutions that duplicate existing workarounds.

**How to test:**
- "Walk me through what happens today. Step by step. No solution yet—just reality."
- "If you had to do this without your proposed solution, what would you do?"
- "Do they have a workaround? What is it? How well does it work?"

**Red flag:** "There's no good solution right now, so this is new." That may be true, but be specific about what the current state is.

#### Gap 4: Attachment Gap
**Question:** Are you too married to one solution form? What's the minimal version that proves success?

Innovation happens when you detach from a specific solution and focus on the problem. "Attendance marking" could be checkboxes, QR scan, voice command, photo recognition, or passive RFID. Which is actually necessary?

**How to test:**
- "You mentioned [specific solution form]. Are you attached to that, or is it negotiable?"
- "What's the simplest thing you could build to prove this idea works?"
- "If you had to cut the scope in half, what stays and what goes?"
- "Are there three fundamentally different ways to solve this? Let's explore each."

**Red flag:** The user has only one solution in mind and won't consider alternatives. This is where you propose 2-3 approaches (see 1.3 below).

#### Gap 5: Durability Gap
**Question:** (For larger products/systems) Will this survive trend changes in 12–24 months?

For small one-time initiatives (sermon series, one classroom module), durability is less critical. For systems, platforms, or repeated content, it matters.

**How to test:**
- "What could change in the next year that would make this obsolete?"
- "If enrollment doubles / shrinks, does this still work?"
- "If a competitor does something, how does our design survive?"
- "What's the 'forever' version of this, not the trend-following version?"

**Red flag:** Design that's fragile to market changes or assumes constant growth.

**▸ MindHive weave:** `graph related "<subsystem>"` surfaces prior durability
decisions on connected components — reuse them rather than re-deriving.

**See also:** [product-implications.md](product-implications.md) for how validated ideas enable progressive disclosure, latent demand discovery, and approval workflows.

### 1.3 Offer Visual Companion (If Applicable)

**Trigger:** If upcoming design questions will involve visual content (mockups, layouts, diagrams, spatial structure, flow charts).

**Offer this as its own message** (do not combine with other content):

> "Some of what we're designing might be easier to explain if I can show it to you in a web browser. I can create mockups, diagrams, layout comparisons, and other visuals as we go. This feature is still new and can be token-intensive. Want to try it? (Requires opening a local URL.)"

Wait for explicit response. **Do not proceed with other content until they respond.**

**Decision rule per question:** Even after they accept the companion, decide FOR EACH QUESTION whether to use the browser or text.

- **Use the browser** for: mockups, wireframes, layout comparisons, architecture diagrams, side-by-side visual designs
- **Use the terminal** for: requirements questions, conceptual choices, trade-off lists, multiple-choice options, scope decisions

**Test:** Would they understand this better by seeing it than reading it?

**▸ Portability note:** the visual companion opens a local browser URL. On Claude
surfaces without local browser access (some Cowork / NewTask sandboxes), fall
back to terminal descriptions or inline diagrams — see
[Compatibility & Portability](#compat).

### 1.4 Propose 2-3 Approaches With Trade-Offs

**Objective:** Show the user that multiple valid paths exist, help them understand trade-offs, and recommend one.

**Structure for each approach:**
1. **Name it clearly** — "Approach A: Manual Attendance with UI Checkboxes"
2. **Describe it** — 2–3 sentences on how it works
3. **Name the trade-offs** — What becomes easier? Harder? What's the cost?
   - Development time
   - Maintenance burden
   - Scalability
   - User experience friction
   - Learning curve
4. **Be honest about your recommendation** — Lead with the approach you think is best and explain why

**Example (for attendance marking):**

> **Approach 1: Manual UI (Recommended)**
> Teachers open the app, see a list of students, tap checkboxes to mark present/absent/excused, and click save. Instant feedback. Works offline on tablets.
>
> **Trade-offs:**
> - **Easy:** Simple to build, intuitive for teachers, no setup required
> - **Hard:** Doesn't scale to 1000+ student rosters; requires manual action per class; no automation
> - **Ongoing:** Moderate UI maintenance
>
> **Approach 2: QR Scan**
> Print QR codes for students, they scan on entry, system marks attendance automatically.
>
> **Trade-offs:**
> - **Easy:** Fully automated once setup, no teacher time, impossible to miss someone
> - **Hard:** Requires hardware (scanner/camera); setup per classroom per period; fails if student forgets QR or camera breaks
> - **Ongoing:** Higher technical maintenance
>
> **Approach 3: Passive RFID (Future-Proof)**
> Student ID cards have RFID chips; reader at classroom door automatically logs attendance when they enter.
>
> **Trade-offs:**
> - **Easy:** Most automated; zero teacher time; works across building
> - **Hard:** High initial cost (~$5K); infrastructure setup required; privacy concerns; setup takes months
> - **Ongoing:** Specialized maintenance

**Key principle:** All three approaches are legitimate. The trade-offs depend on their constraints. Your job is to illuminate the choice, not make it for them.

**▸ MindHive weave:** `recall "<approach / technology>"` per candidate approach.
If a prior run tried an approach and it failed or succeeded, that history belongs
in the trade-offs — lead with it.

### 1.5 Present the Design & Get Section-by-Section Approval

**Objective:** Walk through the chosen design in detail, with explicit approval after each section.

**Structure of a design presentation:**

1. **Concept** (1 sentence) — What is this, in plain language?
2. **Problem & Context** (2–3 sentences) — Why are we building this? What pain does it solve?
3. **User & Their Goal** (3–5 sentences) — Who uses it? What do they achieve?
4. **User Flow** (5–10 steps) — How does someone use it? Step-by-step walkthrough of a real scenario.
5. **Scope Boundaries** (bulleted) — What's in? What's explicitly out? Why is something out of scope?
6. **Success Criteria** (3–5 metrics) — How do we know this worked? Measurable, not vague.
7. **(Optional) Error Handling** (3–5 scenarios) — What breaks? How do we recover?
8. **(Optional) Dependencies** (bulleted) — What else must be true for this to work?

**Scale by complexity:**
- Straightforward sections: 3–5 sentences
- Nuanced sections: 150–300 words
- Complex sections: up to 500 words

**After each section, ask:**
> "Does this look right so far? Anything I got wrong?"

Be ready to clarify or revise. Do not move forward if something doesn't land.

### 1.6 Write a Durable Requirements Document

**Objective:** Create a spec the implementation team can read once and execute without guessing.

**Deliverable:** Markdown or HTML file saved to user's preferred location (e.g., `docs/design/2026-06-30-attendance-requirements.md`).

**Structure:**

```
# [Project Name] — Requirements Document

## Summary
[1-2 sentences: What is this, in plain language?]

## Problem Statement
[2-3 sentences: The pain point, with evidence. Example: "Teachers currently waste 10 minutes per class re-entering attendance data. This is error-prone and blocks timely reporting."]

## User
[2-3 sentences: Specific user, their role, their context. Example: "Ms. Wong is a 7th-grade teacher with 120 students across four class periods. She takes attendance on paper, then manually enters it."]

## Design
[Exact design from Phase 1.5—concept, flow, scope, success criteria, error handling]

## Success Criteria
[Measurable metrics that indicate this worked]

## Out of Scope
[Explicit list of what we are NOT building, and why]

## Dependencies
[What else must be true for this to work?]

## Open Questions
[Anything still unresolved from Phase 1—list it here. These get answered in Phase 3.]

## Assumptions
[What are we assuming about users, the environment, the future?]

## Agent Considerations (If applicable)
[If this will be used by agents or AI: Can agents achieve all the same actions? See action-parity-discipline.md]
```

**Self-Review Checklist (Fix Inline):**

- [ ] Any "TBD," "TODO," or vague language? (Fix it now.)
- [ ] Internal contradictions? (Does the design match the success criteria?)
- [ ] Could any requirement be interpreted two different ways? (Pick one, state it explicitly.)
- [ ] Is this focused enough for one implementation cycle, or does it need decomposition?
- [ ] Are we answering WHAT, not HOW? (No database schemas, no code patterns.)
- [ ] Jargon without definition? (Define it inline or in a glossary.)
- [ ] If a system: Is agent parity addressed? Can agents do what users can do?

**User Review Gate:**

Ask the user to review the written spec:

> "Requirements document written. Please review it: Does it match what we designed? Anything missing or unclear? Once you approve, we'll move to Phase 2 (if applicable) or Phase 3."

Wait for approval. Only proceed once they confirm.

**▸ MindHive weave:** once the spec is approved, log the decision that the
requirements are locked: `python -m mindhive append decision "Requirements
approved for <project>: <one-line summary>" --tags orchestrator,requirements`.
The next run's recall preflight will surface it.

---

## <a name="phase-2"></a>Phase 2: Agent-Native Architecture Review (Conditional)

**Apply this phase if and only if:**
- The project involves building a system (software app, tool, platform, multi-agent workflow), AND
- The scope is Standard or Deep, AND
- The user approved the Phase 1 design

**Skip this phase if:** The project is non-technical (curriculum design, sermon series, organizational process, etc.).

**Objective:** Ensure the system design follows five agent-native principles, enabling long-term flexibility and feature growth without code rewrites.

**▸ MindHive weave:** run `python -m mindhive plugins recommend` before you
review. The plugin catalog shows which installed plugins fit the emerging
system's shape; a strong match may supply primitives you'd otherwise design from
scratch. `plugins scan` refreshes the catalog when it's stale.

### 2.0 Agent-Native Architecture: Five Principles

Agent-native systems treat agents (AI or automation) as first-class citizens. Features emerge from agent reasoning over primitive tools, not from code.

#### Principle 1: Parity
**Statement:** Whatever the user can do through the UI, the agent can achieve through tools.

**Why it matters:** If the agent can't do something a user can, the system isn't composable. You'll need to write code every time a user wants to automate that action.

**How to validate:**
- List every action a user can take in the UI (click button, enter text, delete data, approve request, etc.)
- For each action, ask: "Can the agent do this via a tool?"
- If not, the design has a parity gap.

**Example (attendance app):**
- User action: "Mark student present" → Agent tool: `mark_attendance(student_id, status)`  ✓
- User action: "Export absence report" → Agent tool: ??? (Missing tool = parity gap)

**Fix:** Add the missing tool. Now the agent can export reports; a prompt can automate report generation.

**See also:** [action-parity-discipline.md](action-parity-discipline.md) for maintaining parity over time, and the Parity Audit workflow.

#### Principle 2: Granularity
**Statement:** Tools are atomic primitives (CRUD: Create, Read, Update, Delete) and small. Features emerge from prompts composing primitives, not from large specialized tools.

**Why it matters:** Specialized tools lock behavior into code. Primitive tools let you change behavior by editing prompts.

**Example (bad granularity):**
- Tool: `generate_attendance_report(period, date, format, filter)` — This is bloated. To support a new report format, you need to update code.

**Example (good granularity):**
- Tool: `read_attendance(filters)` — Returns raw attendance data
- Tool: `transform_to_csv(data)` — Generic transformer
- Tool: `send_to_email(data, recipient)` — Generic sender
- Prompt composes: "Read attendance, filter by [criteria], transform to CSV, email to [recipient]"

To add a new report type, just write a new prompt. No code changes.

**How to validate:**
- For each tool in the design, ask: "Does this tool do one thing, or multiple things?"
- Can you describe the tool in 1–2 sentences?
- If the tool description is long or has multiple clauses, it's likely too specialized.

**Fix:** Break specialized tools into primitives. Composability increases, flexibility skyrockets.

**See also:** [mcp-tool-design.md](mcp-tool-design.md) for tool naming, input design, and CRUD completeness. [from-primitives-to-domain-tools.md](from-primitives-to-domain-tools.md) for when to add domain tools deliberately.

#### Principle 3: Composability
**Statement:** New features are written as prompts using existing tools, not as code changes to the architecture.

**Why it matters:** Prompts are cheap to write and change. Code changes are expensive, risky, and slow.

**How to validate:**
- Imagine three new features you might build in the next year
- For each feature, ask: "Can the agent achieve this by composing existing tools via a new prompt?"
- If the answer is "no, we'd need to add a new tool or change code," you have a composability gap

**Example (attendance app):**
- Feature 1: "Alert teachers if >10% of a class is absent" → New prompt, no code needed ✓
- Feature 2: "Export attendance for a student across all teachers" → New prompt, compose existing tools ✓
- Feature 3: "Integrate with parent texting system" → Probably needs a new tool (send SMS) — This is okay, as long as it's a primitive

**Fix:** Identify which tools are genuinely missing. Add them as primitives. Build features as prompts.

#### Principle 4: Emergent Capability
**Statement:** The agent accomplishes things you never explicitly designed for, because it has complete reasoning and primitive tools.

**Why it matters:** This reveals whether your architecture is truly open-ended. Latent demand—requests you didn't anticipate—can be satisfied automatically, without feature requests or code changes.

**How to validate:**
- Ask: "If a user asked the agent to do something we didn't explicitly plan for, could it work?"
- Example: "Summarize attendance trends for one student across the whole semester." (You didn't design this, but the agent might do it by composing read_attendance + analyze tools.)

**Fix:** If gaps are apparent, add missing primitives or reconsider tool specificity. Emergent capability indicates a healthy, flexible system.

**See also:** [product-implications.md](product-implications.md) for latent demand discovery and how agent usage reveals what users actually need.

#### Principle 5: Improvement Over Time
**Statement:** The app gets better through accumulated context (e.g., a `context.md` file that grows) and prompt refinement, without code releases.

**Why it matters:** You can iterate quickly. No deploy pipeline. No risk of introducing bugs.

**How to validate:**
- Is there a `context.md` file or similar persistent store for app context?
- Can you change agent behavior by editing prompts without touching code?
- Does the system learn from past interactions and remember them?

**Example:**
> Teacher feedback: "I want the app to auto-flag students with >5 unexcused absences."
> Solution: Update `context.md` with a rule, update the agent prompt to check the rule. No code change.

**Fix:** Ensure the system has injection points for context (configuration, rules, user preferences) that the agent prompt can reference.

**▸ MindHive is itself a worked example of Principle 5.** Its learning loop
(failures → rules → improvements) is exactly "the app gets better through
accumulated context, without code releases." When you design a system's
improvement mechanism, MindHive's `rules inject` / `rules promote` pattern is a
reference implementation.

**See also:** [system-prompt-design.md](system-prompt-design.md) and [dynamic-context-injection.md](dynamic-context-injection.md) for how to structure prompts and context.

### 2.1 Detect Gaps & Propose Fixes

For each principle that fails, diagnose and fix:

| Principle | Failure Mode | Diagnosis | Fix |
|-----------|-------------|-----------|-----|
| **Parity** | Agent can't do everything user can | Missing tools | Add the missing tool as a primitive |
| **Granularity** | Tools are bloated/specialized | Tool description is >3 sentences | Break tool into smaller CRUD primitives |
| **Composability** | New features require code changes | Prompt alone can't achieve feature | Redesign tools to be primitive enough |
| **Emergent Capability** | Agent is constrained/rigid | Only explicit features work | Add general-purpose tools (analyze, summarize, etc.) |
| **Improvement Over Time** | Behavior changes require code release | No context injection points | Add `context.md` or configuration layer |

**When you identify a gap:**
1. State the gap explicitly
2. Propose a fix
3. Ask the user: "Does this fix make sense? Should we include it in the design?"
4. If yes, update the Phase 1 design document and the requirements spec

---

## <a name="phase-2-5"></a>Phase 2.5: Architecture Decision Checkpoint

**Apply this phase if and only if:**
- The project is a system (software, multi-agent workflow), AND
- You've completed Phase 2 (or Lightweight/Standard skipped Phase 2 but are proceeding with Phase 2.5), AND
- The user has approved Phase 1 design and Phase 2 findings

**Objective:** Make explicit, documented decisions about implementation architecture BEFORE diving into design details or testing. This checkpoint ensures the implementation team has a clear blueprint.

**Deliverable:** An "ARCHITECTURE.md" document that locks these decisions. Do not proceed to Phase 3 until decisions are explicit.

**▸ MindHive weave:** each locked decision below gets an `append decision` so the
architecture survives across sessions:
`python -m mindhive append decision "<the choice + one-clause why>" --tags orchestrator,architecture`.

### 2.5.0 Workspace Architecture

**Decision:** Shared workspace or separate spaces?

**Option A: Shared Workspace (Recommended for Agent-Native)**
- User and agent work in the same file system or data store
- User can inspect, edit, and extend agent work
- Agents can build on user contributions
- No synchronization layer needed
- Complete transparency

**Option B: Separate Spaces**
- User has private workspace
- Agent has isolated workspace
- Requires sync layer to move data between spaces
- User can't easily inspect agent work
- More complexity, less transparency

**Decision Point:** Which approach fits this project?

**See also:** [files-universal-interface.md](files-universal-interface.md) and [shared-workspace-architecture.md](shared-workspace-architecture.md)

### 2.5.1 Tool Design Strategy

**Decision:** Start with primitives, graduate to domain tools deliberately.

**Option A: Primitives First (Recommended)**
- Begin with: `read_file`, `write_file`, `store_item`, `send_message`, `bash` (if appropriate)
- Prove the concept works with agent reasoning
- Add domain tools only when patterns emerge
- Minimal lock-in, maximum flexibility

**Option B: Domain Tools From Start**
- Pre-build domain-specific tools for your core operations
- Reduces agent iteration time
- Risk: Tools encode too much business logic

**Decision Point:** Which progression fits this project?

**Action:** Create a "Tool Roadmap" document:
- Phase 0 (Primitives): List of atomic tools
- Phase 1 (Emerging): Which patterns might call for domain tools?
- Phase 2+ (Optimized): Which operations might graduate to optimized code?

**▸ MindHive weave:** run `python -m mindhive plugins recommend` here — a
cataloged plugin may already ship the primitives you're about to design. Prefer
reuse over reinvention; note the chosen plugin in the Tool Roadmap.

**See also:** [from-primitives-to-domain-tools.md](from-primitives-to-domain-tools.md) and [mcp-tool-design.md](mcp-tool-design.md)

### 2.5.2 Approval Gates

**Decision:** What operations require explicit user approval vs. auto-apply?

Use the **Stakes/Reversibility Matrix:**

| Stakes | Reversibility | Pattern | Example |
|--------|---------------|---------|---------|
| Low | Easy | Auto-apply | Organizing files |
| Low | Hard | Quick confirm | Publishing to a feed |
| High | Easy | Suggest + apply | Code changes with undo |
| High | Hard | Explicit approval | Sending emails, payments |

**Action:** List high-stakes operations and their approval requirements.

**See also:** [architecture-patterns.md](architecture-patterns.md) for approval gate implementation and [product-implications.md](product-implications.md) for approval and agency patterns.

### 2.5.3 Implementation Pattern

**Decision:** Which architecture pattern best fits this project?

**Option A: Event-Driven Agent**
- Agent runs as a long-lived process
- Events (messages, webhooks, timers) trigger agent turns
- Agent decides how to respond based on system prompt

**Option B: Unified Agent Orchestrator**
- One execution engine, many agent types
- All agents use the same lifecycle management
- Different configs for different agent types

**Option C: Two-Layer Git Architecture**
- Code (shared repo) / Data (instance-specific)
- For self-modifying agents

**Option D: Site as Agent Output**
- Agent generates and maintains a website
- Natural output without specialized site tools

**Action:** Choose one pattern. Document how it applies to this project.

**See also:** [architecture-patterns.md](architecture-patterns.md) for complete pattern catalog.

### 2.5.4 Storage & File Organization

**Decision:** How will data be organized? What's the file structure?

**Pattern:** Entity-scoped directories

```
Documents/
├── Research/
│   └── {entityId}/
│       ├── primary.txt
│       └── related/
├── Chats/
│   └── {conversationId}.json
└── context.md
```

**Action:** Document the file structure for this project.

**See also:** [files-universal-interface.md](files-universal-interface.md) for patterns and the context.md pattern.

### 2.5.5 Context Injection Strategy

**Decision:** How does the agent learn about the current app state?

**Option A: System Prompt Injection (On-Demand)**
- Build system prompt dynamically at agent startup
- Fresh context every time
- Works well for short sessions

**Option B: Context File (Persistent)**
- Agent reads/updates `context.md` at session start
- Knowledge persists across sessions
- Better for learning over time

**Option C: Hybrid**
- System prompt has static guidance
- context.md has dynamic, learned state

**Action:** Choose strategy. Document what goes where.

**▸ MindHive is the reference for Option B/C.** Its `MEMORY.md` briefing + `state`
+ `rules inject` are exactly the persistent-context and hybrid patterns. If this
project is MindHive-adjacent, lean on that machinery instead of building a new
context layer.

**See also:** [dynamic-context-injection.md](dynamic-context-injection.md) and [files-universal-interface.md](files-universal-interface.md) for context.md pattern.

### 2.5.6 Model Tier Selection

**Decision:** What Claude model does each agent type use?

| Agent Type | Tier | Reasoning |
|------------|------|-----------|
| Chat | Balanced (Sonnet) | Fast, good reasoning |
| Research | Balanced (Sonnet) | Tool loops, synthesis |
| Quick lookup | Fast (Haiku) | Simple operations, low cost |
| Complex analysis | Powerful (Opus) | Multi-source synthesis |

**Action:** Map each agent type to a model tier.

**See also:** [architecture-patterns.md](architecture-patterns.md) for model tier selection.

### 2.5.7 Mobile Considerations (If Applicable)

**Decision:** If iOS/Android: What's the strategy?

**Key Decisions:**
- Storage: iCloud/Cloud Firestore or local?
- Background execution: How to checkpoint/resume?
- Permissions: What does the agent need access to?
- Cost awareness: Token budgets per session?

**Action:** Document mobile-specific architecture.

**See also:** [mobile-patterns.md](mobile-patterns.md) for iOS-specific patterns (iCloud sync, background execution, permissions, cost awareness).

### 2.5.8 ARCHITECTURE.md Deliverable

**Create a document that captures all these decisions:**

```markdown
# Architecture Decisions — [Project Name]

## Workspace Architecture
[Shared vs. separate?]

## Tool Design Strategy
[Primitives first? Timeline?]

## Approval Gates
[What requires approval?]

## Implementation Pattern
[Event-driven? Orchestrator? Site as output?]

## Storage & File Organization
[Directory structure with examples]

## Context Injection Strategy
[System prompt? Context file? Hybrid?]

## Model Tier Selection
[Agent types → model mapping]

## Mobile Considerations
[If applicable]

## Rationale
[For each decision, why this approach over alternatives?]

## Known Risks & Mitigations
[What could go wrong? How do we prevent it?]
```

**Approval Gate:**

> "Architecture decisions documented. Please review: Do these choices align with your vision? Should we proceed with Phase 3 interrogation and testing?"

**▸ MindHive weave:** after approval, log the lock:
`python -m mindhive append decision "Architecture locked for <project>: <pattern + workspace + tool strategy>" --tags orchestrator,architecture`.

---

## <a name="phase-3"></a>Phase 3: Interrogate & Sharpen (Mandatory for All Projects)

**Objective:** Eliminate ambiguity, test edge cases, lock down terminology, define testing strategy, and prepare for handoff to implementation.

This phase is where you shift from **facilitator** to **challenger**. Your role: interrogate assumptions, force precision, plan verification.

### 3.0 Eliminate Fuzzy Language: Ubiquitous Language

**Principle:** Every term in the spec means exactly one thing. Ambiguity is the enemy of implementation.

**How it works:**

As you review the spec, listen for overloaded or vague terms. When you hear one:

1. **Call it out immediately** (don't wait for this checklist)
2. **Propose a precise definition**
3. **Get agreement**
4. **Record it in CONTEXT.md** (live, real-time)

**Example:**

User says: "The system needs to handle account suspension."

You respond:
> "When you say 'account suspension,' do you mean:
> - A) The teacher's login is disabled, but their student data remains visible?
> - B) The teacher and all their students are hidden from the system?
> - C) Something else?
>
> Let's lock down one term. I'll call it [Definition], and we use that everywhere."

Once they choose, update CONTEXT.md immediately:

```markdown
# CONTEXT.md

## Account Suspension
**Definition:** The teacher's login credentials are disabled, preventing all system access. However, historical student records (grades, attendance) remain visible to administrators.

**Related Terms:** Account Deactivation (opposite action), Session Timeout (different mechanism)

**Last Updated:** 2026-06-30 by Claude
```

**Common Fuzzy Terms to Watch For:**
- "Account" (login? billing? user record?)
- "User" (teacher? student? admin? guest?)
- "Data" (any data? personal data? student data?)
- "Sync" (one-way? two-way? real-time? eventually consistent?)
- "Report" (visual? spreadsheet? automated? manual?)
- "Active" (enrolled? logged in? paid? approved?)
- "Flag" (mark for review? disable? hide? alert?)

**Rule:** If a term could reasonably mean two different things, sharpen it. Document it. Move on.

**▸ MindHive weave:** the project may already have a canonical definition. Run
`recall "<term>"` before you propose a new one — if the ledger (or a repo
`CONTEXT.md`) already defines "Account," reuse that definition rather than minting
a conflicting one. When you resolve a new term, log it:
`python -m mindhive append decision "Canonical term: <term> = <definition>" --tags orchestrator,context`.

### 3.1 Test With Concrete Scenarios: Edge Cases & Reality Checks

**Principle:** Abstract statements hide edge cases. Concrete scenarios force precision.

**How it works:**

For each core behavior in the design, invent 3–5 concrete scenarios that probe boundaries. Walk through the scenario step by step. Ask: "What actually happens?"

**Example (attendance marking):**

**Scenario 1: Normal Case**
> It's 8:00am on Tuesday. Ms. Wong logs in, opens Period 1 (28 students). She taps "present" for 27, one student arrives late at 8:05am. What happens?
> - Is the student already marked absent? Can she un-mark them?
> - Or does she manually add them?
> - When is attendance "locked"—8:10am? On demand?

**Scenario 2: Edge Case**
> It's 8:00am. Ms. Wong opens the app but the network is down. Can she still mark attendance? If so, does it sync later? What if she marks a student present, then network comes back, then she marks them absent—which one wins?

**Scenario 3: Boundary Test**
> Mr. Lee teaches a co-taught class with Ms. Wong. Both need to mark attendance. If they both try to mark the same student, what happens? Last write wins? Conflict alert? Average?

**Scenario 4: Failure Case**
> The app crashes mid-attendance. Ms. Wong marked 15 of 28 students. She reopens the app. Does she see the 15 already marked? Or start over?

**Scenario 5: Volume Test**
> A school has 2,000 students and wants to mark attendance for all of them in one period. Is the UI design still usable? Or does it become unusable at 100+ students?

**For each scenario:**
1. State the scenario clearly
2. Ask: "What happens here?"
3. Listen for clarity or ambiguity
4. If the answer is vague, dig deeper
5. Lock down the behavior

**Update the spec:** If a scenario reveals an edge case the design doesn't address, add it to the "Error Handling" section or "Out of Scope" section.

### 3.2 Cross-Reference With Code and the Ledger (If They Exist)

**Principle:** Words must match reality. If the code does something different than the design says, surfacing the contradiction forces resolution. The *ledger* is a second source of reality: a prior decision may contradict the current idea just as the code can.

**How it works:**

If the system exists (you're designing a redesign or extension):

1. Read the relevant code
2. Compare what the user described to what the code actually does
3. **Also cross-reference the ledger:** run `recall "<claim>"` and
   `graph related "<entity>"` to catch idea-vs-*decision* contradictions, not just
   idea-vs-code
4. Call out contradictions

**Example (code contradiction):**

User says: "We support partial order cancellation."

You check the code and find: `cancelOrder(id)` with no partial flag. Full cancellation only.

You respond:
> "I found a contradiction. You say partial cancellation is supported, but the code shows `cancelOrder(id)` only cancels entire orders. Which is true?
> - A) Partial is supported and the code is outdated?
> - B) Partial is not supported yet (out of scope for this project)?
> - C) Partial is supported via a different code path?"

**Example (ledger contradiction):**

User says: "Let's store attendance in a new separate service."

`recall "attendance storage"` returns a decision from last month: "Attendance
stays in the monolith DB — a separate service was rejected for latency." You
surface it:
> "The ledger shows a decision from 2026-06-10 that rejected a separate
> attendance service for latency reasons. Has that changed, or should we stay in
> the monolith DB?"

Resolve the contradiction before proceeding. Document the resolution.

### 3.3 Update CONTEXT.md In Real-Time

**Principle:** CONTEXT.md is the living glossary of domain terms. It's never complete; it grows as clarity increases.

**Rules for CONTEXT.md:**

1. **Lazy creation:** Only create the file when the first term is resolved. Don't pre-create it.
2. **Real-time updates:** As you resolve a term in conversation, update the file immediately. Don't batch updates.
3. **Zero implementation details:** No database schemas, field names, code paths, or technical architecture. Pure domain language.
4. **One source of truth:** Every term defined once. All references use the shared definition.
5. **Related terms:** When defining a term, list related terms to avoid confusion.

**Format:**

```markdown
# CONTEXT.md — [Project Name]

**Purpose:** This is the glossary of domain terms. Every term here has one definition, used consistently across the project.

**Last Updated:** 2026-06-30

---

## [Term Name]

**Definition:** [1-2 sentence precise definition]

**Related Terms:** [Other terms that are similar/related/opposite]

**Examples:** [1-2 concrete examples]

**When to Use:** [When is this term appropriate?]

**When NOT to Use:** [What term should you use instead?]

---

## [Another Term]

...
```

**See also:** [CONTEXT-FORMAT.md](references/context-format.md) for comprehensive template and examples.

### 3.4 Define Testing Strategy

**Objective:** Before handing off to implementation, define what tests prove this works.

**Testing Tiers (if system):**

1. **Capability Tests** — Can the agent/system do what we designed?
2. **Parity Tests** — Does agent have access to everything user can do?
3. **Edge Case Tests** — Does system handle the scenarios we discussed?
4. **Integration Tests** — Do full flows work end-to-end?

**Action:** For each tier, write 2-3 test prompts/scenarios.

**Example:**

```markdown
## Testing Strategy — Attendance App

### Capability Tests
- Can user mark attendance via UI?
- Can agent mark attendance (if applicable)?
- Can user export absence reports?

### Parity Tests
- User can mark present/absent/excused → Agent can too
- User can export → Agent can export
- [List all UI actions and verify agent equivalents]

### Edge Case Tests
- Network down during marking
- Simultaneous marking by two teachers
- App crash mid-session

### Integration Tests
- Mark attendance → data syncs → counselor sees it
- Export → file available in specified location
```

**See also:** [agent-native-testing.md](agent-native-testing.md) for complete testing framework.

### 3.5 Write ADRs Sparingly: Three-Part Test

**Principle:** ADR (Architecture Decision Record) documents are expensive to write and maintain. Write one only if all three conditions are met. Otherwise, skip it.

**Three-Part Test:**

1. **Hard to Reverse** — The cost of changing your mind later is meaningful (weeks of rework, cascading changes, user data migration)
2. **Surprising Without Context** — A future reader will ask "Why did they do it this way? That seems weird."
3. **Result of Real Trade-Off** — You considered genuine alternatives and made a deliberate choice

**If any condition is missing, skip the ADR.**

**▸ MindHive weave:** an ADR-worthy decision (hard to reverse, surprising, real
trade-off) is exactly the kind of thing the ledger should also hold. When you
write an ADR, mirror its headline into the ledger:
`python -m mindhive append decision "<ADR headline>" --tags orchestrator,adr` so
the next run's recall surfaces it even before anyone opens the ADR file.

**See also:** [ADR-FORMAT.md](references/adr-format.md) for template and examples.

### 3.6 User Final Approval

Ask the user to sign off on the complete spec:

> "Phase 3 complete. We now have:
> - ✅ Durable Requirements Document (Phase 1)
> - ✅ Architecture Decisions (Phase 2.5)
> - ✅ CONTEXT.md (glossary of all terms)
> - ✅ ADRs (hard reversals documented)
> - ✅ Testing Strategy
>
> Please review everything. Once you approve, we'll hand off to IMPLEMENTATION-READINESS for the final checkpoint before coding begins."

Wait for explicit approval. Document their sign-off.

**▸ Where this leads.** This sign-off is the moment the Anti-Leak HARD-GATE #2
clears — the full design (Phases 1, 2, 2.5, and 3) is now sound. Route from here:
for a **design-and-handoff** engagement, hand off to IMPLEMENTATION-READINESS and
proceed to Phase L. For an **execute** engagement, continue to **Phase S (Skill
Selection)** — only now, with the gate cleared, may skills be selected and
invoked.

---

## <a name="phase-s"></a>Phase S: Skill Selection & Recommendation Matrix

This is the orchestrator's core contribution to the fusion: once the design is
validated, sharpened through Phase 3, and (for systems) the architecture is
locked — so the Anti-Leak HARD-GATE has cleared — choose the **skills** that will
execute the work, deliberately, auditably, and anchored on measured outcomes
rather than a hidden guess.

**Run this phase when** the validated idea implies execution (building code,
running a migration, producing an artifact) and installed skills can do that
work. For pure design-and-handoff engagements (the user only wanted the spec),
skip Phase S and Phase X — the workflow is already complete at the end of
Phase 3; hand off to IMPLEMENTATION-READINESS and proceed to Phase L.

### S.0 Read the live inventory and measured telemetry

Before scoring anything, gather ground truth:

- `python -m mindhive skills list` — the live, registry-truth inventory of
  installed plugins and skills. This is what is *actually* callable right now.
- `python -m mindhive skills status` — the green/red last-call outcome per skill.
  Green = last invocation succeeded; red = last invocation failed.
- `python -m mindhive skills stats` — per-skill completion-rate telemetry from the
  ledger. This is *measured* signal; use it to anchor the success-probability
  column.
- `python -m mindhive skills recommend` — a deterministic most-needed-skills
  ranking derived from measured data, useful as a sanity check on your shortlist.

**Hard sourcing rule.** Candidate skills come from the session's available-skills
list (and `skills list`), **verbatim**. Never present a skill name that is not in
that inventory — no remembered names, no guesses, no "there's probably a skill
for X." The available-skills list is the only authoritative runtime registry.

### S.1 Score 2–4 candidates on seven parameters

Shortlist 2–4 genuinely plausible candidates (do not pad the matrix). For each,
run `python -m mindhive recall "<skill name>"` (add `--semantic` if literal
recall is thin) to surface prior outcomes with that skill, and let the ledger
history inform the scores. Score each on all seven parameters using the anchored
scales in
[references/recommendation-matrix.md](references/recommendation-matrix.md):

| Parameter | Meaning |
|-----------|---------|
| **Suitability** | How well the skill matches *this* task (High / Medium / Low) |
| **Compatibility** | Fit with the stack and the other skills in the chain (High / Medium / Low) |
| **Probability of success** | A % — anchored on `skills stats` when measured, else labeled `(est.)` |
| **Risk level** | Worst-case blast radius (None / Local / Repo-wide / Destructive) |
| **Execution speed** | Fast / Moderate / Slow |
| **Resource cost** | Light / Moderate / Heavy |
| **Final verdict** | Use / Use as follow-up / Hold / Skip, plus one clause of why |

Render the comparison as a compact table (scans fastest):

```markdown
| Skill | Suitability | Compatibility | Success | Risk | Speed | Cost | Verdict |
|-------|-------------|---------------|---------|------|-------|------|---------|
| ce-plan | High | High | 90% (est.) | None | Moderate | Moderate | **Use** — built for structured planning |
| ce-work | High | High | 85% (measured) | Local | Moderate | Heavy | **Use** — executes the plan |
| ce-code-review | High | High | 88% (measured) | None | Fast | Moderate | **Use as follow-up** — verify before ship |
```

### S.2 The learn-from-mistakes gate

**Before recommending or invoking any candidate that shows `fail` (red) in
`skills status`, read its prevention lesson and apply it:**

```bash
python -m mindhive rules show "skill:<name>"
```

The lesson tells you *why* it failed last time and the prevention rule to apply.
Prefer skills showing green; treat red as "fix or avoid, with a reason." This is
the learning loop closing on the selection side — the system already paid for the
failure, so you don't repeat it. If the ledger shows a past failure with a
candidate, reflect it in the score and say so in the verdict clause; do not bury
known history.

### S.3 Present and get approval

Present the matrix, state your recommended selection in one or two sentences, and
get the user's approval (or revision) before executing. If no candidate scores
well, say the idea may need a different route (e.g., `/ce-brainstorm` or
`/ce-plan` first) rather than forcing a weak recommendation.

**Honesty guardrails.** Every percentage without measured backing carries
`(est.)`. Never present an estimate as a measured success rate — `skills stats`
provides real completion rates for skills that have been measured; everything
else is a model estimate and must be labeled as such.

---

## <a name="phase-x"></a>Phase X: Execute & Chain

With the selection approved, execute — then chain the quality passes that turn a
first draft into finished work.

### X.0 Two invocation channels

- **In-agent Skill tool (default on interactive surfaces).** Invoke the selected
  skill directly with the Skill/Task primitive. This is the normal path in Claude
  Code, Cowork, and NewTask when you are driving interactively.
- **The MindHive invoke channel (`skills invoke`).** When execution should be
  driven by MindHive itself — inside `/circle`, a headless run, or when the
  dashboard shows live status — call
  `python -m mindhive skills invoke <name> [--plugin <p>]`. It runs the skill via
  a headless `claude` and records the outcome. Exit code is `0` on success and
  `1` on failure, so you can gate the next step on it.

Use the in-agent channel by default; use the invoke channel when you need
recorded, exit-code-gated, headless execution.

### X.1 Record the outcome (feed the learning loop)

Every execution writes an outcome so the next run is smarter:

```bash
# success:
python -m mindhive skills record <name> --ok --note "<what it produced>"
# failure:
python -m mindhive skills record <name> --fail --note "<how it failed>"
```

A `--fail` writes a `tool_failed` event with a `skill:<name>` signature, which
flows automatically into the incidents → reflect → Improvements pipeline. So a
skill that fails or returns poorly **teaches the system a prevention rule**, and
its dashboard indicator turns red until it succeeds again. (When you use
`skills invoke`, the outcome is recorded for you; use `skills record` for
in-agent Skill-tool executions you want reflected in telemetry.)

### X.2 Chain the follow-up quality passes

When execution finishes, **do not stop** — proactively recommend the follow-up
chain that guarantees quality. Resolve every skill name against the
available-skills list; give a one-line **rationale** for each (why it refines the
architecture or de-risks the deployment). Recommend; let the user choose which to
chain.

| Work just done | Recommended follow-up (resolve against available-skills list) | Rationale |
|----------------|---------------------------------------------------------------|-----------|
| New/changed code | a code-review skill (e.g. `compound-engineering:ce-code-review`), then `compound-engineering:ce-simplify-code` | Catch correctness bugs, then delete needless complexity |
| Auth / payments / input handling / external APIs | a security skill (e.g. `security-scanning:security-sast`, `compound-engineering:ce-security-reviewer`) | High blast-radius surfaces earn an explicit security pass |
| Bug-shaped or flaky behavior | `compound-engineering:ce-debug`, `superpowers:systematic-debugging` | Root-cause it instead of patching symptoms |
| UI / frontend | `compound-engineering:ce-test-browser`, `ui-design:accessibility-audit` | Prove it works in a real browser and is accessible |
| About to ship | `compound-engineering:ce-commit-push-pr` | Land it cleanly with a proper PR |

### X.3 Green/red and self-healing

Prefer skills showing green in `skills status`. Before invoking a red one, apply
its prevention rule (`rules show "skill:<name>"`). Every failed invoke is a
`tool_failed` event feeding the incidents → reflect → Improvements pipeline — the
system self-heals by learning a rule from each failure. Treat a red indicator as
"fix or avoid, with a reason," never as "run it again and hope."

---

## <a name="phase-l"></a>Phase L: Learning Loop & Log-Back

After the workflow concludes — whether it ended at an approved spec (design-only)
or ran through execution (Phase X) — evaluate the run and write durable lessons
back so the next `/mindhive` is smarter. This is the movement that makes the
system compound.

### L.0 What to log (only when something memorable happened)

- **Mistake or dead-end hit** →
  `python -m mindhive append caveat "<one line>" --tags orchestrator`
- **Inefficiency / better path discovered** →
  `python -m mindhive append experiment_result "<one line>" --tags orchestrator`
- **New stated preference** →
  `python -m mindhive append decision "<one line>" --tags orchestrator,preference`

Only append when something **memorable** actually happened. A smooth,
unremarkable run writes nothing — do not create noise events. This mirrors
ULT-IDEA's YAGNI: don't manufacture ledger entries for a future that never comes.

### L.1 The failure → rule → improvement loop

The learning loop is largely automatic. Every `tool_failed` event (from a
`skills record --fail`, a `skills invoke` failure, or a `hive flag`) carries a
signature that flows into the incidents pipeline:

```
tool_failed(signature) ──► incidents accumulate ──► rules reflect
       └─► a prevention rule is written ──► rules inject surfaces it next run
```

You can drive or inspect this loop from the CLI:

- `python -m mindhive flag "<what was misjudged>" --category <cat>` — self-flag a
  judgment error; it activates at the first occurrence.
- `python -m mindhive rules summaries` — read the structured lesson records (the
  same shape the dashboard's Improvements view shows).
- `python -m mindhive rules show "<signature>"` — read one incident's full lesson.
- `python -m mindhive rules inject` — the active prevention-rules block (also part
  of the preflight).
- `python -m mindhive rules promote "<signature>"` — promote a proven rule into
  canonical guidance.

**This is how the fused skill "learns which tools work, effective syntax,
successful commands, and the most effective way":** successful executions raise a
skill's measured completion rate (`skills stats`); failures write prevention
rules that the next run's preflight injects. The system's judgment improves
without a code release — Principle 5 (Improvement Over Time) applied to the
orchestrator itself.

### L.2 Refresh the briefing

End by refreshing the auto-loaded briefing so the next session inherits
everything this run learned:

```bash
python -m mindhive sync
```

If the user's Opus budget is nearly spent, also run
`python -m mindhive export --recent 20` for a portable handoff (see
[Context Handoff at ~50%](#handoff)).

---

## <a name="communication-rules"></a>Communication Rules

**Strict rules for clarity and productivity:**

### One Question Per Turn
**Never stack multiple questions.** Ask one, wait for answer, then ask the next.

Why: Stacking causes diluted answers, cognitive overload, and shallow responses.

### Avoid Yes/No Questions
**Prefer open-ended questions.** Closed questions get shallow answers.

❌ "Does this problem exist?"
✅ "Tell me about the problem you're trying to solve."

### Listen for Hesitation
**When the user pauses or gives a vague answer, probe deeper.** Don't move forward.

❌ User: "Everyone would use this."
You: "Great! Moving on..."

✅ User: "Everyone would use this."
You: "Can you tell me about one specific person who would use it?"

### Prefer Multiple-Choice
**When possible, offer 2-3 options.** Reduces cognitive load.

✅ "Is this lightweight (~1-2 hours), standard (~4-6 hours), or deep (2+ days)?"

### Make Decisions Visible
**When you make assumptions or decisions on behalf of the design, state them explicitly.**

✅ "I'm assuming this is about the morning attendance only, not tracking throughout the day. Is that right?"

### No Implementation Talk in Phases 1-2
**Focus on WHAT, never HOW.**

❌ "Should we use a SQL database or NoSQL?"
✅ "What data needs to persist between sessions?"

### Lead With the Ledger's Answer
**When the recall preflight already answered a question, don't ask it — state
what you found and confirm.** This is the MindHive addition to the communication
rules: every question you ask should be one the ledger and codebase genuinely
could not answer.

✅ "The ledger shows you settled on single-school scope. I'll assume that unless
you say otherwise."

### Ask One Question at a Time via the Blocking Tool
Use the platform's blocking-question tool so the user answers before you proceed:
`AskUserQuestion` in Claude Code (load its schema via `ToolSearch` with
`select:AskUserQuestion` first if needed). On surfaces without a blocking-question
tool, fall back to a single numbered-options question in chat and wait for the
reply. See [Compatibility & Portability](#compat).

---

## <a name="anti-leak"></a>Anti-Leak Protection: The HARD-GATE — HARD-GATE #2

**You cannot invoke any implementation skill, write code, or suggest technical architecture until:**

1. The idea has been tested against the five gaps ✅
2. A design has been presented and explicitly approved ✅
3. A durable requirements document has been written and approved ✅
4. (If applicable) Architecture review is complete (Phase 2) ✅
5. (If applicable) Architecture decisions are locked in CONTEXT.md (Phase 2.5) ✅
6. (If applicable) Fuzzy language is eliminated and testing strategy defined (Phase 3) ✅

**This applies to EVERY project, regardless of perceived simplicity.**

Only after all gates have been passed should the conversation move to
implementation-focused tools, deployment, or coding. This is exactly why the flow
places **Phase 3 (Interrogate & Sharpen) before Phase S and Phase X**: Phase 3
closes gate item 6, so the HARD-GATE has already cleared by the time skills are
selected (Phase S) and invoked (Phase X). No skill is invoked until the design is
sound — the ordering enforces it rather than relying on a caveat.

**Two gates, one discipline.** HARD-GATE #1 (recall before you ask) governs the
*front* of the workflow; HARD-GATE #2 (no implementation before approval) governs
the *back*. Together they bracket the rigor: start from what's known, don't build
until it's sound.

---

## <a name="yagni"></a>Ruthless YAGNI: You Aren't Gonna Need It

**Every project is susceptible to speculative complexity.** Your job: ruthlessly cut.

### The YAGNI Questions

For every proposed feature or requirement:

1. **Is this solving today's problem, or a hypothetical future problem?**
   - Today? Include it.
   - Hypothetical? Cut it. Add it later if demand emerges.

2. **How will we know this is needed? What signal would trigger it?**
   - Clear signal? Consider including.
   - No clear signal? Cut it.

3. **What's the simplest version that proves this works?**
   - Build the simplest version. Cut the rest.

### Example

User says: "We need export in PDF, CSV, Excel, and JSON formats."

YAGNI response:
> "Do we actually need all four? What format do 90% of users need?
>
> If the answer is CSV, build CSV first. Add others only if we hear demand.
>
> How about this: build the simplest export (CSV) in MVP. Add more formats if usage data shows demand."

### The Scope Boundaries List

In Phase 1.5, explicitly call out what's NOT being built:

> **Out of Scope (v1.0, may revisit later):**
> - Email notifications (see slack-based alerts instead)
> - PDF export (CSV is sufficient)
> - Historical attendance trends (future use case)
> - Multi-school setup (single school MVP)

This prevents scope creep and sets expectations.

**▸ MindHive weave — YAGNI for memory too.** Apply the same ruthlessness to
ledger writes (Phase L): only append when something memorable happened, and only
recommend skills that measured telemetry or clear suitability justifies. A matrix
padded with skills you'd never pick, or a ledger full of noise events, is the
same speculative complexity YAGNI forbids — just in the memory layer.

---

## <a name="completion"></a>Completion Criteria

**A project is complete when:**

- [ ] **Idea is validated** — Tested against five gaps, evidence is strong
- [ ] **Design is durable** — Requirements document is clear, unambiguous, focused
- [ ] **User has approved** — Explicit sign-off on requirements
- [ ] **Architecture is locked** (if system) — Decisions documented in ARCHITECTURE.md
- [ ] **Language is sharp** — CONTEXT.md has all ambiguous terms defined
- [ ] **Decisions are recorded** — ADRs written for hard-to-reverse choices
- [ ] **Testing is planned** — Capability, parity, edge case, integration tests defined
- [ ] **Zero scope creep** — Out of scope list is explicit and approved
- [ ] **Ready for implementation** — Implementation team can read spec once and execute
- [ ] **(If executed) Skills selected and run** — Recommendation Matrix approved, work executed, quality chain offered
- [ ] **Lessons logged** — Memorable mistakes/inefficiencies/preferences appended; `sync` run so the next session inherits them

**Terminal State:** User confirms spec (and, if executed, the work) is ready. All
deliverables handed off. Next phase is IMPLEMENTATION-READINESS (see separate
document) for design-only engagements, or the offered follow-up chain for
executed ones.

---

## <a name="edge-cases"></a>Troubleshooting & Edge Cases

### "The User Keeps Changing Their Mind"

**This is normal.** Requirements clarify through dialogue. However:

1. **Do the changes represent gap-filling or scope creep?**
   - Gap-filling: incorporate them
   - Scope creep: add to "future considerations," not v1.0

2. **Has the core problem shifted?** If yes, restart Phase 1 with the new understanding.

3. **Set expectations:** "Every change we make means we finish later or cut something else. What should we sacrifice?"

### "The Stakeholders Disagree on the Problem"

**Stop. Don't design yet.**

Multiple stakeholders with conflicting views means you haven't validated the problem. Restart Phase 1 with a clearer scope:
- Which one person is the primary user?
- What's THEIR problem, not the stakeholders' theories?

### "The Idea Is Too Vague"

Example: "Help teachers with feedback."

**This is Phase 1 work.** Ask discovery questions:
- "What specific challenge are teachers facing with feedback right now?"
- "Show me what a frustrated teacher does today."
- "How much time does this waste?"

Keep asking until the problem is concrete.

### "I Can Already See the Implementation"

Resist the temptation to skip design. Yes, you can see the implementation. But:
1. The implementation may not match the user's needs
2. Skipping rigor causes rework down the line
3. Better to discover mismatches in design than in code

Push through phases. Trust the process.

### "This Project Doesn't Need Architecture Decisions"

Every system needs Phase 2.5. Even "simple" apps have:
- Data storage strategy
- Tool design choices
- Approval gates
- Context injection approach

Lightweight projects can do Phase 2.5 briefly (1 hour). But do it.

### "MindHive Isn't Installed on This Surface"

Run the degradation contract: announce "MindHive memory unavailable — running
ULT-IDEA in stateless mode" once, then proceed with full ULT-IDEA rigor. Explore
the codebase directly in place of `recall`/`graph related`. Skip Phases S/X/L's
MindHive-specific commands, but still offer the follow-up quality chain (X.2)
using the in-agent Skill tool. The idea still gets validated; only the memory
grounding is absent.

### "A Recommended Skill Shows Red (fail) in `skills status`"

Do not invoke it blindly. Run `rules show "skill:<name>"`, read the prevention
lesson, and either apply the fix or pick a green alternative — stating why in the
matrix verdict. A red skill is "fix or avoid, with a reason," never "run it again
and hope."

### "The Ledger Contradicts the User"

Surface it, don't override it silently. Cite the decision (with its date) and ask
which is current — the recorded decision or the new direction. Then log the
resolution so the contradiction doesn't recur.

### "Context Is Getting Long Mid-Workflow"

Offer a handoff at the next phase boundary rather than pushing on into a degraded
window — see [Context Handoff at ~50%](#handoff).

---

## <a name="handoff"></a>Context Handoff at ~50%

Long runs degrade or hit an abrupt compaction mid-task. To keep quality high,
monitor context consumption and offer a clean handoff at roughly the halfway
mark.

### When to check

Check at **every phase boundary** (preflight→1, 1→2, 2→2.5, 2.5→3, 3→S, S→X,
X→L), never mid-question. Phase boundaries are cheap, natural pause points;
interrupting an interview question to talk about context would be worse than the
problem.

### Heuristic signals (there is no exact token meter in-session)

Treat the estimate as crossing ~50% when several of these are true:

- Many tool calls have accumulated this session (dozens of reads/edits/greps).
- Several large files or reference docs have been read in full.
- Multiple pipeline phases are complete and the transcript is long.
- The harness has emitted a compaction or "context is getting long" notice — this
  is the **hard backstop**; treat it as ≥50% regardless of the proxies.

### The alert (ask, don't force)

When the estimate crosses the threshold, halt and say, in substance:

> Heads up — we're about halfway through the context window. To keep quality high
> I can **hand off** now: I'll export a portable summary, log the open threads to
> MindHive, and give you a one-line command to resume this exact work in a fresh
> session. Want me to hand off, or push on here?

Wait for the user's choice. If they decline, resume where it left off and
re-check at the next phase boundary.

### Approved-handoff sequence

On approval, run in order:

1. `python -m mindhive export --recent 20` — writes `handoff.md`, the durable
   portable summary of recent state.
2. `python -m mindhive append caveat "Handoff mid-/mindhive: <what's done, what's next>" --tags orchestrator,handoff`
3. `python -m mindhive sync` — refresh the briefing for the next session.
4. Print resume instructions:
   > Handed off. In a fresh session:
   > 1. `python -m mindhive state` (auto-loads; confirms context restored)
   > 2. open `handoff.md` for the full snapshot
   > 3. re-run `/mindhive <the remaining scope>` to continue where we stopped.

Do not begin new pipeline work after a handoff — end this session cleanly so the
next one starts fresh. Full protocol:
[references/handoff-protocol.md](references/handoff-protocol.md).

---

## <a name="compat"></a>Compatibility & Portability — Claude Code, Cowork, NewTask

This skill is written to run on **all parts of Claude**. The ULT-IDEA rigor is
pure prose and runs anywhere; the MindHive machinery layers on where the CLI is
present and degrades cleanly where it is not. Full matrix:
[references/compatibility.md](references/compatibility.md).

### The two portability seams

1. **The MindHive CLI.** Present when `python -m mindhive`, `hive`, or the venv
   path resolves. Absent in sandboxes without the local install. **Contract:** if
   absent, announce stateless mode once and run full ULT-IDEA rigor; never
   hard-fail. Every `python -m mindhive …` block in this document is best-effort.
2. **The blocking-question tool.** Used to ask one question at a time and wait.

| Surface | Blocking-question tool | Skill invocation | MindHive CLI | Visual companion |
|---------|------------------------|------------------|--------------|------------------|
| **Claude Code** | `AskUserQuestion` (load schema via `ToolSearch select:AskUserQuestion`) | `Skill` tool + `skills invoke` | Usually present (local) | Local browser URL |
| **Claude Cowork** | Platform blocking prompt; else numbered options in chat | Platform skill primitive; in-agent only if no CLI | Often absent → stateless mode | Fall back to inline diagrams / text |
| **Claude NewTask** | Platform blocking prompt; else numbered options in chat | Platform skill primitive; in-agent only if no CLI | Often absent → stateless mode | Fall back to inline diagrams / text |

### Portability rules

- **Never hard-depend on a specific tool name.** Where `AskUserQuestion` is
  unavailable, ask a single numbered-options question in chat and wait for the
  reply. Where the `Skill` tool is unavailable, describe the follow-up chain and
  let the user trigger it.
- **Keep frontmatter minimal.** Only `name` + `description` — the lowest common
  denominator every surface parses. Platform-variant mechanics live in this
  section and inline, never in frontmatter.
- **Degrade, don't disable.** The order of graceful degradation is: full fusion
  (CLI + blocking tool + Skill tool) → ULT-IDEA + in-agent skill chaining (no
  CLI) → pure ULT-IDEA rigor (no CLI, no skill primitive). Each rung still
  delivers a validated idea.
- **Announce the mode once, at the top of the run,** so the user knows whether
  memory grounding is active. Don't repeat the announcement every phase.

---

## <a name="integration-map"></a>Integration Map: Which Supporting Document for What?

| Need | See |
|------|-----|
| Tool design discipline | [mcp-tool-design.md](mcp-tool-design.md) |
| Action parity audit | [action-parity-discipline.md](action-parity-discipline.md) |
| Architecture patterns | [architecture-patterns.md](architecture-patterns.md) |
| File organization | [files-universal-interface.md](files-universal-interface.md) |
| Shared workspace | [shared-workspace-architecture.md](shared-workspace-architecture.md) |
| Context injection | [dynamic-context-injection.md](dynamic-context-injection.md) |
| System prompts | [system-prompt-design.md](system-prompt-design.md) |
| From primitives to domain tools | [from-primitives-to-domain-tools.md](from-primitives-to-domain-tools.md) |
| Mobile (iOS) | [mobile-patterns.md](mobile-patterns.md) |
| Agent execution | [agent-execution-patterns.md](agent-execution-patterns.md) |
| Testing strategy | [agent-native-testing.md](agent-native-testing.md) |
| Product implications | [product-implications.md](product-implications.md) |
| CONTEXT.md template | [references/context-format.md](references/context-format.md) |
| ADR template | [references/adr-format.md](references/adr-format.md) |
| Implementation handoff | [IMPLEMENTATION-READINESS.md](IMPLEMENTATION-READINESS.md) |
| **Recommendation Matrix scales** | [references/recommendation-matrix.md](references/recommendation-matrix.md) |
| **Full MindHive command catalog** | [references/mindhive-command-catalog.md](references/mindhive-command-catalog.md) |
| **This skill's capability catalog** | [references/skill-catalog.md](references/skill-catalog.md) |
| **Cross-surface compatibility** | [references/compatibility.md](references/compatibility.md) |
| **Context handoff protocol** | [references/handoff-protocol.md](references/handoff-protocol.md) |

---

## Notes

- This skill orchestrates skills already installed on this machine; it does not
  install or author new skills.
- It never disturbs the V17 live-trading setup or the `~/.mindhive/LIVE_LOCK`
  guard — if a chained skill would run the loop, that skill's own guards apply.
- Percentages in the Recommendation Matrix are honest model estimates labeled
  `(est.)` unless anchored on measured `skills stats`. Building a richer per-skill
  success-rate backend beyond `skills stats` is deferred follow-up work.
- ULT-IDEA content is preserved in full from the source skill; the MindHive weave
  adds memory grounding, skill selection, execution chaining, and a learning loop
  without removing any of ULT-IDEA's rigor.

---

**Version:** MindHive Orchestrator v1.0 (ULT-IDEA v2.0 fused with the MindHive memory spine)

**Last Updated:** 2026-07-09
