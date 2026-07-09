---
name: mindhive-orchestrator
description: >-
  Turn an idea into rigorously validated, executed, and learned-from work. Use
  when the user types /mindhive [idea], asks to "orchestrate this idea", "turn
  this idea into a spec/design", "ult-idea this", "pick the right skill for
  this", "plan and build X the MindHive way", or wants an idea taken from
  validation → design → skill selection → execution → learning. Runs the full
  ULT-IDEA design canvas on top of MindHive's memory: recalls from the ledger
  (or reads the code) before it asks, scores candidate skills against the live
  plugin/skill registry, executes through the invoke channel with green/red
  self-healing, and logs lessons back. Works in Claude Code, Cowork, and NewTask.
---

# MindHive Orchestrator — ULT-IDEA on a memory + learning + execution substrate

Typing **`/mindhive [idea]`** runs the full **ULT-IDEA** design discipline — idea
validation, agent-native architecture review, and ruthless sharpening into an
implementation-ready spec — **fused with the MindHive system** so every step is
memory-informed, every skill choice is data-grounded, every execution is
green/red self-healing, and every lesson is written back for next time.

> **The fusion in one line:** ULT-IDEA is the **canvas** (how you think an idea
> into a sharp, validated spec); MindHive is the **substrate underneath it** — the
> ledger you recall from before you ask, the plugin/skill database you pick tools
> from, the learning loop that remembers which tools and syntax actually work, and
> the dashboard that shows it all live. Neither is diminished: ULT-IDEA keeps 100%
> of its rigor and gains a memory; MindHive gains a world-class front end.

This skill does **not** re-implement planning, coding, or review — it runs the
ULT-IDEA canvas to produce a validated spec, then **selects and chains** the skills
already installed on this machine to execute it, seeded by MindHive memory at every
step, and learning from every run.

## What MindHive contributes to the canvas

| ULT-IDEA needs to… | MindHive provides | Commands |
|--------------------|-------------------|----------|
| Not re-ask what's already decided | The **ledger**: state, recall, knowledge graph | `state`, `recall`, `graph related` |
| Pick the right execution skill | The **plugin/skill database** (breadth-scored, free-first) + live registry | `plugins recommend`, `skills list` |
| Know which tools/syntax actually work | The **learning loop**: green/red status + prevention rules distilled from past failures | `skills status`, `rules summaries`, `rules show` |
| Execute and gate on real success | The **invoke channel** (headless, recorded, self-healing) | `skills invoke` |
| Show the human what's happening | The **local dashboard** (Skills & plugins + Memory→Improvements) | `dashboard` |
| Carry lessons forward | **Append + sync** durable learnings | `append`, `sync` |

## Runs everywhere (Claude Code · Cowork · NewTask)

The pipeline is identical on every surface; only *how you ask a blocking question*
and *how you run a MindHive command* flex. Detect what's available, use it, degrade
gracefully — never abort because one mechanism is missing. **Load
`references/cross-platform.md`** for the detection order and the no-shell fallback.
The essentials:

- **Ask a question:** `AskUserQuestion` in Claude Code (load via `ToolSearch`
  `select:AskUserQuestion` if needed); the surface's native blocking UI in Cowork;
  and in a headless NewTask, lean on recall + code reading and record assumptions
  with `append caveat` instead of blocking.
- **Run a command:** the `Bash`/`PowerShell` tool. Detect the interpreter once (see
  the syntax block below) and reuse it. If **no shell** exists, honor the prime
  directive by reading `MEMORY.md`/`STATE.md`/`CONTEXT.md` and the project code
  directly — reading the code is always available.

## MindHive command syntax you will use

The canonical syntax throughout this skill is **`python -m mindhive <cmd>`**. But
before you run *any* MindHive command, **Step 0: resolve the working interpreter
once** — this is what makes the integration seamless instead of silently failing.

**Step 0 — probe, then reuse.** Run the first form that returns *real* output (not the
Windows Store shim message), and use that exact prefix for every command afterward:

1. `python -m mindhive state` — the canonical form. Use it if it prints `STATE.md`.
2. `C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive state` — the project
   venv. **On this machine this is the known-good form** (verified): the bare `python`
   here is the Microsoft Store *app-execution alias*, which prints "Python was not
   found" **and exits 0 without running** — so treat that message as failure and fall
   through to the venv. `hive` is not on PATH here.
3. `hive state` — the console-script shortcut, on machines where it's installed.

Wherever this skill writes `python -m mindhive …`, substitute the prefix Step 0
resolved (on this machine: `C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …`).
If none of the three works (no shell at all), drop to the no-shell fallback in
`references/cross-platform.md` — read `STATE.md`/`CONTEXT.md`/`MEMORY.md` and the code
directly. Never let the shim message stall the run.

The **three preflight commands** you run before the interview (Requirement 1 — recall
before you ask), using the Step 0 prefix:

1. `python -m mindhive state` — current project status, active work, existing constraints.
2. `python -m mindhive recall "<key term from the idea>"` — previous decisions, warnings, failures on this topic. Add `--semantic` for meaning-based matching when literal recall is limited.
3. `python -m mindhive graph related "<main entity>"` — decisions and related files, when the idea touches an existing subsystem.

