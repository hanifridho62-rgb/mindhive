# Learning Loop — Architecture Decisions (Phase 2.5)

**Status:** Decisions made by Claude per user delegation ("design things"). Awaiting lock.
**Date:** 2026-07-01
**Companion to:** 2026-07-01-learning-loop-requirements.md

---

## Phase 2 — Agent-Native Review (summary)

The consumer *is* the agent (Opus 4.8), so parity is near-trivial. The one human action — **promotion approval** — is intentionally gated, but is *also* exposed as a tool so the agent has full read/propose parity; only the promote *decision* requires the human. CRUD completeness holds: **Create** (auto, via reflection), **Read** (injection + `list`/`show`), **Update** (occurrence count, demote), **Delete** (`forget`/expire). No bloated tools; reflection is a *composed* Claude prompt over primitives, not a specialized code path. ✅

---

## Decisions

### D1. Two-tier execution: deterministic capture, deferred LLM reflection
- **Capture** is deterministic, **no LLM**, sourced from the **transcript harvest path** (`harvest.py`, guaranteed to have the data — verified). It records a `tool_failed` event. **VERIFIED capture contract:** a failure is `tool_result.is_error == true` **and not** `toolUseResult.interrupted` (user cancels are excluded). Signal text = `toolUseResult.stderr` (Bash/PS) or the tool_result `content` (other tools); `exitCode` captured opportunistically when present (it is NOT reliably present — `is_error` is the universal trigger). The live `PostToolUse` hook may enrich in real time but capture does not depend on it.
- **Reflection** runs *out-of-band* (deferred batch), never in the hook. This honors "Separation of Concerns" (executor ≠ evaluator) and keeps all LLM latency off the session path.
- **Trigger:** capture enqueues a reflection job only when a signature's occurrence count crosses **N** (see D3). Reflection is processed by the existing `hive loop` / a scheduled autosync tick.
- *Rationale:* an LLM call in a live hook would violate the <1s hook budget and risk hangs. Deferral costs nothing here — a prevention rule that lands minutes later still fires on the *next* session.

### D2. Recurrence gate N = 2 (tunable)
- A failure signature must occur **twice** before it becomes an active rule. The 2nd occurrence activates it; the 3rd onward is prevented.
- *Rationale:* literal "touch fire twice → flinch." Filters one-off novel errors (unpreventable) while paying off fast. Your data shows real repeats hit 4–14×, so N=2 captures them early. Stored as config, not hardcoded.

### D3. Signature = normalized root cause, NOT raw stderr
- Raw stderr is too brittle (paths, line numbers, timestamps vary). A `signature(failure)` normalizer maps a failure to a canonical key by error family:
  - `ModuleNotFoundError: '<pkg>'` → `pymodule:<top-level-pkg>` (e.g. `pymodule:mindhive`)
  - PowerShell `The term '<x>'` / `is not recognized` → `pscmd:<x>`
  - `SyntaxError: unterminated string literal` → `pysyntax:unterminated-string`
  - fallback → `raw:<sha1(normalized_stderr_first_line)>`
- Recurrence counts by signature; the rule keys off the signature.
- *Rationale:* this is the crux — get normalization right and recurrence detection works; get it wrong and every error looks novel. Extensible family table.

### D4. Store split & layout
- **Canonical** (`SOUL.md` ≈ `directives.py` + `STATE.md`): human-only. Loop never writes.
- **Quarantine** (machine-rule store), per project under `~/.mindhive/<project>/rules/`:
  - append-only rule events in the existing event log (auditable, tagged `rule`);
  - `RULES.md` — projected active set (human-readable);
  - ChromaDB collection `prevention_rules` (reuses `vectors.py`) for semantic match.
- Promotion copies a rule's text into `SOUL.md`/directives — the **only** write to canonical, human-approved.

### D5. Injection: full active set at session_start (semantic top-K deferred)
- At **session_start**, inject the *entire* active rule set as a compact `# PREVENTION RULES (machine-learned)` block, beside the existing directives.
- The recurrence gate + dedup are designed to keep the active set small (target < 20), so the whole set fits the budget — no per-prompt selection needed in v1.
- Pre-prompt semantic top-K is **deferred** (YAGNI) until the active set outgrows the token budget.
- **Pensyve coexistence:** different channel, different content (prevention rules vs. recall memories). This block is namespaced and injected by the mindhive session_start hook, not Pensyve — no double-injection. Capped at a fixed token budget; if exceeded, fall back to top-K-by-occurrence.

### D6. Demotion = failure-based only (no time decay in v1)
- If an active rule was injected yet its signature fails again → increment `leaked`. After **M = 2** leaks, demote (the RCA was likely wrong/insufficient); demoted rules leave injection but stay stored for audit / re-reflection.
- No time-based decay in v1 (a correct rule should persist; small set makes staleness harmless).

### D7. Model tier
- **Executor:** Opus 4.8 (given — the agent being improved).
- **Reflection/RCA pass:** **Sonnet 4.6.** Structured RCA over one failure + a schema is a bounded task; Sonnet is fast and cheap enough to run per-signature. Opus is the fallback if RCA quality proves insufficient. *Rationale:* reflection runs potentially often; don't pay Opus rates for a narrow, schema-constrained job.

