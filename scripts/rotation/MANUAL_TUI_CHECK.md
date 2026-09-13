# Manual TUI check — the one gate a script cannot pass

Everything else about smart-zone rotation is verified automatically:

| Gate | Covers | Run it |
|---|---|---|
| `pytest` | logic, totality, boundaries | `py -m pytest tests/test_rotation_*.py -q` |
| `empirical_gate.py` | real shim, real binary, real hook→artifact→supervisor chain | `py scripts/rotation/empirical_gate.py` |
| `real_session_gate.py` | real 23.6 MB transcript from a real Opus-5 session | `py scripts/rotation/real_session_gate.py` |

Two things none of them prove, because both need a terminal with a human at it:

1. **The TUI renders as a child process** — full-screen redraw, colours, keys.
2. **Ctrl+C quits cleanly** instead of relaunching.

An automated harness cannot check either. A pseudo-terminal would only prove
that *a* PTY works, not that the real console handoff does, and "the process
exited 0" says nothing about whether the screen was legible. So this is a
30-second manual procedure rather than a green check that means less than it
looks.

## Prerequisites

This branch must be **merged to master** first. Before then, `python -m mindhive`
resolves to the main checkout, which has no `rotation` package and no `stop`
stage — the hook is silent and rotation never fires. That is by design (the shim
guards for exactly this), but it also means the check below would trivially
"pass" while testing nothing.

Then register the hook, if it is not already. From `cmd`, the shell the rest of
this check uses:

```
hive session install-hook "%USERPROFILE%\.claude\settings.json"
```

## The check

Open a **real terminal** — not an IDE panel, not an agent session.

```
set MINDHIVE_ROTATION_CAP=2000
hive session run
```

The low cap makes the very first turn cross the boundary, so you do not have to
fill a real context window to see a rotation.

### 1. Does the TUI render? (~10 seconds)

You should be looking at an ordinary Claude Code session. Check:

- [ ] The input box draws at the bottom and redraws as you type
- [ ] Colours and styling appear (not raw escape codes like `←[38;5;…`)
- [ ] Arrow keys, backspace and Ctrl+U behave normally
- [ ] Resizing the window reflows the display

If any of these fail, the inherited-stdio approach is wrong on your terminal —
capture what you see and reopen the PR. **Do not** "fix" it by adding pipes;
piping is what breaks TUIs.

### 2. Does it rotate? (~20 seconds)

Send any short prompt, e.g. `hello`. When the turn ends the Stop hook measures
the session, finds it past the 2000-token cap, and writes the artifact.

- [ ] The session tells you it is wrapping up and a successor is queued
- [ ] The session ends
- [ ] A **new** session starts by itself
- [ ] The new session opens knowing what the last one was doing — not blank

Confirm the supervisor saw it:

```
type %USERPROFILE%\.mindhive\<project>\ROTATION_PENDING
```

(Absent after a successful rotation — the supervisor clears it once consumed.
Seeing it *linger* means the supervisor never picked it up.)

### 3. Does Ctrl+C quit? (~5 seconds)

Press **Ctrl+C**.

- [ ] Everything exits — no new session spawns
- [ ] You are back at your shell prompt

A relaunch here is a bug: it makes the loop unquittable. Report it.

## Cleanup

```
set MINDHIVE_ROTATION_CAP=
```

Without it the threshold returns to `min(500_000, 0.5 × window)` — 500,000
tokens on any current Opus or Sonnet.

## If you want to turn it off entirely

The hook is inert unless `MINDHIVE_ROTATION` is set, and only `hive session run`
sets it — so a plain `claude` session is unaffected whether or not the hook is
registered. To remove it outright, delete the `"Stop"` block from
`~/.claude/settings.json`; a backup from installation sits beside it as
`settings.json.bak-*`.
