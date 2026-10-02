# Recovery runbook

What to do when part of a MIND HIVE store goes wrong: for each failure, how you
notice it, the exact commands, and how to confirm the fix. It documents what the
code does. Where the blueprint spec (§9.4) describes a mechanism the code does
not have, the difference is stated in place and collected at the end.

## The one rule

The event ledger — `events/*.jsonl` in each namespace — is the **only source of
truth**. Everything else is a projection a command rebuilds from it: `STATE.md`,
`LEDGER/`, `index.json`, `chain.json`, `MEMORY.md`, the chroma index, the
Postgres mirror. So recovery is almost always "rebuild the projection".

**Never edit or delete a ledger row.** The ledger is append-only and
hash-chained; an edited row breaks the chain and shows up in the Postgres mirror
as divergence. A correction is a new event, written with `hive append`.

## Where things are

`<store>` is `$MINDHIVE_HOME`, `~/.mindhive` by default. `<ns>` is
`<store>/<project>`, where `<project>` is the namespace (`MINDHIVE_PROJECT`, or
the one detected from the git checkout — `hive projects` lists them).

| path | what it is | rebuilt by |
|---|---|---|
| `<ns>/events/*.jsonl` | the ledger | nothing: it is the source |
| `<ns>/chain.json` | the hash-chain checkpoint (v2: a per-segment count of sealed lines) | `hive project` (re-seals — see scenario 3 first) |
| `<ns>/STATE.md`, `<ns>/LEDGER/`, `<ns>/index.json` | projections | `hive project` |
| `MEMORY.md` (the native memory folder) | the auto-loaded digest | `hive sync` |
| `<ns>/chroma/` | the vector index (three collections, docs/DATA_MODEL.md §4.19) | `hive embed`, the embedding watcher |
| `<temp>/mindhive-chroma-read-<pid>-*` | a reader's private copy of `<ns>/chroma/`: every semantic read answers from one, because chromadb writes to any index it opens | nothing: deleted when its reader is done; a copy of a process that is gone is swept by the next copy taken |
| `<ns>/state/pending_embed.json` | the embed queue `append_event` writes | drained by the watcher |
| `<ns>/state/processing_embed_<ns-time>.json` | a watcher's claim on the queue | `hive watcher recover` |
| the Postgres mirror | a one-way copy of the ledger | `hive pg refresh`, `hive pg rebuild`, `hive db migrate` |
| `<store>/LIVE_LOCK` | "the V17 trader is live": `hive embed` and `hive recall --semantic` refuse while it exists | nothing; `--force` overrides it, only when the box is idle |

The commands below are the same in PowerShell 5.1 and a POSIX shell except
where both spellings are given.

## Scenario 1 — the chroma index is corrupt or unusable

**How you notice.** A semantic read that fails says so; it never answers empty.

- `hive recall "<query>" --semantic` exits 1 with `error: semantic <stage>
  failed on the '<collection>' index of project '<ns>': <error> (<repair>)`.
  The stage — `snapshot`, `open`, `count`, `encode` or `query` — is the step
  that broke, and the parenthesis is the repair for that damage
  (docs/DATA_MODEL.md §4.19 has the table). Exit 2 is a refusal instead —
  `LIVE_LOCK`, or the `vectors` extra missing — and not this scenario.
- The dashboard's recall view paints `Recall failed: ` and the same message. In
  HIVE MIND view a namespace whose index could not be read shows its error
  beside the other namespaces' hits.
- The graph (`hive graph export --semantic`, the dashboard's
  `/api/graph?semantic=1`) shows a `Semantic links unavailable: …` banner and
  no semantic edges.
- `hive bench recall-semantic` shows the semantic row `unavailable` with the
  same message, or `FAILED` when a timed query raised.
- In a process that stays up — the dashboard, `hive ui` — the same failure
  again ends in `[… it is not read again until it changes or 600s pass]`: the
  failed read is remembered against the bytes it happened on instead of being
  repeated. `… until the process restarts` means 16 failed reads are already
  held in that process.

`snapshot` (a busy writer, a temp directory that is full or not writable) and
`encode` (the embedding model) are not damage to the index, and neither is a
message that says chromadb refused the read's arguments: follow the message,
and nothing below applies.

