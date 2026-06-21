# MIND HIVE

Deterministic, event-sourced memory for long-running Claude sessions.
Append events; deterministic projections rebuild everything. Nothing is hand-edited.

## Commands
- `hive append <type> <payload> [--json] [--stdin] [--tags a,b]` — add an event
- `hive project` — rebuild STATE.md + LEDGER/ + index.json + chain.json
- `hive state` — print the live STATE.md
- `hive recall "<query>" [--semantic]` — grep (default) or vector search
- `hive graph export [--out path] [--semantic] [--open]` — write a standalone, offline graph.html
- `hive graph app` — open the desktop graph window (needs `pip install -e .[app]`)
- `hive export [--out path]` — write a portable handoff.md (zero API tokens)
- `hive verify` — check the hash chain
- `hive embed [--force]` — build the on-demand vector index
- `hive selftest` — run the test suite

## Event types
Semantic (you/Claude emit): decision, experiment_result, caveat, next_step,
open_question, state_set.
Deterministic (hooks emit): file_touched, command_run, git_diff, session_start,
session_end, compaction.

## PowerShell 5.1 note
Inline JSON args are mangled by PowerShell 5.1. To set state from PS, pipe via stdin:

```powershell
'{"task":"OFI verdict","next_step":"recapture 24h"}' | hive append state_set --json --stdin
```

(Claude in-session uses the Bash tool, where normal quoting works.)

## V17 safety
`embed` and `recall --semantic` refuse to run while `~/.mindhive/LIVE_LOCK`
exists (V17 is trading). Override with `--force` only when the box is idle.

## Usage-cap fallback
When the Opus window is nearly spent, run `hive export` and paste handoff.md into
any model/client. It is deterministic and costs no API tokens.

## Desktop graph app

`hive graph export` writes a single self-contained `graph.html` (data + renderer
inlined, no network, no keys) you can open in any browser on any PC. It rebuilds
the graph from your current events every time, so the view is never stale — no
need to run `hive graph build` first.

`hive graph app` opens the same always-fresh view in a native window
(`pip install -e .[app]`).

To build a double-click binary for your machine:

    pip install -e .[app,build]
    python build/build_app.py        # -> dist/MindHiveGraph(.exe)

PyInstaller builds for the OS it runs on. The window/exe use `build/icon.ico`.
Built size is ~13 MB (Windows WebView2 is shared, not bundled).
