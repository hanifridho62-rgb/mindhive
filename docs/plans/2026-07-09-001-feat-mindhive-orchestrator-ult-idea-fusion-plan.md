---
title: "feat: Fuse ult-idea rigor into the mindhive-orchestrator skill (≥12,000-word SKILL.md)"
type: feat
date: 2026-07-09
status: ready
depth: deep
origin: (none — solo invocation via LFG pipeline)
target_repo: mindhive (branch: claude/mindhive-orchestrator-skill-272720)
---

# feat: Fuse ult-idea rigor into the mindhive-orchestrator skill

## Summary

Rewrite `skills/mindhive-orchestrator/SKILL.md` so it becomes a single fused skill: the **full ult-idea idea→design→implementation-ready workflow** (preserved in its entirety, never shrunk) is the **main canvas**, and the **MindHive orchestrator's memory/learning/skill-selection machinery** is woven into every phase so that command execution is grounded in the project ledger, past decisions, learned prevention rules, and measured skill telemetry — not in blind questions to the user. The output skill is still named `mindhive-orchestrator` (triggered by `/mindhive [idea]` or direct invocation). The fused `SKILL.md` must be **≥12,000 words** (ult-idea alone is 7,930; the integration and expanded execution context supply the rest), and must remain usable and compatible across **Claude Code, Claude Cowork ("Coworks"), and Claude NewTask**, degrading gracefully to pure ult-idea rigor on any surface where the MindHive CLI is absent.

---

## Problem Frame

Two skills exist independently:

1. **`ult-idea`** (Anthropic hosted skill, `anthropic-skills:ult-idea`, 7,930 words) — a rigorous, phase-gated workflow that transforms a rough idea into a validated, unambiguous, implementation-ready spec. It is strong on *thinking discipline* (five-gap product pressure test, agent-native architecture review, architecture-decision checkpoint, fuzzy-language elimination, testing strategy, YAGNI, anti-leak gating) but has **no memory** — every run starts cold, re-asks questions the project has already answered, and learns nothing from prior mistakes.

2. **`mindhive-orchestrator`** (this repo, `skills/mindhive-orchestrator/SKILL.md`, 1,202 words) — a four-phase pipeline (recall-before-ask interview → scored Recommendation Matrix → execute+chain → log learnings) that is *memory-native*: it reads the MindHive ledger before asking, scores candidate skills, chains quality passes, drives skills through an invoke channel with green/red status, and writes lessons back. But it is thin on *idea-validation rigor* — it interviews and selects, but does not pressure-test the idea, sharpen language, or gate against implementation leaks the way ult-idea does.

The user wants these fused so that ult-idea's rigor executes **inside** a MindHive-aware context: before the workflow asks the user anything, it consults the ledger (`state`, `recall`, `graph related`), the learned prevention rules, and the measured skill telemetry; it catalogs when/why each capability fires; and it learns from every run. The fusion must **add to** ult-idea, never subtract, and must be portable across all Claude surfaces.

### What triggers this skill

- User types `/mindhive [idea]` or `/mindhive-orchestrator [idea]`.
- User asks to "orchestrate this idea", "pressure-test and build X the MindHive way", "take this from idea to implementation-ready", "pick the right skill for this idea".
- Direct Skill-tool invocation of `mindhive-orchestrator`.

---

## Goals & Non-Goals

**Goals**

- Preserve **100%** of ult-idea's content (every phase, gap, principle, communication rule, YAGNI passage, completion checklist, edge case, integration map). Word count of ult-idea-derived material must not drop below its current 7,930.
- Weave MindHive's memory, learning-loop, and skill-selection machinery into the ult-idea canvas at the natural seams (before interviewing, during architecture decisions, at skill selection, during execution, after completion).
- Incorporate the exact MindHive function-call syntax the user specified (`python -m mindhive state`, `recall "<term>"` [`--semantic`], `graph related "<entity>"`) plus the broader command surface that supports execution effectiveness/efficiency.
- Catalog the fused skill's own capabilities (under what conditions, for what tasks, why) so Claude can reason about when to fire it and which sub-capability applies.
- Reach **≥12,000 words in `SKILL.md`** (reference files are additional, not counted toward that floor).
- Guarantee cross-surface compatibility (Claude Code / Cowork / NewTask) with explicit graceful degradation when the MindHive CLI is unavailable.

**Non-Goals**

- Do **not** re-implement planning/coding/review skills — the orchestrator selects and chains existing installed skills.
- Do **not** build a per-skill success-rate telemetry backend beyond what MindHive already exposes (`skills stats`, `skills status`) — honesty guardrails stay ("est." labels where there is no measured data).
- Do **not** touch the V17 live-trading setup or the `~/.mindhive/LIVE_LOCK` guard.
- Do **not** alter MindHive's Python source (`src/mindhive/**`) — this is a skill-authoring change only. (Command syntax is *documented*, not modified.)

