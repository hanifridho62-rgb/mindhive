# SPDX-License-Identifier: Apache-2.0
# src/mindhive/verify.py
"""Mechanical test-run records: the evidence a "the suite is green" claim can
be checked against.

`directives.OPERATING_SPEC` §0 forbids reporting a result you did not observe.
Enforcing that needs something the system observed, and before this module
nothing in the store recorded one: a grep of `src/mindhive/` found no event
type and no parser holding a pytest count anywhere. So "3518 tests pass" was
unfalsifiable — which is exactly how a fabricated baseline survived several
sessions unchallenged.

`run_and_record` runs a command as a real subprocess, reads its real exit code,
and appends one `verification` event stamped `verified_by: "python"`. That
stamp means here what it means in `mindnet/executor.py`, which sets its own
`VERIFIED_BY = "python"` only after a subprocess ran and an exit code was read.
There is one notion of mechanical verification in this codebase and this module
does not invent a second, weaker one.

**The stamp attaches to the exit code, not to the counts.** The exit code was
observed. The counts are a best-effort parse of human-readable output, and when
the parse fails the counts are recorded ABSENT rather than zero — a broken
parse must not be able to read as "a suite where nothing passed".

**Two validators, deliberately, mirroring `mindnet/phases.py`.**
`validate_payload` guards the arbitrary-payload append surfaces (`hive append`,
`POST /api/append`) and REFUSES any payload carrying `verified_by`, so an agent
cannot hand-write a mechanical verification nothing performed. `validate_stored`
is the writer's shape and REQUIRES the stamp. Registering the writer's shape on
the append surface would invert the honesty rule, which is the mistake
`phases.py`'s comment already warns about.

**Output goes to `runlog`, not the ledger.** `runlog.py` is explicit that
multi-kilobyte transcripts on the append-only chain violate memo hygiene and
blow the context budgets `context.py` polices. The event keeps a bounded
excerpt plus the artifact path.

**Never called from a hook.** `premise.py` reads the LAST recorded run; nothing
on the `UserPromptSubmit` path may produce one. A gate that could spawn a
multi-minute subprocess on a keystroke is a denial of service on the user's own
session.

**A targeted run is not a verification.** `orchestrator/gate.py` runs a
unit's own test files in its lane through `run_and_record(record=False)`: a
real subprocess, a real exit code, no event. `replenish` clears the "code
changed, nothing verified since" gap on any exit-0 `verification` and `premise`
reads the last one's counts, so a two-file pass recorded as one would answer
for the whole tree. Only the closing full suite appends, and *extra* lets it
say `scope: full` — under the stamp, never over it.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import events as ev_mod
from . import runlog

VERIFIED_BY = "python"

#: The `gate` of a `hive ci premerge` row (`ci.premerge.GATE`; a test holds the
#: two equal). Such a row is a verdict on a PR's MERGE, run in fresh
#: repositories -- not a run of this checkout's suite -- and its exit code has a
#: fourth value (3: green, but `--merge` did not merge).
PREMERGE_GATE = "premerge"

# What a run killed at its cap answers. The code `claude_cli.run_headless`
# answers for a child that outlived its timeout, so a killed suite and a killed
# child read alike wherever both are reported.
TIMEOUT_EXIT = 124
# How often the caller looks in on the drain thread. Small enough that the cap's
# drain grace is honoured closely, large enough that a four-minute suite costs
# wakeups rather than a spin.
_DRAIN_POLL_S = 0.2

# The payload keys this module writes itself. `extra` may add to a payload; it
# may not stand in for any of these, or the stamp would be forgeable through
# the back door `validate_payload` closes at the front.
_OWN_KEYS = frozenset(("text", "argv", "exit_code", "counts", "excerpt",
                       "verified_by", "artifact", "error"))

# Keywords pytest reports counts against. `errors?` covers both spellings.
_COUNT_RE = re.compile(r"\b(\d{1,7})\s+(passed|failed|errors?|skipped|xfailed|"
                       r"xpassed|deselected)\b")

# Only the tail of the output is parsed. pytest's own summary lives there, and
# scanning the whole transcript would let a failure message that happens to
# quote "3 passed" become the recorded result.
_SUMMARY_TAIL_LINES = 8


def default_argv(root: Path | None = None) -> list:
    """The repo's own suite. `sys.executable` rather than a bare `python`:
    `python` is not on PATH in this environment's Git Bash, and the venv
    interpreter is what has the dependencies."""
    base = Path(root) if root else Path(__file__).resolve().parents[2]
    return [sys.executable, "-m", "pytest", "-q", str(base / "tests")]


def parse_summary(text) -> dict:
    """Counts from pytest's summary line, or ``{}`` when nothing parseable is
    there. Total — never raises, never guesses. An absent key means "not
    observed", which is a different fact from zero and must stay different."""
    if not isinstance(text, str) or not text.strip():
        return {}
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    counts: dict[str, int] = {}
    for line in lines[-_SUMMARY_TAIL_LINES:]:
        for m in _COUNT_RE.finditer(line):
            key = m.group(2)
            if key == "error":
                key = "errors"
            try:
                counts[key] = int(m.group(1))
            except ValueError:                      # pragma: no cover
                continue
    return counts


