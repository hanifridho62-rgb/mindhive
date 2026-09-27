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

## The vacuous pass is now a refusal (blueprint P2, MEB ORCH-01)

A unit whose changed modules no test mentions used to PASS its targeted gate
with `scope: none` and a reason saying the closing full suite would judge it.
That reasoning does not survive contact with the pipeline: the closing suite
runs after the lane has merged, so it cannot fail a unit — it named a judge
with no sentence to pass. And a child learns from the answer it gets. Told its
untested module was validated, the cheapest way to satisfy this gate forever is
to write modules no test mentions.

So the gate refuses, in two different shapes, because they have two different
remedies:

* A changed module with no companion test is `greenfield_needs_test`, and the
  refusal PRESCRIBES the path — `tests/test_<stem>.py`, which is one of the two
  names `_named_for` already accepts, so a child that follows the instruction
  passes on its next attempt. An instruction a child can act on is what ends a
  repair loop; "add tests" is what makes it spin.
* A change no module owns — a README, a migration, a config file — has no path
  to prescribe, so that one is a plain `PELANGGARAN GATE` refusal naming the
  flag that overrides it. `--allow-untested` passes the unit as
  `untested_override`, which is a different fact from "tested" and so gets its
  own scope rather than borrowing `none`; `count_scopes` totals them for the
  report line an operator reads.

The override deliberately does not reach the greenfield case. There the gate
has just told the child exactly which file to write, and that is the way out.

## The judge is not the unit's to edit (K13)

Before a single test runs, `check` refuses a unit that changed the tests it is
judged by, measured from the commit its lane stood at before its FIRST
attempt -- pinned by `SuiteGate.begin`. What counts as the judge, why that
baseline, and what it cannot see are `judge.py`'s docstring.

On a lane that ran an earlier unit, the commit it stands at may hold what that
unit COMMITTED under a refused attempt -- rollback keeps commits -- so the
lane has a TRUSTED commit as well (`SuiteGate._pin`): `HEAD` becomes the next
unit's pin only when `judge.judge_changes` finds nothing of the judge changed
between the two, and otherwise the trusted commit stays the pin, so a
doctored test an earlier unit committed stays every later unit's change until
it is undone. A history git cannot compare pins nothing, which `check`
refuses.

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
from . import judge as judge_mod
from . import resilience as res_mod
from . import worktree as wt_mod
# K13's names, re-exported: the same objects, where the gate's callers and its
# tests have always looked for them. `check` calls `tests_mutated` through this
# module's globals, so patching `gate.tests_mutated` still reaches it.
from .judge import (IMPLICIT_LOAD_NAMES, MODULE_SUFFIXES,  # noqa: F401
                    PYTEST_SECTIONS, ROOT_JUDGE_FILES, STARTUP_HOOKS,
                    is_implicit_load, is_judge_path, judge_changes,
                    judge_files, pytest_section, snapshot_tests, tests_mutated)

# `GateResult.scope`: what the row was decided BY, which is not the same
# question as `ok`. `none` is "no test ran" -- a refusal since P2, except for
# the two named below, which are `none` cases specific enough to count.
TARGETED = "targeted"
FULL = "full"
NONE = "none"
#: A changed module with no companion test. Refused WITH a path to create.
GREENFIELD_NEEDS_TEST = "greenfield_needs_test"
#: Passed because a human said so, not because a test did.
UNTESTED_OVERRIDE = "untested_override"
#: The unit edited a test the lane was checked out WITH. Refused (K13).
IMMUTABLE_TESTS_VIOLATED = "immutable_tests_violated"

DEFAULT_UNIT_TIMEOUT_S = 600
DEFAULT_SUITE_TIMEOUT_S = 1800


class LaneUnreadable(RuntimeError):
    """git could not list what a lane changed. Raised by `_touched` and
    caught by `check`, which refuses the unit: "nothing I could see changed"
    is not "nothing changed"."""

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
_TESTS_DIR = judge_mod.TESTS_DIR


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


def canonical_test_path(rel_path) -> str:
    """The path a greenfield refusal PRESCRIBES for *rel_path*.

    The short of the two names `_named_for` accepts, which is what makes the
    instruction true: a child that creates exactly this file is found by
    `tests_mentioning` on its next attempt and passes. A prescription the gate
    would not then recognise is worse than none -- it is a loop."""
    stem = Path(str(rel_path or "").replace("\\", "/")).stem
    return f"{_TESTS_DIR}/test_{stem}.py"


