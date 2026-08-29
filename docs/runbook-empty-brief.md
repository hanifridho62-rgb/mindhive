# Runbook — an orchestrated agent was briefed with nothing

An agent came back saying it did not know where anything was, or its transcript
shows it hunting for the skill catalog. The briefing is supposed to prevent
exactly that. Five checks, cheapest first. Every one of them is a normal
condition, not a bug — which is why the answer is never "the brief is broken"
until you have run all five.

Start here, because it answers four of the five in one line:

```bash
hive mindnet brief <task-id> --verbose
```

## 1. Is the brief switched on?

The command says so at the top when it is not:

```
mindnet brief: switched OFF — MINDHIVE_BRIEF is set to a false value
```

`MINDHIVE_BRIEF` defaults to ON and is off only for `0`, `off`, `false`, `no`.
Unset it, or set it to anything else.

## 2. Is a source *gated* rather than empty?

Look for `gated=` in the counts line:

```
lint-1 [audit-code]  skills=3  sops=0  constraints=0  symbols=0  gated=symbols
```

`gated=symbols` means nobody switched the symbol index on — `MINDHIVE_SYMBOLS`
is unset, which is the default. `symbols=0` on its own would mean the index ran
and found nothing. These need opposite fixes, which is the entire reason the two
are reported separately.

To turn it on: set `MINDHIVE_SYMBOLS=1`, and set `MINDHIVE_SYMBOLS_ROOT` if the
tree to index is not the one the orchestrator resolves.

## 3. Do the task's own words match anything?

`advice` matches on `title + description` and needs at least
`advice.MIN_OVERLAP` (2) overlapping keywords. A task titled `fix it` matches
nothing, and no amount of configuration changes that.

```bash
hive mindnet show <task-id>
```

If the description is empty or generic, that is the finding. Re-declare it with
words that name the subsystem and the work:

```bash
hive mindnet declare <task-id> --title "..." --description "..."
```

## 4. Does the project actually have anything recorded?

`sops=0` and `constraints=0` are honest answers on a young ledger.

```bash
hive sop list
hive recall "<the task's subject>" --semantic
```

Nothing recorded is nothing to brief with. This is not a failure of the
composer.

## 5. Is the sidecar carrying the phase?

The brief's phase line comes from the sidecar's phase records, not from
`task.phase`. A stale sidecar is still exact about every task it holds, so this
shows up as a phase that is behind, never as an empty brief:

```bash
hive mindnet rebuild
```

## What the telemetry records

Every composed brief appends one `mindnet_brief` event carrying the same counts
the command prints, plus `empty_reason`. To see what a past run actually handed
its agents:

```bash
hive pg tool search_events_by_type --arg event_type=mindnet_brief
```

The brief's full text is not in that row — it is written to the run log under
the task's key plus `-brief`, so it does not put kilobytes per task on the hash
chain that `hive verify` re-reads in full.

## What this runbook cannot tell you

Whether the briefing **helped**. That needs an A/B over real runs and nobody has
measured it — TODOS 23. The counts prove the brief arrived, and that is all they
prove.