def _base_shape(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("verification payload must be an object")
    argv = payload.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError("verification requires a non-empty argv list — the "
                         "command that was actually run")
    code = payload.get("exit_code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise ValueError("verification requires an integer exit_code — the "
                         "real exit status of that command")
    return payload


def validate_payload(payload) -> None:
    """Contract for the ARBITRARY-payload append surfaces.

    Refuses any payload carrying `verified_by`. A `verification` event asserts
    that a machine ran something and read the result; a hand-written append has
    performed no such thing, and letting one carry the stamp would make the
    record worthless for exactly the claim it exists to settle. This mirrors
    `phases.validate_payload`, which refuses `verified_by` for the same
    reason."""
    _base_shape(payload)
    if "verified_by" in payload:
        raise ValueError(
            "verification cannot be appended with a `verified_by` stamp — the "
            "stamp is applied only by a real subprocess run. Run the suite "
            "(`hive selftest`) instead of asserting its result")


def validate_stored(payload) -> None:
    """The WRITER's shape: the stamp is required here."""
    _base_shape(payload)
    if payload.get("verified_by") != VERIFIED_BY:
        raise ValueError(
            f"a stored verification must be stamped verified_by="
            f"{VERIFIED_BY!r} — nothing else may record a mechanical run")


def _stream(argv, echo: bool, emit=None, env=None, cwd=None, timeout_s=None):
    """Run *argv* and return ``(exit_code, output, error)``.

    *env* replaces the child's environment wholesale when given, and ``None``
    means inherit. The caller builds it — `selftest` points `PYTHONPATH` at the
    checkout under test — because this function knows nothing about which
    source tree the command is meant to import. Either way the child gets
    ``PYTHONDONTWRITEBYTECODE=1`` unless a value was already chosen
    (`paths.no_bytecode_env`): a suite run must not leave `__pycache__` in the
    tree it judged.

    *cwd* is where the command runs; ``None`` means here. *timeout_s* arms a
    kill: past it the child's WHOLE TREE is killed
    (`orchestrator.driver.terminate_process_tree`; the child heads its own
    process group and is held for that), the loop drains what it wrote, and the
    run answers `TIMEOUT_EXIT` with the cap named in *error*. It used to be
    `Popen.kill`, which reaches the child alone: a grandchild that inherited the
    pipe held the read open past the cap (blueprint Bug 16). Heading its own
    group also takes the child out of the console's Ctrl+C group on Windows, so
    any other exit from the drain -- an interrupt included -- ends the tree here
    first.

    *error* says what the cap ACTUALLY did. "killed at the Ns cap" is claimed
    only when the kill reports having taken the tree; a cap that could not reach
    it says so, and one that gave up on the read says that too. This string is
    recorded in a `verification` event under ``verified_by="python"``, so it is
    held to the same standard as any other mechanical claim -- and it was
    measured asserting a kill that never happened.

    Line-streamed rather than captured wholesale so a four-minute suite still
    prints as it goes; the buffer is what becomes the artifact. argv list only
    — `shell=True` appears nowhere in this module and a test pins that."""
    # What runs here is a test suite, and in a lane the tests are an agent's:
    # it gets an agent's environment, without the SOP process token (P1d).
    from . import advice as authorship
    from .orchestrator import driver        # lazy: that package imports this module
    chunks: list[str] = []
    kept = 0
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=driver.no_bytecode_env(authorship.scrub_agent_env(env)),
            cwd=str(cwd) if cwd else None,
            **driver.isolation_kwargs(),
        )
    except (OSError, ValueError) as e:
        # A missing program is a real, recordable outcome — not a crash for the
        # caller to handle. 127 is the shell convention for "not found".
        return 127, "", f"{type(e).__name__}: {e}"
    # Held, so the kill still reaches the tree once the direct child has gone:
    # an orphaned grandchild on the inherited pipe used to defeat the cap outright.
    driver.hold_tree(proc)
    killed: list = []
    timer = None
    if timeout_s:
        def _kill():
            killed.append(driver.terminate_process_tree(proc))   # total: it never raises
        timer = threading.Timer(float(timeout_s), _kill)
        timer.daemon = True
        timer.start()
    abandoned = False
    try:
        code, abandoned = _drain_bounded(proc, chunks, kept, emit, echo, killed)
    except BaseException:
        # Nothing else will end a child that left the console's Ctrl+C group.
        driver.terminate_process_tree(proc)
        driver.reap(proc)
        raise
    finally:
        if timer is not None:
            timer.cancel()
        driver.release_tree(proc)
    if killed:
        return TIMEOUT_EXIT, "".join(chunks), _cap_error(timeout_s, killed[0], abandoned)
    return code, "".join(chunks), ""


