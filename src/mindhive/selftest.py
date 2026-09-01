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

## Which checkout gets tested

`mindhive` is installed EDITABLE against the main checkout, so
``Path(__file__).resolve().parents[2]`` resolves THERE whichever worktree the
operator is standing in. Taking the root from that was a false green, which is
the worst kind of wrong answer: `hive selftest` on a feature branch collected the
main checkout's tests, passed, and said so, while the branch's own tests were
never executed.

So the root comes from `paths.worktree_root` — the git worktree the operator is
actually in — and the packaged location is the fall-back for the two cases where
that answers nothing useful: outside a repository, and inside one that is not a
MindHive checkout (`hive` is on PATH everywhere, and pointing pytest at a
stranger's `tests/` is a worse failure than running the packaged suite).

`PYTHONPATH` moves with it, and that is not a separate nicety. Collecting the
branch's tests while the child still imports the editable install is the same
false green one layer down: the branch's assertions would run against the main
checkout's code.
"""
import os
from pathlib import Path

from . import paths, verify


def packaged_root() -> Path:
    """The checkout this module was installed from. The fall-back, never the
    default — under an editable install it is the MAIN checkout regardless of
    where the operator is."""
    return Path(__file__).resolve().parents[2]


def _is_checkout(root) -> bool:
    """Whether *root* holds both halves of a MindHive checkout.

    Both, not either: a directory with a `tests/` and no `src/mindhive` is
    somebody else's repository, and running its suite under this command's name
    would report their result as ours."""
    try:
        base = Path(root)
        return (base / "tests").is_dir() and (base / "src" / "mindhive").is_dir()
    except (OSError, TypeError, ValueError):            # pragma: no cover - odd fs
        return False


def suite_root(cwd=None) -> Path:
    """The checkout whose suite this run should collect."""
    here = paths.worktree_root(cwd)
    if here is not None and _is_checkout(here):
        return Path(here)
    return packaged_root()


def child_env(root):
    """The environment the child runs in, or ``None`` to inherit unchanged.

    Prepended rather than assigned: whatever the operator put on `PYTHONPATH` is
    still theirs, and the checkout under test only has to win the lookup.
    ``None`` when there is no `<root>/src` to point at — a wheel install has
    none, and inventing one would break the import this exists to fix."""
    src = Path(root) / "src"
    if not src.is_dir():
        return None
    env = dict(os.environ)
    prior = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src) + ((os.pathsep + prior) if prior else "")
    return env


def run(emit=None, echo: bool = True, cwd=None) -> dict:
    """Run the suite, record the run, and return verify's full result.

    Returns the whole dict rather than just the exit code: the caller that reports
    to a human wants the parsed counts too, and "1 failed" is the part of "exit 1"
    that is actually useful. `cmd_selftest` takes `exit_code` back out of it for
    the process status.
    """
    root = suite_root(cwd)
    return verify.run_and_record(verify.default_argv(root), emit=emit,
                                 echo=echo, env=child_env(root))
