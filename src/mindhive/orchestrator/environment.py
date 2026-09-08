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

## What it does not do

It does not log in. Credentials are the owner's, and a pipeline that could fix
its own authentication would be a pipeline that could be pointed at someone
else's. It names the fix and stops.
"""
from __future__ import annotations

import json
import shutil
import subprocess

from .. import paths

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
    which = which or shutil.which
    run = run or subprocess.run
    try:
        exe = which("claude")
    except Exception:                                   # noqa: BLE001
        exe = None
    if not exe:
        return CLI_MISSING, "no `claude` executable on PATH"
    try:
        proc = run([str(exe), "auth", "status", "--json"], shell=False,
                   capture_output=True, text=True, timeout=timeout_s,
                   creationflags=paths.NO_WINDOW)
    except Exception:                                   # noqa: BLE001
        return "", ""
    payload = _json_object(getattr(proc, "stdout", ""))
    if payload is None:
        return "", ""
    if payload.get("loggedIn") is False:
        method = str(payload.get("authMethod") or "none")
        return AUTH, f"claude auth status: loggedIn=false (authMethod={method})"
    return "", ""
