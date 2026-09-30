# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/premerge.py
"""`hive ci premerge` -- the pre-merge check GitHub Actions ran, run on this
machine (spec 2026-09-30).

The account's Actions jobs are refused for payment, so no PR here can go
green on GitHub's runners. This runs the same jobs -- ci.yml's matrix and
tiers, pinned to ci.yml by `tests/test_ci_legs.py` -- against the same thing
Actions tested: the MERGE a PR would make, not its head alone.

One run, in order:

1. **Fetch** the base branch and `refs/pull/N/head` from `origin` into refs of
   the run's own (`refs/mindhive-ci/<tag>/...`), so nothing the operator has
   checked out, stashed or branched is read or moved. They are deleted when
   the run ends, whatever happened.
2. **Preview the merge** in the object store (`preview.merge_preview`). A
   conflict is red at once: no leg runs on a merge that cannot be made.
3. **Probe** each leg (`legs.Probe`). A leg this machine cannot run is named,
   never counted -- the verdict says what was not run here.
4. **Run the legs**, a few at a time (`legrun.LegRunner`), each in a fresh
   repository of the tested commit with its own venv and environment.
5. **Decide** (`decide`) and **post commit statuses** on the PR's head:
   `mindhive-ci` for the verdict, `mindhive-ci/<leg>` per leg. A status is a
   plain REST write -- nothing billed, no runner.
6. **Record** one `verification` event (`scope: "ci"`, `gate: "premerge"`)
   with the legs, their counts and the report as a runlog artifact, and
   remember the tested head, base and tree in `state/ci_premerge.json` for
   `hive ci watch`.

**`--merge` is guarded**, because a merge cannot be taken back. After a green
verdict the PR is read again: still OPEN, not a draft, and its head the head
that was tested. The base is fetched again; if it moved, the merge is
previewed again, and a merge other than the tested one is tested again (at
most `MAX_ROUNDS` rounds) -- unless `--allow-base-drift` accepts a clean but
untested tree, which the report then says. Then `gh pr merge N --merge
--match-head-commit <head>` (`github.GitHub.merge`), and the PR is read back:
only GitHub answering MERGED makes the merge state `merged`. The landed merge
commit's tree is compared with the tested tree, and a difference is reported.

**A PR from a fork runs only with `--trust`**: its legs execute the fork's
code on this machine as the operator, so it is read first; `watch` never
trusts. **A partial run** (`--legs`) posts each leg's status but never the
overall `mindhive-ci`, and is never merged: a verdict on some legs is not a
verdict on the PR.

Exit codes: 0 green; 1 red (a conflict, or a leg that did not pass); 2 the
check could not be made (fetch, preview, no full-suite leg runnable); 3 green,
but `--merge` did not merge -- the report says why.
"""
import concurrent.futures
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import events, paths, runlog, verify
from ..core import locks
from . import github as gh_mod
from . import legrun
from . import legs as legs_mod
from . import preview as preview_mod
from . import proc
from .runner import SCOPE, _rmtree_force

__all__ = ["DEFAULT_JOBS", "EXIT_NOT_MERGED", "GATE", "INTERRUPTED", "MAX_ROUNDS", "OVERALL_STATE",
           "Premerge", "PremergeResult", "REF_ROOT", "REMOTE", "SIDECAR", "decide",
           "load_sidecar", "remember", "render", "sidecar_path", "suite_counts",
           "sum_counts"]

GIT = "git"
REMOTE = "origin"
#: Every ref a run makes lives under here, in a directory named by the run's
#: tag, and is deleted when the run ends.
REF_ROOT = "refs/mindhive-ci"
GATE = "premerge"
#: Rounds a guarded merge may test before it gives up on a base that keeps moving.
MAX_ROUNDS = 3
DEFAULT_JOBS = 3
EXIT_NOT_MERGED = 3
SIDECAR = "ci_premerge.json"
#: A watch and a hand-run premerge both write the sidecar: they take turns.
SIDECAR_LOCK = SIDECAR + ".lock"
_SIDECAR_LOCK_POLICY = locks.LockPolicy(timeout_s=10.0, stale_s=60.0)
#: Where the legs run unless told: `<tempdir>/mindhive-ci`, outside the
#: repository. Under it, a leg would sit inside the operator's checkout -- a
#: `.claude/worktrees/...` path when that is a linked worktree, which tests
#: that resolve a memory folder from the path read differently -- and deeper
#: than Windows' MAX_PATH wants.
WORK_DIRNAME = "mindhive-ci"
FETCH_TIMEOUT_S = 600.0
GIT_TIMEOUT_S = 120.0
#: The overall commit status each verdict posts.
OVERALL_STATE = {"green": "success", "red": "failure", "error": "error"}
#: The reason a run stopped by hand (Ctrl+C) is recorded with.
INTERRUPTED = "interrupted: the run was stopped by hand"
#: A merge broken off inside `gh pr merge` -- by Ctrl+C or a crash -- may have
#: landed: the PR's state on GitHub says, this run cannot.
MERGE_UNKNOWN = ("unknown: the run stopped while gh pr merge ran; "
                 "read the PR's state on GitHub")
