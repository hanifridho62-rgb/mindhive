# SPDX-License-Identifier: Apache-2.0
"""The gateway interceptor (blueprint P1f; MEB ORCH-02).

Two hook stages, one fact. `pre_tool_use` sees the INTENT -- a `gh pr merge` or
a `git merge` about to run -- and writes it to a per-session sidecar.
`post_tool_use` sees the RESULT, reads that sidecar back, asks `gh` and git what
actually happened, and appends one `interceptor_signal` naming the actor
(`claude_code`, `ci_bot` or `human_manual`). Nothing else in the system can
answer "who landed this": a tool result says only that a command ran and what it
printed, and `gh pr merge` prints the same thing whether the agent merged its
own PR or a person had already merged it a minute earlier.

## Three properties, and why each one is load-bearing

* **It never blocks.** `allowed` is ALWAYS True. This is an observer sitting on
  the `pre_tool_use` path, where a False would stop the operator's own
  `git merge`. There is deliberately no code path that can answer anything else:
  the field is set once, in the dataclass's default.
* **It never writes to stdout.** `commands.ledger.cmd_hook`'s stdout IS the hook
  protocol -- a JSON decision object, or nothing. The interceptor's entire
  output is the ledger and its own sidecar, and a stray `print` here would be
  parsed by the hook runner as a decision.
* **It is fail-open end to end.** An unparseable payload, a missing `gh`, an
  unwritable state directory, a probe that raises: every one of them ends as a
  verdict carrying a reason, never as an exception reaching the hook.

## Why the intent is written down instead of re-parsed

`post_tool_use` gets the same `tool_input` the pre stage saw, so the command
could be re-read there and the sidecar skipped. The sidecar buys two things the
re-read cannot. It PAIRS the two stages, so a post for any of the thousands of
other tool calls costs one `stat` and no subprocess -- the probe only runs for a
command whose intent was recorded. And it timestamps the intent, so a pre whose
post never arrived (the session died, the tool hung) expires after
`INTENT_TTL_S` instead of attaching itself to an unrelated later command.

## What the probe asks, and in what order

The PR record first, because it is the only thing that survives a squash merge,
which leaves no ancestor commit to find. Git ancestry second, because `gh` may
be absent, unauthenticated, or answering about a different remote. A merge
neither can prove is recorded as unmerged with the reason -- never guessed.

A PR found CLOSED without being merged is abandoned work, and `roadmap.
retire_landed` is the thing that already knows what to do with that. The
interceptor calls it rather than inventing a second retirement path that would
drift from the first.

The whole probe runs under ONE budget (`PROBE_BUDGET_S`), not one timeout per
call. Each subprocess is bounded by `timeout_s` on its own, but a `git merge`
is probed with two of them (`merge-base`, then `rev-parse`), and two 20 s
timeouts stack into 40 s inside a hook whose own runner allows 60. So every
call is handed no more than what is left of the budget, a call nothing is left
for is not made at all, and the reason names it -- a skipped question must
never read as an answered one.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from dataclasses import dataclass, field

from .. import events, paths
# By identity, and at the TOP: `gateway` imports nothing from here, so there is
# no cycle and either module may be imported first.
from .gateway import (ACTION_TYPES, COMMAND_SCAN_CHARS,  # noqa: F401
                      MERGE_ACTORS, _dict, _int, _segments, _text, action_of,
                      branch_of, merge_actor_of)

__all__ = [
    "ACTION_TYPES", "MERGE_ACTORS", "INTENT_TTL_S", "PROBE_BUDGET_S",
    "PHASE_SCAN_CHARS",
    "InterceptorVerdict", "InterceptorPayload", "GatewayInterceptor",
    "validate_payload",
]

# Ten minutes. Long enough for a slow `gh pr merge` behind a CI wait, short
# enough that a dead session's intent cannot attach to tomorrow's command.
INTENT_TTL_S = 600.0

# The whole probe, every subprocess together. Thirty seconds fits one full
# `timeout_s` call with room for a second, shortened one, and stays inside the
# 60 s a hook runner allows the stage that also has to append the signal.
PROBE_BUDGET_S = 30.0

# The stream a `post_tool_use` carries is a tool's whole output, which can be
# megabytes. The phase line, when there is one, is at the top by construction.
PHASE_SCAN_CHARS = 500
MAX_PHASE_CHARS = 64

INTENT_PREFIX = "interceptor_intent_"

# Anchored at a line start on purpose: an indented `fase:` is a quotation
# inside a diff or a log, not this turn's own phase line.
_PHASE_LINE = re.compile(r"^(?:fase|phase|status|step)\s*:[ \t]*(\S[^\r\n]*)",
                         re.IGNORECASE | re.MULTILINE)


# --- the verdict and the payload ---------------------------------------------------

@dataclass(frozen=True)
class InterceptorVerdict:
    """What the interceptor observed. `allowed` is True by construction and
    nothing in this module ever passes it -- see the module docstring."""

    allowed: bool = True
    action_type: str = ""
    phase: str = ""
    merge_actor: str = ""
    ci_status: str = ""
    reason: str = ""


@dataclass(frozen=True)
class InterceptorPayload:
    """The `interceptor_signal` payload, in one place.

    The writer builds it and `validate_payload` checks it against the same
    field list, so the contract and the row cannot drift apart the way two
    hand-written key sets do."""

    tool_name: str = ""
    phase: str = ""
    action_type: str = ""
    pr_number: int = 0
    lane_commit_sha: str = ""
    merge_actor: str = ""
    merged: bool = False
    ci_status: str = ""
    reason: str = ""
    ts: float = 0.0
    exit_code: int = 0

    def as_payload(self) -> dict:
        row = {name: getattr(self, name) for name in REQUIRED_KEYS}
        row["exit_code"] = int(self.exit_code)
        return row


REQUIRED_KEYS = ("tool_name", "phase", "action_type", "pr_number",
                 "lane_commit_sha", "merge_actor", "merged", "ci_status",
                 "reason", "ts")
OPTIONAL_KEYS = ("exit_code",)


def validate_payload(payload) -> None:
    """The `interceptor_signal` contract, enforced at `hive append` and at the
    dashboard's POST (DATA_MODEL.md section 6.2 -- `contracts.py` dispatches
    here). Raises ``ValueError`` describing the first thing that is wrong."""
    if not isinstance(payload, dict):
        raise ValueError("interceptor_signal payload must be an object")
    missing = [k for k in REQUIRED_KEYS if k not in payload]
    if missing:
        raise ValueError("interceptor_signal payload is missing "
                         + ", ".join(missing))
    unknown = sorted(set(payload) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS))
    if unknown:
        raise ValueError("interceptor_signal payload has unknown key(s): "
                         + ", ".join(unknown))
    for key in ("tool_name", "phase", "lane_commit_sha", "ci_status", "reason"):
        if not isinstance(payload[key], str):
            raise ValueError(f"interceptor_signal {key} must be a string")
    if payload["action_type"] not in ACTION_TYPES:
        raise ValueError("interceptor_signal action_type must be one of "
                         + ", ".join(ACTION_TYPES))
    if payload["merge_actor"] not in MERGE_ACTORS:
        raise ValueError("interceptor_signal merge_actor must be one of "
                         + ", ".join(a or "(empty)" for a in MERGE_ACTORS))
    # `bool` before `int`: in Python `True` IS an int, so an unguarded int
    # check would take `merged=1` and a `pr_number=True` alike.
    if not isinstance(payload["merged"], bool):
        raise ValueError("interceptor_signal merged must be true or false")
    number = payload["pr_number"]
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise ValueError("interceptor_signal pr_number must be an integer >= 0")
    if isinstance(payload["ts"], bool) or not isinstance(
            payload["ts"], (int, float)):
        raise ValueError("interceptor_signal ts must be a number")
    code = payload.get("exit_code", 0)
    if isinstance(code, bool) or not isinstance(code, int):
        raise ValueError("interceptor_signal exit_code must be an integer")


# --- the interceptor ---------------------------------------------------------------

@dataclass
class GatewayInterceptor:
    """Observe the git gateway from the two hook stages.

    *repo_root* is the checkout the probes run in; *runner* is
    `repo._default_runner`'s `(argv, cwd, timeout_s) -> (rc, out, err)`, taken
    as a seam so the probe can be driven by a fake. Reusing the incumbent
    runner is deliberate: this module spawns nothing of its own, so there is no
    second subprocess policy to keep in step with the first."""

    repo_root: str = ""
    runner: object = None
    project: object = None
    timeout_s: float = 20.0
    # One deadline across the probe's subprocesses (module docstring); a
    # budget of 0 asks nothing. *monotonic* is the clock it is measured on,
    # a seam so a test can spend the budget without waiting it out.
    probe_budget_s: float = PROBE_BUDGET_S
    monotonic: object = time.monotonic
    warnings: list = field(default_factory=list)

    # --- the two stages ---

    def observe_intent(self, raw) -> InterceptorVerdict:
        """`pre_tool_use`: record what is about to run. Never decides."""
        try:
            return self._intent(raw)
        except Exception as exc:                        # noqa: BLE001
            return self._shrug(exc)

    def observe_result(self, raw, failed: bool = False) -> InterceptorVerdict:
        """`post_tool_use` (and `post_tool_use_failure` with *failed*): pair
        the result with its intent, probe, and record one signal."""
        try:
            return self._result(raw, failed)
        except Exception as exc:                        # noqa: BLE001
            return self._shrug(exc)

    # --- what it recognises ---

    def _enabled(self) -> bool:
        return _text(os.environ.get("MINDHIVE_INTERCEPTOR", "1")).strip() != "0"

    def _shrug(self, exc) -> InterceptorVerdict:
        return InterceptorVerdict(reason=f"{type(exc).__name__}: {exc}")

    def _command_of(self, hook) -> tuple:
        """(tool_name, command). Only the Bash tool is watched: an `Edit`
        writing the text `gh pr merge` into a file is not a merge."""
        tool = _text(hook.get("tool_name"))
        if tool != "Bash":
            return tool, ""
        return tool, _text(_dict(hook.get("tool_input")).get("command")).strip()

    # The grammar lives in `gateway.py` (the MEB 500-line ceiling); these stay
    # as methods because that is how the two stages already read, and because
    # a seam on the instance is the one a future config could use.
    def _action_of(self, command) -> tuple:
        """(action_type, pr_number) -- `gateway.action_of`."""
        return action_of(command)

    def _branch_of(self, command) -> str:
        """The ref a `git merge` names -- `gateway.branch_of`."""
        return branch_of(command)

    def _recognize_phase(self, hook, stream) -> str:
        """Two layers. The tool's own metadata first, because it is asserted
        rather than parsed; then a `fase:`/`phase:`/`status:`/`step:` line at
        the start of the stream. Bounded: the scan reads
        `PHASE_SCAN_CHARS` characters and takes a value no longer than
        `MAX_PHASE_CHARS`, so a 10 MB log costs a fixed slice and a paragraph
        following a colon is not mistaken for a phase."""
        named = _text(_dict(hook).get("phase")).strip()
        if named:
            return named[:MAX_PHASE_CHARS]
        hit = _PHASE_LINE.search(_text(stream)[:PHASE_SCAN_CHARS])
        if not hit:
            return ""
        value = hit.group(1).strip()
        return value if 0 < len(value) <= MAX_PHASE_CHARS else ""

    # --- the intent sidecar ---

    def _intent_path(self, session):
        """Through `events.segment_name`, so a session id carrying a traversal
        is quarantined to a safe stem and lands inside the state directory like
        every other sidecar in the store."""
        return (paths.state_dir(self.project)
                / f"{INTENT_PREFIX}{events.segment_name(session)[:-6]}.json")

    def _intent(self, raw) -> InterceptorVerdict:
        hook = _dict(_loads(raw))
        session = _text(hook.get("session_id")).strip()
        tool, command = self._command_of(hook)
        action, number = self._action_of(command) if command else ("", 0)
        phase = self._recognize_phase(hook, command)
        if not action:
            return InterceptorVerdict(phase=phase, reason="not a gateway act")
        if not self._enabled():
            return InterceptorVerdict(action_type=action, phase=phase,
                                      reason="MINDHIVE_INTERCEPTOR=0")
        if not session:
            return InterceptorVerdict(action_type=action, phase=phase,
                                      reason="no session id; nothing to pair")
        record = {"ts": time.time(), "session_id": session, "tool_name": tool,
                  "action_type": action, "pr_number": number,
                  "command": command[:400], "phase": phase}
        try:
            paths.write_text_atomic(self._intent_path(session),
                                    json.dumps(record))
        except Exception as exc:                        # noqa: BLE001
            self.warnings.append(f"intent not written: {exc}")
            return InterceptorVerdict(action_type=action, phase=phase,
                                      reason=f"intent not written: {exc}")
        return InterceptorVerdict(action_type=action, phase=phase,
                                  reason="intent recorded")

    def _read_intent(self, session):
        """The paired intent, or ``None``. A stale one is deleted as it is
        read: leaving it would let it pair with a later, unrelated command."""
        path = self._intent_path(session)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001
            return None
        if not isinstance(record, dict):
            _unlink(path)
            return None
        if time.time() - float(record.get("ts") or 0.0) > INTENT_TTL_S:
            _unlink(path)
            return "stale"
        return record

    # --- the result ---

    def _result(self, raw, failed) -> InterceptorVerdict:
        hook = _dict(_loads(raw))
        # Recognise the command BEFORE any path is built. `_intent_path` goes
        # through `paths.state_dir`, which mkdirs, so reading the sidecar first
        # made every one of the thousands of Read, Edit and Grep calls in a
        # session pay a mkdir plus a failed open -- for a question a regex over
        # the command answers without touching the disk. `_intent` records a
        # sidecar for a Bash gateway act and nothing else, so anything else can
        # be answered here. A post that does NOT carry its command still falls
        # through to the pairing: absent is not the same as "not a merge".
        tool, command = self._command_of(hook)
        if (tool and tool != "Bash") or (command and not self._action_of(command)[0]):
            return InterceptorVerdict(reason="not a gateway act")
        session = _text(hook.get("session_id")).strip()
        if not session or not self._enabled():
            return InterceptorVerdict(reason="no session id; nothing to pair"
                                             if not session else
                                             "MINDHIVE_INTERCEPTOR=0")
        record = self._read_intent(session)
        if record is None:
            return InterceptorVerdict(reason="no paired intent")
        if record == "stale":
            return InterceptorVerdict(reason="the paired intent was stale")
        _unlink(self._intent_path(session))

        response = _dict(hook.get("tool_response"))
        stream = _text(response.get("stdout")) + _text(response.get("stderr"))
        code = _exit_code(response, failed)
        action = _text(record.get("action_type"))
        number = int(record.get("pr_number") or 0)
        phase = (_text(record.get("phase"))
                 or self._recognize_phase(hook, stream))
        row = InterceptorPayload(
            tool_name=_text(record.get("tool_name")) or "Bash", phase=phase,
            action_type=action, pr_number=number, ts=time.time(),
            exit_code=code)
        if code != 0:
            # The exit code already said nothing was merged. Probing would buy
            # a round trip to learn what is on the line above.
            row = _with(row, merged=False, merge_actor="", reason="tool_failed")
            abandoned = False
        else:
            row, abandoned = self._probe(row, record, session)
        self._append(row, session)
        if abandoned:
            self._sweep(session)
        return InterceptorVerdict(action_type=action, phase=phase,
                                  merge_actor=row.merge_actor,
                                  ci_status=row.ci_status, reason=row.reason)

    # --- the probe ---

    def _left(self, deadline) -> float:
        """Seconds of the probe's budget still unspent, never below zero."""
        return max(0.0, deadline - self.monotonic())

    def _run(self, argv, deadline=None):
        """One subprocess, given `timeout_s` or what is left of the budget,
        whichever is less. Nothing left means nothing is spawned: the answer
        is the runner's own failure shape, so every caller reads one."""
        timeout = self.timeout_s
        if deadline is not None:
            left = self._left(deadline)
            if left <= 0:
                return 1, "", "probe budget spent"
            timeout = min(timeout, left)
        runner = self.runner
        if runner is None:
            from .. import repo as repo_mod
            runner = repo_mod._default_runner
        try:
            return runner(list(argv), self.repo_root or None, timeout)
        except Exception as exc:                        # noqa: BLE001
            return 1, "", f"{type(exc).__name__}: {exc}"

    def _gh_record(self, number=0, deadline=None):
        """The PR record, or ``None``. `gh` is named rather than resolved on
        PATH: the runner already reports a missing executable as a non-zero
        code, and one shape of failure is easier to be total about than two.

        With no *number*, `gh pr view` resolves the CURRENT BRANCH's PR -- the
        same PR `gh pr merge --squash` would have merged. Passing a literal 0
        instead would ask about a PR that cannot exist and answer nothing."""
        argv = ["gh", "pr", "view"]
        if number:
            argv.append(str(number))
        argv += ["--json", "state,mergedAt,url,mergedBy,author,number"]
        rc, out, _err = self._run(argv, deadline)
        if rc != 0:
            return None
        try:
            record = json.loads(out)
        except Exception:                               # noqa: BLE001
            return None
        return record if isinstance(record, dict) else None

    def _is_ancestor(self, branch, deadline=None) -> bool:
        rc, _out, _err = self._run(["git", "merge-base", "--is-ancestor",
                                    str(branch), "HEAD"], deadline)
        return rc == 0

    def _rev_of(self, branch, deadline=None) -> str:
        rc, out, _err = self._run(["git", "rev-parse", "--verify", str(branch)],
                                  deadline)
        return out.strip() if rc == 0 else ""

    def _probe(self, row, record, session):
        """Ask the PR first, git second, and claim nothing neither proved.

        Answers `(row, abandoned)`: *abandoned* is True for a PR closed WITHOUT
        merging, whose roadmap sweep `_result` runs after the signal is
        recorded rather than here. See `_sweep`.

        Every subprocess here shares one deadline, `probe_budget_s` from now
        (module docstring). A step the budget cannot pay for is skipped and
        listed in the reason as not asked; it is never recorded as answered."""
        reasons, skipped = [], []
        merged, actor, status = False, "", ""
        number = row.pr_number
        deadline = self.monotonic() + self.probe_budget_s
        # Every `pr_*` act is probed, with the number when the command named
        # one and without when it did not. Recording `merged: false` for a
        # `gh pr merge --squash` without asking anything would have written a
        # false fact about a PR that WAS merged.
        pr = None
        if row.action_type.startswith("pr_"):
            if self._left(deadline) <= 0:
                skipped.append("gh")
            else:
                pr = self._gh_record(number, deadline)
                if pr is None:
                    reasons.append("gh could not answer")
        abandoned = False
        if pr is not None:
            status = _text(pr.get("state")).upper()
            merged = status == "MERGED"
            if merged:
                actor = merge_actor_of(_dict(pr.get("mergedBy")).get("login"))
            reasons.append(f"gh says {status or 'nothing'}")
            abandoned = status == "CLOSED" and not merged
            number = number or _int(pr.get("number"))
        sha = ""
        branch = self._branch_of(_text(record.get("command")))
        if not merged and row.action_type == "git_merge" and branch:
            if self._left(deadline) <= 0:
                skipped.append("git merge-base")
            elif self._is_ancestor(branch, deadline):
                merged, reasons = True, reasons + ["git says it is an ancestor"]
                actor = actor or "unknown"
            else:
                reasons.append("git says it is not an ancestor")
            if self._left(deadline) <= 0:
                skipped.append("git rev-parse")
            else:
                sha = self._rev_of(branch, deadline)
        if skipped:
            reasons.append(f"{', '.join(skipped)} not asked: probe budget "
                           f"({self.probe_budget_s:g}s) spent")
        if not merged and not actor:
            actor = "unknown" if reasons else ""
        return _with(row, merged=merged, merge_actor=actor, ci_status=status,
                     pr_number=number, lane_commit_sha=sha,
                     reason="; ".join(reasons)), abandoned

    def _sweep(self, session) -> None:
        """A PR closed without merging is abandoned work, and the roadmap sweep
        is what knows how to retire the plan behind it.

        Runs AFTER the signal is appended, never before. `roadmap.retire_landed`
        folds the WHOLE ledger and probes every live plan with up to three
        subprocesses apiece at `repo.PROBE_TIMEOUT_SECONDS` each, so on the
        `post_tool_use` path it is minutes of work -- and a hook killed for
        taking them would have lost the very signal it was holding. The fact is
        recorded first; the sweep is its consequence, and a consequence is
        allowed to be slow."""
        try:
            from .. import roadmap
            roadmap.retire_landed(project=self.project, session_id=session)
        except Exception as exc:                        # noqa: BLE001
            self.warnings.append(f"the roadmap sweep failed: {exc}")

    def _append(self, row, session) -> None:
        payload = row.as_payload()
        try:
            validate_payload(payload)
            events.append_event("interceptor_signal", payload,
                                session_id=session, project=self.project,
                                tags=["auto", "gateway"])
        except Exception as exc:                        # noqa: BLE001
            self.warnings.append(f"signal not recorded: {exc}")


# --- the small totals ---------------------------------------------------------------

def _loads(raw):
    try:
        return json.loads(raw)
    except Exception:                                   # noqa: BLE001
        return {}


def _unlink(path) -> None:
    try:
        path.unlink()
    except Exception:                                   # noqa: BLE001
        pass


def _exit_code(response, failed) -> int:
    for key in ("exit_code", "returncode", "exitCode"):
        value = response.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    if response.get("interrupted") is True:
        return 1
    return 1 if failed else 0


def _with(row, **fields) -> InterceptorPayload:
    return dataclasses.replace(row, **fields)