def _drain_bounded(proc, chunks, kept, emit, echo, killed) -> tuple:
    """`_drain` on a daemon thread, so the read cannot outlive the cap. Answers
    ``(exit_code, abandoned)``.

    ``for line in proc.stdout`` ends at EOF, and EOF is when the LAST holder of
    the write end lets go -- a descendant the kill could not reach holds it for
    as long as it lives, and `hive selftest` runs with ``timeout_s=None``, so
    nothing else bounds this. Once the cap has fired the read gets
    `driver.DRAIN_GRACE_S` more and is then abandoned; the thread is a daemon,
    and the job close in the caller's `finally` is what releases it.

    Whatever `_drain` raised is re-raised HERE, on the caller's thread, so the
    interrupt path above still ends the tree before it propagates."""
    from .orchestrator import driver        # lazy: that package imports this module
    done: dict = {}

    def _body():
        try:
            done["code"] = _drain(proc, chunks, kept, emit, echo)
        except BaseException as exc:        # noqa: BLE001 -- re-raised on the caller's thread
            done["raised"] = exc

    worker = threading.Thread(target=_body, name="hive-verify-drain", daemon=True)
    worker.start()
    deadline = None
    while worker.is_alive():
        worker.join(_DRAIN_POLL_S)
        if not killed:
            continue
        if deadline is None:
            deadline = time.monotonic() + driver.DRAIN_GRACE_S
        elif time.monotonic() > deadline:
            return TIMEOUT_EXIT, True
    raised = done.get("raised")
    if raised is not None:
        raise raised
    return done.get("code", TIMEOUT_EXIT), False


def _cap_error(timeout_s, kill, abandoned: bool) -> str:
    """What the cap may honestly say it did (see `_stream`)."""
    from .orchestrator import driver        # lazy: that package imports this module
    if kill is None or kill.took_the_tree:
        said = f"killed at the {timeout_s}s cap"
    else:
        said = f"the {timeout_s}s cap fired but the tree was not taken: {kill.reason}"
    if abandoned:
        said += (f"; the read was abandoned after a further {driver.DRAIN_GRACE_S}s "
                 f"and the output above may be short")
    return said


def _drain(proc, chunks, kept, emit, echo) -> int:
    """Read *proc* to the end into *chunks* and answer its exit code."""
    with proc:
        if proc.stdout is not None:
            for line in proc.stdout:
                # Bounded in MEMORY, not just at write time. `runlog` caps the
                # artifact when it is written, but the buffer that feeds it was
                # unbounded — a runaway suite printing gigabytes would grow this
                # process until it died, losing the exit code that is the whole
                # point. Past the cap we keep echoing and keep DRAINING the pipe
                # (stopping the read would block the child on a full pipe and
                # hang the run), we just stop retaining.
                if kept < runlog.MAX_ARTIFACT_BYTES:
                    chunks.append(line)
                    kept += len(line)
                if emit is not None:
                    # A second reader, line by line for the same reason `echo`
                    # flushes: the dashboard's job pane is polled while the run is
                    # still going. Its failure must not take the run down — the
                    # exit code is the point, and a broken sink is not a reason to
                    # lose it.
                    try:
                        emit(line)
                    except Exception:           # noqa: BLE001 — see comment
                        emit = None
                if echo:
                    sys.stdout.write(line)
                    # Explicit, because "streams as it goes" is otherwise only
                    # true on a terminal. Redirected to a file or a CI log,
                    # Python block-buffers at 8KB, so a four-minute suite shows
                    # nothing at all until it is already over — measured: the
                    # capture file sat at 0 bytes for the whole run. The flush
                    # costs a syscall per line on a command that is about to
                    # spend minutes in a subprocess.
                    sys.stdout.flush()
        return proc.wait()