def greenfield_files(lane_path, changed) -> list:
    """Of *changed*, the source modules a child could be told to cover.

    Called only when NO test mentions any changed module, so this does not
    re-ask that question. What it decides is which of those changes carries an
    instruction that can actually be followed:

    * still present in the lane -- a DELETED module cannot be given a test, and
      prescribing one would be an instruction no attempt can satisfy;
    * a real module -- `__init__.py` is a package marker, and
      `tests/test___init__.py` is not a thing to tell anyone to write;
    * not itself a test, and not a non-Python file.

    Sorted, because the refusal names ONE file and `resilience` hashes the
    reason into the failure signature its breaker counts. A set-ordered pick
    would make one repeated failure read as several different ones."""
    lane = Path(str(lane_path))
    out = []
    for rel in changed or ():
        rel = str(rel).replace("\\", "/").strip("/")
        if not rel.endswith(".py") or rel.startswith(_TESTS_DIR + "/"):
            continue
        if Path(rel).name == "__init__.py" or not import_name(rel):
            continue
        if (lane / rel).is_file():
            out.append(rel)
    return sorted(dict.fromkeys(out))


def count_scopes(records) -> dict:
    """How many gate rows landed in each scope, for the report's own line.

    Total over junk: it runs after a mission, and a malformed row must not be
    the thing that stops an operator seeing the rest of them."""
    out = {}
    for row in records or ():
        try:
            scope = str(row.get("scope") or "")
        except AttributeError:
            continue
        if scope:
            out[scope] = out.get(scope, 0) + 1
    return out


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
    #: `--allow-untested`. Passes a unit whose change no test could name. It
    #: does NOT reach the greenfield case -- see `_untested`.
    allow_untested: bool = False
    #: K13. On by default: the suite a unit is judged by is not the unit's to
    #: edit. `--no-immutable-tests` switches the check off entirely.
    immutable_tests: bool = True
    #: `--allow-test-edits`. The unit really is a test change.
    allow_test_edits: bool = False
    #: (lane, task) -> the commit that lane stood at before the unit's first
    #: attempt. Filled by `begin`; read by `check`. See `tests_mutated`.
    _bases: dict = field(default_factory=dict, init=False, repr=False,
                         compare=False)
    #: lane -> the last commit on it up to which the judge was found
    #: unchanged: the pin a later unit on that lane inherits. See `_pin`.
    _trusted: dict = field(default_factory=dict, init=False, repr=False,
                           compare=False)

    def _base(self) -> Path:
        return Path(self.root) if self.root else Path.cwd()

    @staticmethod
    def _key(lane_path, task) -> tuple:
        import os
        return (os.path.normcase(os.path.abspath(str(lane_path))),
                str(getattr(task, "task_id", "") or ""))

    def begin(self, lane_path, task) -> str:
        """Pin the commit *task*'s lane stands at, BEFORE its child runs.

        `gated` calls this ahead of every attempt and only the first call per
        lane and unit pins, so the baseline is where the unit started, whatever
        any attempt -- or the rollback after it -- did since. Rollback keeps
        commits, so an edit a child committed stays the unit's change on every
        later attempt too, instead of becoming the next attempt's baseline.

        The commit pinned is `HEAD` only when the lane's TRUSTED commit
        vouches for it -- see `_pin`. On a lane an earlier unit ran on, `HEAD`
        may hold what that unit committed under a refused attempt.

        When git cannot say which commit that is, ``""`` is what gets pinned.
        `check` refuses such a unit rather than judge it against a `HEAD` the
        child may have moved, and because the failure is recorded, no later
        attempt can pin a commit made after this one began.

        A path that is not one of this checkout's lanes pins nothing: `check`
        refuses those anyway, and git runs nowhere else. Answers the pinned
        commit, ``""`` when there is none. Never raises."""
        try:
            lane = Path(str(lane_path or ""))
            if not str(lane_path or "") or not wt_mod.is_lane_path(
                    self._base(), lane):
                return ""
            key = self._key(lane, task)
        except Exception:                               # noqa: BLE001
            return ""
        if key not in self._bases:
            try:
                from ..mindnet import worktree as mn_wt
                sha = str(mn_wt.rev_of(lane, "HEAD") or "")
            except Exception:                           # noqa: BLE001
                sha = ""
            self._bases.setdefault(key, self._pin(lane, key[0], sha))
        return self._bases.get(key, "")

    def _pin(self, lane, lane_key, head) -> str:
        """The commit a unit starting on *lane* at *head* is pinned to.

        *head* itself when the lane has no trusted commit yet, or when nothing
        of the judge changed between the trusted commit and *head*
        (`judge.judge_changes`: the two-commit question, the working tree no
        part of it) -- validated work an earlier unit committed, source and
        the test module the greenfield refusal asked for, moves the baseline
        forward. The TRUSTED commit when something did: a doctored test an
        earlier unit committed under a refused attempt stays the lane's
        change for every later unit until it is undone. ``""`` when git
        cannot compare the two -- unknown is not clean, and `check` refuses
        an unpinned unit. With the immutable-tests check stood down there is
        nothing to inherit, and *head* is the pin.

        A lane whose FIRST pin failed has no trusted commit and never gets
        one: git answered nothing when its first unit began, that unit's
        child may have committed anything since, and a later `HEAD` taken on
        trust in its place would be the very commit the trusted one exists
        to refuse. ``None`` in `_trusted` records that for the life of this
        gate; a pin that fails on a lane that already has a trusted commit
        refuses only its own unit. Never raises."""
        if not self.immutable_tests or self.allow_test_edits:
            return head
        trusted = self._trusted.get(lane_key, "")
        if trusted is None:
            return ""
        if not head:
            self._trusted.setdefault(lane_key, None)
            return ""
        if trusted and trusted != head:
            try:
                changed = judge_mod.judge_changes(lane, trusted, head)
            except Exception:                           # noqa: BLE001
                changed = None
            if changed is None:
                return ""
            if changed:
                return trusted
        self._trusted[lane_key] = head
        return head

    def baseline(self, lane_path, task) -> str:
        """The commit *task* is judged against in *lane_path*: the one `begin`
        pinned; `HEAD` for a caller that never called it; ``""`` when `begin`
        was called and git could not say, which `check` refuses."""
        return self._bases.get(self._key(lane_path, task), "HEAD")

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

    def _touched(self, lane_path, task, base="HEAD") -> list:
        """The repo-relative paths this unit is judged on: what the lane
        changed since *base*, or the unit's own files when the child changed
        nothing.

        Against the pinned *base* and not `HEAD`, for the reason K13 pins it:
        a child that commits its work would otherwise have changed nothing,
        and a new module it committed with no test would fall back to the
        unit's own files and be judged by their tests -- the vacuous pass by a
        different door.

        Read ONCE per `check`, which hands the list to `targets` and to the
        greenfield search, so the two cannot come to different conclusions
        about WHICH files are in question: asking git twice would let a lane
        that changed underneath them produce a refusal naming a file the
        target search never saw.

        "Changed nothing" is ``()``. ``None`` is "git could not say", and it
        raises `LaneUnreadable` rather than falling through to the unit's own
        files -- a stale list that would judge a child's real change by the
        tests of the files it was SENT to edit."""
        from ..mindnet import worktree as mn_wt

        lane = Path(str(lane_path))
        changed = mn_wt.changed_files(lane, base)
        if changed is None:
            raise LaneUnreadable(
                f"git could not list what {lane} changed since {base[:12]}")
        changed = changed or self._own_files(task) or ()
        return [str(rel).replace("\\", "/").strip("/") for rel in changed]

    def targets(self, lane_path, task, base="HEAD", touched=None) -> list:
        """The lane-relative test files *task* is judged by, in a fixed order:
        the tests the unit itself touched, then every test mentioning a module
        it changed -- both measured from *base*. *touched* is `_touched`'s
        list when the caller already read it; `check` reads it once and hands
        it down."""
        lane = Path(str(lane_path))
        if touched is None:
            touched = self._touched(lane, task, base)
        modules, own = [], []
        for rel in touched:
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

    def _untested(self, lane, tid, lane_id, attempt, started,
                  touched) -> GateResult:
        """The gate's answer when no test file names this unit's work.

        Fail-CLOSED since blueprint P2 -- the module docstring has the why. The
        three branches are ordered by how specific a remedy they can offer,
        most specific first, because the most specific one is the instruction
        a child can act on. *touched* is the one list `check` read."""
        def _done(**kw) -> GateResult:
            return GateResult(task_id=tid, lane_id=lane_id, attempt=attempt,
                              duration_s=time.monotonic() - started, **kw)

        try:
            greenfield = greenfield_files(lane, touched)
        except Exception as exc:                        # noqa: BLE001
            return _done(ok=False, scope=NONE, reason=(
                f"could not read the lane's changes: "
                f"{type(exc).__name__}: {exc}"))
        if greenfield:
            path = canonical_test_path(greenfield[0])
            return _done(
                ok=False, scope=GREENFIELD_NEEDS_TEST,
                reason=(f"MODUL BARU TERDETEKSI: Modul '{greenfield[0]}' "
                        f"belum memiliki tes pendamping. Anda WAJIB membuat "
                        f"berkas tes pada: '{path}'."),
                tail=f"Instruksi: Tulis minimal satu pengujian unit di {path}.")
        if not self.allow_untested:
            return _done(ok=False, scope=NONE, reason=(
                "PELANGGARAN GATE: tidak ada tes yang menyebut modul yang "
                "diubah; gunakan --allow-untested untuk mengabaikan secara "
                "eksplisit."))
        return _done(ok=True, scope=UNTESTED_OVERRIDE, reason=(
            "untested override: no test could name this unit's change and "
            "--allow-untested was given; nothing judged it"))

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
        pinned = self.baseline(lane, task)
        base = pinned or "HEAD"
        if self.immutable_tests and not self.allow_test_edits:
            # BEFORE the suite runs, not after. A doctored suite is green, so
            # running it first would report that green as the unit's result for
            # as long as it took to notice where it came from.
            if not pinned:
                return GateResult(
                    ok=False, scope=NONE, task_id=tid, lane_id=lane_id,
                    attempt=attempt, duration_s=time.monotonic() - started,
                    reason=("could not verify that the lane's existing tests "
                            "are unchanged: git could not say which commit "
                            "this unit started from, and the lane's HEAD is "
                            "no baseline -- a child that commits its edit "
                            "moves it. Refused rather than passed; "
                            "--allow-test-edits or --no-immutable-tests skips "
                            "the check."))
            mutated = tests_mutated(lane, base=base)
            if mutated is None:
                return GateResult(
                    ok=False, scope=NONE, task_id=tid, lane_id=lane_id,
                    attempt=attempt, duration_s=time.monotonic() - started,
                    reason=("could not verify that the lane's existing tests "
                            f"are unchanged since {base[:12]}: git could not "
                            "read the lane. Refused rather than passed -- an "
                            "edit this cannot see is not an edit that did not "
                            "happen; --allow-test-edits or "
                            "--no-immutable-tests skips the check."))
            if mutated:
                return GateResult(
                    ok=False, scope=IMMUTABLE_TESTS_VIOLATED, task_id=tid,
                    lane_id=lane_id, attempt=attempt,
                    duration_s=time.monotonic() - started,
                    reason=("PELANGGARAN GATE: berkas uji yang sudah ada "
                            "diubah selama implementasi: "
                            f"{', '.join(mutated)}; gunakan --allow-test-edits "
                            "untuk mengizinkan secara eksplisit."),
                    tail=("Instruksi: kembalikan berkas uji itu seperti "
                          "semula dan perbaiki kodenya, bukan ujinya."))
        try:
            touched = self._touched(lane, task, base)
        except Exception as exc:                        # noqa: BLE001
            return GateResult(
                ok=False, scope=NONE, task_id=tid, lane_id=lane_id,
                attempt=attempt, duration_s=time.monotonic() - started,
                reason=(f"could not read the lane's changes: "
                        f"{type(exc).__name__}: {exc}"))
        try:
            targets = self.targets(lane, task, base, touched=touched)
        except Exception as exc:                        # noqa: BLE001
            return GateResult(
                ok=False, scope=NONE, task_id=tid, lane_id=lane_id,
                attempt=attempt, duration_s=time.monotonic() - started,
                reason=f"could not choose tests: {type(exc).__name__}: {exc}")
        if not targets:
            return self._untested(lane, tid, lane_id, attempt, started,
                                  touched)
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