**Commands.** Follow the repair the message names. By stage:

1. `count` or `query` on `ledger` or `events`, or an `open` refused for a cut
   segment of either (`DamagedSegment`; the message names the segment's
   collection, whichever one was read): drop and refill both collections
   recall reads. The rebuild drops a collection without reading it, which
   mends this damage; if the read still fails, do step 2:

       hive embed --rebuild --project <project>

2. Any other `open` — chromadb cannot open the files, and `hive embed
   --rebuild` opens them the same way — or any stage on the watcher's
   `mindhive_<slug>` collection, a cut segment of it included: delete the
   directory and embed from scratch. First stop everything that WRITES the
   index: the dashboard (`hive dashboard`) or `hive ui` (their background
   watcher thread keeps `chroma.sqlite3` open, which on Windows makes the
   directory undeletable), and any `hive watcher run`. Readers need not stop:
   each holds its own copy in the temp directory. Then:

       rm -rf "${MINDHIVE_HOME:-$HOME/.mindhive}/<project>/chroma"          # POSIX
       Remove-Item -Recurse -Force "$env:USERPROFILE\.mindhive\<project>\chroma"   # PowerShell, default store root

       hive embed --project <project>

3. Return any claim a dead watcher left behind and drain the queue:

       hive watcher recover
       hive watcher run --once

Pass `--project` the namespace exactly as the message prints it.
`MINDHIVE_PROJECT` would be normalised on the way in (`my_proj` becomes
`my-proj`), and the repair would run on another namespace.

**How you confirm it.** `hive embed` prints how many files and events it indexed.
`hive recall "<query>" --semantic` exits 0 with event-level hits (a type, a
timestamp, a session) again. `hive bench recall-semantic` measures the semantic
row instead of reporting it unavailable. `hive watcher status` reads
`pending 0 row(s), processing 0 marker(s)`. A dashboard that stayed up reads the
repaired index once its bytes have changed; one whose error said `until the
process restarts` needs a restart first.

**What differs from spec §9.4.**

- The spec deletes `~/.mindhive//chroma/`, with the project segment empty. The
  index is per namespace: `<store>/<project>/chroma/`.
- The spec runs `hive embed --force --rebuild`. `--force` does not force a
  rebuild: it only overrides `LIVE_LOCK` (`guard.ensure_safe`). `--rebuild` is
  what drops and refills the collections. It opens the index to do it, so it
  repairs nothing chromadb cannot open — measured, it failed on a
  `chroma.sqlite3` that is not a database and on a collection configuration
  chromadb could not deserialize — but it drops a collection without reading
  it, which mended every damaged `ledger` or `events` collection measured
  that chromadb could open (docs/DATA_MODEL.md §4.19). After the directory is
  deleted, neither flag is needed.
- The spec says the index "recovers 100% in seconds". A rebuild re-encodes every
  semantic event with a local model. On a large ledger that takes minutes, not
  seconds: P8's bench measured the cold model load alone at 8 to 67 s on this
  machine, and `hive pg embed` measured 664 vectors in 47.9 s.
- `hive embed` rebuilds the `ledger` and `events` collections, which are what
  `hive recall --semantic` reads. The watcher writes a third collection,
  `mindhive_<slug>`. No command backfills that collection from the ledger, and
  no search reads it — only the watcher's own id probe does
  (docs/DATA_MODEL.md §4.19). Deleting `chroma/` loses it, and only rows queued
  after that point refill it.

## Scenario 2 — the embed queue is stuck

**How you notice.** `hive watcher status` (or `--json`: `{"markers": [...],
"pending": N, "processing": M}`) keeps showing the same `processing_embed_*.json`
claim. A growing `pending` count while no dashboard is open is not a fault. It
is what a week with the dashboard closed looks like, and the command exits 0
either way.

**Why it happens.** A watcher claims the queue by renaming
`pending_embed.json` to `processing_embed_<time>.json`. Two things leave that
claim on disk. The process can die mid-embed. Or the embed can fail — for
example because the `vectors` extra is missing — and a failed embed leaves its
claim on disk by design, so nothing is lost.

