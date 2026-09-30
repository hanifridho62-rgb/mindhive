# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.preview` -- the merge a PR would make, computed in the object store.

Real git throughout, on toy repositories under tmp_path: what is worth pinning
is what git ANSWERS -- the tree line, the exit code, the conflicted paths -- and
a fake would only restate what the module assumes. One test keeps a fake, for
the shape no repository can be made to produce on demand: exit 1 and no tree.
"""
import re
import subprocess

import pytest

from mindhive.ci import preview


def _git_version() -> tuple:
    try:
        out = subprocess.run(["git", "version"], capture_output=True, text=True).stdout
    except OSError:
        return (0, 0)
    m = re.search(r"(\d+)\.(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


pytestmark = pytest.mark.skipif(_git_version() < (2, 38),
                                reason="git merge-tree --write-tree needs git >= 2.38")

IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def git(repo, *args) -> str:
    proc = subprocess.run(["git", *IDENT, *args], cwd=repo, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def commit(repo, files, message) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """root: a.txt, b.txt. master then changes b.txt. From root: `clean` adds
    c.txt; `clash` and `other` both rewrite a.txt's first line (they conflict
    with EACH OTHER, not with master); `against` rewrites b.txt (it conflicts
    with master). `ahead` is cut from the new master."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    root = commit(r, {"a.txt": "one\ntwo\n", "b.txt": "b\n"}, "root")
    heads = {}
    for name, files in (("clean", {"c.txt": "c\n"}),
                        ("clash", {"a.txt": "ONE\ntwo\n"}),
                        ("other", {"a.txt": "uno\ntwo\n"}),
                        ("against", {"b.txt": "bee\n"})):
        git(r, "checkout", "-q", "-b", name, root)
        heads[name] = commit(r, files, name)
    git(r, "checkout", "-q", "master")
    base = commit(r, {"b.txt": "b2\n"}, "master moves")
    git(r, "checkout", "-q", "-b", "ahead")
    heads["ahead"] = commit(r, {"d.txt": "d\n"}, "ahead")
    git(r, "checkout", "-q", "master")
    return {"path": r, "root": root, "base": base, **heads}


def test_a_clean_merge_is_committed_as_the_merge_github_would_make(repo):
    r = repo["path"]
    head_before, status_before = git(r, "rev-parse", "HEAD"), git(r, "status", "--porcelain")
    pv = preview.merge_preview(r, repo["base"], repo["clean"])
    assert pv.clean and pv.conflicts == () and pv.error == "" and not pv.up_to_date
    assert git(r, "rev-parse", f"{pv.tested_sha}^{{tree}}") == pv.tree
    assert git(r, "rev-list", "--parents", "-n", "1", pv.tested_sha).split()[1:] == [
        repo["base"], repo["clean"]]
    assert git(r, "log", "-1", "--format=%an <%ae>|%cn", pv.tested_sha) == (
        "mindhive-ci <mindhive-ci@localhost.invalid>|mindhive-ci")
    assert git(r, "ls-tree", "-r", "--name-only", pv.tree).split() == [
        "a.txt", "b.txt", "c.txt"]
    assert git(r, "show", f"{pv.tree}:b.txt") == "b2"       # the base's side kept
    # Nothing checked out moved: the merge happened in the object store.
    assert git(r, "rev-parse", "HEAD") == head_before
    assert git(r, "status", "--porcelain") == status_before


def test_a_head_that_already_holds_the_base_is_tested_as_itself(repo):
    pv = preview.merge_preview(repo["path"], repo["base"], repo["ahead"])
    assert pv.clean and pv.up_to_date and pv.tested_sha == repo["ahead"]
    assert pv.tree == git(repo["path"], "rev-parse", f"{repo['ahead']}^{{tree}}")


def test_a_conflict_names_its_files_and_nothing_is_committed(repo):
    pv = preview.merge_preview(repo["path"], repo["base"], repo["against"])
    assert not pv.clean and pv.conflicts == ("b.txt",)
    assert pv.tested_sha == "" and pv.error == ""


def test_a_commit_that_does_not_exist_is_an_error_never_a_conflict(repo):
    pv = preview.merge_preview(repo["path"], repo["base"], "0" * 40)
    assert not pv.clean and pv.conflicts == () and pv.error
    assert pv.error.startswith("git merge-tree: ")


def test_merge_tree_reads_the_tree_line_not_the_exit_code_alone():
    tree = "a" * 40
    seen = []

    def answering(answer):
        def run(argv, **kw):
            seen.append(list(argv))
            return answer
        return run

    assert preview.merge_tree(".", "x", "y", run=answering((0, tree + "\0", ""))) == (
        tree, (), "")
    assert seen[0] == ["git", "merge-tree", "--write-tree", "--name-only",
                       "--no-messages", "-z", "x", "y"]
    got = preview.merge_tree(".", "x", "y", run=answering(
        (1, f"{tree}\0p/q.py\0p/q.py\0r.txt\0", "")))
    assert got == (tree, ("p/q.py", "r.txt"), "")
    t, c, e = preview.merge_tree(".", "x", "y", run=answering(
        (1, "", "merge-tree: x - not something we can merge")))
    assert (t, c) == ("", ()) and "not something we can merge" in e
    t, c, e = preview.merge_tree(".", "x", "y", run=answering((128, "", "fatal: bad")))
    assert (t, c, e) == ("", (), "fatal: bad")


def test_the_conflict_matrix_names_base_conflicts_and_conflicting_pairs(repo):
    heads = {12: repo["clean"], 13: repo["clash"], 14: repo["other"],
             15: repo["against"]}
    got = preview.conflict_matrix(repo["path"], repo["base"], heads)
    assert got["errors"] == {}
    assert got["base"] == {12: [], 13: [], 14: [], 15: ["b.txt"]}
    assert got["pairs"] == [{"a": 13, "b": 14, "paths": ["a.txt"]}]


def test_the_conflict_matrix_reports_a_head_it_could_not_merge(repo):
    got = preview.conflict_matrix(repo["path"], repo["base"],
                                  {12: repo["clean"], 99: "0" * 40})
    assert got["base"] == {12: []}
    assert set(got["errors"]) == {"99"}
