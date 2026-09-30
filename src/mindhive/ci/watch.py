# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/watch.py
"""`hive ci watch` -- keep every open PR's pre-merge verdict current
(spec 2026-09-30).

A pass reads the open PRs and, for each, what `premerge` last tested
(`state/ci_premerge.json`):

* **a head it never tested** -- a new PR, or a push -- is tested in full
  (`Premerge.run_pr`, never with `--merge`);
* **the same head on the same base** is left alone;
* **the same head on a base that moved** is checked with one `git merge-tree`
  -- seconds, not the hour a full run costs. A merge that now conflicts turns
  the PR red at once, with a `mindhive-ci` failure status naming the files. A
  merge that is still clean keeps its verdict -- as a PR's checks survive a
  push to its base on GitHub -- and `premerge --merge` re-checks the tree
  before it merges anyway. A PR that was red for a conflict alone and now
  merges cleanly is tested in full: no leg ever ran on it. A PR whose last
  check could not be made is tried again when the base moves.

A conflict is recorded as `premerge` records a verdict (`Premerge.
record_conflict`), so the ledger and `hive ci status` see the PR go red. A red
that errors alone made -- a leg whose setup broke, no test failing and no
conflict -- is tested again once for its head: what broke may have been this
machine. A verdict GitHub refused to take as a status is posted again on the
next pass.

A PR from a fork is skipped: its legs would run its code here, and only
`hive ci premerge N --trust`, once its diff is read, runs it. A PR whose last
run tested fewer legs than this watch asks for is tested again.

It never merges: that is `hive ci premerge N --merge`, asked for by name.
`--once` makes one pass, for a scheduled task. Without it the watch loops,
sleeping `--interval` seconds between passes; a pass that fails is reported
and the loop goes on.
"""
import time

from . import github as gh_mod
from . import legs as legs_mod
from . import premerge as premerge_mod
from . import preview as preview_mod

__all__ = ["DEFAULT_INTERVAL_S", "Watcher", "describe"]

DEFAULT_INTERVAL_S = 300.0


def describe(action) -> str:
    """One line for one action, the way the watch prints it."""
    n = action.get("pr")
    subject = f"PR #{n}" if n else "watch"
    kind, detail = action.get("action"), action.get("detail") or ""
    verdict = str(action.get("verdict") or "")
    if kind == "tested":
        refused = len(action.get("status_errors") or ())
        return (f"{subject}: tested -- {verdict.upper()}: {detail}"
                + (f"; {refused} status(es) could not be posted" if refused else ""))
    if kind == "republished":
        return f"{subject}: its {verdict} verdict is posted again -- {detail}"
    if kind == "conflict":
        return f"{subject}: RED -- {detail}"
    if kind == "base-moved":
        return (f"{subject}: the base moved and it still merges cleanly; "
                f"the {verdict} verdict stands")
    if kind == "unchanged":
        return f"{subject}: unchanged since its last test ({verdict})"
    if kind == "skipped":
        return f"{subject}: skipped ({detail})"
    return f"{subject}: could not check -- {detail}"


