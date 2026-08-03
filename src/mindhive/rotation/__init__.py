# src/mindhive/rotation/__init__.py
"""Smart-zone session rotation for INTERACTIVE Claude Code sessions.

`hive loop` already rotates headless sessions: it ends an iteration when context
fills and reseeds the next from the compact briefing. An interactive session has
no equivalent, because nothing inside a session can clear its own context or
spawn a successor. This package supplies the two halves that do it from outside:

  window     model id -> context window, and window -> smart-zone threshold
  occupancy  read the true context size out of a session transcript
  hook       the Stop-hook decision (measure, and signal when over)
  artifact   the ROTATION_PENDING file the hook writes and the supervisor reads
  supervisor `hive session run` - wraps the interactive CLI, relaunches on rotation
  install    idempotent registration of the Stop hook in the global settings.json

The hook and the supervisor never talk directly. A Stop hook cannot terminate a
session — its only outcomes are "allow the stop" and "block the stop" (which
*continues* the conversation, the opposite of rotating). So the hook writes a
file, and the supervisor reads that file after the child exits, whatever caused
the exit. The artifact is the entire contract between them.
"""
