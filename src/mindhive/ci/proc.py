# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/proc.py
"""The one door every pre-merge CI child goes through (spec 2026-09-30).

`legs`, `preview`, `github`, `legrun` and `premerge` start processes only by
calling `run`, so a test replaces ONE callable and sees every argv a run would
spawn -- the `FakeRunner` pattern `tests/test_ci_runner.py` set -- and the scrub
below cannot be forgotten by a caller.

Two shapes, chosen by *log*:

* ``log=None``, captured: stdout and stderr come back as text. For the short
  probes (git, gh, wsl, the interpreter checks), which print little and exit.
* ``log=<path>``, logged: the child writes into that file, stderr merged, and
  only the exit code comes back. For the long steps (venv, pip, pytest). A file
  has no reader to hold open, so a grandchild that outlives a killed pytest
  cannot wedge the wait -- the shape `subprocess.run` gets wrong on Windows,
  where the second `communicate()` after a timeout has no cap. The child heads
  its own process group and is held (`orchestrator.driver`), so the cap takes
  the whole tree, and the error text says what the kill reached.

Every child is code under test or a tool acting on it, and what it prints is
recorded, so every environment passes through
`advice.scrub_agent_env(withhold_secret=True)`: neither the SOP process token
nor the secret reaches it (the rule `ci/runner.py` set, PR #136 review P2).

**stdin is a file**, never a pipe: a pipe blocks its writer once full, and a
child that never reads keeps it full -- past any cap. It is spooled to an
unnamed temporary file first, so the cap holds whatever the child does.

**Ctrl+C.** A logged child heads its own process group, where the console's
Ctrl+C never reaches it, so every live one is kept here and `stop_all` takes
them all -- and starts no new one until `clear_stop`. Captured children share
the console and take its Ctrl+C themselves.

Never raises: a missing executable, a timeout and a crash all come back as a
non-zero code with the reason in the error text.
"""
import subprocess
import tempfile
import threading
from pathlib import Path

from .. import advice as authorship

__all__ = ["NOT_STARTED", "TIMEOUT_EXIT", "clear_stop", "run", "stop_all"]

#: What a capped child answers: the code `verify.TIMEOUT_EXIT` and `claude_cli`
#: use, and the one GNU `timeout` exits with inside a WSL leg.
TIMEOUT_EXIT = 124
#: What a child that never started answers: the shell's "could not run it".
#: 1 would read as a child that ran and failed -- a pytest that never started
#: as a red suite.
NOT_STARTED = 127
_LIVE = set()
_LIVE_LOCK = threading.Lock()
_STOPPING = threading.Event()


def stop_all() -> None:
    """Kill every logged child alive now, and start no new one until
    `clear_stop`. Never raises."""
    from ..orchestrator import driver          # lazy: `hive --help` pays nothing
    with _LIVE_LOCK:
        _STOPPING.set()
        live = list(_LIVE)
    for child in live:
        try:
            driver.terminate_process_tree(child)
        except Exception:                               # noqa: BLE001
            pass


def clear_stop() -> None:
    """Let logged children start again, once the run that stopped is unwound."""
    _STOPPING.clear()


def _text(data) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _where(cwd):
    return None if cwd is None else str(cwd)


def run(argv, *, cwd=None, env=None, timeout_s=None, stdin=None, log=None) -> tuple:
    """``(returncode, stdout, stderr)`` for one child.

    *stdout* is ``""`` when *log* names a file, because the output went there.
    *stdin* is text or bytes fed to the child; without it the child reads an
    empty stdin, never the console's, which `wsl.exe` would sit waiting on."""
    argv = [str(a) for a in argv]
    env = authorship.scrub_agent_env(env, withhold_secret=True)
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    try:
        feed = _spool(data) if data is not None else None
    except OSError as exc:
        return NOT_STARTED, "", f"{argv[0]}: cannot spool its stdin: {exc}"
    try:
        if log is not None:
            return _logged(argv, Path(log), cwd, env, timeout_s, feed)
        return _captured(argv, cwd, env, timeout_s, feed)
    finally:
        if feed is not None:
            feed.close()


def _spool(data):
    """*data* in an unnamed temporary file, rewound, for a child's stdin."""
    spool = tempfile.TemporaryFile()
    spool.write(data)
    spool.seek(0)
    return spool


def _captured(argv, cwd, env, timeout_s, feed) -> tuple:
    try:
        proc = subprocess.run(argv, cwd=_where(cwd), env=env, capture_output=True,
                              timeout=timeout_s,
                              stdin=subprocess.DEVNULL if feed is None else feed)
    except subprocess.TimeoutExpired as exc:
        return (TIMEOUT_EXIT, _text(exc.stdout),
                f"{argv[0]}: killed at the {timeout_s:g}s cap")
    except OSError as exc:
        return NOT_STARTED, "", f"{argv[0]}: {exc}"
    except Exception as exc:                            # noqa: BLE001
        return 1, "", f"{argv[0]}: {exc}"
    return proc.returncode, _text(proc.stdout), _text(proc.stderr)


def _logged(argv, log, cwd, env, timeout_s, feed) -> tuple:
    from ..orchestrator import driver          # lazy: `hive --help` pays nothing
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        out = open(log, "ab")
    except OSError as exc:
        return NOT_STARTED, "", f"{argv[0]}: cannot open {log}: {exc}"
    with out:
        # Started and counted under one lock: a `stop_all` between the two
        # would miss a child it never saw.
        with _LIVE_LOCK:
            if _STOPPING.is_set():
                return NOT_STARTED, "", f"{argv[0]}: not started: the run is being stopped"
            try:
                proc = subprocess.Popen(
                    argv, cwd=_where(cwd), env=env, stdout=out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL if feed is None else feed,
                    **driver.isolation_kwargs())
            except Exception as exc:                    # noqa: BLE001
                return NOT_STARTED, "", f"{argv[0]}: {exc}"
            _LIVE.add(proc)
        driver.hold_tree(proc)
        try:
            return proc.wait(timeout=timeout_s), "", ""
        except subprocess.TimeoutExpired:
            kill = driver.terminate_process_tree(proc)
            return (TIMEOUT_EXIT, "",
                    f"{argv[0]}: killed at the {timeout_s:g}s cap "
                    f"({kill.how}: {kill.reason})")
        finally:
            with _LIVE_LOCK:
                _LIVE.discard(proc)
            driver.release_tree(proc)