The **full function catalog** — every command grouped by memory / plugin-DB /
learning-loop / dashboard, each with *when, for what, and why* — lives in
**`references/mindhive-functions.md`**. Load it whenever you need a capability beyond
the preflight (e.g. checking a skill's green/red history before invoking it).

## When to use this skill — conditions, tasks, why

So Claude knows exactly when this fused skill fires and what it's for:

| Condition / trigger | Task it's for | Why this skill (not something narrower) |
|---------------------|---------------|-----------------------------------------|
| User types `/mindhive [idea]` or "orchestrate this idea" | Any idea that must become executed, quality-checked work | You get validation + memory + skill selection + execution + learning in one pipeline |
| "Turn this into a spec", "ult-idea this", "design X the MindHive way" | Idea → implementation-ready spec with the rigor gates | The ULT-IDEA canvas runs in full, now recall-informed |
| "Which skill should I use for this?" | Choosing among installed skills/plugins | The Recommendation Matrix scores candidates against the live registry + ledger history |
| A vague or unproven idea | Pressure-testing before investing | The five-gap product test rejects or sharpens weak ideas early |
| Building an agent-facing system/tool | Ensuring agent-native architecture | Phase 2 + 2.5 enforce parity, granularity, composability, and lock decisions |
| Work in an area that's bitten you before | Avoiding known failures | The learning loop surfaces prevention rules before you repeat the mistake |

**Do NOT** use this for a pure one-line mechanical edit with no design surface, or to
install/author new skills (this orchestrates skills already installed). For those,
act directly or use `writing-skills`.

## Prime directive: recall (or read the code) before you ask — Requirement 1

**Before any interview question, you are required to check the MindHive ledger *or*
the project code.** Run the recall preflight and let its results pre-answer or skip
questions:

1. `python -m mindhive state` — current project state, active work, standing constraints.
2. `python -m mindhive recall "<key terms from the idea>"` — prior decisions, caveats, failures on this topic. Add `--semantic` for meaning-based matches when literal recall is thin.
3. `python -m mindhive graph related "<main entity>"` — connected decisions and files, when the idea touches an existing subsystem.

**Crucial rule:** if a question can be answered by exploring the codebase or the
ledger, explore instead of asking. Only genuinely user-owned decisions — scope,
priorities, taste, product intent — reach the user. When the CLI can't run (see
cross-platform), read `STATE.md`/`CONTEXT.md`/`MEMORY.md` and the source directly:
reading the code is a first-class way to honor this directive.

> **⬡ MindHive hook —** every "See also" in the canvas below points to an ULT-IDEA
> companion doc *and*, where relevant, a MindHive command. The canvas text is
> preserved in full; the `⬡` callouts are the *additions* that wire each phase into
> the ledger, the plugin database, and the learning loop.

---
---

# ▓▓▓ THE MAIN CANVAS: ULT-IDEA ▓▓▓

*Everything from here to the "After the canvas" section is the complete ULT-IDEA
skill, preserved in full. The `⬡ MindHive hook` callouts are additive — they detail
how each step draws on the MindHive system so the command executes clearly and
efficiently. Nothing in ULT-IDEA is removed; only augmented.*

## ULT-IDEA: Enhanced Complete Idea → Design → Implementation-Ready Spec Workflow

**Transform rough ideas into rigorously validated designs with built-in guardrails against implementation leaks, vague language, unexamined assumptions, and architectural misalignment. Includes agent-native architecture discipline and testing strategy.**

---

## Table of Contents