---

## Key Technical Decisions

### KTD1 — ult-idea is the structural spine; MindHive is woven in as an execution substrate, not bolted on as a preamble

The fused `SKILL.md` follows ult-idea's section order and headings verbatim (Overview & North Star → Anti-Pattern Doctrine → Process Flow → Phase 1 → Phase 2 → Phase 2.5 → Phase 3 → Communication Rules → Anti-Leak → YAGNI → Completion → Edge Cases → Integration Map). MindHive integration appears **inside** those sections as clearly marked sub-blocks (e.g., "▸ MindHive preflight", "▸ Recall-before-ask", "▸ Learn-back") plus **new** top-and-tail sections (Prime Directive, Function-Call Syntax catalog, Skill-Selection / Recommendation Matrix phase, Execute-and-Chain phase, Learning-Loop phase, Compatibility). This keeps ult-idea's rigor legible while making memory-grounding the default operating mode. *Rationale:* the user explicitly said ult-idea is "the main canvas" and MindHive should "detail ult-idea in the context and situation that exists in executing commands." A weave, not a wrapper, is the only structure that satisfies "don't decrease ult-idea, only increase."

### KTD2 — Prime Directive "recall before you ask" is elevated to a HARD-GATE that mirrors ult-idea's anti-leak HARD-GATE

Just as ult-idea forbids implementation until the five gaps pass, the fused skill forbids asking the user any question that the ledger or codebase can answer. The preflight (`state` → `recall` → `graph related`) runs before Phase 1's first question, and each of the five gaps and each Phase-1.1 dimension gets a "check the ledger first" instruction. *Rationale:* this is integration requirement #1 ("before you ask the user you are required to check the ledger from the MindHive database or the code in the project"). Making it a gate — not a suggestion — is what gives the fusion its teeth.

### KTD3 — MindHive invocation is documented with a three-tier portable form and a graceful-degradation contract

Every MindHive command is shown in a portable shape: preferred `python -m mindhive …`, the `hive …` alias where the console-script is installed, and the explicit venv fallback `C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive …`. A **degradation contract** states: if none of these resolve (Cowork/NewTask sandboxes without the local install), the skill announces "MindHive memory unavailable — running ult-idea in stateless mode" and proceeds with full ult-idea rigor, substituting in-conversation reasoning for ledger recall. *Rationale:* integration requirement #4 (usable across all Claude surfaces). A skill that hard-fails without the CLI would not be "compatible with all parts of Claude."

### KTD4 — The Recommendation Matrix and skill catalog are grounded in *measured* MindHive telemetry, with honesty guardrails preserved

Skill selection reads `skills list` (live inventory), `skills status` (green/red last outcome), and `skills stats` (completion-rate telemetry), and cross-checks candidates against `recall "<skill name>"` for prior outcomes. Percentages without a telemetry backing stay labeled `(est.)`. Before invoking a skill showing `fail`, the skill reads its prevention lesson (`rules show "skill:<name>"`) and applies it. *Rationale:* this is the "learn from mistakes / which tools work / effective syntax / successful commands" requirement, realized through the machinery MindHive already ships, without fabricating measured rates.

### KTD5 — Frontmatter stays minimal and portable; platform-specific tool names are handled in-body, not in frontmatter

