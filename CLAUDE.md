# MIND HIVE — project instructions for Claude

These instructions override default behavior. Read them before touching the
live local dashboard or this checkout's git state.

## Never redirect the live dashboard's code with PYTHONPATH / sys.path

The user's real local dashboard runs at `http://127.0.0.1:54461` and is always
started with a **bare** `python -m mindhive dashboard` (no `--no-browser`
needed unless asked, no env overrides). The venv has `mindhive` installed
**editable**, so it always serves whatever branch is currently checked out at
`C:\Users\hanif\mindhive` — that's the entire mechanism, and it's correct by
default. Do not "fix" it.

**Do not** override `PYTHONPATH`/`sys.path` to point the user's real dashboard
process at a different worktree or branch (e.g. to preview an unmerged PR
branch). This silently changes what the dashboard serves with **no error** —
older or differently-scoped code can be missing entire sections. This already
happened once: pointing the live dashboard at an unrelated worktree branch
made the whole **Oversee** section vanish from the UI, because that branch
predated the Oversee feature. The event store/ledger was never touched — it
was a served-code regression, not data loss — but from the user's side it
looked exactly like their work had been deleted.

**If you need to preview an unmerged branch's dashboard changes:** run it on a
different port (e.g. 54471) via a `.claude/launch.json` config and the browser
preview tool, in whatever worktree that branch lives in. Never touch the
process bound to port 54461 for that purpose — restart it only when the user
asks, and restart it plainly (`python -m mindhive dashboard`), letting it
resolve to whatever this checkout's current branch actually is.

**Before declaring a dashboard restart "done":** confirm the sections the user
already relies on are still being served (grep the served page for known
hooks, e.g. `showOversee`, `renderOversee`) — not just that the port answers
with HTTP 200.

## Check for an existing PR before assuming work is unbacked-up

`C:\Users\hanif\mindhive` is usually on a feature branch ahead of
`origin/master` — that alone does **not** mean the work is unpushed or
unbacked-up; it may already have an open PR. Before flagging "this looks
unpushed" to the user, check `git push` (does it say "Everything up-to-date"?)
and `gh pr list --head <branch>` — don't reason from `ahead-of-master` count
alone, that only reflects the PR hasn't merged yet. If a branch genuinely has
no remote copy, say so plainly and ask before pushing/opening a PR — don't do
it unilaterally, since the user may still be iterating.

## Every code change MUST get a change-log entry (mandatory)

Any change to `src/mindhive/` or `tests/` — no matter how small — must be
recorded in the code change-log before the work is called done:

```
hive log <category> "<brief explanation of what changed>"
```

Category is one of: `update`, `minor_change`, `deletion`, `reduction`,
`major_change`, `replacement`. The explanation is a real sentence (min 10
chars) describing what changed and why; affected files are auto-detected
(pending marker → session's touched files → git diff), or pass `--files`
explicitly. Entries are `code_change` events in the ledger and appear in the
dashboard's **Log** section.

This is enforced: `session_end` queues any unlogged source files into a
pending marker, and STATE.md/briefing shows a blocking "⚠ Unlogged code
changes" notice until the entry is recorded. Do not clear the notice any
other way — write the entry.

## The global Stop hook must stay fail-open and opt-in

`hooks/stop.ps1` + `mindhive.rotation.hook` measure context occupancy at the end
of every agent turn. Once registered in `~/.claude/settings.json` it fires in
**every** Claude Code session on this machine, in every project — so a bug in it
does not break one feature, it breaks all work everywhere, and it breaks it from
inside the tool you would use to fix it.

Two properties keep that safe. Do not weaken either:

- **Fail-open.** Any exception, any unparseable payload, any unreadable
  transcript → return `None` and print nothing. The blanket `except` in
  `hook_decision` is deliberate, not sloppiness; a narrower catch lets some
  unanticipated failure reach a surface where every exception is a user-visible
  outage. `tests/test_rotation_hook.py` proves this by injecting failures at the
  module seam rather than trusting that the try/except looks right.
- **Opt-in.** The hook does nothing unless `MINDHIVE_ROTATION` is set, and only
  `hive session run` sets it — for the children it spawns. Registering the hook
  and switching it on are separate steps on purpose. Never make it
  active-by-default "for convenience".

**Never emit `{"decision": "block"}` from this hook.** For a Stop hook, `block`
means *do not stop — keep working*. It would pin a full session open instead of
rotating it, which is the exact opposite of the intent. Rotation is signalled by
the `ROTATION_PENDING` artifact, because a Stop hook cannot end a session at all.

## Any change here must land green

Whenever you touch code in this checkout, run the test suite before calling
the work done. If the user's ask implies shipping via PR, don't report success
until CI (or the local test run standing in for it) is actually green — not
"looks right."
