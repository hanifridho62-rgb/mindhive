# Handoff Protocol

The orchestrator monitors context consumption and offers a clean handoff to a
fresh session at roughly the halfway mark, so long runs don't degrade or hit an
abrupt compaction mid-task.

## When to check

Check at **every phase boundary** (Phase 1→2, 2→3, 3→4), never mid-question.
Phase boundaries are cheap, natural pause points; interrupting an interview
question to talk about context would be worse than the problem.

## Heuristic signals (there is no exact token meter in-session)

Estimate consumption from observable proxies. Treat the estimate as crossing
~50% when several of these are true:

- Many tool calls have accumulated this session (dozens of reads/edits/greps).
- Several large files or reference docs have been read in full.
- Multiple pipeline phases are complete and the transcript is long.
- The harness has emitted a compaction or "context is getting long" notice —
  this is the **hard backstop**; treat it as ≥50% regardless of the proxies.

The existing PreCompact hook still fires independently; this protocol is a
courtesy checkpoint that hands off *before* an abrupt compaction, not a
replacement for the hook.

## The alert (ask, don't force)

When the estimate crosses the threshold, halt and say, in substance:

> Heads up — we're about halfway through the context window. To keep quality
> high I can **hand off** now: I'll export a portable summary, log the open
> threads to MindHive, and give you a one-line command to resume this exact work
> in a fresh session. Want me to hand off, or push on here?

Wait for the user's choice. If they decline, resume the pipeline where it left
off and re-check at the next phase boundary.

## Approved-handoff sequence

On approval, run in order:

1. **Export the portable handoff.**
   `python -m mindhive export --recent 20`
   (writes `handoff.md` — the durable, portable summary of recent state.)

2. **Append the open threads** so the ledger carries what's unfinished:
   `python -m mindhive append caveat "Handoff mid-/mindhive: <what's done, what's next>" --tags orchestrator,handoff`

3. **Refresh the briefing** for the next session:
   `python -m mindhive sync`

4. **Print resume instructions** to the user, e.g.:

   > Handed off. In a fresh session:
   > 1. `python -m mindhive state` (auto-loads; confirms context restored)
   > 2. open `handoff.md` for the full snapshot
   > 3. re-run `/mindhive <the remaining scope>` to continue where we stopped.

Do not begin new pipeline work after a handoff — the point is to end this
session cleanly so the next one starts fresh.
