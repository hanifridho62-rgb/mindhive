# Interactive session rotation: the four options, and which one is already built

**Written 2026-08-14.** Closes the third of the five gaps the five-surfaces plan
left open (`docs/plans/2026-08-14-001-feat-close-five-gaps-plan.md`, U3). The gap
was never missing code. It was a choice nobody had written down, which meant the
choice kept getting re-argued and the fact that it was already made kept getting
lost.

## The finding, first

**The option this evaluation selects already ships end to end.** `hive session run`
launches an interactive session under smart-zone rotation, and
`rotation/supervisor.py`'s `run()` closes the loop:

| Step | Where |
|---|---|
| clear a stale artifact *before* spawning (so a crashed run cannot double-spawn) | `supervisor.py:226` |
| spawn the CLI with `MINDHIVE_ROTATION=1` and the supervised marker | `supervisor.py:229`, env built at `:104-127` |
| the Stop hook measures occupancy each turn and writes `ROTATION_PENDING` past the boundary | `hooks/stop.ps1` → `rotation/hook.py` |
| read the artifact; `None` means the user simply quit | `supervisor.py:243` |
| clear it, check the spin guard, reseed a successor, relaunch | `supervisor.py:246-257` |

So the honest description of the gap is: the mechanism was complete and the
writing about it never happened. This document is the missing half, not a
specification for work.

The one link no human has exercised is the 30-second terminal check in
`scripts/rotation/MANUAL_TUI_CHECK.md` — whether a real rotation *looks* right in
a real terminal, which no automated tier can answer. That is why the roadmap card
`smart-zone-interactive-rotation` is archived **without a work link**: attaching
one would let the retire sweep mark that human step done on the strength of a
merged pull request.

## What has to hold, whatever is chosen

Two properties of the Stop hook are load-bearing across every option below, and
no option here touches either:

* **Fail-open.** Any exception, unparseable payload, or unreadable transcript
  returns `None` and prints nothing. The hook fires in *every* Claude Code session
  on this machine, in every project, so a bug in it does not break one feature —
  it breaks all work everywhere, from inside the tool you would use to fix it.
* **Opt-in.** The hook does nothing unless `MINDHIVE_ROTATION` is set, and
  `supervisor._child_env` is the only thing that sets it. Registering the hook and
  switching it on are deliberately separate steps.

And one rule that is a trap rather than a preference: **the hook must never emit
`{"decision": "block"}`**. For a Stop hook, `block` means *do not stop — keep
working*, which would pin a full session open instead of rotating it. Rotation is
signalled by the `ROTATION_PENDING` artifact precisely because a Stop hook cannot
end a session at all.

`MINDHIVE_ROTATION_SUPERVISED` is **not** a second enable switch and must not
become one. It means "a supervisor is actually waiting on you", it is set only in
`_child_env` (`supervisor.py:126`), and all it changes is which advice the hook
emits. With `MINDHIVE_ROTATION` unset the hook stays silent whatever that marker
says.

## Option 1 — supervisor always-on

Make every interactive session a supervised one: rotation on by default, with an
opt-out.

* **Cost.** Low in code (the supervisor exists), high in blast radius. Every
  session on the machine would inherit a wrapper process, and the Stop hook would
  go from opt-in to effectively always-live.
* **Blast radius.** Machine-wide. A defect in the wrapper reaches every project,
  including the one you would use to fix the wrapper.
* **Evidence it can produce.** Real rotation counts from ordinary daily use — the
  richest evidence of the four, and the only option that would produce it without
  anyone deciding to run something.
* **Verdict: rejected.** It contradicts the opt-in property directly, and the
  project instructions name "active-by-default for convenience" as the specific
  thing not to do. The evidence is worth wanting; not at that price.

## Option 2 — a launcher shim

A thin command that switches rotation on and launches `claude`, so a user gets a
rotating session without knowing about the supervisor.

* **Cost.** A name, a module, a test file.
* **Blast radius.** Small.
* **Evidence it can produce.** None that `hive session run` does not already
  produce.
* **Verdict: selected — and already satisfied.** `hive session run` *is* this
  option: it detects the project from cwd, carries the spin guard, and consumes
  the artifact. Building a second command that wraps it would add three files for
  zero new behaviour.

  **The sub-variant is worse than doing nothing, and this is the reason to write
  the option down rather than merely not choose it.** "Set `MINDHIVE_ROTATION` and
  exec plain `claude`" looks like the cheap version of the same idea. It is not:
  the hook then measures occupancy, writes the artifact, and advises — while
  nothing reads the artifact and nothing relaunches. The session just ends. The
  hook's own advice already says so plainly when the supervised marker is absent,
  which is exactly why that marker must stay a *description*, never a switch.

## Option 3 — signal plus handoff

Leave the session alone; have the hook write a handoff and notify the user, who
starts the successor themselves.

* **Cost.** Low. Most of it exists — `default_reseed` (`supervisor.py:176`)
  already builds the successor's opening context out of a consolidated ledger and
  the active roadmap's ask.
* **Blast radius.** Minimal: nothing spawns anything.
* **Evidence it can produce.** Weak and unfalsifiable — whether a human acted on
  the notice is not observable from the ledger, so "rotation works" would rest on
  self-report.
* **Verdict: recorded, not built.** It is a strictly weaker version of what
  already ships, and its evidence is the kind this project has spent five surfaces
  learning not to accept.

## Option 4 — state the limit as the result

Ship no rotation change; write down that rotation works end to end except for one
human-only step, and stop claiming more.

* **Cost.** This document.
* **Blast radius.** None.
* **Evidence it can produce.** The strongest available for the part that is
  automated, and an honest gap for the part that is not.
* **Verdict: adopted alongside option 2.** Nothing is built; what exists is
  described accurately, and the unproven link is named instead of being papered
  over by a merged PR.

## What is deliberately still open

**Discoverability.** `hive session run` exists and nothing tells anyone about it.
The obvious fix — advertise it from the prompt path — adds work to a path this
plan's U6 has just finished measuring and found already over its stated budget
(699 ms end to end, min of 5, against a "~0.4 s" note). Deferred on purpose,
recorded here so the next reader does not mistake silence for oversight.