Frontmatter carries only `name` + `description` (the lowest common denominator that every Claude surface parses). Platform-variant mechanics (blocking-question tool: `AskUserQuestion` in Claude Code vs. numbered-options fallback elsewhere; the Skill/Task invocation primitive; the `webbrowser`-backed visual companion) are described in a Compatibility section and inline where used. *Rationale:* frontmatter schemas differ subtly across surfaces; keeping it minimal maximizes portability (integration requirement #4).

### KTD6 — Word-count floor is met by preservation + expansion, verified mechanically

The 12,000-word floor is reached by (a) preserving ult-idea's 7,930 words, (b) folding in the orchestrator's 1,202 words, and (c) adding ~3,000–4,000 words of woven MindHive execution context, a function-call catalog, a skill-selection phase, a learning-loop phase, and a compatibility section. A `wc -w` check on the final `SKILL.md` is the verification gate. *Rationale:* the floor is a hard, checkable requirement; expansion must be substantive (real execution guidance), not padding.

---

## High-Level Technical Design

### Document architecture (fused SKILL.md)

```
┌─ FRONTMATTER (name: mindhive-orchestrator; portable description) ─────────────┐
│                                                                               │
├─ [NEW] Prime Directive: Recall Before You Ask  ◄── HARD-GATE #1               │
├─ [NEW] MindHive Function-Call Syntax (state / recall / graph + full surface)  │
├─ [NEW] What this skill is / when it fires / capability catalog                │
│                                                                               │
├─ Overview & North Star            ── ult-idea preserved + 4th movement (memory)│
├─ Anti-Pattern Doctrine            ── ult-idea preserved                        │
├─ High-Level Process Flow          ── ult-idea flow + MindHive preflight lanes  │
│                                                                               │
├─ Phase 1: EXPLORE                 ── ult-idea preserved  ▸ recall preflight    │
│     5 gaps, approaches, req doc         woven into each gap & dimension        │
├─ Phase 2: Agent-Native Review     ── ult-idea preserved  ▸ plugin-catalog note │
├─ Phase 2.5: Architecture Checkpt  ── ult-idea preserved  ▸ ledger-decision log │
│                                                                               │
├─ [NEW] Phase S: Skill Selection & Recommendation Matrix  ◄── orchestrator core │
│     live inventory · status/stats · scored matrix · skill catalog             │
├─ [NEW] Phase X: Execute & Chain (invoke channel, green/red, self-heal)         │
│                                                                               │
├─ Phase 3: INTERROGATE & SHARPEN   ── ult-idea preserved  ▸ ledger cross-ref    │
│     ubiquitous language, scenarios, CONTEXT.md, testing, ADRs                  │
│                                                                               │
├─ [NEW] Phase L: Learning Loop & Log-Back (failures→rules→improvements)         │
├─ Communication Rules              ── ult-idea preserved                        │
├─ Anti-Leak HARD-GATE              ── ult-idea preserved  ◄── HARD-GATE #2       │
├─ Ruthless YAGNI                   ── ult-idea preserved                        │
├─ Completion Criteria              ── ult-idea preserved + memory criteria      │
├─ Troubleshooting & Edge Cases     ── ult-idea preserved + MindHive edge cases  │
├─ [NEW] Context Handoff at ~50%    ── from orchestrator handoff-protocol        │
├─ [NEW] Compatibility & Portability (Claude Code / Cowork / NewTask + degrade)  │
└─ Integration Map                  ── ult-idea preserved + MindHive references  │
```

### Preflight sequence (the recall-before-ask gate)

```
/mindhive [idea]
   │
   ▼
Resolve MindHive invocation ──► none available? ──► announce stateless mode ──┐
   │ available                                                                │
   ▼                                                                          │
python -m mindhive state            (project status, active work, constraints)│
python -m mindhive recall "<terms>" (prior decisions, caveats, failures)      │
   └─ thin? add --semantic                                                    │
python -m mindhive graph related "<entity>" (connected decisions + files)     │
python -m mindhive rules inject     (active prevention rules for this project) │
   │                                                                          │
   ▼                                                                          ▼
Pre-answer / skip Phase-1 questions the ledger already settled ──► Phase 1 EXPLORE
(only scope / taste / product-intent decisions reach the user)
```

### Learning-loop closure

```
Execute skill ──► outcome ──► ok?  ──yes──► (smooth run writes nothing)
                                └──no──► hive skills record <name> --fail --note "…"
                                          └─► tool_failed(signature=skill:<name>)
                                              └─► incidents → reflect → Improvements
                                                  └─► next run: rules show / rules inject
                                                      applies the prevention rule
```

---

## Output Structure

The change is confined to the skill directory plus two manifest files:

```
skills/mindhive-orchestrator/
├── SKILL.md                         # REWRITTEN — fused canvas, ≥12,000 words
└── references/
    ├── recommendation-matrix.md     # PRESERVED (already exists) — light edits
    ├── handoff-protocol.md          # PRESERVED (already exists) — light edits
    ├── mindhive-command-catalog.md  # NEW — full CLI surface + when/why each fires
    ├── skill-catalog.md             # NEW — capability catalog (conditions/tasks/why)
    ├── compatibility.md             # NEW — Claude Code / Cowork / NewTask + degradation
    ├── context-format.md            # NEW — ult-idea CONTEXT.md glossary template (folded in)
    └── adr-format.md                # NEW — ult-idea ADR template (folded in)
.claude-plugin/plugin.json           # description touch-up (orchestrator now idea→ship)
.claude-plugin/marketplace.json      # matching description touch-up
```

The per-unit `**Files:**` lists remain authoritative for what each unit creates or modifies.

---

## Implementation Units

### U1. Fused SKILL.md skeleton — frontmatter, Prime Directive, Function-Call Syntax, capability catalog

**Goal:** Establish the fused document's opening spine: portable frontmatter, the "recall before you ask" Prime Directive (HARD-GATE #1), the MindHive Function-Call Syntax section (the three commands the user named plus the invocation-resolution + degradation contract), and a "what this skill is / when it fires / capability catalog" block.

**Requirements:** Integration #1 (check ledger before asking), Integration #2 (function-call syntax), the "catalog your new skills — under what conditions, for what tasks, why" requirement, KTD1, KTD2, KTD3, KTD5.

**Dependencies:** none (first authored section).

**Files:**
- `skills/mindhive-orchestrator/SKILL.md` (create the new fused version; this unit writes the top ~1,500 words)

**Approach:**
- Frontmatter: `name: mindhive-orchestrator`; a `description` that names both the ult-idea rigor and the memory-grounded orchestration, and lists the trigger phrases (so the skill auto-selects). Keep to `name` + `description` only (KTD5).
- **Prime Directive** section: state the HARD-GATE — no question reaches the user that the ledger or codebase can answer; run the preflight first. Include the three user-specified commands verbatim with their one-line purposes, `--semantic` guidance on `recall`, and `graph related` "when the idea touches an existing subsystem."
- **Function-Call Syntax** section: the three-tier portable invocation form (KTD3), the degradation contract ("MindHive memory unavailable — running ult-idea in stateless mode"), and a compact table of the broader supporting commands (`briefing`, `rules inject`, `skills list/status/stats`, `plugins recommend`, `append`, `sync`, `export`) with one-line "use when" notes. Point to `references/mindhive-command-catalog.md` for the full surface.
- **Capability catalog** block: under what conditions the skill fires, for what tasks, and why (idea validation + memory-grounded execution). Point to `references/skill-catalog.md` for the deep catalog.

**Patterns to follow:** the current `skills/mindhive-orchestrator/SKILL.md` "Prime directive: recall before you ask" section (lines 29–40) and sibling skills' frontmatter style (`skills/circle/SKILL.md`, `skills/engineering-loop/SKILL.md`).

**Test scenarios (verification checks — documentation artifact):**
- Frontmatter parses as valid YAML with exactly `name` and `description` keys; `name` is `mindhive-orchestrator`.
- The three user-specified commands appear verbatim: `python -m mindhive state`, `python -m mindhive recall "<...>"` (with `--semantic` noted), `python -m mindhive graph related "<...>"`.
- The degradation contract sentence is present and names all three surfaces indirectly (works with or without the CLI).
- Prime Directive is phrased as a gate ("do not ask what the ledger/codebase can answer").

**Verification:** Section renders in markdown; the opening is legible as "gate first, then ult-idea." `Covers Integration #1, #2.`

---

### U2. Phase 1 EXPLORE — ult-idea preserved verbatim + recall preflight woven into every gap and dimension

**Goal:** Reproduce all of ult-idea Phase 1 (1.0 Project Diagnosis, 1.1 three dimensions, 1.2 the five gaps, 1.3 Visual Companion, 1.4 propose 2–3 approaches, 1.5 section-by-section approval, 1.6 durable requirements document + self-review checklist + user gate) with **no loss of content**, and interleave "▸ MindHive preflight" instructions so that each gap and dimension is checked against `state`/`recall`/`graph related` before it is asked of the user.

**Requirements:** Integration #1, Requirement "ult-idea must not decrease," KTD1, KTD2.

**Dependencies:** U1.

**Files:**
- `skills/mindhive-orchestrator/SKILL.md` (append Phase 1 block, ~2,600+ words — ult-idea Phase 1 is the single largest chunk)

**Approach:**
- Copy ult-idea Phase 1 content faithfully (headings, the five gaps with their How-to-test / Red-flag structure, the attendance worked examples, the requirements-doc template, the self-review checklist, the user-review gate).
- After the diagnosis and before 1.1, insert a "▸ Recall preflight for this idea" block: run `state`, `recall "<key terms>"`, `graph related "<entity>"`, and `rules inject`; record what the ledger already answers so those questions are skipped ("The ledger shows you settled X here, so I'll assume that").
- On each of the five gaps, add a one-line "check the ledger first" cue (e.g., Evidence Gap → `recall "<problem> evidence"` before asking for proof; Durability Gap → `graph related "<subsystem>"` to see prior durability decisions).
- Preserve the "WHAT not HOW" guiding principle and the one-question-at-a-time rule.

**Patterns to follow:** ult-idea SKILL.md Phase 1 (source lines 188–483); orchestrator "Phase 1 — Relentless interview" (current SKILL.md lines 44–57).

**Test scenarios (verification checks):**
- All five gaps (Evidence, Specificity, Counterfactual, Attachment, Durability) are present with their How-to-test and Red-flag subsections.
- Project-diagnosis Lightweight/Standard/Deep classification and the subsystem-decomposition special rule are present.
- The requirements-document template and self-review checklist survive intact.
- Each gap carries a MindHive preflight cue; the recall preflight block precedes the first user question.

**Verification:** Diff the Phase-1 prose against source ult-idea — no dimension, gap, example, or checklist item dropped. `Covers Integration #1 / "ult-idea not decreased".`

---

### U3. Phases 2 & 2.5 — agent-native review + architecture checkpoint preserved + plugin/ledger grounding

**Goal:** Reproduce ult-idea Phase 2 (five agent-native principles: Parity, Granularity, Composability, Emergent Capability, Improvement Over Time; the gap→fix table; the update-design step) and Phase 2.5 (all eight decision points 2.5.0–2.5.7 + the ARCHITECTURE.md deliverable 2.5.8 + approval gate) with no loss, and add MindHive grounding: consult `plugins recommend` / `plugins scan` for which installed plugin suits the emerging system, and log locked architecture decisions back with `append decision`.

**Requirements:** "ult-idea not decreased," the "learn from the skill & plugin database which plugin is suitable" requirement, KTD1.

**Dependencies:** U2.

**Files:**
- `skills/mindhive-orchestrator/SKILL.md` (append Phases 2 & 2.5, ~2,200+ words)

**Approach:**
- Copy Phase 2 (source lines 486–609) and Phase 2.5 (source lines 612–825) faithfully, including the stakes/reversibility approval matrix, the model-tier table, the entity-scoped directory example, and the ARCHITECTURE.md template.
- Insert "▸ MindHive plugin grounding" in Phase 2.5.1 (Tool Design Strategy): run `python -m mindhive plugins recommend` to see which cataloged plugins fit; note that `plugins scan` refreshes the catalog.
- Insert "▸ Log the decision" cues at each Phase-2.5 lock point: `python -m mindhive append decision "<architecture choice>" --tags orchestrator,architecture`.
- Preserve the conditional-application rules (skip for non-software; Lightweight does a brief 2.5).

**Patterns to follow:** ult-idea SKILL.md Phases 2 / 2.5; orchestrator Recommendation-Matrix sourcing rules for plugin/skill grounding.

**Test scenarios (verification checks):**
- All five agent-native principles present with their validate/fix guidance and the summary gap→fix table.
- All eight Phase-2.5 decision points present, plus the ARCHITECTURE.md deliverable template and approval gate.
- Plugin-grounding cue references a real command (`plugins recommend`); decision-logging cue references real `append` syntax.

**Verification:** Diff Phases 2/2.5 against source — nothing dropped. Plugin/append commands validate against `src/mindhive/__main__.py`. `Covers "plugin suitability" requirement.`

---

### U4. Phase S — Skill Selection & Recommendation Matrix (orchestrator core) + skill catalog

**Goal:** Add the memory-grounded skill-selection phase that ult-idea lacks: read the live inventory (`skills list`), the green/red status (`skills status`), and completion telemetry (`skills stats`); score 2–4 candidate skills on the seven Recommendation-Matrix parameters; consult prior outcomes via `recall "<skill>"` and prevention lessons via `rules show "skill:<name>"`; present the matrix; get selection approval. Author the accompanying capability catalog (conditions/tasks/why) that the user asked for.

**Requirements:** Integration #2 (learn which tools work / effective syntax / successful commands), the "catalog your new skills — conditions/tasks/why" requirement, KTD4.

**Dependencies:** U3 (skill selection follows architecture lock).

**Files:**
- `skills/mindhive-orchestrator/SKILL.md` (append Phase S, ~1,200 words)
- `skills/mindhive-orchestrator/references/recommendation-matrix.md` (preserve; light edit to reference the new phase name)
- `skills/mindhive-orchestrator/references/skill-catalog.md` (create)

**Approach:**
- Phase S body: the hard sourcing rule (candidates come verbatim from the session available-skills list), the seven-parameter scoring (suitability, compatibility, success-prob `(est.)`, risk, speed, cost, verdict), the telemetry reads, and the honesty guardrails. Point to `references/recommendation-matrix.md` for the anchored scales and rendering.
- **Learn-from-mistakes gate:** before recommending/invoking a candidate showing `fail` in `skills status`, read `rules show "skill:<name>"` and apply the prevention rule; prefer green skills; treat red as "fix or avoid, with a reason."
- **Skill catalog** (`references/skill-catalog.md`): for the fused skill and its sub-capabilities, document *under what conditions* it fires (triggers), *for what tasks* (idea validation, architecture review, skill selection, execution, learning), and *why* (memory-grounded rigor beats cold re-asking). Include the follow-up chain mapping table.

**Patterns to follow:** existing `references/recommendation-matrix.md`; orchestrator Phase 2 and Phase 3b (current SKILL.md lines 59–115).

**Test scenarios (verification checks):**
- The seven matrix parameters are all present; success probabilities carry `(est.)`.
- The candidate-sourcing rule ("verbatim from available-skills list, never a guessed name") is stated.
- Telemetry commands (`skills list/status/stats`) and the `rules show "skill:<name>"` learn-gate reference real commands.
- `skill-catalog.md` answers conditions/tasks/why explicitly.

**Verification:** Every referenced command validates against `__main__.py`; the matrix example renders as a table. `Covers Integration #2 / capability-catalog requirement.`

---

### U5. Phase X — Execute & Chain (invoke channel, green/red, self-heal)

**Goal:** Add the execution phase: invoke the selected skill(s) (via the Skill/Task primitive in-agent, or `python -m mindhive skills invoke <name>` when MindHive should drive and record the outcome), then proactively recommend the follow-up quality chain with a one-line rationale each, and record outcomes (`skills record --ok/--fail`) so the learning loop is fed.

**Requirements:** Integration #2 (successful commands / most effective way), follow-up chaining, KTD4.

**Dependencies:** U4.

**Files:**
- `skills/mindhive-orchestrator/SKILL.md` (append Phase X, ~700 words)

**Approach:**
- Two execution channels: in-agent Skill tool (default, interactive surfaces) vs. `skills invoke` (headless/dashboard-driven runs, exit-code-gated). State when each applies.
- The follow-up chain table (new/changed code → code-review + simplify; auth/payments/input → security; bug-shaped → debug; UI → browser-test + a11y; about-to-ship → commit-push-pr), each with a rationale, resolved against the available-skills list.
- Outcome recording: `skills record <name> --ok|--fail --note "<one line>"` (agent-assessed provenance), which feeds `tool_failed(signature=skill:<name>)` → incidents → reflect → Improvements.

**Patterns to follow:** orchestrator Phase 3 + Phase 3b (current SKILL.md lines 76–115).

**Test scenarios (verification checks):**
- Both execution channels documented; `skills invoke` exit-code gating noted (0 ok / 1 fail).
- Follow-up chain table present with a rationale column; skills resolved from the available-skills list, not hard-coded guesses.
- `skills record` syntax matches `__main__.py` (mutually exclusive `--ok`/`--fail`).

**Verification:** Commands validate against source; follow-up table renders. `Covers follow-up-chaining requirement.`

---

### U6. Phase 3 INTERROGATE & SHARPEN — preserved + ledger cross-reference and decision logging

**Goal:** Reproduce all of ult-idea Phase 3 (3.0 ubiquitous language, 3.1 concrete scenarios/edge cases, 3.2 cross-reference with code, 3.3 real-time CONTEXT.md, 3.4 testing strategy, 3.5 ADR three-part test, 3.6 final approval) with no loss, and extend the cross-reference step to also consult the ledger (`recall`/`graph related`) so contradictions between the idea, the code, and prior decisions all surface. Fold the CONTEXT.md and ADR templates into reference files.

**Requirements:** "ult-idea not decreased," KTD1.

**Dependencies:** U2 (Phase 3 follows the preserved Phase 1). Sequenced after U4/U5 in the document per the process-flow (interrogation deepens the approved design before/around execution) — see Assumptions on ordering.

**Files:**
- `skills/mindhive-orchestrator/SKILL.md` (append Phase 3, ~1,600 words)
- `skills/mindhive-orchestrator/references/context-format.md` (create — ult-idea CONTEXT.md template)
- `skills/mindhive-orchestrator/references/adr-format.md` (create — ult-idea ADR template)

**Approach:**
- Copy Phase 3 (source lines 828–1057) faithfully, including the fuzzy-term watchlist, the five attendance scenarios, the contradiction-resolution example, the CONTEXT.md rules/format, the testing tiers, and the ADR three-part test.
- Extend 3.2 (cross-reference with code): add "▸ also cross-reference the ledger" — `recall "<claim>"` and `graph related "<entity>"` to catch idea-vs-decision contradictions, not just idea-vs-code.
- Add "▸ Log resolved terms/decisions": `append decision "<canonical term or hard reversal>" --tags orchestrator,context` so sharpened language persists across runs.

**Patterns to follow:** ult-idea SKILL.md Phase 3; the repo's own `CONTEXT.md` convention.

**Test scenarios (verification checks):**
- All Phase-3 subsections (3.0–3.6) present; the fuzzy-term watchlist and the five scenarios survive.
- CONTEXT.md and ADR templates preserved (in-body pointer + reference files).
- Ledger cross-reference cue references real commands.

**Verification:** Diff Phase 3 against source — nothing dropped. `Covers "ult-idea not decreased".`

---

### U7. Governance sections preserved + Learning Loop + Handoff + Compatibility

**Goal:** Reproduce the remaining ult-idea sections with no loss (Communication Rules, Anti-Leak HARD-GATE, Ruthless YAGNI, Completion Criteria, Troubleshooting & Edge Cases, Integration Map), and add the three new closing sections: **Phase L — Learning Loop & Log-Back**, **Context Handoff at ~50%**, and **Compatibility & Portability**.

**Requirements:** "ult-idea not decreased," Integration #2 (learn from mistakes), Integration #4 (cross-surface compatibility), KTD3, KTD5.

**Dependencies:** U6.

**Files:**
- `skills/mindhive-orchestrator/SKILL.md` (append closing sections, ~2,000+ words)
- `skills/mindhive-orchestrator/references/handoff-protocol.md` (preserve; light edit only)
- `skills/mindhive-orchestrator/references/compatibility.md` (create)
- `skills/mindhive-orchestrator/references/mindhive-command-catalog.md` (create)

**Approach:**
- Preserve Communication Rules, Anti-Leak HARD-GATE (source 1103–1116), YAGNI (1120–1160), Completion Criteria (1164–1178) — extend the completion checklist with memory criteria ("learnings logged, sync run"), Edge Cases (1182–1232), and the Integration Map table (extend with MindHive reference rows).
- **Phase L:** the failures→rules→improvements loop; append decision/caveat/experiment_result only when something memorable happened; end with `sync`. Reference `flag` for judgment errors and `rules inject`/`rules summaries` for reading learned rules.
- **Context Handoff:** the ~50% phase-boundary check and the approved-handoff sequence (`export` → append open threads → `sync` → print resume). Point to `references/handoff-protocol.md`.
- **Compatibility & Portability:** the Claude Code / Cowork / NewTask matrix — blocking-question tool per surface (AskUserQuestion vs. numbered-options fallback), Skill/Task invocation primitive, the visual-companion browser dependency, and the MindHive-CLI graceful-degradation contract (KTD3). Point to `references/compatibility.md`.
- **Command catalog** (`references/mindhive-command-catalog.md`): the full CLI surface grouped (memory, learning-loop, skills, plugins, dashboard, sync/export, loop) with "use when" per command, derived from `src/mindhive/__main__.py`.

**Patterns to follow:** existing `references/handoff-protocol.md`; ult-idea closing sections; `src/mindhive/__main__.py` for the authoritative command list.

**Test scenarios (verification checks):**
- Communication Rules, Anti-Leak, YAGNI, Completion, Edge Cases, Integration Map all present and unshrunk.
- Compatibility section names all three surfaces with a concrete fallback for the blocking-question tool and the CLI-absent case.
- Command catalog commands each exist in `__main__.py` (no invented subcommands).

**Verification:** Diff governance sections against source; `references/mindhive-command-catalog.md` cross-checks 1:1 against `__main__.py` subparsers. `Covers Integration #4 / "ult-idea not decreased".`

---

### U8. Manifests, cross-link integrity, and ≥12,000-word verification

**Goal:** Final assembly checks: touch up the plugin descriptions to reflect the orchestrator's expanded idea→design→ship scope, verify every internal reference link resolves, verify every documented MindHive command exists, and verify `SKILL.md` is **≥12,000 words**.

**Requirements:** KTD6 (word-count floor), Integration #4, overall completeness.

**Dependencies:** U1–U7.

**Files:**
- `.claude-plugin/plugin.json` (description touch-up)
- `.claude-plugin/marketplace.json` (matching description touch-up)
- `skills/mindhive-orchestrator/SKILL.md` (final pass — expand any thin section if the word count falls short)

**Approach:**
- Update both manifest descriptions so `/mindhive` reads as "orchestrates an idea end-to-end with ult-idea rigor — pressure-tests the idea, reviews agent-native architecture, selects skills via a scored matrix, executes and chains quality passes, and logs learnings back." Keep `/circle` and `/engineering-loop` descriptions intact.
- Run `wc -w skills/mindhive-orchestrator/SKILL.md`; if < 12,000, expand the woven MindHive execution-context blocks (real guidance, not filler) until the floor is cleared with margin.
- Grep the fused `SKILL.md` for every `python -m mindhive …` / `hive …` command and confirm each subcommand appears in `src/mindhive/__main__.py`.
- Confirm every `references/*.md` link in `SKILL.md` points to a file that now exists.

**Patterns to follow:** current `.claude-plugin/plugin.json` and `marketplace.json` phrasing.

**Test scenarios (verification checks):**
- `wc -w` on `SKILL.md` returns ≥ 12,000.
- Zero broken `references/*.md` links (every referenced file exists).
- Zero invented MindHive subcommands (all cross-check against `__main__.py`).
- Both manifests remain valid JSON; only descriptions changed; version bump decision recorded (see Open Questions).

**Verification:** `wc -w`, link-existence grep, command cross-check, `python -c "import json,,,"` JSON validity on both manifests. `Covers KTD6 / word-count floor.`

---

## Scope Boundaries

**In scope**
- Rewriting `skills/mindhive-orchestrator/SKILL.md` as the fused canvas (≥12,000 words).
- New/edited reference files under `skills/mindhive-orchestrator/references/`.
- Description-only touch-ups to `.claude-plugin/plugin.json` and `marketplace.json`.

**Explicit non-goals**
- No changes to MindHive Python source (`src/mindhive/**`), hooks, or tests.
- No new MindHive CLI commands — only documentation of existing ones.
- No changes to `/circle` or `/engineering-loop` skills.
- No changes to the V17 live-trading path or the `LIVE_LOCK` guard.

### Deferred to Follow-Up Work
- Building a real per-skill success-rate backend beyond `skills stats` (the matrix keeps `(est.)` labels until then).
- Folding in the remaining ult-idea supporting docs that the source SKILL.md references but that are not present in the local copy (e.g., `mcp-tool-design.md`, `architecture-patterns.md`, `action-parity-discipline.md`) — the fused skill keeps the Integration Map pointers; materializing those files is a separate effort.
- A `commands/mindhive-orchestrator.md` slash-command alias (today `/mindhive` maps to the skill via `commands/mindhive.md`; a second alias is optional polish).

---

## Assumptions (headless — Inferred bets, confirm if wrong)

- **Output target is the existing skill file.** "Output of this combination = /mindhive-orchestrator" is read as: rewrite/expand `skills/mindhive-orchestrator/SKILL.md` in place (keeping the skill name), not create a new `ult-idea` skill directory. The user framed the current orchestrator as the canvas they "updated with ult-idea," so in-place fusion matches intent.
- **Document ordering.** ult-idea's phase order is preserved; the new orchestration phases slot in as: Skill-Selection (Phase S) and Execute-&-Chain (Phase X) after the architecture checkpoint, then ult-idea Phase 3 interrogation, then Learning-Loop (Phase L). This mirrors "validate → architect → select → execute → sharpen → learn." An alternative (interrogate before selecting skills) is defensible; the chosen order keeps ult-idea's own Phase-3 position relative to Phases 1–2.5 intact while adding orchestration around it.
- **Word count is met by substantive expansion**, not padding — the fused doc lands well above 12,000 because ult-idea (7,930) is preserved and the woven integration + three new phases + catalogs add ~4,000–5,000 words.
- **`python -m mindhive` is the canonical documented form**, with `hive` and the venv-python path as the two fallbacks, per the current skill and `commands/mindhive.md`.

---

## Open Questions (non-blocking; sensible defaults chosen)

- **Plugin version bump?** Default: bump `mindhive` from `0.3.0` → `0.4.0` in both manifests since the orchestrator gains major capability. Deferred as a one-line change inside U8 if desired; not blocking. *(Default taken: bump to 0.4.0.)*
- **Second slash-command alias `/mindhive-orchestrator`?** Default: no — `/mindhive` already routes to the skill and direct Skill invocation works by name. Listed under Deferred.

---

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ult-idea content silently shrinks during the rewrite | Medium | High (violates core requirement) | U2/U3/U6/U7 each carry a "diff against source ult-idea — nothing dropped" verification; U8 word-count floor is a backstop |
| A documented MindHive command is misspelled or invented | Medium | Medium (skill tells the agent to run a non-existent command) | U4/U5/U7/U8 cross-check every command against `src/mindhive/__main__.py` |
| Skill hard-fails on surfaces without the MindHive CLI (Cowork/NewTask sandboxes) | Medium | High (fails compatibility requirement) | KTD3 degradation contract + U7 Compatibility section: announce stateless mode and continue with full ult-idea rigor |
| Blocking-question tool (`AskUserQuestion`) unavailable on a surface | Medium | Medium | U7 documents the numbered-options-in-chat fallback per surface |
| SKILL.md becomes so long it strains context when loaded | Low | Medium | Heavy detail pushed to `references/*` files; SKILL.md keeps the operating procedure, references carry catalogs/templates |
| Word count padded with filler to hit 12,000 | Low | Medium | Expansion is real execution guidance tied to actual commands; U8 reviews for substance, not just count |

---

## Dependencies & Sequencing

Linear by document assembly: **U1 → U2 → U3 → U4 → U5 → U6 → U7 → U8.** U1 lays the spine; U2/U3/U6/U7 preserve ult-idea in order; U4/U5 insert the orchestration phases; U8 is the final verification and manifest pass. Reference-file creation is co-located with the unit that first points to it. All work lands on branch `claude/mindhive-orchestrator-skill-272720`.

---

## Verification Strategy (whole-plan)

Because the deliverable is a documentation/skill artifact with no runtime behavior, "tests" are structural checks, run at U8 and per-unit:

1. **Preservation check** — section-by-section diff of the ult-idea-derived prose against the source SKILL.md; every phase, gap, principle, rule, checklist, and example accounted for.
2. **Word-count floor** — `wc -w skills/mindhive-orchestrator/SKILL.md` ≥ 12,000.
3. **Command accuracy** — every `mindhive` command in the doc exists as a subparser in `src/mindhive/__main__.py`.
4. **Link integrity** — every `references/*.md` referenced from SKILL.md exists on disk.
5. **Manifest validity** — `plugin.json` and `marketplace.json` remain valid JSON; only descriptions (and optional version) changed.
6. **Load smoke test** — the skill is discoverable/loadable by name `mindhive-orchestrator` (frontmatter valid; no broken YAML).
