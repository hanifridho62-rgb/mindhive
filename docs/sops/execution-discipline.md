Applies to EVERY task here, whether or not the prompt mentions it.

1. BEFORE ANY WORK — run `hive sop preflight` and read the output. It answers,
   from the ledger: is this session NEW or a CONTINUATION, which SOPs are
   mandatory, what the improvements record already warns about, and what is in
   the catalog. On CONTINUATION, absorb the prior context BEFORE working —
   `hive state`, `hive briefing`, `hive recall "<topic>"` — one item at a time.
   Never start on a continuation you have not read.

2. PREPARE; NEVER PROCEED BLIND. Name the skills, tools, ledgers, and plugins
   the work will use, and know what each does, before the first edit
   (`hive skills list`, `hive plugins list`). A needed skill or plugin that is
   not in the catalog gets installed before execution, not during.

3. LEARN FROM THE RECORD FIRST. Past execution failures are already written
   down — read them (`hive rules list`, `hive rules summaries`, `hive failures`)
   so a tool, syntax, or method error is not repeated and usage is not wasted.
   Search the record before asking the user; when the record is silent, ask.

4. UNDERSTAND BEFORE DECIDING. Study the prompt relentlessly: source data, the
   ledger, the code architecture, the intent, how it actually runs. Walk each
   branch of the decision tree, resolve dependencies one at a time, pick the
   best branch, and say why. A fact discoverable from the filesystem or a tool
   is yours to find, not the user's to supply.

5. USE THE WHOLE HIVE. Every MindHive function is an execution or understanding
   method. When one prompt implies several skills, decide deliberately whether
   to combine, parallelize, or sequence them — on technical and usage grounds.

6. USE THE ESTABLISHED FRAMEWORK, not improvised infrastructure: this project's
   own stack and deterministic tooling — pytest, argparse, the event-sourced
   fold, the atomic sidecar pattern. (The standing ask names Laravel; this is a
   Python project with no PHP, so the intent is honoured and the framework is
   this one.)

7. MAP THE ADDRESSES. Keep the architecture navigable — know which module owns
   what, so any address in it can be found the moment it is needed.

8. LOOP UNTIL THE PROMPT IS SATISFIED. After finishing anything, re-read the
   ORIGINAL prompt and list what is still unimplemented. If anything remains,
   keep going: add, replace, delete, or continue tasks and sub-tasks as needed.
   The mechanism may differ from what was asked; the outcome may not.

9. NEVER DECLARE DONE WITH A GAP. "Done" means the real flow was exercised and
   its real output read. Name what was skipped, untested, or assumed. Be
   critical, grounded in absolute truth, and never lie about a result.
