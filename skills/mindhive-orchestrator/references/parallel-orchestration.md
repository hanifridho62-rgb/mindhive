# Parallel Multi-Agent Orchestration — the "like Fable 5" upper layer

At the **execution stage** (stage 8), the orchestrator stops working one file at
a time and fans work out to **parallel sub-agents**. An upper orchestration layer
classifies the codebase deterministically, then dispatches one sub-agent per
**lane** — a set of whole directory-clusters no other lane owns, so two agents
never edit the same file.

The classification is done by MindHive's deterministic planner
(`src/mindhive/clusters.py`, exposed as `hive clusters …`). The **dispatch** is
done by the agent runtime under `superpowers:dispatching-parallel-agents`. Python
plans; the agent executes — Python never spawns agents.

## Step 1 — build the cluster database

```bash
python -m mindhive clusters build            # writes cluster_db.json (a sidecar)
python -m mindhive clusters plan --lanes N   # prints the shard plan for N lanes
```

- **cluster database** — every source file grouped by its directory into a
  cluster (the ownership unit). Derived data; stored in an overwrite-in-place
  `cluster_db.json` sidecar, never the ledger.
- **file complexity** — each file scored from static metrics (LOC + nesting
  depth + a branch/def proxy) into a `low`/`med`/`high` tier; `--git` adds churn
  weighting when git is available.
- **shard plan** — clusters packed heaviest-first into `N` lanes so aggregate
  complexity is balanced, with the invariant that **no file appears in two
  lanes**. `--lanes` is clamped to `[1, number_of_clusters]`; a non-numeric value
  is rejected.

Choose `N` from the work-set: default to one lane per independent cluster the
kanban touches, capped by a sane parallelism ceiling (the runtime caps concurrent
sub-agents anyway). When the touched work is a single cluster, or git is
unavailable and the tree is small, **skip the fan-out and execute serially** —
parallelism has overhead that only pays off across independent modules.

## Step 2 — dispatch one sub-agent per lane

Read the lanes from `hive clusters plan`. For each lane, dispatch a sub-agent
(the `Agent`/Task tool) under `superpowers:dispatching-parallel-agents` and, for
plan-unit execution, `superpowers:subagent-driven-development`. Give each
sub-agent:

- the plan path and the specific kanban units whose `Files:` fall inside this
  lane's clusters;
- an explicit **file-ownership boundary**: "you own exactly these files/clusters;
  do not edit files outside them";
- worktree isolation (`isolation: "worktree"`) when agents may write
  concurrently, so the git index and test runs don't collide.

The lane partition already guarantees disjoint files, so lanes run truly in
parallel. Dependent units (a lane that must land after another) run in the next
batch, not the same one.

## Step 3 — integrate and gate

After a batch completes:

1. Merge/collect each lane's work in dependency order. On a merge conflict, abort
   and re-run that unit serially against the merged tree — never hand-pick a side.
2. Run the relevant tests after each integration.
3. Proceed to stage 9 (quality assessment) on the integrated tree — the parallel
   lanes do not skip the quality gate.

## Optional — feed outcomes back (deferred)

Wiring each lane's success/failure back as `invoke_result` / `tool_failed`
(`skill:<name>`) events would light up the dashboard's green/red and the
Improvements loop, exactly as the `hive skills invoke` channel does. That is
deferred follow-up work; the shard planner and dispatch protocol above stand on
their own without it.

## Safety

- The partition invariant (no shared files) is the core race-safety guarantee —
  keep it. Never manually reassign a file from one lane to another.
- Respect the V17 `~/.mindhive/LIVE_LOCK` guard and any loop safeguards a chained
  skill enforces — the parallel layer never overrides them.
