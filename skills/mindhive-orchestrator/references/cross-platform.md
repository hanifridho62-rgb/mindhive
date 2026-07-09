# Cross-Platform Compatibility — Claude Code · Cowork · NewTask

This skill must run identically wherever Claude runs. The pipeline (recall →
interview → matrix → execute → learn) is the same everywhere; only two mechanisms
vary by surface: **how you ask a blocking question** and **how you run a MindHive
command**. Detect what's available, use it, and degrade gracefully — never abort
the pipeline because one mechanism is missing.

## The two surface-dependent mechanisms

### 1. Blocking questions (the one-question-at-a-time interview)

| Surface | Primary mechanism | Fallback |
|---------|-------------------|----------|
| **Claude Code** | `AskUserQuestion` tool. Load its schema first via `ToolSearch` with `select:AskUserQuestion` if not already loaded. | A single plain-text question; then stop and wait for the reply. |
| **Claude Cowork** | The platform's native blocking-question / approval UI if present. | Plain-text single question, wait. |
| **Claude NewTask** | If the task runs non-interactively, you cannot block for input. | **Lean harder on the recall preflight and code exploration** to pre-answer questions; ask only genuinely user-owned questions and batch them into the task's normal reply channel. If truly headless, proceed on the recall-derived best answer and record the assumption via `append caveat`. |

The rule that survives everywhere: **one question at a time, lead with your
recommended answer, and never ask what recall or the code already answers.** The
*mechanism* flexes; the *discipline* does not.

### 2. Running MindHive commands (needs a shell)

| Surface | Mechanism | Notes |
|---------|-----------|-------|
| **Claude Code** | The `Bash` tool (or `PowerShell` on Windows). | Full access — the preferred path. |
| **Claude Cowork** | Whatever terminal/exec tool the surface exposes. | Same commands, same output. |
| **Claude NewTask** | The task's shell if it has one. | If none, see "No-shell degradation" below. |

**Interpreter detection (do this once per run):** try in order and keep the first
that returns real MindHive output rather than the Store-shim message:

1. `python -m mindhive state`
2. `C:\Users\hanif\mindhive\.venv\Scripts\python.exe -m mindhive state`  ← reliable on this machine
3. `hive state`

On Windows, a bare `python` is frequently the Microsoft Store *app-execution alias*:
it prints `Python was not found; run without arguments to install from the Microsoft
Store…` **and still exits 0**. Treat that output as "not found" and fall through to
the venv interpreter. On this machine the venv interpreter
(`.venv\Scripts\python.exe`) is the dependable one.

## No-shell degradation (when a surface can't run commands at all)

The recall preflight is *ideal*, not *mandatory*. If no shell is reachable:

1. **Use auto-loaded memory.** `MEMORY.md` / the session briefing is already loaded
   into context at session start — read it for the same signals `state`/`recall`
   would surface.
2. **Read the ledger and project files directly.** The requirement is "check the
   ledger *or* the code in the project before you ask." Reading `STATE.md`,
   `CONTEXT.md`, and the relevant source with the file tools satisfies the prime
   directive when the CLI is unavailable.
3. **Say so, once.** Note briefly that live recall was unavailable and you fell back
   to loaded memory + code reading. Then continue the pipeline unchanged.

Never let a missing CLI turn into "I'll just ask the user everything." Reading the
code is always available and is a first-class way to honor recall-before-ask.

## Skill invocation across surfaces

- **Claude Code / Copilot CLI:** the `Skill` tool. If its schema isn't loaded,
  `ToolSearch` with `select:Skill` first.
- **Gemini CLI:** `activate_skill`.
- **Through MindHive (any surface with a shell):** `skills invoke <name>` runs the
  skill via a headless `claude` and records green/red — use this when the run should
  be driven by MindHive itself (e.g. inside `/circle` or a dashboard-watched run).

## What is identical everywhere (do not vary these)

- The five-gap validation, the anti-leak HARD-GATE, and ruthless YAGNI.
- Recall (or read the code) **before** any user question.
- The scored Recommendation Matrix grounded in `skills list` (the live registry is
  the same file on every surface).
- Logging durable learnings back with `append`, then `sync`.

Compatibility means the *rigor and the memory loop* are constant; only the plumbing
for asking and executing adapts to the host.
