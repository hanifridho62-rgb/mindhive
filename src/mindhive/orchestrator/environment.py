# SPDX-License-Identifier: Apache-2.0
# src/mindhive/orchestrator/environment.py
"""Environment failure is not task failure, and the mission has to tell them apart.

A lintester mission fans N children at a repository. Each child is a `claude -p`
process, and each is scored by whether it ran and what it answered. When a child
FAILS, the pipeline does the right thing: `resilience.RepairLoop` moves the task
back a phase, quotes the failure into the next prompt, rolls the lane's checkout
back, and tries again -- twice, then the breaker opens and `reflect_on_open`
records the anomaly.

That whole machine assumes the child ran. On this machine, on 2026-09-07, it did
not: the `claude` CLI answered ``Not logged in · Please run /login`` at exit 1,
and `mindnet.agent._classify` correctly reported `refused` with
``executed=False``. `lintester._run_child` then collapsed that into `fail`, and
every downstream stage did its job on a failure that never happened:

    fall_back         a `mindnet_task` row moving the task back a phase it
                      never left
    stderr injection  "Not logged in" quoted into the retry prompt as if the
                      child had said it
    rollback          a lane checkout reset to discard work no child did
    max_attempts      N units x max_attempts identical refusals
    breaker, reflect  a per-task breaker opened for a fault the task does not
                      own, then a reflection that fails for the same reason
    calibration       a wall-time sample recorded for a unit that never started

Every lane kept dispatching, because nothing told the scheduler that the ninth
refusal would say what the first had. `mindnet.orchestrator` already brakes on
this in the wave loop (`agent.USAGE_MARK`); the lane loop had no brake.

## What this module does

`environment_failure(entry)` answers a kind -- one of `KINDS` -- when a queue
entry's result says the child was never started for a reason that belongs to
the MACHINE, and ``""`` otherwise. It is deliberately conservative. A child that
ran and printed one of these phrases is a task failure; a `refused` with no
recognisable cause is left to the repair loop rather than claimed here. The one
exception is the Windows Store interpreter stub, which DOES create a process --
the redirector -- and is recognised by its exit code and its message together.

One AUTH failure is not a login at all: on 2026-09-03 this machine answered
``403: Your organization has disabled Claude subscription access for Claude
Code`` while `claude auth status` reported it signed in. `describe` answers
that case with the fix that clears it -- an API key, or the organisation
setting -- rather than sending the operator to log in again.

`preflight()` asks `claude auth status` the same question before a single lane
is made, so the common case is refused at the front door with the fix named.
Inconclusive is NOT a refusal: a CLI without that subcommand, a probe that times
out, an unparseable answer -- all fall through to the run path, where the first
real refusal halts the mission anyway. Fail-closed lives in the run path; the
preflight is only the cheaper place to say the same thing.

## The gate, blueprint §10.9.2

`check_claude_environment()` and `enforce_preflight_gate()` are the spec's two
names for that same probe. The first answers ``(ok, message, details)``; the
second turns a refusal into ONE line on stderr -- `refusal_line`: the
`FATAL_REFUSAL_FRAME` first, as the spec writes it, then the command's name and
the fix -- and leaves with `EXIT_ENVIRONMENT_REFUSAL`, the number
`cli.ENVIRONMENT_STATUS` is bound to.

`hive lintester` does not call the gate. Its refusal also has to print the
mission-shaped JSON report on `--json` and hand its status back to `main`
instead of leaving the process, and it names the kind in that report, which
the gate's ``(ok, message)`` does not carry. It shares everything else: the
probe (`preflight` -> `_probe`), `refusal_line`, and the status by reference.
`enforce_preflight_gate` is the spec's entry point for any other caller. Four
departures from the spec's letter are deliberate:

- Inconclusive passes. The spec's check fails closed on a timeout, a non-zero
  exit or a body that is not JSON; this one refuses only where `preflight`
  does, for the reason given above.
- The body is read whatever the exit code. Signed out, `claude auth status
  --json` exits 1 AND prints the JSON that says so; a check that tested the
  code first would never reach the ``loggedIn`` it exists for.
- The probe waits `PREFLIGHT_TIMEOUT_S`, 15 s, not the spec's 10.0: one probe
  serves both callers and keeps the one timeout it had, and since a timeout
  passes, the longer wait costs latency, never a refusal.
- The fix is named in `describe`'s words, `claude auth login`, where the spec
  wrote ``claude login``: the CLI's login lives under `auth`. The frame is the
  contract; the sentence after it -- which opens with the name of the command
  that refused -- is advice.

## What it does not do

It does not log in. Credentials are the owner's, and a pipeline that could fix
its own authentication would be a pipeline that could be pointed at someone
else's. It names the fix and stops.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

from . import driver

# Result strings that mean "no child ran". `mindnet.executor.RunResult` and
# `mindnet.agent._classify` both answer these with ``executed=False``.
NEVER_RAN = ("refused", "spawn-failed")

AUTH = "auth"
USAGE = "usage"
CLI_MISSING = "cli-missing"
INTERPRETER_STUB = "interpreter-stub"
SPAWN = "spawn"
KINDS = (AUTH, USAGE, CLI_MISSING, INTERPRETER_STUB, SPAWN)

# The two exit codes a Windows App Execution Alias answers when the program it
# stands for is not installed: 49 from the Store's python redirector, 9009 from
# cmd.exe for a name it could not resolve. Neither is ENOENT, and that is the
# trap: a loop that tries the next interpreter on ENOENT never gets its turn.
STUB_EXIT_CODES = (49, 9009)

PREFLIGHT_TIMEOUT_S = 15

# The status a machine that can start no child leaves the process with.
# `cli.ENVIRONMENT_STATUS` is bound to this name, not to a number of its own,
# so the front door and the gate cannot drift into two codes for one refusal.
EXIT_ENVIRONMENT_REFUSAL = 3

# How the one line a refusal writes to stderr opens, verbatim from §10.9.2, so
# a wrapper greps one fixed string for it. Built from the status it announces
# so the two cannot disagree; the front door prints it too, by reference.
FATAL_REFUSAL_FRAME = f"[FATAL PREFLIGHT REFUSAL - EXIT {EXIT_ENVIRONMENT_REFUSAL}]: "

# Lower-cased substrings. Each list is short on purpose: a hint that matches a
# task's own traceback would turn a real failure into a halted mission, which
# is the mirror image of the bug this module fixes.
_AUTH_HINTS = ("not logged in", "please run /login", "run /login",
               "authentication_error", "invalid api key",
               "subscription access", "unauthorized")
_USAGE_HINTS = ("usage limit", "weekly limit", "rate limit", "quota",
                "credit balance", "429")
_STUB_HINTS = ("python was not found", "app execution alias",
               "install from the microsoft store", "windowsapps")
_CLI_HINTS = ("claude` cli is not installed", "claude` cli is not on path",
              "claude: command not found", "claude: not found")

# Sub-cases of AUTH that a login cannot fix. Read ONLY by `describe`: by the
# time these are consulted the classifier has already answered AUTH, so a hint
# here chooses a sentence and can never halt a mission on its own -- which is
# why this list is allowed to sit beside the deliberately short ones above.
#
# Observed on this machine on 2026-09-03: `claude auth status` reported the CLI
# logged in and `claude -p` answered ``403: Your organization has disabled
# Claude subscription access for Claude Code - Use an Anthropic API key
# instead, or ask your admin to enable access``. The AUTH sentence told the
# operator to log in, which is the one thing that was already true.
_ACCOUNT_HINTS = ("subscription access", "invalid api key", "api key instead")

# Said by `agent._classify` when the envelope's own turn count shows the child
# had worked before the refusal landed. Matched as a phrase because that is how
# this module already reads every other fact off an agent's reason -- see the
# hint tuples above. Read ONLY by `describe`, so a miss costs a less specific
# sentence and can never change which missions halt.
_PARTIAL_HINTS = ("may hold partial work",)

# How much of the CLI's own sentence the account branch quotes back. Bounded
# because `agent._classify` wraps the message in a sentence of its own, and an
# unbounded quote would put a paragraph in the middle of one line of advice.
_DETAIL_CHARS = 240


def _text_of(entry) -> str:
    """Everything the entry and its result said, lower-cased, for the hints."""
    parts = []
    result = getattr(entry, "result", None)
    for source in (result, entry):
        if source is None:
            continue
        for name in ("reason", "stderr", "stdout"):
            try:
                value = getattr(source, name, "")
            except Exception:                           # noqa: BLE001
                value = ""
            if value:
                parts.append(str(value))
    return "\n".join(parts).lower()


def _started(result):
    """``True``, ``False``, or ``None`` when the result does not say.

    Direct attribute access, never a quoted name: invariant 10
    (`tests/test_mindnet_phases.py`) forbids that word as a string literal."""
    if result is None:
        return None
    try:
        value = result.executed
    except AttributeError:
        return None
    return None if value is None else bool(value)


def _exit_code(result):
    try:
        return int(getattr(result, "exit_code", None))
    except (TypeError, ValueError):
        return None


def environment_failure(entry) -> str:
    """The environment kind behind *entry*'s failure, or ``""`` for a task's own.

    Total: anything this cannot read answers ``""``, which routes the entry to
    the repair loop -- the behaviour the pipeline had before this module."""
    try:
        result = getattr(entry, "result", None)
        kind = str(getattr(result, "result", "") or "")
        text = _text_of(entry)
        started = _started(result)
        # The stub FIRST, and regardless of `executed`: the process that ran was
        # the Store redirector, not an interpreter, and both the code and the
        # message have to agree before that is claimed.
        if _exit_code(result) in STUB_EXIT_CODES and _any(_STUB_HINTS, text):
            return INTERPRETER_STUB
        if started is True:
            return ""
        if kind not in NEVER_RAN and started is not False:
            return ""
        if _any(_AUTH_HINTS, text):
            return AUTH
        if _any(_USAGE_HINTS, text):
            return USAGE
        if _any(_STUB_HINTS, text):
            return INTERPRETER_STUB
        if _any(_CLI_HINTS, text):
            return CLI_MISSING
        if kind == "spawn-failed" or (started is False and kind != "refused"):
            return SPAWN
        return ""
    except Exception:                                   # noqa: BLE001
        return ""


def _any(hints, text) -> bool:
    return bool(text) and any(h in text for h in hints)


def describe(kind, reason="") -> str:
    """One sentence an operator can act on, naming the fix. ``""`` for no kind."""
    detail = str(reason or "").strip()
    tail = f" ({detail})" if detail and kind not in (AUTH, USAGE) else ""
    if kind == AUTH:
        # Collapsed ONCE, and matched on the same text that is quoted. Matching
        # the raw string while quoting a normalised one was a real defect: a CLI
        # sentence wrapped across lines puts a newline inside "subscription
        # access", the hint misses, and an operator whose organisation disabled
        # the account is told to log in again -- the exact misdirection this
        # branch exists to end.
        flat = " ".join(detail.split())
        if _any(_ACCOUNT_HINTS, flat.lower()):
            quoted = flat[:_DETAIL_CHARS]
            # No status code and no claim about the login state are asserted
            # here: the branch also fires on a rejected key, where neither
            # would be true. The CLI's own sentence carries the specifics.
            return ("the API refused this account, so no child could start; "
                    "signing in again does not clear that -- supply an "
                    "Anthropic API key (ANTHROPIC_API_KEY in the environment "
                    "the mission runs in) or ask the organisation admin to "
                    "enable Claude Code access, then re-run -- the mission "
                    f"does not retry this (the CLI said: {quoted})")
        return ("the Claude CLI on this machine is not logged in, so no child "
                "could start; log in (`claude auth login`, or /login inside "
                "claude) and re-run -- the mission does not retry this")
    if kind == USAGE:
        # "no child could start" is false whenever a child ran first and the
        # limit landed mid-flight. On 2026-09-08 this sentence said exactly that
        # for a mission whose two lanes held 146 and 7 lines of uncommitted
        # edits, sending the operator away from work sitting on disk.
        if _any(_PARTIAL_HINTS, " ".join(detail.split()).lower()):
            return ("the account's usage limit is exhausted; a child had "
                    "already started, so a lane may hold partial work worth "
                    "looking at before it is discarded -- wait for the window "
                    "to reset and re-run -- the mission does not retry this")
        return ("the account's usage limit is exhausted, so no child could "
                "start; wait for the window to reset and re-run -- the mission "
                "does not retry this")
    if kind == CLI_MISSING:
        return ("the `claude` CLI is not installed or not on PATH, so no child "
                f"could start; install it and re-run{tail}")
    if kind == INTERPRETER_STUB:
        return ("`python` resolved to the Windows Store redirector (an App "
                "Execution Alias), not an interpreter; disable the alias under "
                "Settings > Apps > Advanced app settings > App execution "
                f"aliases, or name the interpreter by path{tail}")
    if kind == SPAWN:
        return f"no child process could be created{tail or ' (no reason given)'}"
    return ""


def _json_object(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    for candidate in (raw, raw[raw.find("{"):raw.rfind("}") + 1]):
        try:
            got = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(got, dict):
            return got
    return None


def preflight(which=None, run=None, timeout_s=PREFLIGHT_TIMEOUT_S) -> tuple:
    """``(kind, reason)`` before any lane is made; ``("", "")`` to proceed.

    Two seams, *which* and *run*, so the suite can drive every branch without
    a CLI on the machine. Only two answers refuse: no `claude` on PATH, and an
    `auth status` that says ``loggedIn`` is false. Everything else -- an
    exception, a timeout, a CLI that answers no JSON -- is inconclusive and
    proceeds, because the run path halts on the first real refusal anyway and
    a preflight that refused on its own uncertainty would block missions on
    machines whose CLI merely lacks the subcommand."""
    return _probe(which, run, timeout_s)[:2]


def _run_bounded(argv, *, timeout, **popen_kw):
    """`subprocess.run`'s contract for the probe, with `PREFLIGHT_TIMEOUT_S`
    holding for its whole process TREE -- this module's one process start.

    `subprocess.run` kills only its direct child on a timeout and then, on
    Windows, drains the pipes with no bound of its own. `claude` is
    `claude.CMD` on this machine, the CLI a grandchild of cmd.exe holding the
    inherited pipes, so a 2 s probe came back after 30.12 s (measured;
    tests/test_orchestrator_environment.py). The same shape as
    `plugin_cache._run_bounded`, over the same `driver` pieces: its own process
    group, held, the tree killed on the timeout, the rest of the wait bounded
    by `driver.reap`. The environment is inherited: `auth status` is the CLI's
    own probe, not an agent."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **popen_kw,
                            **driver.isolation_kwargs())
    driver.hold_tree(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:               # the timeout, or an interrupt
            driver.terminate_process_tree(proc)
            driver.reap(proc, 0.0)
            raise
    finally:
        driver.release_tree(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _probe(which, run, timeout_s) -> tuple:
    """``(kind, reason, payload)``: `preflight`'s verdict and what it read.

    The one place this module asks for a process (through `_run_bounded`),
    shared so that `preflight` and `check_claude_environment` cannot reach two
    verdicts about one answer. *payload* is the ``auth status`` object, or
    ``None`` where there is none -- no CLI, a probe that raised, a body that is
    not a JSON object."""
    which = which or shutil.which
    run = run or _run_bounded
    try:
        exe = which("claude")
    except Exception:                                   # noqa: BLE001
        exe = None
    if not exe:
        return CLI_MISSING, "no `claude` executable on PATH", None
    try:
        proc = run([str(exe), "auth", "status", "--json"], shell=False,
                   text=True, timeout=timeout_s)
    except Exception:                                   # noqa: BLE001
        return "", "", None
    payload = _json_object(getattr(proc, "stdout", ""))
    if payload is None:
        return "", "", None
    if payload.get("loggedIn") is False:
        method = str(payload.get("authMethod") or "none")
        reason = f"claude auth status: loggedIn=false (authMethod={method})"
        return AUTH, reason, payload
    return "", "", payload


def check_claude_environment(which=None, run=None) -> tuple:
    """``(ok, message, details)``: can this machine start a child at all?

    `preflight`'s verdict, answered for a caller that wants a sentence either
    way. *details* is the CLI's ``auth status`` object whenever it printed one,
    a signed-out answer included, and ``{}`` otherwise. ``ok`` is ``False``
    exactly where `preflight` refuses, and *message* is then `describe`'s fix;
    an inconclusive probe passes with a message saying the login was not
    confirmed, never one claiming that it was."""
    kind, reason, payload = _probe(which, run, PREFLIGHT_TIMEOUT_S)
    details = payload if payload is not None else {}
    if kind:
        return False, describe(kind, reason), details
    if details.get("loggedIn") is True:
        method = str(details.get("authMethod") or "unknown")
        return True, f"the Claude CLI is logged in (authMethod={method})", details
    return True, ("the Claude CLI's login could not be confirmed; proceeding, "
                  "and the run path halts on the first real refusal"), details


def refusal_line(argv0, message) -> str:
    """The one stderr line an environment refusal writes, without its newline:
    `FATAL_REFUSAL_FRAME` FIRST, as §10.9.2 writes it, so a wrapper can anchor
    on it (``^\\[FATAL PREFLIGHT REFUSAL``); then *argv0*, the command that
    refused, and *message*. The gate and `hive lintester` both build their line
    here, so the two cannot drift into two shapes."""
    return f"{FATAL_REFUSAL_FRAME}{argv0}: {message}"


def enforce_preflight_gate(argv0="hive lintester", exit=sys.exit):
    """Refuse this process before any lane is made, or hand back the details.

    A refusal writes ONE line to stderr -- `refusal_line`: the frame, *argv0*,
    the fix -- and calls *exit* with `EXIT_ENVIRONMENT_REFUSAL`. The default
    does not return; an *exit* that does gets ``None`` back, never a value a
    caller could read as a pass. A pass prints nothing and answers
    `check_claude_environment`'s *details*."""
    ok, message, details = check_claude_environment()
    if ok:
        return details
    print(refusal_line(argv0, message), file=sys.stderr, flush=True)
    exit(EXIT_ENVIRONMENT_REFUSAL)
    return None
