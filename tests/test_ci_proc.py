# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.proc` -- the one door every pre-merge CI child goes through.

What carries the weight: a captured child answers its code and both streams; a
logged child writes into its file and nothing else; a cap returns promptly and
says so, even when the child left a grandchild running; stdin is empty unless
given; no child -- captured or logged, inheriting or handed an environment --
sees the SOP secret or the token; and none can stop to ask for a credential.
"""
import json
import os
import sys
import threading
import time

import pytest

from mindhive import advice
from mindhive.ci import proc

SECRET, TOKEN = advice.SECRET_ENV, advice.TOKEN_ENV
FAKE = "fake-sop-secret-for-this-test-only"
NAMES = ("import json, os; print(json.dumps(sorted("
         "k for k in os.environ if k.startswith('MINDHIVE_'))))")
# What git, its credential manager and gh each read as "never ask".
QUIET = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "GH_PROMPT_DISABLED": "1"}
SEEN = ("import json, os; print(json.dumps({k: os.environ.get(k) for k in "
        + repr(sorted(QUIET)) + "}))")


def test_a_captured_child_answers_its_code_and_both_streams():
    rc, out, err = proc.run([sys.executable, "-c",
                             "import sys; print('out'); "
                             "print('err', file=sys.stderr); sys.exit(3)"])
    assert (rc, out.strip(), err.strip()) == (3, "out", "err")


def test_stdin_reaches_the_child_as_bytes_or_as_text():
    code = "import sys; print(sys.stdin.buffer.read().decode().upper())"
    assert proc.run([sys.executable, "-c", code], stdin=b"abc")[1].strip() == "ABC"
    assert proc.run([sys.executable, "-c", code], stdin="xyz")[1].strip() == "XYZ"


def test_without_stdin_the_child_reads_an_empty_one_not_the_console():
    rc, out, _ = proc.run([sys.executable, "-c",
                           "import sys; print(repr(sys.stdin.read()))"],
                          timeout_s=60)
    assert rc == 0 and out.strip() == "''"


def test_a_missing_program_is_a_code_and_a_reason_never_a_raise(tmp_path):
    # 127, the shell's "could not run it": 1 would read as a child that ran
    # and failed -- a pytest that never started as a red suite.
    for log in (None, tmp_path / "leg.log"):
        rc, out, err = proc.run([str(tmp_path / "no-such-program")], log=log)
        assert rc == proc.NOT_STARTED == 127 and out == ""
        assert "no-such-program" in err


def test_a_captured_child_past_its_cap_answers_124_promptly():
    started = time.monotonic()
    rc, _, err = proc.run([sys.executable, "-c", "import time; time.sleep(60)"],
                          timeout_s=1)
    assert rc == proc.TIMEOUT_EXIT
    assert "1s cap" in err
    assert time.monotonic() - started < 30


def test_a_logged_child_writes_both_streams_into_its_file(tmp_path):
    log = tmp_path / "deep" / "leg.log"
    rc, out, err = proc.run([sys.executable, "-c",
                             "import sys; print('to-out'); "
                             "print('to-err', file=sys.stderr); sys.exit(1)"],
                            log=log)
    assert (rc, out, err) == (1, "", "")
    text = log.read_text(encoding="utf-8")
    assert "to-out" in text and "to-err" in text


def test_logged_steps_append_so_a_leg_keeps_one_log(tmp_path):
    log = tmp_path / "leg.log"
    proc.run([sys.executable, "-c", "print('one')"], log=log)
    proc.run([sys.executable, "-c", "print('two')"], log=log)
    assert log.read_text(encoding="utf-8").split() == ["one", "two"]


def test_a_logged_child_reads_the_stdin_it_is_given(tmp_path):
    log = tmp_path / "leg.log"
    rc, _, _ = proc.run([sys.executable, "-c",
                         "import sys; print(sys.stdin.read()[::-1])"],
                        log=log, stdin="abc")
    assert rc == 0
    assert log.read_text(encoding="utf-8").strip() == "cba"


def test_a_logged_child_past_its_cap_is_killed_with_the_grandchild_it_left(tmp_path):
    """The shape `subprocess.run` gets wrong on Windows: a grandchild still
    holding what the parent waits on. Here the output is a file and the tree
    is held, so the cap returns in seconds, not when the grandchild is done."""
    log = tmp_path / "leg.log"
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
            "time.sleep(120)")
    started = time.monotonic()
    rc, _, err = proc.run([sys.executable, "-c", code], log=log, timeout_s=3)
    assert rc == proc.TIMEOUT_EXIT
    assert "3s cap" in err
    assert time.monotonic() - started < 60


def test_no_child_sees_the_sop_secret_or_the_token(monkeypatch, tmp_path):
    monkeypatch.setenv(SECRET, FAKE)
    monkeypatch.setenv(TOKEN, FAKE)
    handed = dict(os.environ)
    for env in (None, handed):
        rc, out, err = proc.run([sys.executable, "-c", NAMES], env=env)
        assert rc == 0, err
        seen = json.loads(out)
        assert "MINDHIVE_HOME" in seen            # canary: the environment DID arrive
        assert SECRET not in seen and TOKEN not in seen
    log = tmp_path / "leg.log"
    assert proc.run([sys.executable, "-c", NAMES], env=handed, log=log)[0] == 0
    seen = json.loads(log.read_text(encoding="utf-8"))
    assert "MINDHIVE_HOME" in seen and SECRET not in seen and TOKEN not in seen
    assert FAKE in handed.values()                # the caller's own copy is untouched


def test_no_child_can_stop_to_ask_for_a_credential(monkeypatch, tmp_path):
    """A pass nobody is watching: a `git fetch` or a `gh` call that stops to
    ask for a credential holds the run until its cap. Every child, in both
    shapes, inheriting or handed an environment, is told not to ask -- and a
    value the caller chose is kept, the way `paths.no_bytecode_env` keeps one."""
    for name in QUIET:
        monkeypatch.delenv(name, raising=False)
    for env in (None, dict(os.environ)):
        rc, out, err = proc.run([sys.executable, "-c", SEEN], env=env)
        assert rc == 0, err
        assert json.loads(out) == QUIET
    log = tmp_path / "leg.log"
    assert proc.run([sys.executable, "-c", SEEN], env=dict(os.environ), log=log)[0] == 0
    assert json.loads(log.read_text(encoding="utf-8")) == QUIET
    chosen = dict(os.environ, GIT_TERMINAL_PROMPT="1")
    seen = json.loads(proc.run([sys.executable, "-c", SEEN], env=chosen)[1])
    assert seen == dict(QUIET, GIT_TERMINAL_PROMPT="1")


def _bounded(call, limit_s=45):
    """Run *call* in a thread and answer what it returned -- or fail, rather
    than hang the suite, when it is still running after *limit_s*."""
    box = {}
    worker = threading.Thread(target=lambda: box.update(out=call()), daemon=True)
    worker.start()
    worker.join(limit_s)
    assert not worker.is_alive(), f"still running after {limit_s}s"
    return box["out"]


def test_a_captured_child_past_its_cap_is_killed_with_the_grandchild_it_left(tmp_path):
    """The shape `subprocess.run` gets wrong on Windows, on the captured path
    this time: past the cap it kills the child and then waits, with no cap,
    for the pipes to close -- and a grandchild that inherited them holds them
    open for as long as it lives. Measured before the fix: a 2s cap came back
    after 25.2s, the grandchild's own lifetime, with the error text claiming
    the kill. Here the tree is held, so the cap returns in seconds, keeps what
    the child had printed, and the grandchild's heartbeat stops with it."""
    beat = tmp_path / "beat"
    grand = tmp_path / "grand.py"
    grand.write_text("import sys, time\n"
                     "for _ in range(350):\n"          # 35s, then it goes by itself
                     "    open(sys.argv[1], 'a').write('x'); time.sleep(0.1)\n",
                     encoding="utf-8")
    code = ("import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, {str(grand)!r}, {str(beat)!r}], "
            "stdout=sys.stdout, stderr=sys.stderr); "
            "print('parent up', flush=True); time.sleep(40)")
    started = time.monotonic()
    rc, out, err = _bounded(lambda: proc.run([sys.executable, "-c", code], timeout_s=2))
    elapsed = time.monotonic() - started
    assert rc == proc.TIMEOUT_EXIT and "2s cap" in err
    assert "parent up" in out                 # what it printed before the cap is kept
    assert elapsed < 30, elapsed              # seconds, not the grandchild's lifetime
    beats = beat.stat().st_size if beat.exists() else 0
    time.sleep(1.5)
    assert (beat.stat().st_size if beat.exists() else 0) == beats, "the grandchild lives"