**Commands.**

1. If the failure is environmental, fix it first. For the missing extra that is
   `pip install -e .[vectors]`.
2. Requeue claims older than 600 s. A younger claim belongs to a watcher that
   is still working and is left alone. Each claim is reconciled against the
   index, and only what the index does not already hold is requeued:

       hive watcher recover
       hive watcher recover --stale 60      # a shorter age, when you know no watcher is running

3. Drain:

       hive watcher run --once

**How you confirm it.** `hive watcher status` reads
`pending 0 row(s), processing 0 marker(s)`, and the run appended
`vector_sync_started` and `vector_sync_completed` rows (`{marker, count,
duration_s}`).

## Scenario 3 — `hive verify` reports the hash chain broken

**How you notice.**

- `hive verify` prints `{"ok": false, "reason": ...}`. The default exits 0 and
  reports on stdout; `hive verify --strict` exits 1, which is the form for a
  script.
- `hive db status` shows `chain_ok: false`.
- `hive ci run` goes red at its fourth gate.

**Stop first.** Do **not** run `hive project` yet. It rewrites `chain.json` over
whatever is on disk now, so it would seal the damage as the new baseline and the
evidence would be gone.

**Read the verdict.**

    hive verify --json

| `reason` | meaning |
|---|---|
| `hash_mismatch` with `truncated: [...]` | sealed lines are missing: a named segment was deleted or is shorter than the checkpoint sealed |
| `hash_mismatch` without `truncated` | a sealed line's bytes changed (a hand edit, or a restore from an older copy) |
| `corrupt_chain_file` | `chain.json` itself is unreadable, or claims v2 without a `segments` map |
| `no chain.json (run: hive project)` | the namespace was never checkpointed; nothing is broken |
| `unsupported_checkpoint_version` | a newer build wrote the checkpoint; unverifiable by this one, not corrupt |
| `read_error`, `corrupt_event_log` | the ledger could not be read (`corrupt_event_log` is the v1 path) |
| `ok: true` with `ahead: N` | normal: N rows were appended since the last `hive project` |

`corrupt_rows` is reported beside the verdict: torn or malformed rows
(`{segment, line, reason, preview}`) that every reader skips. Note that `line`
is **zero-based**, so `sed -n 40p` shows the row reported as 39. Seven
historical rows on the main store are reported permanently, on purpose (see
`events.corrupt_rows`). A count that *grows* means an unserialised writer
exists again.

**Commands.**

1. Find out what changed. The ledger is plain JSONL. Compare the named segment
   against a backup or against the Postgres mirror (`hive pg parity`); if it is
   still consistent, the mirror holds the rows as they were loaded.
2. Restore the original bytes if you have them (a backup, version control of
   the store). If the checkpoint still describes the restored disk,
   `hive verify` is green again.
3. If the original cannot be restored, record what happened as a new event,
   then accept the current disk as the new baseline, knowingly:

       hive append caveat "chain re-sealed after <what happened>; original bytes of <segment> lost"
       hive project

**How you confirm it.** `hive verify --strict` exits 0.

**What differs from spec §9.4.**

- The spec says `hive verify --json` identifies "the exact line where the chain
  broke". It does not. It names truncated segments and corrupt rows. For a
  changed line it reports only that the sealed prefix's head no longer matches
  (`stored` against `actual`).
- The spec offers "hash chain reconciliation through chain.py". There is no
  reconciliation command or function. `hive project` (via `chain.write_chain`)
  re-anchors the checkpoint over the current disk. That accepts the damage; it
  does not repair it. A compensating event (`hive append`) records the
  correction, as the spec also says.

## Scenario 4 — a lock looks stuck

**How you notice.** A command waits, or reports a lock timeout. A harvest
silently does nothing because another holds `harvest.lock`. A `hive ci watch`
pass prints `skipped (another watch pass holds ...)` and does nothing, because
another pass holds `ci_watch.lock`. Past 12 hours it prints
`could not check -- ... it looks stuck` and exits 2 instead: find the process
by the pid it names and stop it if it is wedged.

