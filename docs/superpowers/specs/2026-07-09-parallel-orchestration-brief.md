# Parallel multi-agent orchestration — design brief

Date: 2026-07-09
Status: brief only — needs its own brainstorm → spec → plan cycle before any code

## The ask

An upper orchestration layer "like Fable 5's workflows": cluster the work,
classify file/task complexity, and run **parallel** sub-agents — where today's
`/mindhive` orchestrator is a strictly sequential pipeline
(interview → matrix → execution → learning) and the hive loop
(`loop/engine.py`) drives exactly one `claude` CLI worker at a time.

## Why this is a separate epic, not a task

This touches concurrency, process management, git isolation, budget
accounting, and failure semantics all at once. Every one of those has sharp
edges already recorded in the hive's own memory (Windows `.cmd`-shim spawn
bug; cross-process write locks for incident files; budget/breaker interplay
in the loop engine). Bolting parallelism onto the existing loop without its
own design pass would multiply those hazards. It also has zero overlap with
the notification center — separate spec, separate plan, separate PR train.

## Architecture sketch (starting point for the future brainstorm)

```
 orchestrator (new: src/mindhive/orchestrate/)
 ├─ 1. discover     work-list from the fix_plan / prompt / repo scan
 ├─ 2. classify     per-item complexity score (deterministic, cheap):
 │                  LOC, import fan-in, test coverage presence, churn
 │                  (git log), past tool_failed signatures on the file
 ├─ 3. shard        group items into N independent shards with disjoint
 │                  file ownership (the agent-teams pattern); items that
 │                  can't be isolated stay on the sequential main lane
 ├─ 4. run          N parallel drivers — each a loop-engine instance in its
 │                  own git worktree, budget split from the shared cap,
 │                  breaker per worker + a fleet-level breaker
 ├─ 5. merge        sequential integration lane: merge worktrees, run the
 │                  suite, route conflicts back to a resolver worker
 └─ 6. learn        per-shard loop_iteration events (already ledger-native)
                    + a fleet_run summary event for the notification center
```

Reuse, don't rebuild: `loop/driver.py` (CLI spawn — including the
`shutil.which` `.cmd` fix), `loop/budget.py` (split budgets), the circuit
breaker, and the notification center (each worker's session becomes a task
card for free — the fleet is observable the day it ships).

## Decisions the brainstorm must settle

1. **Complexity classifier:** deterministic heuristics only, or a
   cheap-model scorer? (Recommendation: deterministic first — measurable,
   free, and testable; a model scorer is Phase 2.)
2. **Concurrency cap and budget split:** fixed N vs. derived from the
   budget config; what happens when one shard exhausts its slice.
3. **Failure semantics:** one worker's breaker opening — kill the fleet,
   or degrade to sequential?
4. **Merge strategy:** octopus-style batch merge vs. sequential rebase per
   shard (recommendation: sequential rebase, conflicts routed to a resolver).
5. **Windows process management:** N concurrent `claude` CLI children —
   validate spawn, kill, and timeout behavior on Windows before committing
   to the design.

## Prerequisites already in flight

- The Sessions notification center (approved) gives per-worker observability.
- The skill-effectiveness plan gives the learning loop real outcome data —
  the orchestrator's "which skills does a worker get" question depends on it.

## Next step

Run `/superpowers:brainstorming` on this brief once the two prerequisite
plans have shipped and soaked long enough to produce data.