def run_and_record(argv=None, project=None, root=None, echo: bool = True,
                   emit=None, env=None, cwd=None, timeout_s=None,
                   record: bool = True, extra=None) -> dict:
    """Run a test command, record the result, return
    ``{exit_code, counts, error, artifact, excerpt, tail}``.

    The returned dict is a convenience for the caller; the EVENT is the
    artifact. Never raises for a failing or missing command — a failure is a
    result to record, not an exception to hide.

    ``record=False`` skips the event and nothing else: the subprocess, the
    exit code and the artifact are as real as ever, and the caller holds the
    dict. It exists for the per-unit gate — see the module docstring for why a
    targeted run must not read as a tree verification. *extra* is merged UNDER
    the payload's own keys, so a caller can label a run (`scope`,
    `mission_id`, `tree`) and cannot forge its stamp, its exit code or its
    command."""
    cmd = list(argv) if argv else default_argv(root)
    code, output, error = _stream(cmd, echo, emit, env=env, cwd=cwd,
                                  timeout_s=timeout_s)
    counts = parse_summary(output)
    text = output.strip()
    excerpt = text[:runlog.EXCERPT_CHARS]
    # The END of the output, for a caller that wants the diagnosis rather than
    # the collection banner: pytest's failures and its summary are its last
    # lines, and `excerpt` — the event's bounded quote — is its first.
    tail = text[-runlog.EXCERPT_CHARS:]

    artifact = None
    try:
        key = f"verify-{ev_mod.now_iso()}"
        artifact = runlog.write_once(key, output, project=project)
    except Exception:                                   # noqa: BLE001
        artifact = None                 # evidence lost; the excerpt survives

    summary = (f"{cmd[0]}: exit {code}"
               + (f" — {counts.get('passed', 0)} passed, "
                  f"{counts.get('failed', 0)} failed" if counts
                  else " — counts not parseable"))
    payload = ({str(k): v for k, v in extra.items() if str(k) not in _OWN_KEYS}
               if isinstance(extra, dict) else {})
    payload.update({
        "text": summary,
        "argv": [str(c) for c in cmd],
        "exit_code": int(code),
        "counts": counts,
        "excerpt": excerpt,
        "verified_by": VERIFIED_BY,
    })
    if artifact:
        payload["artifact"] = artifact
    if error:
        payload["error"] = error
    validate_stored(payload)
    if record:
        try:
            ev_mod.append_event("verification", payload,
                                tags=["verify", "auto"], project=project)
        except Exception:                               # noqa: BLE001
            pass      # a down ledger must not turn a real run into a crash
    return {"exit_code": int(code), "counts": counts, "error": error,
            "artifact": artifact, "excerpt": excerpt, "tail": tail}


def is_pr_verdict(payload) -> bool:
    """True for a `hive ci premerge` row. A reader asking whether the suite
    passed HERE, or how many tests it counted, must not take one for its
    answer: the row judges a PR's merge, run somewhere else."""
    return isinstance(payload, dict) and payload.get("gate") == PREMERGE_GATE


def last_run(evs) -> dict | None:
    """The most recent recorded run's payload, or None when the store has
    none. None is the honest answer for an empty store — `premise` renders it
    as "no recorded run to check against", never as a contradiction.

    A `hive ci premerge` row is passed over (`is_pr_verdict`): it is a verdict
    on a PR's merge, and its counts are one leg's of another tree."""
    latest = None
    for e in (evs or []):
        if getattr(e, "type", None) != "verification":
            continue
        if not isinstance(getattr(e, "payload", None), dict):
            continue
        if is_pr_verdict(e.payload):
            continue
        latest = e.payload
    return latest