def test_stop_all_takes_a_captured_child_too(tmp_path):
    """A captured child heads its own process group, where the console's Ctrl+C
    never reaches it: `stop_all` must, or a `git fetch` in a leg's thread would
    run on to its cap after the operator's Ctrl+C."""
    up = tmp_path / "up"
    box = {}
    worker = threading.Thread(target=lambda: box.update(out=proc.run(
        [sys.executable, "-c",
         f"import pathlib, time; pathlib.Path({str(up)!r}).touch(); time.sleep(40)"],
        timeout_s=60)), daemon=True)
    worker.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not up.exists():
        time.sleep(0.05)
    started = time.monotonic()
    try:
        proc.stop_all()
        worker.join(30)
    finally:
        proc.clear_stop()
    assert not worker.is_alive() and time.monotonic() - started < 30
    assert box["out"][0] != 0


@pytest.mark.parametrize("logged", [False, True])
def test_a_child_that_never_reads_its_stdin_is_still_capped(tmp_path, logged):
    """Feeding stdin through a pipe blocks once the pipe is full, and a child
    that never reads keeps it full: the cap must hold through that too."""
    started = time.monotonic()
    rc, _, err = _bounded(lambda: proc.run(
        [sys.executable, "-c", "import time; time.sleep(40)"],
        stdin=b"x" * (8 * 1024 * 1024), timeout_s=2,
        log=tmp_path / "leg.log" if logged else None))
    assert rc == proc.TIMEOUT_EXIT and "2s cap" in err
    assert time.monotonic() - started < 30


def test_stop_all_takes_every_long_child_and_starts_no_new_one(tmp_path):
    log = tmp_path / "leg.log"
    box = {}
    worker = threading.Thread(target=lambda: box.update(out=proc.run(
        [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(40)"],
        log=log, timeout_s=60)), daemon=True)
    worker.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and "up" not in (
            log.read_text(encoding="utf-8") if log.exists() else ""):
        time.sleep(0.05)
    started = time.monotonic()
    try:
        proc.stop_all()
        worker.join(30)
        assert not worker.is_alive() and time.monotonic() - started < 30
        assert box["out"][0] != 0
        rc, _, err = proc.run([sys.executable, "-c", "print(1)"], log=tmp_path / "b.log")
        assert rc == proc.NOT_STARTED and "being stopped" in err
        assert proc.run([sys.executable, "-c", "print(1)"])[0] == 0   # a short probe runs
    finally:
        proc.clear_stop()
    assert proc.run([sys.executable, "-c", "print(1)"], log=tmp_path / "c.log")[0] == 0