def _lane_path(lane, task) -> str:
    return (str(getattr(lane, "worktree_path", "") or "")
            or str(getattr(task, "worktree_path", "") or ""))


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

    Before the child runs, a gate that has a ``begin(lane_path, task)`` is told
    the attempt is starting, so it can pin what the unit will be judged
    against while the lane still stands where the unit found it (K13; see
    `SuiteGate.begin`). A gate without one -- a stand-in -- is not asked.

    A coroutine runner gets a coroutine wrapper -- `queue._run_one` dispatches
    on `inspect.iscoroutinefunction` -- with the gate pushed to the loop's
    executor so a pytest run does not stall the scheduler."""
    begin = getattr(gate, "begin", None) if gate is not None else None

    def _begin(entry, lane):
        if begin is None:
            return
        task = getattr(entry, "task", None)
        try:
            begin(_lane_path(lane, task), task)
        except Exception:                               # noqa: BLE001
            pass

    def _finish(entry, lane, out):
        if not queue_mod._succeeded(out):
            return out
        if gate is not None:
            task = getattr(entry, "task", None)
            attempt = _int(getattr(entry, "attempts", 0))
            path = _lane_path(lane, task)
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
            loop = asyncio.get_running_loop()
            if begin is not None:
                await loop.run_in_executor(None, _begin, entry, lane)
            out = await runner(entry, lane)
            return await loop.run_in_executor(None, _finish, entry, lane, out)
        return _gated_async

    def _gated(entry, lane):
        _begin(entry, lane)
        return _finish(entry, lane, runner(entry, lane))
    return _gated
