# src/mindhive/selftest.py
"""`hive selftest` — run the suite, and RECORD that it ran.

The recording is the point. Before `verify.py`, running the suite left no trace
in the store, so "the suite is green at N" was an unfalsifiable claim and a
fabricated baseline could survive for sessions. Delegating here means the
ordinary act of running the tests is what produces the evidence
`premise.check` later reads — no separate command an operator has to remember,
and no second way to run the suite that quietly records nothing.

Output still streams line by line, so a four-minute run is not four minutes of
silence; `verify._stream` echoes while it buffers.

`emit` is the second reader of that stream. The CLI's reader is a terminal, so
`echo` writes to stdout; the dashboard's reader is a browser polling a job, and
the server's stdout is not where that person is looking. Both are line-by-line
for the same reason — the alternative is a pane that stays empty for four minutes
and then fills all at once.
"""
from pathlib import Path

from . import verify


def run(emit=None, echo: bool = True) -> dict:
    """Run the suite, record the run, and return verify's full result.

    Returns the whole dict rather than just the exit code: the caller that reports
    to a human wants the parsed counts too, and "1 failed" is the part of "exit 1"
    that is actually useful. `cmd_selftest` takes `exit_code` back out of it for
    the process status.
    """
    root = Path(__file__).resolve().parents[2]
    return verify.run_and_record(verify.default_argv(root), emit=emit, echo=echo)
