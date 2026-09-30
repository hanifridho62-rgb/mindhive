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

It never merges: that is `hive ci premerge N --merge`, asked for by name.
`--once` makes one pass, for a scheduled task. Without it the watch loops,
sleeping `--interval` seconds between passes; a pass that fails is reported
and the loop goes on.
"""
import time

from .. import events
from . import github as gh_mod
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
        return f"{subject}: tested -- {verdict.upper()}: {detail}"
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
            else:
                last = known.get(str(n)) or {}
                same = (str(pr.get("headRefOid") or "") == last.get("head_sha")
                        and str(pr.get("baseRefName") or "") == last.get("base_ref"))
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

    def _test(self, n) -> dict:
        res = self.premerge.run_pr(n, legs_spec=self.legs_spec)
        return {"pr": n, "action": "tested", "verdict": res.verdict,
                "exit_code": res.exit_code, "detail": res.reason}

    def _recheck(self, n, pr, last) -> dict:
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
            return self._conflict(n, base_ref, base_now, head, conflicts, last)
        if last.get("verdict") == "red" and last.get("conflicts"):
            return self._test(n)
        premerge_mod.remember(self.project, n, {**last, "checked_base_sha": base_now,
                                                "checked_tree": tree})
        return {"pr": n, "action": "base-moved", "verdict": last.get("verdict"),
                "detail": f"{base_ref} at {base_now[:12]}"}

    def _conflict(self, n, base_ref, base_now, head, conflicts, last) -> dict:
        shown = ", ".join(conflicts[:5])
        if len(conflicts) > 5:
            shown += f" and {len(conflicts) - 5} more"
        detail = f"conflicts with {base_ref} at {base_now[:12]}: {shown}"
        if self.premerge.post_statuses:
            self.github.post_status(head, "failure", gh_mod.CONTEXT, detail)
        premerge_mod.remember(self.project, n, {
            **last, "verdict": "red", "exit_code": 1, "conflicts": list(conflicts),
            "checked_base_sha": base_now, "ts": events.now_iso()})
        return {"pr": n, "action": "conflict", "verdict": "red", "detail": detail}
