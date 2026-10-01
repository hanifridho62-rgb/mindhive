# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/runner.py
"""The native CI runner (blueprint P1g; MEB CI-01).

Four gates, in an ephemeral git worktree, stopping at the first red, and one
durable record of the verdict. It exists so the gate that decides whether work
lands is one an operator can run on their own machine, offline, in seconds --
`.github/workflows/ci.yml` is manual-and-tag only (RP III.2) precisely because
this is the daily gate.

The order is cheapest-first: `GATE1_FILES` (the repo's own guards, filtered by
what the tree actually HAS, because pytest answers exit 4 for a path that is
not there and a branch predating one of these files is not broken -- GAP 2),
then `pytest -q`, then the pg tier and `hive pg parity` only when
`MINDHIVE_PG_DSN_ADMIN` names a server, then `chain.verify()` IN-PROCESS --
spawning a child to ask about the ledger would only add a way for the answer
to be about a different store.

**The ephemeral worktree may not check out the base.** `git worktree add
--detach` puts it on a detached HEAD and nothing afterwards names `master` as
a checkout target, because git refuses -- `fatal: master is already checked
out` -- whenever the operator has it open in the main checkout, which is the
normal case (RP KRITIK 2). So the offline merge either fast-forwards the base
by `git fetch . <branch>:<base>` when nothing holds it, or builds
`ci/integration-<pr>` here and hands the operator a two-command notice.

**The verdict is appended once**, as a `verification` carrying `scope: "ci"`
and the gate that decided, built from output already captured rather than
through `verify.run_and_record`: re-running the deciding gate to record it
would double the longest run in the build, and gate 4 has no argv to re-run at
all. `verify.validate_stored` still owns the payload's shape and its stamp, so
nothing here can record a run it did not observe.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import advice as authorship
from .. import chain, events, hookcapture, paths, roadmap, runlog, verify

__all__ = ["APPROVAL_TEMPLATE", "CIRunResult", "CIRunnerError", "GATE1_FILES",
           "GATE_NAMES", "MERGE_STATES", "MODES", "NativeCIRunner", "SCOPE"]

GIT = "git"
GH = "gh"

#: The repo's own guards. Cheap, and each one catches a whole class of mistake
#: the suite would not name as clearly.
GATE1_FILES = ("tests/test_file_size_cap.py", "tests/test_docs_parity.py",
               "tests/test_cli_arg_wiring.py", "tests/test_architecture_drift.py",
               "tests/test_pg_repo_gates.py")
#: What a record's `gate` key may say -- the gate that DECIDED the run.
GATE_NAMES = ("gate1_repo_guards", "gate2_suite", "gate3_postgres",
              "gate4_chain")
ACQUIRE_STAGE = "acquire_worktree"
#: `github` when `gh` is logged in, `offline` when it is not (RP III.3).
MODES = ("github", "offline")
#: `skipped` is the honest answer for a run that was never asked to merge, and
#: `pending_operator` for one that prepared a merge it may not complete itself.
MERGE_STATES = ("merged", "pending_operator", "refused", "skipped")
SCOPE = "ci"

WORKTREE_DIRNAME = ".worktrees"
INTEGRATION_PREFIX = "ci/integration-"
BASE_BRANCH = "master"
REMOTE_BASE = "origin/master"
PG_DSN_ENV = "MINDHIVE_PG_DSN_ADMIN"
APPROVAL_TEMPLATE = "MindHive Native CI: All {n} tests verified green."

#: Three attempts, 200 ms apart (MEB CI-01). Windows holds a file handle open
#: for a moment after the process that had it exits, and a single attempt fails
#: on a tree pytest was reading a heartbeat ago.
CLEANUP_ATTEMPTS = 3
CLEANUP_PAUSE_S = 0.2
#: `FAILED …` lines carried on the result: enough to see the shape of a
#: breakage, bounded because a suite can fail in the hundreds.
_FAILED_CAP = 20

#: `chain.verify` answers not-ok for a store nothing has ever checkpointed,
#: and for a checkpoint this build cannot read. Neither says the history is
#: WRONG -- the first is every fresh machine and the second is a newer writer
#: -- so gate 4 notes them and moves on. Everything else not-ok is red: an
#: integrity gate that guesses in the other direction is not a gate.
CHAIN_UNVERIFIABLE = ("no chain.json", "unsupported_checkpoint_version")

GATE_TIMEOUT_S = 900.0
SUITE_TIMEOUT_S = 3600.0
PROBE_TIMEOUT_S = 120.0


class CIRunnerError(RuntimeError):
    """The runner could not even set up -- no worktree, so no gates."""


def _default_runner(argv, cwd=None, env=None, timeout_s=None) -> tuple:
    """``(returncode, stdout, stderr)`` for one child. Never raises -- a
    missing executable, a timeout and a crash all arrive as a non-zero code
    with the text in *stderr* (the `repo._default_runner` convention, plus the
    *env* this one needs)."""
    try:
        proc = subprocess.run([str(a) for a in argv],
                              cwd=None if cwd is None else str(cwd), env=env,
                              capture_output=True, text=True, timeout=timeout_s)
    except Exception as exc:                            # noqa: BLE001
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@dataclass
class CIRunResult:
    """One CI run's whole verdict. Every field is present on construction --
    a key that appears only sometimes is how a renderer starts failing on the
    quiet case rather than the loud one."""

    pr_id: int = 0
    branch: str = ""
    commit_sha: str = ""
    ok: bool = False
    exit_code: int = 0
    duration_s: float = 0.0
    counts: dict = field(default_factory=dict)
    failed_tests: tuple = ()
    report_text: str = ""
    verified_by: str = verify.VERIFIED_BY
    mode: str = MODES[0]
    merge_state: str = "skipped"
    cleanup_failed: bool = False

    def as_dict(self) -> dict:
        """The machine-readable shape `hive ci run --json` prints."""
        return {"pr_id": self.pr_id, "branch": self.branch,
                "commit_sha": self.commit_sha, "ok": self.ok,
                "exit_code": self.exit_code,
                "duration_s": round(self.duration_s, 3),
                "counts": dict(self.counts),
                "failed_tests": list(self.failed_tests),
                "report_text": self.report_text,
                "verified_by": self.verified_by, "mode": self.mode,
                "merge_state": self.merge_state,
                "cleanup_failed": self.cleanup_failed}


class NativeCIRunner:
    """Runs the gates for one PR in a worktree of its own, and records once."""

    def __init__(self, repo_root, project="test", runner=None,
                 clock=time.monotonic, sleep=time.sleep, now=time.time):
        self.repo_root = Path(repo_root)
        self.project = project
        self.runner = runner or _default_runner
        self._clock = clock
        self._sleep = sleep
        self._now = now

    # --- the world ------------------------------------------------------------------

    def _run(self, argv, cwd=None, env=None, timeout_s=PROBE_TIMEOUT_S) -> tuple:
        return self.runner([str(a) for a in argv], cwd=cwd, env=env,
                           timeout_s=timeout_s)

    def _gate_env(self, wt) -> dict:
        """The environment every gate child runs in (RP I.2, MEB K14).

        PYTHONPATH is PREPENDED, not assigned: without it the child imports
        the editable install and verifies a different tree than the one it is
        judging; assigning it would take the operator's own entries away. The
        no-bytecode default is `paths.no_bytecode_env`'s: a gate leaves no
        `__pycache__` in the tree it is about to delete unless the operator
        chose otherwise. Never the SOP secret or token (PR #136 review, P2):
        `authorship.scrub_agent_env` says why code under test sees neither."""
        env = paths.no_bytecode_env(authorship.scrub_agent_env(withhold_secret=True))
        src, prior = str(Path(wt) / "src"), env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (src + os.pathsep + prior) if prior else src
        return env

    def _base_ref(self) -> str:
        """What an integration branch is cut FROM: the remote's base when it
        resolves, else the local one. A checkout with no remote is the offline
        case this runner is for, so a missing `origin/master` is normal."""
        rc, _, _ = self._run([GIT, "rev-parse", "--verify", REMOTE_BASE],
                             cwd=self.repo_root)
        return REMOTE_BASE if rc == 0 else BASE_BRANCH

    def _detect_mode(self) -> str:
        rc, _, _ = self._run([GH, "auth", "status"], cwd=self.repo_root)
        return MODES[0] if rc == 0 else MODES[1]

    def _acquire_worktree(self, pr_id, branch) -> Path:
        """`.worktrees/ci-<pr>-<ts>`, on a DETACHED head (RP KRITIK 2).

        Detached so `gh pr merge --delete-branch` never tries to move this
        worktree onto the base, and so the branch under test stays owned by
        whoever else has it open."""
        wt = self.repo_root / WORKTREE_DIRNAME / f"ci-{int(pr_id)}-{int(self._now())}"
        rc, out, err = self._run(
            [GIT, "worktree", "add", "--detach", str(wt), str(branch)],
            cwd=self.repo_root)
        if rc != 0:
            raise CIRunnerError(
                f"git worktree add --detach {wt} {branch} exit {rc}: "
                f"{(err or out).strip()}")
        return wt

    def _get_head_sha(self, wt) -> str:
        """The commit the gates actually ran against, or `""`. Never a guess:
        a record naming the wrong commit is worse than one naming none."""
        rc, out, _ = self._run([GIT, "rev-parse", "HEAD"], cwd=wt)
        return out.strip() if rc == 0 else ""

    # --- the gates ------------------------------------------------------------------

    def _pytest(self, wt, env, extra, timeout_s) -> tuple:
        argv = [sys.executable, "-m", "pytest", "-q", *extra]
        rc, out, err = self._run(argv, cwd=wt, env=env, timeout_s=timeout_s)
        return argv, rc, (out or "") + (err or "")

    def _execute_gates(self, wt, notes) -> tuple:
        """`(gate, argv, exit_code, output)` for the gate that DECIDED.

        A green run is decided by the suite, so its counts are the ones the
        record and the approval body quote."""
        env = self._gate_env(wt)
        present = [f for f in GATE1_FILES if (Path(wt) / f).exists()]
        missing = [f for f in GATE1_FILES if f not in present]
        if missing:
            notes.append("gate1_repo_guards: not in this tree, skipped: "
                         + ", ".join(missing))
        if present:
            argv, rc, out = self._pytest(wt, env, present, GATE_TIMEOUT_S)
            if rc != 0:
                return GATE_NAMES[0], argv, rc, out

        suite = (GATE_NAMES[1],) + self._pytest(wt, env, (), SUITE_TIMEOUT_S)
        if suite[2] != 0:
            return suite

        if os.environ.get(PG_DSN_ENV):
            argv, rc, out = self._pytest(wt, env, ("-m", "pg"), GATE_TIMEOUT_S)
            if rc != 0:
                return GATE_NAMES[2], argv, rc, out
            argv = [sys.executable, "-m", "mindhive", "pg", "parity"]
            rc, o, e = self._run(argv, cwd=wt, env=env, timeout_s=GATE_TIMEOUT_S)
            if rc != 0:
                return GATE_NAMES[2], argv, rc, (o or "") + (e or "")
        else:
            notes.append(f"gate3_postgres: skipped, no {PG_DSN_ENV}")

        verdict = chain.verify(self.project)
        reason = str(verdict.get("reason") or "")
        if not verdict.get("ok"):
            if reason.startswith(CHAIN_UNVERIFIABLE):
                notes.append(f"gate4_chain: unverifiable, not broken: {reason}")
            else:
                return (GATE_NAMES[3],
                        [sys.executable, "-m", "mindhive", "verify"], 1,
                        json.dumps(verdict, ensure_ascii=False, sort_keys=True))
        return suite

    # --- the merge (RP KRITIK 2, III.3) ---------------------------------------------

    def _merge_github(self, res, wt, notes) -> None:
        body = APPROVAL_TEMPLATE.format(n=int(res.counts.get("passed", 0)))
        self._run([GH, "pr", "review", str(res.pr_id), "--approve", "-b", body],
                  cwd=wt)
        rc, out, err = self._run(
            [GH, "pr", "merge", str(res.pr_id), "--squash", "--delete-branch"],
            cwd=wt)
        if rc != 0:
            # A note, not a verdict: `--delete-branch` failing on a protected
            # or already-gone branch exits non-zero for a PR that DID merge.
            notes.append(f"gh pr merge exit {rc}: {(err or out).strip()}")
        rc, out, _ = self._run(
            [GH, "pr", "view", str(res.pr_id), "--json", "state,mergedAt"],
            cwd=wt)
        state = ""
        try:
            got = json.loads(out or "{}")
            state = str((got or {}).get("state") or "")
        except Exception:                               # noqa: BLE001
            state = ""
        if state.upper() == "MERGED":
            res.merge_state = "merged"
            return
        res.merge_state = "refused"
        notes.append(f"PR #{res.pr_id} is {state or 'in an unknown state'} "
                     f"after the merge attempt -- not merged")

    def _merge_offline(self, res, wt, notes) -> None:
        rc, listing, _ = self._run([GIT, "worktree", "list", "--porcelain"],
                                   cwd=self.repo_root)
        held = f"branch refs/heads/{BASE_BRANCH}" in (listing or "")
        if not held:
            # Nothing has the base open, so it can be fast-forwarded in place.
            # `fetch` into a local ref is ff-only implicitly: a non-ff answers
            # non-zero rather than rewriting anyone's history.
            rc, out, err = self._run(
                [GIT, "fetch", ".", f"{res.branch}:{BASE_BRANCH}"],
                cwd=self.repo_root)
            if rc == 0:
                res.merge_state = "merged"
            else:
                res.merge_state = "refused"
                notes.append(f"git fetch . {res.branch}:{BASE_BRANCH} exit "
                             f"{rc}: {(err or out).strip()}")
            return
        integ = f"{INTEGRATION_PREFIX}{res.pr_id}"
        rc, out, err = self._run([GIT, "checkout", "-B", integ, self._base_ref()],
                                 cwd=wt)
        if rc == 0:
            rc, out, err = self._run([GIT, "merge", "--ff-only", res.branch],
                                     cwd=wt)
        if rc != 0:
            res.merge_state = "refused"
            notes.append(f"{integ}: exit {rc}: {(err or out).strip()}")
            return
        res.merge_state = "pending_operator"
        notes.append(f"{BASE_BRANCH} is checked out elsewhere, so this run "
                     f"prepared the merge and stopped: "
                     f"git switch {BASE_BRANCH} && "
                     f"git merge --ff-only {integ}")

    def _execute_merge(self, res, wt, notes) -> None:
        if res.mode == MODES[0]:
            self._merge_github(res, wt, notes)
        else:
            self._merge_offline(res, wt, notes)

    # --- the record -----------------------------------------------------------------

    def _record_verification(self, res, *, gate, argv, exit_code, output) -> None:
        """One `verification`, carrying `scope: "ci"` and the deciding gate.

        Built from output already captured: re-running the deciding gate to
        record it would double the longest run in the build, and gate 4 has no
        argv to re-run. `verify.validate_stored` still owns the shape and the
        stamp, so this cannot record a run it did not observe. Never raises --
        a down ledger must not turn a real verdict into a crash."""
        text = (output or "").strip()
        artifact = None
        try:
            artifact = runlog.write_once(f"ci-{events.now_iso()}", output,
                                         project=self.project)
        except Exception:                               # noqa: BLE001
            artifact = None                 # evidence lost; the excerpt stays
        payload = {
            "text": f"hive ci: PR #{res.pr_id} {gate} exit {int(exit_code)}",
            "argv": [str(a) for a in argv],
            "exit_code": int(exit_code),
            "counts": dict(res.counts),
            "excerpt": text[:runlog.EXCERPT_CHARS],
            "verified_by": verify.VERIFIED_BY,
            "scope": SCOPE, "gate": gate,
            "pr_id": res.pr_id, "branch": res.branch,
            "commit_sha": res.commit_sha,
            "mode": res.mode, "merge_state": res.merge_state,
        }
        if artifact:
            payload["artifact"] = artifact
        try:
            verify.validate_stored(payload)
            events.append_event("verification", payload,
                                tags=["verify", "auto", SCOPE],
                                project=self.project)
        except Exception:                               # noqa: BLE001
            pass

    def _file_incident(self, res) -> None:
        """A red run crosses into incidents through the same bridge a failed
        tool call uses, so `hive incidents` sees it without knowing about CI."""
        try:
            hookcapture.record_post_tool_use_failure(json.dumps({
                "tool_name": "hive ci",
                "tool_input": {"pr": res.pr_id, "branch": res.branch},
                "error": res.report_text[:runlog.EXCERPT_CHARS],
            }, ensure_ascii=False), project=self.project)
        except Exception:                               # noqa: BLE001
            pass

    def _retire_roadmap(self, res) -> None:
        if res.merge_state != "merged":
            return
        try:
            roadmap.retire_landed(project=self.project, root=str(self.repo_root))
        except Exception:                               # noqa: BLE001
            pass

    # --- cleanup (MEB CI-01) ---------------------------------------------------------

    def _cleanup_worktree(self, wt_path, keep: bool = False) -> bool:
        """Remove the ephemeral worktree. Returns whether cleanup FAILED.

        The caches go first, by hand: pytest and the interpreter write files
        the removing process may not own the handle to, and `git worktree
        remove` reports the whole removal as failed over one of them.

        A directory that is already gone is NOT an early return, which is
        where this parts company with the MEB spelling: git keeps the
        registration in `.git/worktrees/<name>` either way, and leaving it
        means `git worktree list` shows a path that does not exist and a later
        `add` of the same name is refused. Never raises -- a worktree that
        would not go is a note on a real verdict, not a new failure.
        """
        if keep:
            return False
        wt_path = Path(wt_path)
        # Best-effort: `git worktree prune` is next.
        paths.rmtree_force(wt_path / ".pytest_cache")
        if wt_path.exists():
            for pyc in wt_path.rglob("__pycache__"):
                paths.rmtree_force(pyc)
        for attempt in range(CLEANUP_ATTEMPTS):
            rc, _, _ = self._run(
                [GIT, "worktree", "remove", "--force", str(wt_path)],
                cwd=self.repo_root)
            if rc == 0:
                return False
            if attempt + 1 < CLEANUP_ATTEMPTS:
                self._sleep(CLEANUP_PAUSE_S)
        self._run([GIT, "worktree", "prune"], cwd=self.repo_root)
        return True

    # --- the whole run ---------------------------------------------------------------

    def run_pr_gate(self, pr_id, branch, auto_merge: bool = False) -> CIRunResult:
        """Run the gates for *branch* and report. Never raises."""
        started = self._clock()
        res = CIRunResult(pr_id=int(pr_id), branch=str(branch))
        res.mode = self._detect_mode()
        notes = [] if res.mode == MODES[0] else [
            "gh is not logged in, so this run is offline: the review and "
            "squash steps were skipped"]
        try:
            wt = self._acquire_worktree(res.pr_id, res.branch)
        except CIRunnerError as exc:
            res.ok, res.exit_code = False, 1
            res.duration_s = self._clock() - started
            res.report_text = str(exc)
            self._record_verification(res, gate=ACQUIRE_STAGE,
                                      argv=[GIT, "worktree", "add", "--detach"],
                                      exit_code=1, output=res.report_text)
            self._file_incident(res)
            return res

        res.commit_sha = self._get_head_sha(wt)
        gate, argv, code, output = self._execute_gates(wt, notes)
        res.exit_code = int(code)
        res.ok = code == 0
        res.counts = verify.parse_summary(output)
        res.failed_tests = tuple(
            ln.strip() for ln in (output or "").splitlines()
            if ln.startswith("FAILED "))[:_FAILED_CAP]
        if auto_merge and res.ok:
            self._execute_merge(res, wt, notes)
        res.cleanup_failed = self._cleanup_worktree(wt, keep=not res.ok)
        if res.cleanup_failed:
            notes.append(f"the worktree would not go: {wt}")
        res.duration_s = self._clock() - started
        res.report_text = "\n".join(
            [f"{gate}: exit {res.exit_code}"] + notes
            + [(output or "").strip()[:runlog.EXCERPT_CHARS]]).strip()
        self._record_verification(res, gate=gate, argv=argv,
                                  exit_code=res.exit_code, output=output)
        if not res.ok:
            self._file_incident(res)
        self._retire_roadmap(res)
        return res
