# src/mindhive/orchestrator/gate.py
"""The per-unit test gate: "validated" means the lane's own tests passed.

## What "validated" used to mean

`lintester._run_child` reported a unit validated when its child exited cleanly.
Nothing ran a test against the lane, so a child that broke the module it was
sent to fix and exited 0 was a validated unit, and the `hive selftest` gate the
PRD names existed in this package only as a sentence in a docstring. This
module is that gate, on the lane path, in two scopes:

* **targeted**, per unit, in the unit's own lane checkout: every test file
  that mentions a module the unit changed, plus every test file the unit itself
  added or changed. Narrow, because a mission fans N units at one repository
  and N full suites would cost more than the children did.
* **full**, once per tree at mission end: `verify.default_argv` over the whole
  suite. This is the mission-level claim; the targeted run is the unit's.

## Two facts this module keeps straight

**A red gate is the task's failure, not the machine's.** `failure_outcome`
answers the shape `_run_child` answers, with `executed=True` and
`result="fail"`, so `queue._succeeded` reads it as a failure and
`environment.environment_failure` reads it as the unit's own. Anything else
would route a child's bug to the halt that exists for a logged-out CLI, and
stop every lane for it.

**A targeted run is not a ledger verification.** `replenish` clears the "code
changed, nothing verified since" gap on ANY exit-0 `verification` event and
scores injected work on it; `premise` reads the last one's counts. A two-file
pass recorded as one would clear that gap falsely. So `SuiteGate.check` runs
with `record=False`, and only `SuiteGate.full` appends — stamped `scope: full`
with the tree and the mission, so a reader can tell what it proved.

## The vacuous pass is recorded, not hidden

A unit whose changed modules no test mentions passes its targeted gate with
`scope: none` and a reason saying so. The fail-closed alternative — refuse every
unit that adds an untested module — was rejected: it fails the unit for the
SUITE's gap, and the closing full suite is where the tree's claim is made. The
row is in the report so an operator can see which units were judged by nothing.

## The first real stderr the repair loop has ever received

No real child populates `_RunOutcome.stderr` (`agent.run` answers a JSON
envelope), so `resilience.repair_prompt` had never quoted anything real into a
retry and the board's "self-correcting (stderr injected)" caption described
nothing that happened. The gate's `tail` is the last lines of pytest's output,
run through `stabilise` so the failure signature the breaker hashes does not
change with the wall clock or the temp directory, and it becomes the retry's
`stderr`.

Total. `gated` never raises into the scheduler, never touches a unit whose
child failed, and `SuiteGate` runs pytest in a lane checkout or not at all.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import verify as verify_mod
from . import queue as queue_mod
from . import resilience as res_mod
from . import worktree as wt_mod

# `GateResult.scope`. `none` is the recorded vacuous pass -- or a refusal,
# which the `ok` flag separates from it.
TARGETED = "targeted"
FULL = "full"
NONE = "none"

DEFAULT_UNIT_TIMEOUT_S = 600
DEFAULT_SUITE_TIMEOUT_S = 1800

# What `verify._stream` answers when it kills a run at its cap: the code
# `claude_cli.run_headless` answers for a child that outlived its timeout, so a
# killed gate and a killed child read alike in a report. Read from the module
# that answers it, so the two cannot drift.
TIMEOUT_EXIT = verify_mod.TIMEOUT_EXIT

# Stop early on a lane that is on fire. The tail carries the diagnosis either
# way, and the twenty-sixth failure of one broken import adds nothing to it.
MAX_FAIL = 25

# Where a source tree keeps its packages and its suite. A changed path under
# `src/` maps to a module by dropping the prefix; one under `tests/` is a test
# file the unit itself touched.
_SRC_PREFIX = "src/"
_TESTS_DIR = "tests"


@dataclass(frozen=True)
class GateResult:
    """One gate run. Frozen: it is reported, hashed and stored, never edited.

    `targets` and `argv` are what ran; `tail` is what it said, already
    stabilised and bounded to `resilience.MAX_STDERR_CHARS`; `duration_s` is
    the whole gate, target discovery included, because none of it is the
    child's time and `lintester._calibrate` subtracts it."""

    ok: bool = False
    scope: str = NONE
    targets: tuple = ()
    argv: tuple = ()
    exit_code: object = None
    counts: dict = field(default_factory=dict)
    tail: str = ""
    artifact: object = None
    reason: str = ""
    duration_s: float = 0.0
    task_id: str = ""
    lane_id: int = -1
    attempt: int = 0

    def as_dict(self) -> dict:
        return {"ok": bool(self.ok), "scope": self.scope,
                "targets": list(self.targets), "argv": list(self.argv),
                "exit_code": self.exit_code, "counts": dict(self.counts),
                "tail": self.tail, "artifact": self.artifact,
                "reason": self.reason,
                "duration_s": round(float(self.duration_s), 3),
                "task_id": self.task_id, "lane_id": int(self.lane_id),
                "attempt": int(self.attempt)}


