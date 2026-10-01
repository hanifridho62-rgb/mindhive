# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/github.py
"""Every `gh` call the pre-merge CI makes, in one place (spec 2026-09-30).

Commit statuses (`POST repos/{owner}/{repo}/statuses/{sha}`) are a plain REST
write: no Actions minutes, no runner, nothing billed -- which is the point
while the account's Actions jobs are refused for payment. They show on the PR
where the Actions checks used to, as `mindhive-ci` and `mindhive-ci/<leg>`.

The merge is the one irreversible call, so its argv is fixed here and nowhere
else: ``gh pr merge N --merge --match-head-commit <sha>``. A merge commit,
which is this repository's convention; the head pinned, so GitHub itself
refuses the merge when the head moved after it was tested; nothing that
rewrites the branch, deletes it, bypasses a rule or merges later.

A PR from a fork is `from_fork`: its code is not the owner's, and running its
legs runs that code here, so `premerge` asks for `--trust` first.

Reads raise `GitHubError` in gh's own words. `post_status` never raises: a PR
that could not be annotated still has a verdict, and the caller records why.
"""
import json

from . import proc

__all__ = ["CONTEXT", "DESCRIPTION_MAX", "GH", "GitHub", "GitHubError",
           "PR_FIELDS", "STATES", "clip", "from_fork"]

GH = "gh"
#: The overall context; each leg posts `mindhive-ci/<leg>`.
CONTEXT = "mindhive-ci"
STATES = ("pending", "success", "failure", "error")
#: GitHub refuses a status description past 140 characters.
DESCRIPTION_MAX = 140
PR_FIELDS = ("number,state,isDraft,isCrossRepository,headRefName,headRefOid,"
             "headRepositoryOwner,baseRefName,title,url")
TIMEOUT_S = 120.0
MERGE_TIMEOUT_S = 300.0


class GitHubError(RuntimeError):
    """A `gh` read that had to succeed did not."""


def clip(text, limit=DESCRIPTION_MAX) -> str:
    """*text* on one line, at most *limit* characters, ASCII ellipsis."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def from_fork(pr) -> bool:
    """Whether *pr*'s head lives in another repository. A PR gh did not place
    counts as one: its legs would run someone else's code on this machine."""
    return pr.get("isCrossRepository") is not False


def _json(out, what):
    try:
        return json.loads(out or "")
    except ValueError:
        raise GitHubError(f"{what}: not JSON: {clip(out, 200)}") from None


class GitHub:
    """`gh`, run in *repo_root* through one injectable *run*."""

    def __init__(self, repo_root, run=proc.run):
        self.repo_root = repo_root
        self._run = run
        self._slug = None

    def _gh(self, args, timeout_s=TIMEOUT_S) -> tuple:
        return self._run([GH, *args], cwd=self.repo_root, timeout_s=timeout_s)

    def _must(self, args, timeout_s=TIMEOUT_S) -> str:
        rc, out, err = self._gh(args, timeout_s)
        if rc != 0:
            why = (err or "").strip() or (out or "").strip() or f"exit {rc}"
            raise GitHubError(f"gh {' '.join(args[:2])}: {clip(why, 500)}")
        return out or ""

    def ready(self) -> str:
        """``""`` when `gh` is installed and logged in, else why not."""
        rc, out, err = self._gh(["auth", "status"])
        return "" if rc == 0 else clip((err or "").strip() or (out or "").strip()
                                       or f"gh auth status: exit {rc}", 300)

    def slug(self) -> str:
        """``owner/name`` of the repository, asked once."""
        if self._slug is None:
            slug = self._must(["repo", "view", "--json", "nameWithOwner",
                               "-q", ".nameWithOwner"]).strip()
            if slug.count("/") != 1:
                raise GitHubError(f"gh repo view: no owner/name in {slug!r}")
            self._slug = slug
        return self._slug

    def pr(self, number) -> dict:
        got = _json(self._must(["pr", "view", str(int(number)), "--json", PR_FIELDS]),
                    f"gh pr view {number}")
        if not isinstance(got, dict):
            raise GitHubError(f"gh pr view {number}: not an object")
        return got

    def open_prs(self) -> list:
        got = _json(self._must(["pr", "list", "--state", "open", "--limit", "100",
                                "--json", PR_FIELDS]), "gh pr list")
        if not isinstance(got, list):
            raise GitHubError("gh pr list: not a list")
        return sorted((p for p in got if isinstance(p, dict)),
                      key=lambda p: int(p.get("number") or 0))

    def post_status(self, sha, state, context=CONTEXT, description="") -> str:
        """Post one commit status. ``""`` when it landed, else why not."""
        if state not in STATES:
            return f"not a commit status state: {state!r}"
        try:
            slug = self.slug()
        except GitHubError as exc:
            return str(exc)
        rc, out, err = self._gh(["api", "-X", "POST", f"repos/{slug}/statuses/{sha}",
                                 "-f", f"state={state}", "-f", f"context={context}",
                                 "-f", f"description={clip(description)}"])
        if rc == 0:
            return ""
        why = (err or "").strip() or (out or "").strip() or f"exit {rc}"
        return f"status {context}={state} on {str(sha)[:12]}: {clip(why, 300)}"

    def merge(self, number, head_sha) -> tuple:
        """``(exit_code, gh's words)``. Never judged by the code alone: the
        caller reads the PR back with `merged`."""
        rc, out, err = self._gh(["pr", "merge", str(int(number)), "--merge",
                                 "--match-head-commit", str(head_sha)],
                                timeout_s=MERGE_TIMEOUT_S)
        return rc, ((out or "") + (err or "")).strip()

    def merged(self, number) -> tuple:
        """``(state, merge commit sha or "")`` as GitHub has it now."""
        got = _json(self._must(["pr", "view", str(int(number)), "--json",
                                "state,mergeCommit"]), f"gh pr view {number}")
        commit = got.get("mergeCommit") if isinstance(got, dict) else None
        state = str(got.get("state") or "") if isinstance(got, dict) else ""
        oid = str(commit.get("oid") or "") if isinstance(commit, dict) else ""
        return state, oid