### D8. Tool surface (primitives)
`hive rules list [--status]`, `hive rules show <id>`, `hive rules promote <id>` (human gate), `hive rules demote <id>`, `hive rules forget <id>`, `hive rules stats`, `hive rules reconcile` (re-read human edits → emit revision events). Reflection is `hive rules reflect` (batch, invoked by the loop). Full CRUD → parity + composability.

### D9. Incident Record — one file per signature, dual evaluation (Auto + Human)
Every unique signature gets ONE human-editable Markdown record at
`~/.mindhive/<project>/rules/incidents/<signature>.md`. It is the *workspace/projection*; the event log stays canonical truth. Layout:

```markdown
---
signature: pymodule:mindhive
task_category: dependency
rule_id: <uuid>
status: candidate|active|demoted|promoted
rule_source: auto|human|merged      # which section produced the ACTIVE rule
occurrences: 13
prevented: 0
leaked: 0
first_seen: 2026-06-21T10:24:00Z
---

# Incident: pymodule:mindhive

## Raw Signal            (immutable — captured, never edited)
- Failed action: `python mindhive.py state`
- Error excerpt: `ModuleNotFoundError: No module named 'mindhive'`
- Sample sessions: 270a1bcf…, 2adfae8f… (+11)

## Auto Evaluation       (Sonnet 4.6 — machine-generated; do not edit)
- Root cause: ran the package as a script from outside repo root; `mindhive` not importable.
- Lesson: run as a module from repo root with the venv.
- Prevention rule: use `…\.venv\Scripts\python -m mindhive` from repo root; never `python mindhive.py`.

## Human Evaluation      (hanif — AUTHORITATIVE; overrides Auto when non-empty)
<!-- Leave blank to accept Auto. Fill to override or augment. -->

## Active Prevention Rule (projected — the text that is actually injected)
> Always run mindhive via `…\.venv\Scripts\python -m mindhive` from repo root.
```

- **Precedence:** the *Active Prevention Rule* is derived — **Human Evaluation if non-empty, else Auto**. Human never blocked by machine.
- **Round-trip:** files are the interface. `hive rules reconcile` (run on each loop tick) diffs the Human Evaluation section; if changed, it appends an append-only `rule_revised` event (`source: human`), re-projects the Active rule, sets `rule_source: human|merged`. Auditable per §6 — the edit becomes an event, the file is just the surface.
- **Raw Signal is immutable**; only the two Evaluation sections differ in authorship, exactly as requested (Auto section = Sonnet, Human section = you, same file).

### D10. Human authority changes the demotion & promotion rules
- **Auto-sourced rule** → subject to D6 failure-based auto-demotion (2 leaks → demote).
- **Human-sourced rule** → **never silently demoted.** A leak instead flags it (`needs_review`) and surfaces to you; only you demote it. Respects that a human decision outranks the machine's failure heuristic.
- A rule carrying a Human Evaluation is the **prime candidate for promotion** to canonical `SOUL.md` (still your one-click gate, D4) — human-vetted rules are what should graduate.

---

## Upgraded success metric (replaces grep-only)

Add an **internal efficacy counter** as the primary proof, with the grep as external corroboration:
- Every active rule tracks `prevented` (signature matched an injected rule and did *not* recur that session) vs. `leaked` (recurred despite injection).
- **System is working when:** aggregate `prevented / (prevented + leaked)` climbs over time, and total distinct recurring signatures per new session trends down (the grep).
- *Why better:* measures efficacy directly and per-rule, drives D6 demotion automatically, and doesn't depend on manually re-running a grep.

---

## Architecture pattern / workspace / gates (2.5 checklist)

- **Workspace:** shared (MIND HIVE store; all files inspectable). 
- **Pattern:** event-driven capture (hook) + deferred batch reflection (loop/scheduled). 
- **Approval gates (stakes/reversibility):** Quarantine write = auto (low stakes, reversible/demotable); Promotion to canonical = explicit human approval (high stakes, hard to reverse). 
- **Context injection:** hybrid — static directives + dynamic `RULES.md` block at session_start. 
- **Mobile:** N/A.

## Known risks & mitigations

- **Wrong RCA poisons injection** → isolated to Quarantine, demoted after M leaks, never auto-reaches canonical.
- **Signature over-normalization** (two different bugs collapse to one key) → keep families specific; fallback hashes the normalized first line.
- **Injection bloat** → recurrence gate + dedup + token cap + occurrence-ranked fallback.
- ~~PostToolUse payload may lack stderr/exit code~~ → **RESOLVED (verified 2026-07-01):** transcripts carry `tool_result.is_error` (119 sessions), `stderr`/`stdout` (94), `interrupted` (45). Field is `is_error`, not `returncode`/`exitCode` as the blueprint assumed. Capture keys off `is_error` via the harvest path — no dependency on live-hook payload shape.
- **`is_error` semantics vary by tool** → some tools set `is_error` for benign conditions (e.g. grep "no matches"). Mitigation: the signature normalizer + recurrence gate (N=2) filter benign one-offs; a per-tool allowlist of "errors that count" can be added if noise appears.