**What to know before deleting anything.** Every lock recovers on its own. A lock
is either released by the kernel when its holder dies, or taken over once it is
older than its stale age. Deleting a lock file by hand is only ever safe when you
know its holder is dead, and even then waiting does the same thing. First look
for a live holder: a running `python`, `hive dashboard` or `hive ui` in Task
Manager, or `ps aux | grep mindhive`. Stop it if it is wedged.

| lock | where | kind | when its holder dies |
|---|---|---|---|
| `append.lock` | `<ns>/` | a kernel byte-range lock (`msvcrt.locking` / `fcntl.flock`) on a file that stays on disk **by design** | the OS releases it with the process. A waiter tries for 10 s (`events._LOCK_WAIT_S`) and then appends **unlocked**, rather than lose the event. There is no stale age because none is needed; deleting the file achieves nothing |
| `harvest.lock` | `<store>/` | a kernel byte-range lock, exactly as `append.lock`, on a file that stays on disk **by design** | the OS releases it with the process. A second harvester skips its pass rather than waiting. There is no stale age: a leftover file from a dead harvester blocks nothing, and deleting the file achieves nothing |
| `ci_watch.lock` | `<ns>/state/` | a kernel byte-range lock, as `harvest.lock`, on a file that stays on disk **by design**; its first 64 bytes name the holder and since when, which the skip prints (`type` and editors cannot read the file while it is held, since they read past them) | the OS releases it with the process. A second watch pass skips rather than waiting (exit 0), naming the holder's pid and how long it has held it; past 12 hours that is an error (exit 2) and still no pass. There is no stale age. Never delete the file: Windows refuses while it is held, and on POSIX a new file would let a second pass run beside the live one |
| `chroma.lock` | `<ns>/` | `core.locks.file_lock` | waits up to 30 s; older than 600 s it is taken over (a cold model load plus a first index really takes minutes) |
| `.tasktool.lock` | `<ns>/` | `tasktool.task_lock` | 15 s timeout; older than 60 s it is stolen; a Windows delete-pending `PermissionError` counts as contention (20 spins) |
| `task_tools.json.lock` | beside the task sidecar | exclusive-create file | waits 2 s, then writes unlocked; older than 30 s it is stale |
| `.worksession.lock` | `<ns>/` | `core.locks` | 15 s timeout; older than 10 s it is stale |
| `.storage.lock` | `<ns>/` | `core.locks` | 5 s timeout; older than 60 s it is stale |
| `integration_breaker.lock` | `<ns>/state/` | `core.locks`, fail-open | 5 s timeout; older than 60 s it is stale |
| `ci_premerge.json.lock` | `<ns>/state/` | `core.locks` | 10 s timeout, then that write to `ci_premerge.json` is not made and the sidecar keeps what it had; older than 60 s with its holder's process gone it is stale |
| `rules/incidents/.write.lock` | `<ns>/` | exclusive-create file | waits 30 s, then writes unlocked; older than 60 s it is stolen |
| `rules/.reflect.lock` | `<ns>/` | the reflection gate | older than 600 s it is stale |
| any other `core.locks` lock | | `core.locks.LockPolicy` defaults | 2 s timeout, 10–50 ms jittered backoff; older than 30 s with its holder's process gone it is stale (the file carries `<pid> <clock>`; a live holder keeps it at any age, and a pid the OS reused for an unrelated live process holds it until that process exits or the file is removed by hand) |

**Commands.** Usually none: wait out the stale age. For abandoned embed claims,
use `hive watcher recover` (scenario 2). None of these ages is configurable.
There is deliberately no `MINDHIVE_LOCK_*` variable: the values are code
constants (blueprint decision K4). The `task_lock` ones (`_LOCK_TIMEOUT_SECS`,
`_STALE_LOCK_AGE_SECS`, `_TASK_LOCK_DENIED_SPINS` and the retry backoff) are
pinned against the MEB's appendix 10.1 by `tests/test_constants_registry.py`.

**How you confirm it.** Re-run the command that waited. For a harvest, run
`hive harvest --catch-up`.

**What differs from spec §9.4.** The spec describes one rule for all locks:
"delete the leftover lock file", and stale-lock stealing "after 60 seconds". In
the code:

