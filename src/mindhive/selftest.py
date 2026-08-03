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
"""
from pathlib import Path

from . import verify


def run() -> int:
    root = Path(__file__).resolve().parents[2]
    return verify.run_and_record(verify.default_argv(root))["exit_code"]