_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_NOT_STAMP = re.compile(r"[^0-9TZ]")


def _why(rc, out, err) -> str:
    text = (err or "").strip() or (out or "").strip()
    return text.splitlines()[-1][:300] if text else f"exit {rc}"


def _short(sha) -> str:
    return str(sha or "")[:12] or "(none)"


def decide(preview, legs, unavailable=()) -> tuple:
    """``(verdict, exit_code, reason)``: green 0, red 1, error 2.

    Red is anything a merge would carry: a conflict, or a leg that did not
    pass -- one whose setup failed included, since the suite did not pass
    there either. Error is a check that could not be made. Green needs at
    least one full-suite (default tier) leg: the node or browser tier alone is
    not a verdict on the suite."""
    if preview is None or preview.error:
        return "error", 2, (preview.error if preview is not None else "no merge preview")
    if preview.conflicts:
        shown = ", ".join(preview.conflicts[:5])
        more = len(preview.conflicts) - 5
        return "red", 1, (f"conflicts with the base in {len(preview.conflicts)} "
                          f"file(s): {shown}" + (f" and {more} more" if more > 0 else ""))
    bad = [leg for leg in legs if leg.state != "success"]
    if bad:
        return "red", 1, "red: " + ", ".join(f"{leg.name} ({leg.state})" for leg in bad)
    if not any(leg.tier == legs_mod.TIER_DEFAULT for leg in legs):
        return "error", 2, "no full-suite leg ran on this machine"
    reason = f"green: {len(legs)} leg(s) passed"
    names = [u.get("name", "") if isinstance(u, dict) else str(u) for u in unavailable]
    if names:
        reason += "; not run here: " + ", ".join(names)
    return "green", 0, reason


def _merge_refusal(pr, tested_head, tested_base) -> str:
    """Why a green PR must not be merged now, or ``""``."""
    state = str(pr.get("state") or "")
    if state != "OPEN":
        return f"the PR is {state or 'in no state gh reported'}"
    if pr.get("isDraft"):
        return "the PR is a draft"
    base = str(pr.get("baseRefName") or "")
    if base != tested_base:
        return f"the PR now targets {base or 'no base'}, not the tested {tested_base}"
    head = str(pr.get("headRefOid") or "")
    if head != tested_head:
        return f"the head moved to {_short(head)} after {_short(tested_head)} was tested"
    return ""


@dataclass
class PremergeResult:
    """One pre-merge run. Every field is present on construction."""

    pr_id: int = 0
    branch: str = ""
    base_ref: str = ""
    mode: str = "github"
    argv: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    base_sha: str = ""
    head_sha: str = ""
    tested_sha: str = ""
    tree: str = ""
    conflicts: tuple = ()
    up_to_date: bool = False
    legs: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)
    verdict: str = "error"
    exit_code: int = 2
    reason: str = ""
    rounds: int = 0
    duration_s: float = 0.0
    status_errors: list = field(default_factory=list)
    #: Whether GitHub took the overall `mindhive-ci` status: None when this
    #: run posted none (statuses off, a partial run, a branch).
    published: object = None
    merge_state: str = ""
    merge_commit: str = ""
    landed_tree_matches: object = None
    notes: list = field(default_factory=list)
    report_text: str = ""

    @property
    def partial(self) -> bool:
        """Some legs by choice (`--legs`), not the whole catalog."""
        return bool(self.selected) and len(self.selected) < len(legs_mod.CATALOG)

    def as_dict(self) -> dict:
        return {"pr_id": self.pr_id, "branch": self.branch, "base_ref": self.base_ref,
                "mode": self.mode, "argv": list(self.argv),
                "selected": list(self.selected), "partial": self.partial,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha, "tested_sha": self.tested_sha,
                "tree": self.tree, "conflicts": list(self.conflicts),
                "up_to_date": self.up_to_date,
                "legs": [leg.as_dict() for leg in self.legs],
                "unavailable": [dict(u) for u in self.unavailable],
                "verdict": self.verdict, "exit_code": int(self.exit_code),
                "reason": self.reason, "rounds": self.rounds,
                "duration_s": round(self.duration_s, 3),
                "status_errors": list(self.status_errors),
                "published": self.published,
                "merge_state": self.merge_state, "merge_commit": self.merge_commit,
                "landed_tree_matches": self.landed_tree_matches,
                "notes": list(self.notes)}