- `append.lock` has no staleness because it is a kernel lock. Its file is meant
  to stay on disk.
- `harvest.lock` has no staleness either, since 2026-09-29. It was an
  exclusive-create file that went stale at 120 s, and a pass over the live
  store can outlive that, so the scheduled task could take a live harvester's
  lock mid-pass and walk the same cursor window, which appended the same rows
  twice, byte for byte (`harvest.harvest_lock` tells the whole story).
- `ci_watch.lock` is a kernel lock for the same reason (2026-10-02): a watch
  pass has no bound, one full run is most of an hour, so any stale age would
  let a second pass test the same heads beside a live one. Its age decides only
  how a skip is reported: past 12 hours, as an error.
- 60 s is the stale age of `.tasktool.lock`, `.storage.lock`,
  `integration_breaker.lock`, `ci_premerge.json.lock` and the incidents write
  lock.
- Every other lock has its own age, as in the table above.

## Scenario 5 — the Postgres mirror disagrees with the ledger

This applies only when the mirror is in use (`MINDHIVE_PG_DSN_ADMIN`,
`pip install -e .[pg]`). The mirror is a read-only copy: nothing is lost with
it, and the file ledger stays the writer under every `MINDHIVE_EVENT_BACKEND`
value.

**How you notice.**

- `hive pg parity` exits non-zero. It exits non-zero when it cannot *prove*
  parity, including when it could not look at all.
- `hive pg status` exits non-zero.
- A tool envelope's `freshness` says `parity_ok: false`.

**Commands.**

    hive pg status
    hive pg refresh                  # reload only the segments whose bytes moved, then re-check parity
    hive pg rebuild --project <p>    # reload every segment of one namespace, in one transaction
    hive pg embed                    # afterwards, if meaning search is used; incremental

`hive db migrate` is the other route in: an event-by-event replay into
`mindhive.events` with the hash chain verified as it goes.

    hive db status
    hive db migrate --dry-run
    hive db migrate

`hive db migrate` refuses to start when `chain.json` disagrees with the disk
(exit 1). If it refuses, resolve scenario 3 first. Exit 2 means the environment
stopped it: no DSN, no driver, or the server is unreachable.

**How you confirm it.** `hive pg parity` exits 0. `hive db status` shows the
checkpoint `ok` and `mirrored_sessions` equal to `sessions`.

## Scenario 6 — projections are missing or stale

**How you notice.** `hive state` prints nothing current. `LEDGER/` or
`index.json` is missing. The dashboard shows an empty namespace that has events.
Or `hive verify` says `no chain.json (run: hive project)`.

**Commands.**

    hive verify          # FIRST: know the chain's state before re-sealing it
    hive project         # STATE.md, LEDGER/, index.json, chain.json
    hive sync            # the MEMORY.md digest the next session auto-loads
    hive briefing        # BRIEFING.md

**How you confirm it.** `hive state` shows the current record. `hive verify`
reads `ok: true`. `hive context --check` exits 0.

## Mechanisms spec §9.4 names that the code does not have

| spec §9.4 says | the code |
|---|---|
| `rm -rf ~/.mindhive//chroma/` | the index is `<store>/<project>/chroma/`, one per namespace |
| `hive embed --force --rebuild` rebuilds the index | `--rebuild` drops and refills; `--force` only overrides `LIVE_LOCK` |
| the index recovers "in seconds" | minutes on a large ledger; a cold model load alone was measured at 8–67 s |
| stale-lock stealing after 60 s | per lock (table in scenario 4); `append.lock` is a kernel lock with no staleness rule |
| delete the leftover lock file | `append.lock`'s file stays by design; the others expire on their own |
| `hive verify --json` names the exact broken line | it names truncated segments and corrupt rows (zero-based `line`), not the first changed line |
| hash-chain reconciliation through `chain.py` | no such command; `hive project` re-seals over the current disk, which accepts rather than repairs |
| the rebuilt vector index is what semantic recall uses | `hive recall --semantic` reads the `events` and `ledger` collections `hive embed` writes; no search reads the watcher's `mindhive_<slug>` collection (only its own id probe does), and nothing backfills it |
