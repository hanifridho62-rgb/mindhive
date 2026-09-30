# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/preview.py
"""The merge a PR would make, computed without touching a checkout
(spec 2026-09-30).

`git merge-tree --write-tree` (git >= 2.38) merges two commits entirely in the
object store: no index, no working tree, no ref moved. It uses the same `ort`
strategy as GitHub's merge button, so the tree it answers is the tree a merge
commit would record -- the legs test THAT, which is what the Actions checkout
of `refs/pull/N/merge` gave CI, not the branch head alone.

Its answers are told apart by the TREE LINE, because the exit code alone cannot
do it (measured on git 2.53 and 2.43):

* exit 0 and ``<tree>\\0`` -- clean;
* exit 1 and ``<tree>\\0<path>\\0...`` -- conflicted, and the paths say where;
* exit 1 with no tree (``merge-tree: X - not something we can merge``), or any
  other exit -- an ERROR, which is never reported as a conflict.

A clean merge whose base is already inside the head needs no merge commit of
its own: GitHub's merge commit would carry the head's tree, so the head itself
is tested. Otherwise `git commit-tree` records the preview as an unreferenced
merge commit (parents base and head, identity `mindhive-ci`), so the run has
one sha to name and `git archive` one commit to export.
"""
import os
import re
from dataclasses import dataclass

from . import proc

__all__ = ["CI_IDENTITY", "Preview", "conflict_matrix", "merge_preview",
           "merge_tree"]

GIT = "git"
TIMEOUT_S = 300.0
CI_IDENTITY = {"GIT_AUTHOR_NAME": "mindhive-ci",
               "GIT_AUTHOR_EMAIL": "mindhive-ci@localhost.invalid",
               "GIT_COMMITTER_NAME": "mindhive-ci",
               "GIT_COMMITTER_EMAIL": "mindhive-ci@localhost.invalid"}
_OID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


@dataclass(frozen=True)
class Preview:
    """One merge preview. `clean` is True only when `tested_sha` names the
    commit whose tree the legs should test."""

    base_sha: str
    head_sha: str
    clean: bool = False
    conflicts: tuple = ()
    tree: str = ""
    tested_sha: str = ""
    up_to_date: bool = False
    error: str = ""


def _why(rc, out, err) -> str:
    text = (err or "").strip() or (out or "").strip()
    return text[-500:] if text else f"exit {rc}"


def merge_tree(repo_root, a, b, run=proc.run) -> tuple:
    """``(tree, conflicted_paths, error)`` for merging *b* into *a*.

    Exactly one of three shapes: ``(tree, (), "")`` clean, ``(tree, paths,
    "")`` conflicted, ``("", (), why)`` could not merge at all."""
    rc, out, err = run([GIT, "merge-tree", "--write-tree", "--name-only",
                        "--no-messages", "-z", str(a), str(b)],
                       cwd=repo_root, timeout_s=TIMEOUT_S)
    parts = (out or "").split("\0")
    tree = parts[0].strip()
    if rc in (0, 1) and _OID.match(tree):
        if rc == 0:
            return tree, (), ""
        paths = []
        for path in parts[1:]:
            if path and path not in paths:
                paths.append(path)
        # Exit 1 with a tree is a conflict even if no path came with it: a
        # conflict with no name is still not a clean merge.
        return tree, tuple(paths) or ("(unnamed conflict)",), ""
    return "", (), _why(rc, out, err)


def merge_preview(repo_root, base_sha, head_sha, run=proc.run, environ=None) -> Preview:
    """The merge of *head_sha* into *base_sha*, and the commit to test for it."""
    tree, conflicts, error = merge_tree(repo_root, base_sha, head_sha, run)
    if error:
        return Preview(base_sha, head_sha, error=f"git merge-tree: {error}")
    if conflicts:
        return Preview(base_sha, head_sha, conflicts=conflicts, tree=tree)
    rc, out, err = run([GIT, "merge-base", "--is-ancestor", base_sha, head_sha],
                       cwd=repo_root, timeout_s=TIMEOUT_S)
    if rc == 0:
        return Preview(base_sha, head_sha, clean=True, tree=tree,
                       tested_sha=head_sha, up_to_date=True)
    if rc != 1:
        return Preview(base_sha, head_sha, tree=tree,
                       error=f"git merge-base --is-ancestor: {_why(rc, out, err)}")
    env = {**(os.environ if environ is None else environ), **CI_IDENTITY}
    message = f"mindhive-ci merge preview of {head_sha[:12]} into {base_sha[:12]}"
    rc, out, err = run([GIT, "commit-tree", "--no-gpg-sign", "-p", base_sha,
                        "-p", head_sha, "-m", message, tree],
                       cwd=repo_root, env=env, timeout_s=TIMEOUT_S)
    sha = (out or "").strip()
    if rc != 0 or not _OID.match(sha):
        return Preview(base_sha, head_sha, tree=tree,
                       error=f"git commit-tree: {_why(rc, out, err)}")
    return Preview(base_sha, head_sha, clean=True, tree=tree, tested_sha=sha)


def conflict_matrix(repo_root, base_sha, heads, run=proc.run) -> dict:
    """Which open PRs conflict with the base, and with each other.

    *heads* maps a PR number to its head sha. The answer is
    ``{"base": {pr: [paths]}, "pairs": [{"a", "b", "paths"}], "errors": {}}``.
    A pair is merged head against head, from their own merge base: a pair that
    conflicts cannot both land as they stand, in either order."""
    out = {"base": {}, "pairs": [], "errors": {}}
    order = sorted(heads)
    for pr in order:
        _, paths, error = merge_tree(repo_root, base_sha, heads[pr], run)
        if error:
            out["errors"][str(pr)] = error
        else:
            out["base"][pr] = list(paths)
    ok = [pr for pr in order if pr in out["base"]]
    for i, a in enumerate(ok):
        for b in ok[i + 1:]:
            _, paths, error = merge_tree(repo_root, heads[a], heads[b], run)
            if error:
                out["errors"][f"{a}+{b}"] = error
            elif paths:
                out["pairs"].append({"a": a, "b": b, "paths": list(paths)})
    return out