def suite_counts(legs) -> tuple:
    """``(leg name, counts)`` of one full-suite leg -- the first in catalog
    order that has counts -- or ``("", {})``. A row's `counts` is one suite's,
    as every other writer's is; the sum over the legs is `counts_total`."""
    for leg in legs:
        if leg.tier == legs_mod.TIER_DEFAULT and leg.counts:
            return leg.name, dict(leg.counts)
    return "", {}


def sum_counts(legs) -> dict:
    """Every leg's pytest counts, added up."""
    total = {}
    for leg in legs:
        for key, n in (leg.counts or {}).items():
            if isinstance(n, int) and not isinstance(n, bool):
                total[key] = total.get(key, 0) + n
    return total


def render(res) -> str:
    """The report `hive ci premerge` prints and the artifact keeps."""
    subject = f"PR #{res.pr_id} ({res.branch})" if res.pr_id else f"branch {res.branch}"
    lines = [f"hive ci premerge: {subject}"
             + (f" into {res.base_ref}" if res.base_ref else "")]
    if res.base_sha or res.head_sha:
        lines.append(f"base {_short(res.base_sha)}  head {_short(res.head_sha)}"
                     + (f"  merge tree {_short(res.tree)}" if res.tree else ""))
    if res.tested_sha:
        what = ("the head itself: the base is already in it" if res.up_to_date
                else "the merge commit a merge would make")
        lines.append(f"tested: {_short(res.tested_sha)} ({what})")
    if res.conflicts:
        lines.append(f"conflicts ({len(res.conflicts)}): " + ", ".join(res.conflicts))
    if res.legs:
        width = max(len(leg.name) for leg in res.legs)
        lines.append("legs:")
        for leg in res.legs:
            lines.append(f"  {leg.name:<{width}}  {leg.state:<7}  {leg.summary()}")
            if leg.state != "success":
                lines.extend(f"      {test}" for test in leg.failed_tests[:5])
                if leg.artifact:
                    lines.append(f"      log: {leg.artifact}")
    if res.unavailable:
        lines.append("not run on this machine:")
        lines.extend(f"  {u.get('name')}: {u.get('reason')}" for u in res.unavailable)
    lines.append(f"verdict: {res.verdict.upper()} (exit {res.exit_code}) -- {res.reason}")
    if res.merge_state:
        tail = ""
        if res.landed_tree_matches is True:
            tail = "; the landed tree is the tested tree"
        elif res.landed_tree_matches is False:
            tail = "; the landed tree is NOT the tested tree"
        lines.append(f"merge: {res.merge_state}"
                     + (f" as {_short(res.merge_commit)}" if res.merge_commit else "")
                     + tail)
    if res.status_errors:
        lines.append(f"statuses: {len(res.status_errors)} could not be posted; "
                     f"first: {res.status_errors[0]}")
    lines.extend(f"note: {note}" for note in res.notes)
    return "\n".join(lines)


# --- what `watch` reads ------------------------------------------------------------------


def sidecar_path(project=None) -> Path:
    return paths.state_dir(project) / SIDECAR


def _read_sidecar(project) -> dict:
    """The sidecar. A missing file is an empty one, and so is one too damaged
    to parse; one that is there but cannot be read raises OSError."""
    try:
        text = paths.read_text_retry(sidecar_path(project))
    except FileNotFoundError:
        text = ""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for bucket in ("prs", "branches"):
        if not isinstance(data.get(bucket), dict):
            data[bucket] = {}
    data["version"] = 1
    return data


def load_sidecar(project=None) -> dict:
    """The last tested head, base and tree per PR (and per branch). Total: a
    missing, damaged or unreadable file is an empty one."""
    try:
        return _read_sidecar(project)
    except OSError:
        return {"version": 1, "prs": {}, "branches": {}}


def remember(project, key, entry, bucket="prs") -> bool:
    """Record *entry* under *key* with the sidecar's lock held, so two writers
    cannot drop each other's entries, and never over a file that is there but
    could not be read. Never raises: False when it could not."""
    try:
        with locks.file_lock(paths.state_dir(project) / SIDECAR_LOCK,
                             _SIDECAR_LOCK_POLICY, name=SIDECAR_LOCK):
            data = _read_sidecar(project)
            data[bucket][str(key)] = dict(entry)
            paths.write_text_atomic(sidecar_path(project),
                                    json.dumps(data, indent=2, sort_keys=True,
                                               ensure_ascii=False) + "\n")
        return True
    except Exception:                                   # noqa: BLE001
        return False