# --- which tests -------------------------------------------------------------

def import_name(rel_path) -> str:
    """``mindhive.orchestrator.gate`` for ``src/mindhive/orchestrator/gate.py``;
    ``""`` for anything that is not a Python source file."""
    text = str(rel_path or "").replace("\\", "/").strip("/")
    if not text.endswith(".py"):
        return ""
    if text.startswith(_SRC_PREFIX):
        text = text[len(_SRC_PREFIX):]
    parts = [p for p in text[:-3].split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return ""
    return ".".join(parts)


def _mention_patterns(module) -> list:
    """Regexes that say a test file reaches *module*: the dotted name anywhere
    (an `import a.b.c`, a `from a.b.c import`, a patch-target string), or a
    `from a.b import (... c ...)` naming the leaf -- the form this repo's own
    tests use, parenthesised or not."""
    name = str(module or "").strip()
    if not name:
        return []
    out = [re.compile(r"\b" + re.escape(name) + r"\b")]
    if "." in name:
        parent, leaf = name.rsplit(".", 1)
        out.append(re.compile(
            r"from\s+" + re.escape(parent) + r"\s+import\s+"
            r"(?:\([^)]*\b" + re.escape(leaf) + r"\b[^)]*\)"
            r"|[^\n]*\b" + re.escape(leaf) + r"\b)"))
    return out


def _named_for(module) -> set:
    """`tests/test_<leaf>.py` and `tests/test_<pkg>_<leaf>.py`: the two names
    this repo gives a module's own test file."""
    parts = str(module or "").split(".")
    leaf = parts[-1]
    names = {f"test_{leaf}.py"}
    if len(parts) >= 2:
        names.add(f"test_{parts[-2]}_{leaf}.py")
    return names


def tests_mentioning(tests_root, modules) -> list:
    """Sorted, *tests_root*-relative POSIX paths of every `test_*.py` under it
    that mentions one of *modules* or is named for one.

    Total: a file that cannot be decoded is read with replacement, one that
    cannot be read is skipped, and a missing tree answers ``[]``."""
    root = Path(str(tests_root or ""))
    wanted = [str(m) for m in (modules or ()) if m]
    if not wanted:
        return []
    patterns = [p for m in wanted for p in _mention_patterns(m)]
    names = set().union(*(_named_for(m) for m in wanted))
    try:
        files = sorted(root.rglob("test_*.py"))
    except OSError:                                     # pragma: no cover - odd fs
        return []
    out = []
    for path in files:
        try:
            if not path.is_file():
                continue
            if path.name not in names:
                text = path.read_text(encoding="utf-8", errors="replace")
                if not any(p.search(text) for p in patterns):
                    continue
            out.append(path.relative_to(root).as_posix())
        except (OSError, ValueError):
            continue
    return out


# --- what it said ------------------------------------------------------------

_DURATION_RE = re.compile(r"\bin \d+(?:\.\d+)?s\b(?: \(\d+:\d\d:\d\d\))?")
_TMPDIR_RE = re.compile(r"pytest-of-[^\\/\s]+([\\/])pytest-\d+")


def stabilise(text) -> str:
    """*text* with the wall clock and the temp directory taken out, so two runs
    of one failure read as ONE failure to the signature the breaker hashes."""
    out = str(text or "")
    out = _DURATION_RE.sub("in <t>s", out)
    return _TMPDIR_RE.sub(r"pytest-of-<user>\1pytest-<n>", out)


def failure_outcome(res):
    """The scheduler-facing outcome for a red gate: the shape
    `lintester._run_child` answers, so nothing downstream can tell a unit that
    failed its tests from a unit whose child failed -- which is the point.
    `executed=True` because a child DID run; see the module docstring."""
    from .lintester import _RunOutcome

    return _RunOutcome(ok=False, result="fail", executed=True,
                       exit_code=res.exit_code,
                       reason=res.reason or f"{res.scope} tests failed",
                       stderr=res.tail)


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _result(rec, scope, targets, argv, started, task_id="", lane_id=-1,
            attempt=0) -> GateResult:
    """A `GateResult` from what `verify.run_and_record` answered."""
    rec = dict(rec or {})
    code = rec.get("exit_code")
    counts = dict(rec.get("counts") or {})
    raw = str(rec.get("tail") or rec.get("excerpt") or "")
    tail = stabilise(res_mod._tail(raw, res_mod.MAX_STDERR_CHARS))
    error = str(rec.get("error") or "")
    ok = code == 0
    if ok:
        reason = ""
    else:
        bits = [f"{scope} tests: exit {code}"]
        if counts.get("failed"):
            bits.append(f"{counts['failed']} failed")
        if counts.get("errors"):
            bits.append(f"{counts['errors']} error(s)")
        if error:
            bits.append(error)
        reason = ", ".join(bits)
    return GateResult(ok=ok, scope=scope, targets=tuple(targets),
                      argv=tuple(str(a) for a in argv), exit_code=code,
                      counts=counts, tail=tail, artifact=rec.get("artifact"),
                      reason=reason, duration_s=time.monotonic() - started,
                      task_id=str(task_id or ""), lane_id=_int(lane_id, -1),
                      attempt=_int(attempt))


# --- the gate ------------------------------------------------------------------

@dataclass
class SuiteGate:
    """The gate, bound to one main checkout and one namespace.

    *runner* is the seam: anything with `verify.run_and_record`'s signature.
    ``None`` means the real one, which runs a real subprocess. *root* is the
    MAIN checkout -- the guard `check` stands on -- and ``None`` means the
    working directory, the default the rest of the pipeline takes."""

    root: object = None
    project: object = None
    unit_timeout_s: int = DEFAULT_UNIT_TIMEOUT_S
    suite_timeout_s: int = DEFAULT_SUITE_TIMEOUT_S
    runner: object = None

    def _base(self) -> Path:
        return Path(self.root) if self.root else Path.cwd()

    def _run(self, argv, cwd, timeout_s, record, extra) -> dict:
        """One run through the seam, in *cwd*, importing *cwd*'s own `src`.

        `selftest.child_env` is the editable-install trap's fix: without it a
        lane's pytest imports whatever checkout the venv was installed
        against, and judges a child's work against code it never touched."""
        from .. import selftest as st_mod

        runner = self.runner or verify_mod.run_and_record
        return dict(runner(list(argv), project=self.project, echo=False,
                           emit=None, env=st_mod.child_env(cwd), cwd=str(cwd),
                           timeout_s=int(timeout_s), record=bool(record),
                           extra=extra) or {})

    def _own_files(self, task) -> list:
        """A unit's own files, repo-relative: the fallback for a lane whose
        child changed nothing, because the unit still claimed a job and its
        files say which tests that job is judged by."""
        try:
            anchor = self._base().resolve()
        except OSError:                                 # pragma: no cover - odd fs
            return []
        out = []
        for path in getattr(task, "files", ()) or ():
            try:
                out.append(Path(path).resolve().relative_to(anchor).as_posix())
            except (OSError, ValueError):
                continue
        return out

    def targets(self, lane_path, task) -> list:
        """The lane-relative test files *task* is judged by, in a fixed order:
        the tests the unit itself touched, then every test mentioning a module
        it changed."""
        from ..mindnet import worktree as mn_wt

        lane = Path(str(lane_path))
        changed = mn_wt.changed_files(lane)
        if not changed:
            changed = self._own_files(task)
        modules, own = [], []
        for rel in changed:
            rel = str(rel).replace("\\", "/").strip("/")
            if not rel.endswith(".py"):
                continue
            if rel.startswith(_TESTS_DIR + "/"):
                if (lane / rel).is_file():
                    own.append(rel)
                continue
            name = import_name(rel)
            if name:
                modules.append(name)
        found = [f"{_TESTS_DIR}/{rel}" for rel
                 in tests_mentioning(lane / _TESTS_DIR, modules)]
        return list(dict.fromkeys([*own, *found]))

    def check(self, lane_path, task, attempt=0) -> GateResult:
        """The targeted gate for *task*, run IN its lane. Never raises."""
        started = time.monotonic()
        tid = str(getattr(task, "task_id", "") or "")
        lane_id = _int(getattr(task, "lane_id", -1), -1)
        attempt = _int(attempt)
        lane = Path(str(lane_path or ""))
        if not str(lane_path or "") or not wt_mod.is_lane_path(self._base(), lane):
            return GateResult(
                ok=False, scope=NONE, task_id=tid, lane_id=lane_id,
                attempt=attempt, duration_s=time.monotonic() - started,
                reason=(f"refused: {lane} is not a lane checkout under "
                        f"{self._base()}, and the gate runs pytest nowhere "
                        f"else"))
        try:
            targets = self.targets(lane, task)
        except Exception as exc:                        # noqa: BLE001
            return GateResult(
                ok=False, scope=NONE, task_id=tid, lane_id=lane_id,
                attempt=attempt, duration_s=time.monotonic() - started,
                reason=f"could not choose tests: {type(exc).__name__}: {exc}")
        if not targets:
            return GateResult(
                ok=True, scope=NONE, task_id=tid, lane_id=lane_id,
                attempt=attempt, duration_s=time.monotonic() - started,
                reason=("no test mentions the changed modules; the closing "
                        "full suite is this unit's only judge"))
        argv = [sys.executable, "-m", "pytest", "-q", f"--maxfail={MAX_FAIL}",
                *(str(lane / t) for t in targets)]
        try:
            rec = self._run(argv, lane, self.unit_timeout_s, record=False,
                            extra=None)
        except Exception as exc:                        # noqa: BLE001
            return GateResult(
                ok=False, scope=TARGETED, targets=tuple(targets),
                argv=tuple(argv), task_id=tid, lane_id=lane_id,
                attempt=attempt, duration_s=time.monotonic() - started,
                reason=f"the gate could not run: {type(exc).__name__}: {exc}")
        return _result(rec, TARGETED, targets, argv, started, tid, lane_id,
                       attempt)

    def full(self, cwd, mission_id="") -> GateResult:
        """The whole suite, once, in *cwd*. This one IS recorded as a
        `verification`, stamped with its scope, tree and mission."""

        started = time.monotonic()
        tree = Path(str(cwd or ""))
        argv = verify_mod.default_argv(tree)
        try:
            rec = self._run(argv, tree, self.suite_timeout_s, record=True,
                            extra={"scope": FULL,
                                   "mission_id": str(mission_id or ""),
                                   "tree": str(tree)})
        except Exception as exc:                        # noqa: BLE001
            return GateResult(
                ok=False, scope=FULL, argv=tuple(argv),
                duration_s=time.monotonic() - started,
                reason=f"the suite could not run: {type(exc).__name__}: {exc}")
        return _result(rec, FULL, (), argv, started)


def full_row(gate, tree_path, lane_id, mission_id="") -> dict:
    """One closing-suite row for a lane tree, as a dict. Total: a suite that
    could not run is a red row naming the exception, never a raise -- the
    teardown after it has to happen either way."""
    try:
        res = gate.full(tree_path, mission_id=mission_id)
        res = dataclasses.replace(res, lane_id=lane_id)
    except Exception as exc:                            # noqa: BLE001
        res = GateResult(ok=False, scope=FULL, lane_id=lane_id,
                         reason=f"the suite could not run: "
                                f"{type(exc).__name__}: {exc}")
    return res.as_dict()


# --- the wrapper -----------------------------------------------------------------

def _keep(record, res) -> None:
    if record is None:
        return
    try:
        record.append(res.as_dict())
    except Exception:                                   # noqa: BLE001
        pass


def _note(emit, res) -> None:
    if emit is None:
        return
    line = (f"gate {res.task_id}: {'ok' if res.ok else 'red'}, "
            f"{len(res.targets)} test file(s)")
    if res.reason and (not res.ok or res.scope == NONE):
        line += f" -- {res.reason}"
    try:
        emit(line)
    except Exception:                                   # noqa: BLE001
        pass


def gated(runner, gate=None, record=None, emit=None, after=None):
    """*runner*, wrapped so a green child is then judged by *gate*.

    ``runner(entry, lane)`` runs first, exactly as the scheduler would have
    called it. A failed child is answered as it is -- it is already a failure
    and the gate has nothing to add. A green child's lane is then checked by
    ``gate.check(lane_path, task, attempt)``: the `GateResult` goes to *record*
    (anything with `append`; a list's is atomic under the GIL, which is all the
    executor threads need) and one line to ``emit(text)``, and a red gate
    becomes the unit's failure. *after* is the merge-back's committer, called
    as ``after(entry, lane, out)`` only past a green gate; its failure never
    fails the unit, because the gate already proved the work.

    A coroutine runner gets a coroutine wrapper -- `queue._run_one` dispatches
    on `inspect.iscoroutinefunction` -- with the gate pushed to the loop's
    executor so a pytest run does not stall the scheduler."""
    def _finish(entry, lane, out):
        if not queue_mod._succeeded(out):
            return out
        if gate is not None:
            task = getattr(entry, "task", None)
            attempt = _int(getattr(entry, "attempts", 0))
            path = (str(getattr(lane, "worktree_path", "") or "")
                    or str(getattr(task, "worktree_path", "") or ""))
            try:
                res = gate.check(path, task, attempt)
            except Exception as exc:                    # noqa: BLE001
                res = GateResult(
                    ok=False, scope=NONE, attempt=attempt,
                    task_id=str(getattr(task, "task_id", "") or ""),
                    reason=f"the gate raised {type(exc).__name__}: {exc}")
            _keep(record, res)
            _note(emit, res)
            if not res.ok:
                return failure_outcome(res)
        if after is not None:
            try:
                after(entry, lane, out)
            except Exception:                           # noqa: BLE001
                # The committer records its own outcome; this is the last
                # line of defence for a unit the gate has already passed.
                pass
        return out

    if inspect.iscoroutinefunction(runner):
        async def _gated_async(entry, lane):
            out = await runner(entry, lane)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, _finish, entry, lane, out)
        return _gated_async

    def _gated(entry, lane):
        return _finish(entry, lane, runner(entry, lane))
    return _gated