class Watcher:
    """Passes over the open PRs through one `premerge.Premerge`."""

    def __init__(self, premerge, *, github=None, project=None, legs_spec=None,
                 skip_drafts=False, interval_s=DEFAULT_INTERVAL_S, sleep=time.sleep,
                 say=None, merge_tree=None):
        self.premerge = premerge
        self.github = github if github is not None else premerge.github
        self.project = premerge.project if project is None else project
        self.legs_spec = legs_spec
        self._wanted = {leg.name for leg in legs_mod.select(legs_spec)}
        self.skip_drafts = bool(skip_drafts)
        self.interval_s = float(interval_s)
        self._sleep = sleep
        self._say = say or (lambda text: None)
        self._merge_tree = merge_tree or (
            lambda a, b: preview_mod.merge_tree(premerge.repo_root, a, b,
                                                run=premerge.spawn))

    def once(self) -> list:
        """One pass. Returns what it did, one action per open PR."""
        try:
            prs = self.github.open_prs()
        except gh_mod.GitHubError as exc:
            action = {"pr": 0, "action": "error", "detail": str(exc)}
            self._say(describe(action))
            return [action]
        known = premerge_mod.load_sidecar(self.project)["prs"]
        actions = []
        for pr in prs:
            n = int(pr.get("number") or 0)
            if self.skip_drafts and pr.get("isDraft"):
                action = {"pr": n, "action": "skipped", "detail": "a draft"}
            elif gh_mod.from_fork(pr):
                action = {"pr": n, "action": "skipped",
                          "detail": f"from a fork; run it by hand with `hive ci premerge "
                                    f"{n} --trust` once its diff is read"}
            else:
                last = known.get(str(n)) or {}
                same = (str(pr.get("headRefOid") or "") == last.get("head_sha")
                        and str(pr.get("baseRefName") or "") == last.get("base_ref")
                        and self._covers(last))
                action = self._recheck(n, pr, last) if same else self._test(n)
            self._say(describe(action))
            actions.append(action)
        return actions

    def loop(self, passes=None) -> int:
        """Pass after pass, *passes* times or for ever. Returns the passes made."""
        done = 0
        while passes is None or done < passes:
            try:
                self.once()
            except Exception as exc:                    # noqa: BLE001
                self._say(f"watch: a pass failed: {type(exc).__name__}: {exc}")
            done += 1
            if passes is None or done < passes:
                self._sleep(self.interval_s)
        return done

    def _covers(self, last) -> bool:
        """Whether the run *last* remembers tested every leg this watch asks
        for. An entry kept before runs named their legs tested them all."""
        selected = last.get("selected")
        return selected is None or self._wanted <= set(selected)

    @staticmethod
    def _retryable(last) -> bool:
        """A red that errors alone made -- no conflict, no failing test -- and
        not tried again yet for this head."""
        if last.get("verdict") != "red" or last.get("conflicts") or last.get("retried"):
            return False
        states = set((last.get("legs") or {}).values())
        return "error" in states and "failure" not in states

    def _test(self, n, why="") -> dict:
        res = self.premerge.run_pr(n, legs_spec=self.legs_spec)
        action = {"pr": n, "action": "tested", "verdict": res.verdict,
                  "exit_code": res.exit_code, "detail": why + res.reason}
        if res.status_errors:
            action["status_errors"] = list(res.status_errors)
        return action

    def _republish(self, n, last) -> dict:
        verdict = str(last.get("verdict") or "error")
        reason = str(last.get("reason") or "")
        why = self.github.post_status(str(last.get("head_sha") or ""),
                                      premerge_mod.OVERALL_STATE.get(verdict, "error"),
                                      gh_mod.CONTEXT, reason)
        if why:
            return {"pr": n, "action": "error",
                    "detail": f"the {verdict} verdict still could not be posted: {why}"}
        premerge_mod.remember(self.project, n, {**last, "published": True})
        return {"pr": n, "action": "republished", "verdict": verdict, "detail": reason}

    def _recheck(self, n, pr, last) -> dict:
        if last.get("published") is False and self.premerge.post_statuses:
            return self._republish(n, last)
        # The mark goes down before the run: a run that cannot record it
        # must not be tried again on every pass.
        if self._retryable(last) and premerge_mod.remember(
                self.project, n, {**last, "retried": True}):
            return self._test(n, why="tried again once, its last red was errors alone: ")
        base_ref = str(pr.get("baseRefName") or "")
        base_now, error = self.premerge.fetch_base(base_ref)
        if error:
            return {"pr": n, "action": "error", "detail": error}
        if base_now == (last.get("checked_base_sha") or last.get("base_sha")):
            return {"pr": n, "action": "unchanged", "verdict": last.get("verdict"),
                    "detail": ""}
        if last.get("verdict") == "error":
            return self._test(n)
        head = str(last.get("head_sha") or "")
        tree, conflicts, error = self._merge_tree(base_now, head)
        if error:
            return self._test(n)
        if conflicts:
            return self._conflict(n, pr, base_ref, base_now, head, tree, conflicts)
        if last.get("verdict") == "red" and last.get("conflicts"):
            return self._test(n)
        premerge_mod.remember(self.project, n, {**last, "checked_base_sha": base_now,
                                                "checked_tree": tree})
        return {"pr": n, "action": "base-moved", "verdict": last.get("verdict"),
                "detail": f"{base_ref} at {base_now[:12]}"}

    def _conflict(self, n, pr, base_ref, base_now, head, tree, conflicts) -> dict:
        res = self.premerge.record_conflict(
            n, branch=str(pr.get("headRefName") or ""), base_ref=base_ref,
            base_sha=base_now, head_sha=head, conflicts=conflicts, tree=tree)
        detail = res.reason
        if res.status_errors:
            detail += f"; the status could not be posted: {res.status_errors[0]}"
        return {"pr": n, "action": "conflict", "verdict": res.verdict, "detail": detail}