# --- the run -------------------------------------------------------------------------------


class Premerge:
    """Pre-merge runs for one repository. Every collaborator is injectable;
    the defaults are the real ones."""

    def __init__(self, repo_root, *, project=None, run=proc.run, environ=None,
                 github=None, probe=None, leg_runner=None, previewer=None,
                 clock=time.monotonic, now_iso=events.now_iso, work_root=None,
                 jobs=DEFAULT_JOBS, timeout_s=legrun.LEG_TIMEOUT_S,
                 post_statuses=True, keep=False, say=None):
        self.repo_root = Path(repo_root)
        self.project = project
        self.spawn = run
        self.environ = dict(os.environ if environ is None else environ)
        self.github = github if github is not None else gh_mod.GitHub(self.repo_root, run=run)
        self.probe = probe if probe is not None else legs_mod.Probe(run=run,
                                                                     environ=self.environ)
        self._leg_runner = leg_runner
        self._previewer = previewer
        self._clock = clock
        self._now_iso = now_iso
        self.work_root = (Path(work_root) if work_root
                          else Path(tempfile.gettempdir()) / WORK_DIRNAME)
        self.jobs = max(1, int(jobs))
        self.timeout_s = float(timeout_s)
        self.post_statuses = bool(post_statuses)
        self.keep = bool(keep)
        self._say = say or (lambda text: None)

    # --- the two entry points -------------------------------------------------------------

    def run_pr(self, number, *, legs_spec=None, merge=False, allow_base_drift=False,
               trust=False) -> PremergeResult:
        """Test PR *number*; with *merge*, merge it when green. A PR from a fork
        runs only with *trust*. Never raises, but for Ctrl+C: every leg is
        stopped, the run is recorded as an error -- and not remembered, so
        `hive ci watch` tests the head again -- and the KeyboardInterrupt goes
        on up."""
        started = self._clock()
        number = int(number)
        argv = ["hive", "ci", GATE, str(number)]
        if legs_spec:
            argv += ["--legs", str(legs_spec)]
        for flag, on in (("--merge", merge), ("--allow-base-drift", allow_base_drift),
                         ("--trust", trust), ("--no-status", not self.post_statuses)):
            if on:
                argv.append(flag)
        res = PremergeResult(pr_id=number, mode="github", argv=argv)
        try:
            chosen = legs_mod.select(legs_spec)
            res.selected = [leg.name for leg in chosen]
            if merge and res.partial:
                raise ValueError("--merge tests every leg: a partial run (--legs) is "
                                 "never merged")
            pr = self.github.pr(number)
        except (ValueError, gh_mod.GitHubError) as exc:
            res.reason = str(exc)
            return self._finish(res, started, None)
        res.branch = str(pr.get("headRefName") or "")
        res.base_ref = str(pr.get("baseRefName") or "")
        state = str(pr.get("state") or "")
        if state != "OPEN":
            res.reason = f"PR #{number} is {state or 'in no state gh reported'}, not OPEN"
            return self._finish(res, started, None)
        if gh_mod.from_fork(pr) and not trust:
            owner = pr.get("headRepositoryOwner")
            login = str(owner.get("login") or "") if isinstance(owner, dict) else ""
            res.reason = (f"PR #{number} comes from a fork ({login or 'another repository'}):"
                          f" its code would run here as you; read its diff, then run "
                          f"`hive ci premerge {number} --trust`")
            return self._finish(res, started, None)
        if res.partial and self.post_statuses:
            res.notes.append(f"a partial run ({len(res.selected)} of "
                             f"{len(legs_mod.CATALOG)} legs): each leg's status is "
                             f"posted, the overall {gh_mod.CONTEXT} is left alone")
        tag = self._tag(f"pr{number}")
        stopped = None
        try:
            for _ in range(MAX_ROUNDS):
                res.rounds += 1
                self._round(res, tag, chosen, number)
                if not merge or res.verdict != "green":
                    break
                if self._guarded_merge(res, tag, number, allow_base_drift) != "retest":
                    break
            else:
                res.merge_state = (f"refused: the base kept moving; {MAX_ROUNDS} rounds "
                                   f"tested without landing")
        except (KeyboardInterrupt, Exception) as exc:   # noqa: BLE001
            stopped = exc if isinstance(exc, KeyboardInterrupt) else None
            res.verdict, res.exit_code = "error", 2
            res.reason = (INTERRUPTED if stopped else
                          f"premerge broke: {type(exc).__name__}: {exc}")
            if self.post_statuses and res.head_sha and not res.partial:
                res.published = self._status(res, "error", gh_mod.CONTEXT, res.reason)
        finally:
            self._drop(tag)
        if stopped is not None:
            self._finish(res, started, tag, sidecar=False)
            raise stopped
        if merge and res.verdict == "green" and res.merge_state != "merged":
            res.exit_code = EXIT_NOT_MERGED
        return self._finish(res, started, tag)

    def run_branch(self, branch, *, base="master", legs_spec=None) -> PremergeResult:
        """Test the merge of a local *branch* into *base*: no statuses, no
        merge. Ctrl+C as for `run_pr`."""
        started = self._clock()
        argv = ["hive", "ci", GATE, "--branch", str(branch), "--base", str(base)]
        if legs_spec:
            argv += ["--legs", str(legs_spec)]
        res = PremergeResult(branch=str(branch), base_ref=str(base), mode="offline",
                             argv=argv)
        try:
            chosen = legs_mod.select(legs_spec)
        except ValueError as exc:
            res.reason = str(exc)
            return self._finish(res, started, None)
        res.selected = [leg.name for leg in chosen]
        tag = self._tag("branch")
        stopped = None
        try:
            base_sha, error = self._fetch_base(tag, base)
            if error:
                res.notes.append(f"{error}; tested against the local {base}")
                base_sha = self._rev(base)
            res.base_sha, res.head_sha = base_sha, self._rev(branch)
            if not res.base_sha:
                res.reason = f"no commit for the base {base}"
            elif not res.head_sha:
                res.reason = f"no commit for the branch {branch}"
            else:
                res.rounds = 1
                self._test(res, f"{tag}-r1", chosen, statuses=False)
        except (KeyboardInterrupt, Exception) as exc:   # noqa: BLE001
            stopped = exc if isinstance(exc, KeyboardInterrupt) else None
            res.verdict, res.exit_code = "error", 2
            res.reason = (INTERRUPTED if stopped else
                          f"premerge broke: {type(exc).__name__}: {exc}")
        finally:
            self._drop(tag)
        if stopped is not None:
            self._finish(res, started, tag, sidecar=False)
            raise stopped
        return self._finish(res, started, tag)

    # --- what `watch` and `conflicts` use ---------------------------------------------------

    def fetch_base(self, base_ref="master") -> tuple:
        """``(sha, error)``: *base_ref* as origin has it now."""
        tag = self._tag("watch")
        try:
            return self._fetch_base(tag, base_ref)
        finally:
            self._drop(tag)

    def fetch_heads(self, numbers, base_ref="master") -> tuple:
        """``(base_sha, {pr: head_sha}, error)`` in one fetch. The objects stay
        in the store after the refs are dropped, for `preview.conflict_matrix`."""
        tag = self._tag("conflicts")
        refs = f"{REF_ROOT}/{tag}"
        try:
            specs = [f"+refs/heads/{base_ref}:{refs}/base"]
            specs += [f"+refs/pull/{int(n)}/head:{refs}/pr{int(n)}" for n in numbers]
            error = self._fetch(specs)
            if error:
                return "", {}, error
            heads = {int(n): self._rev(f"{refs}/pr{int(n)}") for n in numbers}
            base = self._rev(f"{refs}/base")
            missing = [f"#{n}" for n, sha in heads.items() if not sha]
            if not base or missing:
                return base, heads, ("git fetch: nothing resolves for "
                                     + ", ".join(([base_ref] if not base else []) + missing))
            return base, heads, ""
        finally:
            self._drop(tag)

    def record_conflict(self, number, *, branch, base_ref, base_sha, head_sha,
                        conflicts, tree="") -> PremergeResult:
        """`hive ci watch` found PR *number*'s head no longer merging into
        *base_ref* at *base_sha*. One `git merge-tree` was the check -- a
        conflict needs no leg -- and the verdict is posted, recorded and
        remembered as any run's is, so the ledger sees the PR go red."""
        started = self._clock()
        res = PremergeResult(pr_id=int(number), branch=str(branch),
                             base_ref=str(base_ref), mode="github",
                             argv=["hive", "ci", "watch"],
                             selected=[leg.name for leg in legs_mod.CATALOG],
                             base_sha=str(base_sha), head_sha=str(head_sha),
                             tree=str(tree or ""), conflicts=tuple(conflicts), rounds=1)
        res.verdict, res.exit_code, _ = decide(
            preview_mod.Preview(res.base_sha, res.head_sha, conflicts=res.conflicts), [])
        shown = ", ".join(res.conflicts[:5])
        more = len(res.conflicts) - 5
        res.reason = (f"conflicts with {res.base_ref} at {_short(res.base_sha)}: {shown}"
                      + (f" and {more} more" if more > 0 else ""))
        if self.post_statuses:
            res.published = self._status(res, OVERALL_STATE[res.verdict], gh_mod.CONTEXT,
                                         res.reason)
        return self._finish(res, started, self._tag(f"pr{res.pr_id}"),
                            checked_base_sha=res.base_sha)

    # --- one round ----------------------------------------------------------------------------

    def _round(self, res, tag, chosen, number) -> None:
        res.base_sha = res.head_sha = res.tested_sha = res.tree = ""
        res.conflicts, res.up_to_date, res.legs, res.unavailable = (), False, [], []
        res.verdict, res.exit_code, res.reason, res.published = "error", 2, "", None
        refs = f"{REF_ROOT}/{tag}"
        error = self._fetch([f"+refs/heads/{res.base_ref}:{refs}/base",
                             f"+refs/pull/{number}/head:{refs}/head"])
        if not error:
            res.base_sha, res.head_sha = self._rev(f"{refs}/base"), self._rev(f"{refs}/head")
            if not (res.base_sha and res.head_sha):
                error = f"git fetch {REMOTE}: the fetched base or head does not resolve"
        if error:
            res.reason = error
            return
        self._test(res, f"{tag}-r{res.rounds}", chosen, statuses=self.post_statuses)

    def _test(self, res, run_tag, chosen, *, statuses) -> None:
        pv = self._preview(res.base_sha, res.head_sha)
        res.tree, res.conflicts = pv.tree, tuple(pv.conflicts)
        res.tested_sha, res.up_to_date = pv.tested_sha, pv.up_to_date
        runnable = []
        if pv.clean:
            for leg in chosen:
                avail = self.probe.check(leg)
                if avail.ok:
                    runnable.append((leg, avail))
                else:
                    res.unavailable.append({"name": leg.name, "reason": avail.reason})
        post = statuses and bool(res.head_sha)
        overall = post and not res.partial
        if runnable:
            self._say(f"testing {_short(pv.tested_sha)} on {len(runnable)} leg(s)")
            if overall:
                self._status(res, "pending", gh_mod.CONTEXT,
                             f"testing the merge into {res.base_ref} on "
                             f"{len(runnable)} leg(s)")
            if post:
                for leg, _ in runnable:
                    self._status(res, "pending", f"{gh_mod.CONTEXT}/{leg.name}", "queued")
        res.legs = self._run_legs(run_tag, runnable, pv.tested_sha, res, post)
        res.verdict, res.exit_code, res.reason = decide(pv, res.legs, res.unavailable)
        if overall:
            res.published = self._status(res, OVERALL_STATE[res.verdict], gh_mod.CONTEXT,
                                         res.reason)

    def _runner_for(self, run_tag):
        if self._leg_runner is not None:
            return self._leg_runner(run_tag)
        return legrun.LegRunner(self.repo_root, self.work_root / run_tag, run_id=run_tag,
                                run=self.spawn, environ=self.environ,
                                project=self.project, timeout_s=self.timeout_s,
                                wsl_prefix=self.probe.wsl_prefix(),
                                wsl_env=self.probe.wsl_env(), keep=self.keep)

    def _run_legs(self, run_tag, runnable, commit, res, post) -> list:
        """Run *runnable* a few at a time. Statuses and progress are posted
        from this thread as each leg finishes, never from a worker. Ctrl+C --
        or anything else that breaks the wait -- stops every leg's children at
        once (`proc.stop_all`), marks each leg not yet reported as interrupted,
        and goes on up."""
        if not runnable:
            return []
        runner = self._runner_for(run_tag)
        results, reported = [], set()
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.jobs, len(runnable)))
        try:
            futures = {pool.submit(runner.run, leg, avail, commit): leg
                       for leg, avail in runnable}
            for future in concurrent.futures.as_completed(futures):
                leg = futures[future]
                try:
                    out = future.result()
                except Exception as exc:            # noqa: BLE001
                    out = legrun.LegResult(name=leg.name, job=leg.job,
                                           ci_name=leg.ci_name, tier=leg.tier,
                                           state="error", step="runner",
                                           reason=f"{type(exc).__name__}: {exc}")
                results.append(out)
                self._say(f"{out.name}: {out.state} -- {out.summary()}")
                if post:
                    self._status(res, out.state, f"{gh_mod.CONTEXT}/{out.name}",
                                 out.summary())
                reported.add(out.name)
        except BaseException:
            proc.stop_all()
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            finally:
                proc.clear_stop()
            if post:
                for leg, _ in runnable:
                    if leg.name not in reported:
                        self._status(res, "error", f"{gh_mod.CONTEXT}/{leg.name}",
                                     "interrupted")
            raise
        finally:
            pool.shutdown(wait=True)
            if not self.keep:
                _rmtree_force(self.work_root / run_tag)
        order = {leg.name: i for i, leg in enumerate(legs_mod.CATALOG)}
        return sorted(results, key=lambda r: order.get(r.name, len(order)))

    # --- the guarded merge --------------------------------------------------------------------

    def _guarded_merge(self, res, tag, number, allow_base_drift) -> str:
        """Merge a green PR, or say why not: ``"merged"``, ``"refused"``,
        ``"failed"``, or ``"retest"`` when the base moved to a different merge."""
        try:
            pr = self.github.pr(number)
        except gh_mod.GitHubError as exc:
            res.merge_state = f"refused: could not read the PR again: {exc}"
            return "refused"
        why = _merge_refusal(pr, res.head_sha, res.base_ref)
        if why:
            res.merge_state = f"refused: {why}"
            return "refused"
        base_now, error = self._fetch_base(tag, res.base_ref)
        if error:
            res.merge_state = f"refused: {error}"
            return "refused"
        if base_now != res.base_sha:
            moved = (f"the base moved to {_short(base_now)} after "
                     f"{_short(res.base_sha)} was tested")
            pv = self._preview(base_now, res.head_sha)
            if pv.error:
                res.merge_state = f"refused: {moved}, and the new merge failed: {pv.error}"
                return "refused"
            if pv.conflicts or pv.tree != res.tree:
                if pv.conflicts or not allow_base_drift:
                    res.notes.append(f"{moved} and the merge changed; testing again")
                    return "retest"
                res.notes.append(f"{moved}; --allow-base-drift merged a tree this run "
                                 f"did not test")
            else:
                res.notes.append(f"{moved}; the merge tree is the one tested")
        # Until GitHub is read back, a Ctrl+C or a crash leaves this behind.
        res.merge_state = MERGE_UNKNOWN
        rc, text = self.github.merge(number, res.head_sha)
        try:
            state, oid = self.github.merged(number)
        except gh_mod.GitHubError as exc:
            state, oid = "", ""
            res.notes.append(f"could not read the PR back after the merge: {exc}")
        if state != "MERGED":
            res.merge_state = f"failed: gh pr merge exit {rc}: {gh_mod.clip(text, 300)}"
            return "failed"
        res.merge_state, res.merge_commit = "merged", oid
        res.landed_tree_matches = self._landed_tree_matches(tag, res, oid)
        if res.landed_tree_matches is False:
            res.notes.append(f"the merge commit {_short(oid)} carries a tree other than "
                             f"the tested {_short(res.tree)}")
        return "merged"

    def _landed_tree_matches(self, tag, res, oid):
        """True or False once the landed commit is here to read; None if not."""
        if not _OID.match(oid or ""):
            return None
        _, error = self._fetch_base(tag, res.base_ref)
        if error:
            return None
        rc, out, _ = self._git(["rev-parse", "--verify", "--quiet", f"{oid}^{{tree}}"])
        tree = (out or "").strip()
        return (tree == res.tree) if rc == 0 and tree else None

    # --- git ---------------------------------------------------------------------------------

    def _git(self, args, timeout_s=GIT_TIMEOUT_S, stdin=None) -> tuple:
        return self.spawn([GIT, *args], cwd=self.repo_root, timeout_s=timeout_s,
                          stdin=stdin)

    def _fetch(self, specs) -> str:
        rc, out, err = self._git(["fetch", "--no-tags", "--quiet", REMOTE, *specs],
                                 timeout_s=FETCH_TIMEOUT_S)
        return "" if rc == 0 else f"git fetch {REMOTE}: {_why(rc, out, err)}"

    def _fetch_base(self, tag, base_ref) -> tuple:
        ref = f"{REF_ROOT}/{tag}/base"
        error = self._fetch([f"+refs/heads/{base_ref}:{ref}"])
        if error:
            return "", error
        sha = self._rev(ref)
        return (sha, "") if sha else ("", f"git fetch {REMOTE}: {ref} does not resolve")

    def _rev(self, name) -> str:
        rc, out, _ = self._git(["rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"])
        sha = (out or "").strip()
        return sha if rc == 0 and _OID.match(sha) else ""

    def _drop(self, tag) -> None:
        """Delete every ref under this run's tag. Best effort, never raises."""
        try:
            rc, out, _ = self._git(["for-each-ref", "--format=%(refname)",
                                    f"{REF_ROOT}/{tag}/"])
            doomed = [line.strip() for line in (out or "").splitlines() if line.strip()]
            if rc == 0 and doomed:
                self._git(["update-ref", "--stdin"],
                          stdin="".join(f"delete {ref}\n" for ref in doomed))
        except Exception:                               # noqa: BLE001
            pass

    def _preview(self, base, head):
        if self._previewer is not None:
            return self._previewer(base, head)
        return preview_mod.merge_preview(self.repo_root, base, head, run=self.spawn,
                                         environ=self.environ)

    def _status(self, res, state, context, description) -> bool:
        """Post one commit status: False, with GitHub's reason kept, when refused."""
        why = self.github.post_status(res.head_sha, state, context, description)
        if why:
            res.status_errors.append(why)
        return not why

    def _tag(self, prefix) -> str:
        return f"{prefix}-{_NOT_STAMP.sub('', self._now_iso())}-{os.getpid()}"

    # --- the record ---------------------------------------------------------------------------

    def _finish(self, res, started, tag, *, sidecar=True, **remembered) -> PremergeResult:
        """Report, and -- for a run that reached git -- record and, unless
        *sidecar* is False (a run stopped by hand, which the watch must test
        again), remember (*remembered* added to the sidecar entry)."""
        res.duration_s = max(0.0, self._clock() - started)
        res.reason = res.reason or "the check could not be made"
        res.report_text = render(res)
        if tag:
            self._record(res, tag)
            if sidecar:
                self._remember(res, **remembered)
            res.report_text = render(res)
        return res

    def _record(self, res, tag) -> None:
        """One `verification` event. Never raises: a ledger that cannot be
        written must not turn a verdict into a crash; the report says so."""
        try:
            artifact = runlog.write_once(f"premerge-{tag}", res.report_text,
                                         project=self.project) or ""
        except Exception:                               # noqa: BLE001
            artifact = ""
        subject = f"PR #{res.pr_id}" if res.pr_id else f"branch {res.branch}"
        counts_leg, counts = suite_counts(res.legs)
        payload = {
            "text": (f"hive ci premerge: {subject} {res.verdict.upper()} "
                     f"(exit {int(res.exit_code)}): {res.reason}"),
            "argv": list(res.argv),
            "exit_code": int(res.exit_code),
            "counts": counts, "counts_leg": counts_leg,
            "counts_total": sum_counts(res.legs),
            "excerpt": res.report_text[:runlog.EXCERPT_CHARS],
            "verified_by": verify.VERIFIED_BY,
            "scope": SCOPE, "gate": GATE, "mode": res.mode,
            "pr_id": res.pr_id, "branch": res.branch, "base_ref": res.base_ref,
            "base_sha": res.base_sha, "commit_sha": res.head_sha,
            "tested_sha": res.tested_sha, "tree": res.tree,
            "verdict": res.verdict, "reason": res.reason,
            "conflicts": list(res.conflicts),
            "legs": [leg.as_dict() for leg in res.legs],
            "selected": list(res.selected), "partial": res.partial,
            "unavailable": [dict(u) for u in res.unavailable],
            "merge_state": res.merge_state or "skipped",
            "merge_commit": res.merge_commit, "published": res.published,
            "landed_tree_matches": res.landed_tree_matches,
            "rounds": res.rounds, "duration_s": round(res.duration_s, 3),
        }
        if artifact:
            payload["artifact"] = artifact
        try:
            verify.validate_stored(payload)
            events.append_event("verification", payload,
                                tags=["verify", "auto", SCOPE, GATE],
                                project=self.project)
        except Exception as exc:                        # noqa: BLE001
            res.notes.append(f"the run was not recorded: {type(exc).__name__}: {exc}")

    def _remember(self, res, **extra) -> None:
        """Keep what was tested for `hive ci watch`. A `retried` mark -- the
        watch tried an error-only red again -- lasts as long as the head does."""
        if not res.head_sha:
            return
        entry = {"head_sha": res.head_sha, "base_ref": res.base_ref,
                 "base_sha": res.base_sha, "tree": res.tree,
                 "tested_sha": res.tested_sha, "verdict": res.verdict,
                 "exit_code": int(res.exit_code), "ts": self._now_iso(),
                 "conflicts": list(res.conflicts),
                 "legs": {leg.name: leg.state for leg in res.legs},
                 "selected": list(res.selected), "published": res.published,
                 "reason": res.reason, **extra}
        if res.pr_id:
            prior = load_sidecar(self.project)["prs"].get(str(res.pr_id)) or {}
            if prior.get("retried") and prior.get("head_sha") == res.head_sha:
                entry["retried"] = True
        ok = (remember(self.project, res.pr_id, entry) if res.pr_id
              else remember(self.project, res.branch, entry, bucket="branches"))
        if not ok:
            res.notes.append(f"{SIDECAR} could not be written; `hive ci watch` will "
                             f"test this head again")
