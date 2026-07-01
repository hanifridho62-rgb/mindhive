# 7. Learning Loop: autonomous quarantine store, human evaluation authoritative over auto

Date: 2026-07-01
Status: Accepted

## Context

Claude Code sessions are stateless, so Claude rediscovers the same execution
errors session after session. Measured across 195 real transcripts: the *same*
failures recur — `ModuleNotFoundError: 'mindhive'` ×13, `'hive_pipeline'` ×14,
`'tools'` ×10, plus PowerShell/here-string errors — with one dominant root cause
(running project code from the wrong cwd/venv, not as a module from repo root).

MIND HIVE already has the back half of an error-prevention loop: a vector store
(ChromaDB), append-only events, and session_start/prompt injection. It lacks the
front half — **failure capture** and **reflection** into reusable rules.

The tension: the user wants a **fully autonomous** loop (rules created and active
with no human step), but the MIND HIVE `OPERATING_SPEC` (§4/§6, ADR 0005) requires
memory to be append-only and forbids unconfirmed mutation of canonical memory or
auto-merging concepts. A loop that silently rewrites the canonical directives
(`SOUL.md`) would contradict the system's own injected rules, and a single wrong
machine rule would poison every future session.

## Decision

**Split the store. The loop writes autonomously only to a quarantined machine-rule
store; the canonical `SOUL.md` is reached only by an explicit one-click human
promotion. Within an incident, the Human Evaluation outranks the machine's.**

- **Quarantine store** (`~/.mindhive/<project>/rules/`: incident records, `RULES.md`,
  a `prevention_rules` ChromaDB collection). The loop appends here autonomously;
  rules are **active immediately**. Append-only and auditable — consistent with §6.
- **Canonical `SOUL.md`** (`directives.py` + `STATE.md`): the loop **never** writes
  here. **Promotion** (one-click human approval) is the only path from Quarantine to
  canonical, and the only human gate in the loop.
- **Dual-evaluation incident record**, one file per signature:
  `## Raw Signal` (immutable) → `## Auto Evaluation` (Sonnet 4.6) →
  `## Human Evaluation` (authoritative; overrides Auto when non-empty) →
  `## Active Prevention Rule` (projected). Human edits round-trip as append-only
  `rule_revised` events via `reconcile`.
- **Human authority in demotion:** auto-sourced rules that leak (fire yet the error
  recurs) ≥2× are auto-demoted; **human-sourced rules are never silently demoted** —
  a leak flags them `needs_review` for the human. Human judgment outranks the
  failure heuristic.
- **Capture is deterministic and LLM-free**, keyed off the verified transcript
  field `tool_result.is_error` (excluding `interrupted`), sourced from the harvest
  path — not a live-hook payload field, and not `exitCode` (unreliable/absent).
  **Reflection is deferred** (out-of-band Sonnet pass), never in the hook, so no LLM
  latency touches a session. A signature must recur **N=2** times before its rule
  activates; the signature is a **normalized root cause** key, not raw stderr.

## Consequences

- A wrong Auto rule is contained: it lives only in Quarantine, is demoted after 2
  leaks, and can never reach canonical memory without human promotion. The blast
  radius of autonomy is bounded by construction.
- The user gets a genuine hands-off loop *and* a manual-analysis channel (the Human
  Evaluation section) in the same file, without either blocking the other.
- Capture never blocks or slows a session (inherits the hang-proof/fail-silent hook
  discipline); reflection cost is paid off-path on a cheaper tier (Sonnet).
- The recurrence gate + dedup keep the injected active set small (target <20), so
  session_start can inject the whole set with no per-prompt selection in v1.
- New surface to maintain: the `signature()` normalizer (family table) and a
  `hive rules` CRUD command set. Normalizer correctness is load-bearing — over-
  normalization collapses distinct bugs; the recurrence gate partially absorbs this.

## Alternatives considered

- **Autonomous write straight into canonical `SOUL.md`:** rejected — directly
  violates `OPERATING_SPEC` §4/§6; one bad rule poisons every future session with no
  human backstop.
- **Curated static rules only (hand-written `SOUL.md`, no capture/reflection):**
  rejected by the user, who wants a self-growing autonomous loop; but retained as the
  conceptual MVP and as the content that promotion produces.
- **Semi-automatic (capture + draft, human approves before a rule goes active):**
  rejected — the user wants rules active immediately; human authority is preserved
  instead via the authoritative Human Evaluation section and the promotion gate.
- **Capture via `exitCode`/`returncode` (per the original blueprint):** rejected —
  verification showed those fields are absent/unreliable in transcripts; `is_error`
  is the universal signal.
- **Reflection inline in the PostToolUse hook:** rejected — an LLM call would blow
  the <1s hook budget and risk hangs; deferral costs nothing here.