1. [Overview & North Star](#overview)
2. [Anti-Pattern Doctrine](#anti-pattern)
3. [High-Level Process Flow](#process-flow)
4. [Phase 1: Explore (Validate Idea)](#phase-1)
5. [Phase 2: Agent-Native Architecture Review (If System)](#phase-2)
6. [Phase 2.5: Architecture Decision Checkpoint (New)](#phase-2-5)
7. [Phase 3: Interrogate & Sharpen](#phase-3)
8. [Communication Rules](#communication-rules)
9. [Anti-Leak Protection](#anti-leak)
10. [Ruthless YAGNI](#yagni)
11. [Completion Criteria](#completion)
12. [Troubleshooting & Edge Cases](#edge-cases)

---

## <a name="overview"></a>Overview & North Star

**ULT-IDEA** is a skill that embodies rigorous thinking at every stage of idea development. It exists to eliminate waste caused by:
- **Unvalidated assumptions** — "We think there's a problem, but is there real proof?"
- **Fuzzy language** — "Account" means three different things; which one is correct?
- **Premature implementation** — Starting to code before the spec is clear
- **Unexamined complexity** — Adding features "just in case" for a future that never arrives
- **Misaligned architecture** — Building systems that require code changes to add new features
- **Architectural mismatch** — Tool design that locks in behavior instead of enabling it

**Three core movements drive the skill:**

### Movement 1: Validate the Idea Itself (Phase 1)
**Question:** Does this problem exist? Is the user real? Can you build the simplest version?

Before proposing any design, rigorously test the idea against five critical gaps:
- **Evidence Gap** — Is there concrete proof this problem exists? (Time wasted? Money spent? Workflow blocked?)
- **Specificity Gap** — Who is the specific user? Can you name them? How does their life improve?
- **Counterfactual Gap** — What do they do *right now* without this solution? What workaround exists?
- **Attachment Gap** — Are you married to one solution form? What's the minimal version that proves success?
- **Durability Gap** — (For large products) Will this survive trend changes in 12–24 months?

If gaps exist, fill them. If evidence is weak, reject the idea or demand proof first.

**See also:** [product-implications.md](./product-implications.md) for progressive disclosure, latent demand discovery, and approval frameworks that emerge from validated ideas.

> **⬡ MindHive hook — validate with memory, not just intuition.** Before you decide
> the Evidence Gap is unfilled, run `recall "<problem>"` and `recall "<problem>" --semantic`:
> the ledger may already hold the evidence (a prior `experiment_result`, a user
> decision, a caveat about a failed attempt). Prior art in the ledger *is* evidence.
> If the idea touches an existing subsystem, `graph related "<entity>"` shows what's
> already been decided and which files are in the blast radius — that often answers
> the Counterfactual Gap ("what do they do today") directly from the code.

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

**See also:**
- [mcp-tool-design.md](./mcp-tool-design.md) for tool primitives vs workflows, CRUD completeness, dynamic capability discovery
- [action-parity-discipline.md](./action-parity-discipline.md) for maintaining agent-user parity
- [architecture-patterns.md](./architecture-patterns.md) for event-driven, unified orchestrator, approval gates, model tier selection
- [files-universal-interface.md](./files-universal-interface.md) for file organization and shared workspace
- [shared-workspace-architecture.md](./shared-workspace-architecture.md) for agent-user collaboration patterns

> **⬡ MindHive hook — MindHive is itself an agent-native system; mirror its patterns.**
> The five principles here are the exact principles MindHive was built on (primitive
> CLI commands, agent-user parity via `skills invoke`, composability). When reviewing
> architecture, `recall "architecture decision"` and `graph related "<subsystem>"`
> surface prior agent-native rulings already made in this project so your review is
> consistent with them rather than re-deciding.

### Movement 3: Eliminate Fuzzy Language & Plan Implementation (Phase 3)
**Question:** Does every term mean exactly one thing? Are we ready to implement?

Interrogate the design by:
- **Sharpening terminology** — "Account" becomes "Customer Login" vs. "Billing Entity" (precise definition)
- **Testing edge cases** — Invent concrete scenarios that force precision about boundaries
- **Cross-referencing reality** — Does the code actually support what you claim? If not, fix it before designing new code
- **Recording decisions** — CONTEXT.md (glossary) and ADRs (hard reversals) capture what was decided and why
- **Planning testing** — Define what tests prove this works (capability tests, parity tests, integration tests)

**See also:**
- [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md) and [ADR-FORMAT.md](./ADR-FORMAT.md) for templates
- [agent-native-testing.md](./agent-native-testing.md) for testing strategy and capability verification
- [IMPLEMENTATION-READINESS.md](./IMPLEMENTATION-READINESS.md) for the handoff from design to implementation

> **⬡ MindHive hook — "cross-referencing reality" IS a MindHive query.** `recall` over
> the ledger and `graph related` over the code graph are the mechanized form of
> Phase 3's cross-reference-with-code step. Use them to catch the contradiction
> before you design new code (see Phase 3.2).

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
START: User presents an idea
  ↓
0. MINDHIVE PREFLIGHT (Requirement 1 — always, before any question)
   ├─ python -m mindhive state
   ├─ python -m mindhive recall "<key terms>"  (+ --semantic if thin)
   └─ python -m mindhive graph related "<main entity>"  (if touches a subsystem)
  ↓
1. DIAGNOSE PROJECT TYPE
   └─ Ask: Lightweight / Standard / Deep scope?
   └─ If multiple independent subsystems → FORCE DECOMPOSITION
  ↓
2. PHASE 1: EXPLORE (Always)
   ├─ Understand the idea (purpose, constraints, success criteria)
   ├─ Test against 5 gaps (Evidence, Specificity, Counterfactual, Attachment, Durability)
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
   ├─ Propose fixes
   └─ Update design if scope changes
  ↓
5. PHASE 2.5: ARCHITECTURE DECISION CHECKPOINT (New - if system)
   ├─ Workspace architecture (shared vs separate)
   ├─ Tool design strategy (primitives vs domain tools)
   ├─ Approval gates (what needs user sign-off)
   ├─ Implementation pattern (event-driven, orchestrator, etc.)
   ├─ Storage & file organization
   ├─ Context injection strategy
   ├─ Model tier selection (if agent-powered)
   ├─ Mobile considerations (if iOS/Android)
   └─ LOCK ARCHITECTURE DECISIONS
  ↓
6. PHASE 3: INTERROGATE & SHARPEN (Always)
   ├─ Eliminate fuzzy language → canonical terms
   ├─ Test with 3-5 concrete scenarios + edge cases
   ├─ Cross-reference with code (if exists) — via recall / graph related
   ├─ Update CONTEXT.md (inline, real-time)
   ├─ Write ADRs (only if 3-part test passes)
   ├─ Define testing strategy (what proves this works)
   └─ USER FINAL APPROVAL of complete spec
  ↓
7. (MINDHIVE) RECOMMENDATION MATRIX → EXECUTE → CHAIN → LEARN
   ├─ Score candidate skills (grounded in `skills list` + ledger history)
   ├─ Execute via Skill tool or `skills invoke` (green/red, self-healing)
   ├─ Chain follow-up quality passes
   └─ Append durable learnings + `sync`
  ↓
END: Spec + CONTEXT.md + ADRs + Testing Strategy ready for IMPLEMENTATION-READINESS,
     executed through the chosen skills, with lessons written back to the ledger
```

---

## <a name="phase-1"></a>Phase 1: EXPLORE (Mandatory for All Projects)

Phase 1 answers the question: **"Is this idea valid, and what is the simplest design that solves it?"**

> **⬡ MindHive hook — run the preflight before 1.0.** Do the three preflight commands
> now (`state`, `recall`, `graph related`). Carry their findings into every step
> below: when a diagnostic or gap question is already answered by the ledger or the
> code, *state what you found and skip the question* ("The ledger shows you settled on
> X here, so I'll assume that unless you say otherwise").

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

**1.1.2 Constraints**
- What are the boundaries? What's off-limits?
- Budget, timeline, audience, technical limits?
- Regulatory or organizational constraints?
- Existing systems or workflows that must be respected?

**Example questions:**
- "Are there any hard constraints—budget, timeline, platform, people—we need to work within?"
- "What can't we change, even if we wanted to?"
- "Who else needs to approve this, and what are their constraints?"

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

> **⬡ MindHive hook — constraints often already live in `state`.** Standing
> constraints (the V17 `LIVE_LOCK`, platform/OS realities, prior scope rulings) are
> in `STATE.md` and the ledger. Pull the "Constraints" dimension from there first and
> only ask the user for constraints memory doesn't already hold.

### 1.2 Test Against the Five Gaps: Product Pressure Test

**Objective:** Rigorously validate the idea before investing in design.

Once you believe you understand the idea, interrogate it systematically. For each gap, ask a clarifying question and listen for weakness.

#### Gap 1: Evidence Gap
**Question:** Is there real, concrete proof the problem exists?

Not "we think there's a problem." Real evidence: time wasted, money spent, workflow blocked, people abandoning a tool.

**How to test:**
- "What's the most concrete thing someone's already done about this—paid for a workaround, built a DIY solution, spent extra time manually?"
- "Can you show me the bottleneck? What does a frustrated teacher do right now?"
- "How much time does this actually waste per week/month?"

**Red flag:** Vague answers like "people struggle with this" or "I feel like this would be useful." Demand proof.

#### Gap 2: Specificity Gap
**Question:** Who is the specific user? Can you name them? How does their life change?

Not "teachers" or "students." Specific: "Mrs. Rodriguez, a 9th-grade math teacher with 30 students, teaches three sections." Specific: "She spends 10 minutes per class taking attendance manually."

**How to test:**
- "Describe one specific person who would use this. What's their role? Where do they work? What's their day like?"
- "How many people fit this description? 1? 10? 1000?"
- "If we built this, how would their 9am Tuesday look different?"

**Red flag:** Abstract personas or "anyone who needs to" answers. Push for concreteness.

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

**See also:** [product-implications.md](./product-implications.md) for how validated ideas enable progressive disclosure, latent demand discovery, and approval workflows.

> **⬡ MindHive hook — the ledger is the fastest way to fill the gaps.** For each gap,
> a targeted `recall` frequently returns the answer without asking: Evidence →
> `recall "<problem> evidence"`; Counterfactual → read the current code path with the
> file tools or `graph related "<entity>"`; Durability → `recall "<domain> risk"` for
> prior caveats about fragility. Ask the user only for the genuinely user-owned
> residue (taste, priorities, product intent) after memory and code are exhausted.

### 1.3 Offer Visual Companion (If Applicable)

**Trigger:** If upcoming design questions will involve visual content (mockups, layouts, diagrams, spatial structure, flow charts).

**Offer this as its own message** (do not combine with other content):

> "Some of what we're designing might be easier to explain if I can show it to you in a web browser. I can create mockups, diagrams, layout comparisons, and other visuals as we go. This feature is still new and can be token-intensive. Want to try it? (Requires opening a local URL.)"

Wait for explicit response. **Do not proceed with other content until they respond.**

**Decision rule per question:** Even after they accept the companion, decide FOR EACH QUESTION whether to use the browser or text.

- **Use the browser** for: mockups, wireframes, layout comparisons, architecture diagrams, side-by-side visual designs
- **Use the terminal** for: requirements questions, conceptual choices, trade-off lists, multiple-choice options, scope decisions

**Test:** Would they understand this better by seeing it than reading it?

> **⬡ MindHive hook — MindHive already has a local visual surface.** The MindHive
> **dashboard** (`python -m mindhive dashboard`, localhost, fixed port 54461) is a
> ready-made local URL for showing the knowledge graph (`graph export --open`), the
> Skills & plugins inventory, and the Improvements view. When the visual you want to
> show is *about the project's memory or skill choices*, prefer the dashboard over
> building a throwaway mockup.

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

> **⬡ MindHive hook — let past outcomes weight the recommendation.** Before you name
> the recommended approach, `recall "<approach keyword>"` for prior successes or
> dead-ends with each shape. If the ledger shows a previous approach failed here, say
> so and let it lower that approach's standing — memory-weighted recommendations beat
> intuition-only ones.

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

> **⬡ MindHive hook — record the validated idea as a decision.** Once the user
> approves the requirements doc, that is a memorable event. Append it:
> `python -m mindhive append decision "Validated idea: <one line> — scope <L/S/D>, chosen approach <X>" --tags orchestrator,ult-idea`.
> This makes the next `/mindhive` recall-aware of what was already validated here.
> (Only append on approval — not for every discarded draft.)

---

## <a name="phase-2"></a>Phase 2: Agent-Native Architecture Review (Conditional)

**Apply this phase if and only if:**
- The project involves building a system (software app, tool, platform, multi-agent workflow), AND
- The scope is Standard or Deep, AND
- The user approved the Phase 1 design

**Skip this phase if:** The project is non-technical (curriculum design, sermon series, organizational process, etc.).

**Objective:** Ensure the system design follows five agent-native principles, enabling long-term flexibility and feature growth without code rewrites.

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

**See also:** [action-parity-discipline.md](./action-parity-discipline.md) for maintaining parity over time, and the Parity Audit workflow.

> **⬡ MindHive hook — MindHive is the reference implementation of parity.** Every
> dashboard action has a CLI/agent equivalent (the **▶ call this skill** button ==
> `skills invoke`; the Reflect button == `rules reflect`; scan == `plugins scan`).
> Use that as the bar: if your design can't express a user action as a command, it
> has a parity gap.

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

**See also:** [mcp-tool-design.md](./mcp-tool-design.md) for tool naming, input design, and CRUD completeness. [from-primitives-to-domain-tools.md](./from-primitives-to-domain-tools.md) for when to add domain tools deliberately.

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

**See also:** [product-implications.md](./product-implications.md) for latent demand discovery and how agent usage reveals what users actually need.

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

**See also:** [system-prompt-design.md](./system-prompt-design.md) and [dynamic-context-injection.md](./dynamic-context-injection.md) for how to structure prompts and context.

> **⬡ MindHive hook — "improvement over time" is literally the MindHive learning loop.**
> Principle 5 is what `failures capture → rules reflect → rules inject` implements: the
> system gets better by accumulating lessons (prevention rules), not by shipping code.
> When you design for improvement-over-time, design a `context.md`/rules store that an
> agent can read and append — exactly like MindHive's incident/rules store.

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

## <a name="phase-2-5"></a>Phase 2.5: Architecture Decision Checkpoint (New)

**Apply this phase if and only if:**
- The project is a system (software, multi-agent workflow), AND
- You've completed Phase 2 (or Lightweight/Standard skipped Phase 2 but are proceeding with Phase 2.5), AND
- The user has approved Phase 1 design and Phase 2 findings

**Objective:** Make explicit, documented decisions about implementation architecture BEFORE diving into design details or testing. This checkpoint ensures the implementation team has a clear blueprint.

**Deliverable:** An "ARCHITECTURE.md" document that locks these decisions. Do not proceed to Phase 3 until decisions are explicit.

> **⬡ MindHive hook — recall prior architecture rulings before deciding.** Run
> `recall "architecture decision"` and `recall "workspace"` / `recall "approval gate"`
> before this checkpoint. This project's ledger already carries agent-native
> decisions (e.g. the shared-workspace ruling); reuse them so ARCHITECTURE.md is
> consistent with what's been decided, and only re-open a decision deliberately.

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

**See also:** [files-universal-interface.md](./files-universal-interface.md) and [shared-workspace-architecture.md](./shared-workspace-architecture.md)

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

**See also:** [from-primitives-to-domain-tools.md](./from-primitives-to-domain-tools.md) and [mcp-tool-design.md](./mcp-tool-design.md)

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

**See also:** [architecture-patterns.md](./architecture-patterns.md) for approval gate implementation and [product-implications.md](./product-implications.md) for approval and agency patterns.

> **⬡ MindHive hook — MindHive already encodes real approval gates; match them.**
> Destructive store ops (`gc --prune`) and autonomous `loop` runs are refused while
> the V17 `~/.mindhive/LIVE_LOCK` is set. That is a "High stakes / Hard reversibility
> → explicit approval (or hard refusal)" gate in production. Model your gates on it,
> and never design a flow that defeats that guard.

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

**See also:** [architecture-patterns.md](./architecture-patterns.md) for complete pattern catalog.

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

**See also:** [files-universal-interface.md](./files-universal-interface.md) for patterns and the context.md pattern.

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

**See also:** [dynamic-context-injection.md](./dynamic-context-injection.md) and [files-universal-interface.md](./files-universal-interface.md) for context.md pattern.

> **⬡ MindHive hook — MindHive's injection strategy is the hybrid, working.** Static
> guidance lives in `SOUL.md`/the skill; dynamic learned state lives in the ledger and
> is injected via `sync` → native `MEMORY.md` (auto-loaded) and `rules inject`. If you
> pick Option C, you can literally reuse this machinery instead of inventing one.

### 2.5.6 Model Tier Selection

**Decision:** What Claude model does each agent type use?

| Agent Type | Tier | Reasoning |
|------------|------|-----------|
| Chat | Balanced (Sonnet) | Fast, good reasoning |
| Research | Balanced (Sonnet) | Tool loops, synthesis |
| Quick lookup | Fast (Haiku) | Simple operations, low cost |
| Complex analysis | Powerful (Opus) | Multi-source synthesis |

**Action:** Map each agent type to a model tier.

**See also:** [architecture-patterns.md](./architecture-patterns.md) for model tier selection.

### 2.5.7 Mobile Considerations (If Applicable)

**Decision:** If iOS/Android: What's the strategy?

**Key Decisions:**
- Storage: iCloud/Cloud Firestore or local?
- Background execution: How to checkpoint/resume?
- Permissions: What does the agent need access to?
- Cost awareness: Token budgets per session?

**Action:** Document mobile-specific architecture.

**See also:** [mobile-patterns.md](./mobile-patterns.md) for iOS-specific patterns (iCloud sync, background execution, permissions, cost awareness).

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

> **⬡ MindHive hook — persist the hard calls.** After approval, append the
> irreversible choices as decisions so they survive into the next session:
> `python -m mindhive append decision "ARCH: <choice> because <reason>" --tags orchestrator,architecture`.
> A Phase 3.5 ADR still gets written for the truly hard-to-reverse ones — the ledger
> entry is the searchable index; the ADR is the full record.

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

> **⬡ MindHive hook — the ledger is a pre-seeded glossary.** `recall "<term>"` before
> you propose a definition: the project may already have locked that term. Reusing the
> established definition keeps CONTEXT.md consistent with the code and prevents the
> same word meaning two things across two features.

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

> **⬡ MindHive hook — mine past failures for the edge cases you'd miss.**
> `rules summaries` and `failures list` show what has actually broken in this project.
> A recurring `tool_failed` signature is a ready-made "Failure Case" scenario — pull
> real incidents in as scenarios instead of inventing only hypothetical ones.

### 3.2 Cross-Reference With Code (If It Exists)

**Principle:** Words must match reality. If the code does something different than the design says, surfacing the contradiction forces resolution.

**How it works:**

If the system exists (you're designing a redesign or extension):

1. Read the relevant code
2. Compare what the user described to what the code actually does
3. Call out contradictions

**Example:**

User says: "We support partial order cancellation."

You check the code and find: `cancelOrder(id)` with no partial flag. Full cancellation only.

You respond:
> "I found a contradiction. You say partial cancellation is supported, but the code shows `cancelOrder(id)` only cancels entire orders. Which is true?
> - A) Partial is supported and the code is outdated?
> - B) Partial is not supported yet (out of scope for this project)?
> - C) Partial is supported via a different code path?"

Resolve the contradiction before proceeding. Document the resolution.

> **⬡ MindHive hook — `graph related` accelerates the cross-reference.** Before reading
> files blind, `graph related "<entity>"` returns the decisions and files connected to
> the thing you're changing — the fastest path to the relevant code and the reasons
> behind it. Pair it with `recall "<feature>"` to catch a design claim the ledger
> already contradicts (e.g. "we decided *against* partial cancellation in <date>").

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

**See also:** [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md) for comprehensive template and examples.

> **⬡ MindHive hook — this project already has a `CONTEXT.md`.** MindHive keeps a
> repo-root `CONTEXT.md` as its domain glossary. When you sharpen a term, update *that*
> file (or the project's designated one) rather than starting a parallel glossary, and
> optionally mirror the definition into the ledger with `append decision "TERM: …"` so
> `recall` finds it later.

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

**See also:** [agent-native-testing.md](./agent-native-testing.md) for complete testing framework.

> **⬡ MindHive hook — MindHive ships its own verification you can chain.** `selftest`
> runs the suite; `skills status` tells you which execution skills are green before you
> rely on them; a thermo-nuclear `review queue` can be queued for touched files. Fold
> these into the testing strategy so "what proves this works" includes the project's
> existing gates, not just new tests.

### 3.5 Write ADRs Sparingly: Three-Part Test

**Principle:** ADR (Architecture Decision Record) documents are expensive to write and maintain. Write one only if all three conditions are met. Otherwise, skip it.

**Three-Part Test:**

1. **Hard to Reverse** — The cost of changing your mind later is meaningful (weeks of rework, cascading changes, user data migration)
2. **Surprising Without Context** — A future reader will ask "Why did they do it this way? That seems weird."
3. **Result of Real Trade-Off** — You considered genuine alternatives and made a deliberate choice

**If any condition is missing, skip the ADR.**

**See also:** [ADR-FORMAT.md](./ADR-FORMAT.md) for template and examples.

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

> **⬡ MindHive hook — approval is the gate into the execution pipeline.** This sign-off
> is exactly the Anti-Leak HARD-GATE clearing. On approval, the run moves out of the
> ULT-IDEA canvas and into **"After the canvas: execute through MindHive"** below — the
> Recommendation Matrix, invoke-channel execution, and learning write-back.

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

> **⬡ MindHive hook — ask through the platform's blocking-question tool.** In Claude
> Code use `AskUserQuestion` (one question, lead with your recommended answer). The
> "one question per turn" and "make decisions visible" rules pair with the prime
> directive: state what the ledger/code already told you *before* the question, so the
> user only adjudicates the genuinely-open part. See `references/cross-platform.md` for
> the per-surface asking mechanism.

---

## <a name="anti-leak"></a>Anti-Leak Protection: The HARD-GATE

**You cannot invoke any implementation skill, write code, or suggest technical architecture until:**

1. The idea has been tested against the five gaps ✅
2. A design has been presented and explicitly approved ✅
3. A durable requirements document has been written and approved ✅
4. (If applicable) Architecture review is complete (Phase 2) ✅
5. (If applicable) Architecture decisions are locked in CONTEXT.md (Phase 2.5) ✅
6. (If applicable) Fuzzy language is eliminated and testing strategy defined (Phase 3) ✅

**This applies to EVERY project, regardless of perceived simplicity.**

Only after all gates have been passed should the conversation move to implementation-focused tools, deployment, or coding.

> **⬡ MindHive hook — the HARD-GATE governs the invoke channel too.** Do not run
> `skills invoke`, a code-writing skill, or `loop`/`/circle` until these six gates
> clear. Recall, `state`, `graph related`, `plugins recommend`, and `skills list` are
> *reads* — safe and encouraged before the gate. Execution (`skills invoke`,
> code-writing skills) is *forbidden* until the gate clears. This keeps MindHive's
> power on the right side of the leak protection.

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

> **⬡ MindHive hook — YAGNI governs skill selection, not just features.** In the
> Recommendation Matrix, apply the same discipline: shortlist 2–4 genuinely plausible
> skills, not every skill that *could* touch the task. A padded chain is speculative
> complexity in execution form. `plugins recommend` breadth-scores toward the smallest
> set that covers the work.

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

**Terminal State:** User confirms spec is ready. All deliverables handed off. Next phase is IMPLEMENTATION-READINESS (see separate document).

> **⬡ MindHive hook — in the fused pipeline, "complete" extends one step further.** The
> ULT-IDEA terminal state (approved spec) is the *handoff into execution*, not the end
> of the `/mindhive` run. The run is complete when the spec is executed through the
> chosen skills **and** the learnings are written back (`append` + `sync`). See "After
> the canvas" below.

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

> **⬡ MindHive hook — one more edge case: the CLI won't run.** If `python -m mindhive`
> only prints the Windows Store shim message, switch to the venv interpreter
> (`.venv\Scripts\python.exe -m mindhive`). If no shell exists at all, honor the prime
> directive by reading `STATE.md`/`CONTEXT.md`/`MEMORY.md` and the code directly, note
> the fallback once, and continue. Never let a missing CLI become an excuse to ask the
> user everything. See `references/cross-platform.md`.

---

## Integration Map: Which Supporting Document for What?

| Need | See |
|------|-----|
| Tool design discipline | [mcp-tool-design.md](./mcp-tool-design.md) |
| Action parity audit | [action-parity-discipline.md](./action-parity-discipline.md) |
| Architecture patterns | [architecture-patterns.md](./architecture-patterns.md) |
| File organization | [files-universal-interface.md](./files-universal-interface.md) |
| Shared workspace | [shared-workspace-architecture.md](./shared-workspace-architecture.md) |
| Context injection | [dynamic-context-injection.md](./dynamic-context-injection.md) |
| System prompts | [system-prompt-design.md](./system-prompt-design.md) |
| From primitives to domain tools | [from-primitives-to-domain-tools.md](./from-primitives-to-domain-tools.md) |
| Mobile (iOS) | [mobile-patterns.md](./mobile-patterns.md) |
| Agent execution | [agent-execution-patterns.md](./agent-execution-patterns.md) |
| Testing strategy | [agent-native-testing.md](./agent-native-testing.md) |
| Product implications | [product-implications.md](./product-implications.md) |
| CONTEXT.md template | [CONTEXT-FORMAT.md](./CONTEXT-FORMAT.md) |
| ADR template | [ADR-FORMAT.md](./ADR-FORMAT.md) |
| Implementation handoff | [IMPLEMENTATION-READINESS.md](./IMPLEMENTATION-READINESS.md) |

> **⬡ MindHive hook — the MindHive-side references.** Alongside the ULT-IDEA companions
> above, this fused skill adds: **`references/mindhive-functions.md`** (the full
> command catalog — when/for-what/why), **`references/recommendation-matrix.md`** (the
> scored skill-selection method), **`references/handoff-protocol.md`** (context handoff
> at ~50%), and **`references/cross-platform.md`** (Claude Code / Cowork / NewTask
> compatibility). The ULT-IDEA companion `.md` files (mcp-tool-design, etc.) ship with
> the ULT-IDEA knowledge base; load them on demand for the deep-dive they name.

---

**Version:** ULT-IDEA v2.0 (Enhanced with Architecture, Testing, and Implementation Readiness) — fused into MindHive Orchestrator

**Last Updated:** 2026-06-30 (ULT-IDEA core) · fused 2026-07-09

---
---

# ▓▓▓ AFTER THE CANVAS: EXECUTE THROUGH MINDHIVE ▓▓▓

*The ULT-IDEA canvas ends at an approved, implementation-ready spec (its terminal
state). In the fused `/mindhive` pipeline that approval is the gate into execution.
What follows is the MindHive orchestration layer — it turns the validated spec into
finished, quality-checked work and writes the lessons back. It never re-opens the
canvas gates; it runs only after they clear.*

## Step A — Recommendation Matrix (the plugin/skill database picks the skills)

Once the spec is approved, propose the most appropriate skills to execute it.
**Load `references/recommendation-matrix.md`** and produce a matrix per candidate
skill covering: suitability, compatibility, probability of success, risk level,
execution speed, resource cost, and a final verdict.

- Resolve candidate skill names **verbatim from `python -m mindhive skills list`**
  (the live registry) or the session's available-skills list — never recommend a
  skill name that is not present there.
- Ground breadth/suitability in `python -m mindhive plugins recommend` (the
  breadth-scored, free-first catalog).
- For each candidate, run `python -m mindhive recall "<skill name>"` and check
  `python -m mindhive skills status` to surface prior outcomes and green/red history —
  and let that inform the scores. **If a candidate shows red, read its lesson with
  `rules show "skill:<name>"` and reflect it in the verdict.**
- Label every probability/percentage as a **model estimate `(est.)`** — the only real
  per-skill telemetry is the binary green/red last-call status.
- Present the matrix, state your recommended selection, and get the user's approval
  (or revision) before executing.

## Step B — Execute, then chain

Execute through the selected skill(s) by invoking them with the Skill tool (or, when
the run should be MindHive-driven, `skills invoke` — see Step C). When execution
finishes, **do not stop** — proactively recommend the follow-up chain that
guarantees quality:

| Work just done | Recommended follow-up skills (resolve against `skills list`) |
|----------------|--------------------------------------------------------------|
| New/changed code | a code-review skill (e.g. `compound-engineering:ce-code-review`), `compound-engineering:ce-simplify-code` |
| Auth / payments / input handling / external APIs | a security skill (e.g. `security-scanning:security-sast`, `compound-engineering:ce-security-reviewer`) |
| Bug-shaped or flaky behavior | `compound-engineering:ce-debug`, `superpowers:systematic-debugging` |
| UI / frontend | `compound-engineering:ce-test-browser`, `ui-design:accessibility-audit` |
| About to ship | `compound-engineering:ce-commit-push-pr` |

Give a one-line **rationale** for each follow-up (why it refines the architecture or
de-risks the deployment). Recommend; let the user choose which to chain.

## Step C — Invoke through MindHive (green/red + self-healing)

Any installed skill is callable **through MindHive**, not just a hand-picked few. When
execution should be driven by MindHive itself (e.g. inside `/circle` or a headless run
where the dashboard shows live status), use the invoke channel instead of only the
in-agent Skill tool:

- **List what's callable:** `python -m mindhive skills list` — the live, registry-truth
  inventory (updates the instant a plugin is installed/removed).
- **Call any skill:** `python -m mindhive skills invoke <name> [--plugin <p>]` — runs it
  via a headless `claude` and records the outcome. Exit code is `0` on success, `1` on
  failure, so you can gate the next step on it.
- **Check health first:** `python -m mindhive skills status` shows the last outcome per
  skill (`ok`/`fail`). **Before invoking a skill that shows `fail`, read its Improvements
  lesson** (`python -m mindhive rules show "skill:<name>"`) and apply the prevention rule
  — this is the learning loop closing.

Every failed invoke is written as a `tool_failed` event with a `skill:<name>`
signature, which flows automatically into the incidents → reflect → Improvements
pipeline. So a skill that fails or returns poorly **teaches the system a prevention
rule**, and its dashboard indicator turns red until it succeeds again. Prefer skills
showing green; treat red as "fix or avoid, with a reason." Point the user to the
**dashboard** (`python -m mindhive dashboard`, Skills & plugins view) to watch this live.

## Step D — Log the learnings back into MindHive

After the workflow concludes, evaluate the run and write durable lessons to the ledger
so the next `/mindhive` is smarter:

- Mistake or dead-end hit → `python -m mindhive append caveat "<one line>" --tags orchestrator`
- Inefficiency / better path discovered → `python -m mindhive append experiment_result "<one line>" --tags orchestrator`
- New stated preference → `python -m mindhive append decision "<one line>" --tags orchestrator,preference`

Only append when something **memorable** actually happened. A smooth, unremarkable run
writes nothing — do not create noise events. End by running `python -m mindhive sync`
so next session's briefing reflects the new lessons.

## Context management: monitor and hand off at ~50%

At **every phase boundary** (including the canvas phase boundaries), estimate context
consumption heuristically (tool-call volume, files read, phases completed, any harness
compaction notices). When the estimate reaches roughly **50%** of the window, **halt,
alert the user, and request permission to hand off** — do not silently push on. **Load
`references/handoff-protocol.md`** for the alert wording and the approved-handoff
sequence (`export` → append open threads → print resume instructions). The existing
PreCompact hook remains the hard backstop; this heuristic is a courtesy checkpoint.

## Notes & guardrails

- This skill runs the ULT-IDEA canvas to produce a validated spec, then **orchestrates
  skills already installed on this machine** to execute it. It does not install or
  author new skills.
- It never disturbs the V17 live-trading setup or the `~/.mindhive/LIVE_LOCK` guard — if
  a chained skill would run the loop, that skill's own guards apply, and you never pass
  `--force` to defeat the guard on the user's behalf.
- Percentages in the matrix are honest model estimates, not measured rates. The only
  real per-skill signal is the binary green/red `skills status`.
- **ULT-IDEA is preserved in full** in the canvas above; the MindHive integration is
  additive (`⬡` hooks + this execution layer). Nothing from ULT-IDEA was removed.
