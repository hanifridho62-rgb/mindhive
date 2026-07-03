# Using MIND HIVE every session

1. START  -> memory auto-loads from native MEMORY.md. Do nothing.
2. DURING -> after a real decision/result/lesson, tell Claude "remember this"
             (Claude runs: hive append decision|caveat|experiment_result "<one line>" --tags ...).
3. END    -> run `hive sync` to refresh the auto-loaded briefing for next session.
             If your Opus usage is about to run out, also run `hive export` for a portable handoff.

Recall details on demand:  hive recall "<query>"   (add --semantic for meaning-based)
See connections:           hive graph related "<thing>"
Full current state:        hive state        Full briefing: hive briefing
Open everything in one window:  hive dashboard   (or double-click MindHive.exe)
Run the autonomous loop:        hive loop   (or /circle) — auto-opens the dashboard;
                                add --no-dashboard to skip it
Orchestrate an idea end-to-end:  /mindhive <idea> — recall-informed interview,
                                a scored skill Recommendation Matrix, execution
                                with follow-up chaining, and learnings logged back.
