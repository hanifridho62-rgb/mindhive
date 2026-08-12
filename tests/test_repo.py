# tests/test_repo.py
"""Merge detection that degrades honestly.

Every test here drives `repo.merge_state` through the `runner` seam, so the
suite proves the DEGRADATION paths — no `gh`, no remote, no network — on a
machine that happens to have all three. The seam exists for exactly that: the
four `unknown` reasons are the whole product of this module, and a test that
could only exercise them by uninstalling `gh` would never be written.

The invariant under test throughout: **`merged` is only ever returned on
proof.** Anything softer is `open` or `unknown`-with-a-reason.
"""
import shutil

import pytest
from mindhive import repo

SHA = "0123456789abcdef0123456789abcdef01234567"
ROOT = "/repo"           # never touched: the runner is fake in every test
GH_REMOTE = (0, "origin\thttps://github.com/o/r.git (fetch)\n", "")
NO_REMOTE = (0, "origin\thttps://gitlab.com/o/r.git (fetch)\n", "")


def _runner(responses, log=None):
    """A fake child process. Keys are substrings of the joined argv; the first
    match wins, and an unstubbed call answers 128 — git's "the question was
    never asked" code — so a test that forgets a stub fails loudly instead of
    quietly taking a degraded path it did not mean to test."""
    def run(argv, cwd, timeout_s):
        joined = " ".join(argv)
        if log is not None:
            log.append(joined)
        for needle, answer in responses.items():
            if needle in joined:
                return answer
        return (128, "", f"unstubbed: {joined}")
    return run


def _have_gh(monkeypatch, present=True):
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/gh" if present else None)


# --- the four unknown reasons -------------------------------------------------

def test_no_work_link_is_its_own_reason():
    answer = repo.merge_state(root=ROOT, runner=_runner({}))
    assert answer["state"] == repo.UNKNOWN
    assert answer["reason"] == repo.NO_WORK_LINK
    assert "hive roadmap set" in answer["detail"]      # says how to fix it


def test_outside_a_git_checkout_reports_no_github_remote(monkeypatch):
    monkeypatch.setattr(repo, "worktree_root", lambda: None)
    answer = repo.merge_state(pr=7, runner=_runner({}))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                   repo.NO_GITHUB_REMOTE)


def test_missing_gh_reports_no_gh_cli(monkeypatch):
    _have_gh(monkeypatch, False)
    answer = repo.merge_state(pr=103, root=ROOT, runner=_runner({}))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN, repo.NO_GH_CLI)
    assert "103" in answer["detail"]


def test_a_non_github_remote_reports_no_github_remote(monkeypatch):
    _have_gh(monkeypatch)
    answer = repo.merge_state(pr=103, root=ROOT,
                              runner=_runner({"remote -v": NO_REMOTE}))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                   repo.NO_GITHUB_REMOTE)


def test_a_rate_limited_probe_reports_probe_failed_with_the_detail(monkeypatch):
    _have_gh(monkeypatch)
    answer = repo.merge_state(pr=103, root=ROOT, runner=_runner({
        "remote -v": GH_REMOTE,
        "pr view": (1, "", "HTTP 403: API rate limit exceeded\n"),
    }))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.PROBE_FAILED)
    assert "rate limit exceeded" in answer["detail"]


def test_unreadable_gh_output_is_probe_failed_not_a_guess(monkeypatch):
    _have_gh(monkeypatch)
    answer = repo.merge_state(pr=103, root=ROOT, runner=_runner({
        "remote -v": GH_REMOTE, "pr view": (0, "not json at all", ""),
    }))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.PROBE_FAILED)


# --- the definite answers -----------------------------------------------------

@pytest.mark.parametrize("gh_state,expected", [
    ("MERGED", repo.MERGED), ("CLOSED", repo.CLOSED), ("OPEN", repo.OPEN)])
def test_the_pr_record_answers_all_three_states(monkeypatch, gh_state, expected):
    _have_gh(monkeypatch)
    answer = repo.merge_state(pr=103, root=ROOT, runner=_runner({
        "remote -v": GH_REMOTE,
        "pr view": (0, '{"state": "%s", "mergedAt": "2026-08-12T00:00:00Z", '
                       '"url": "https://github.com/o/r/pull/103"}' % gh_state,
                    ""),
    }))
    assert answer["state"] == expected
    assert "pull/103" in answer["evidence"]


def test_an_unrecognised_pr_state_is_unknown_rather_than_open(monkeypatch):
    """A state gh has not taught us is a gap in this module, not a fact about
    the code. Mapping it to `open` would be the guess the module exists to
    refuse."""
    _have_gh(monkeypatch)
    answer = repo.merge_state(pr=103, root=ROOT, runner=_runner({
        "remote -v": GH_REMOTE,
        "pr view": (0, '{"state": "DRAFTISH", "url": "u"}', ""),
    }))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                   repo.PROBE_FAILED)


def test_commit_ancestry_proves_merged_with_no_gh_and_no_network(monkeypatch):
    """The reason the probes are ordered by availability rather than by
    authority alone: a machine without `gh` still gets a real answer."""
    _have_gh(monkeypatch, False)
    answer = repo.merge_state(commits=[SHA], root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (0, "abc\n", ""),
        "merge-base --is-ancestor": (0, "", ""),
    }))
    assert answer["state"] == repo.MERGED
    assert "origin/HEAD" in answer["evidence"]


def test_a_commit_outside_the_integration_ref_is_open(monkeypatch):
    _have_gh(monkeypatch, False)
    answer = repo.merge_state(commits=[SHA], root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (0, "abc\n", ""),
        "merge-base --is-ancestor": (1, "", ""),
    }))
    assert answer["state"] == repo.OPEN


def test_an_unknown_commit_is_probe_failed_never_open(monkeypatch):
    """git exits 1 for "not an ancestor" and 128 for "no such object". Reading
    both as "not merged" would turn a bad sha into a claim about the code —
    the exact false negative that would pin a finished card open forever."""
    _have_gh(monkeypatch, False)
    answer = repo.merge_state(commits=["deadbeef"], root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (0, "abc\n", ""),
        "merge-base --is-ancestor": (128, "", "fatal: Not a valid object name\n"),
    }))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.PROBE_FAILED)
    assert "Not a valid object name" in answer["detail"]


def test_the_integration_ref_falls_back_past_an_unset_origin_head(monkeypatch):
    _have_gh(monkeypatch, False)
    log = []
    answer = repo.merge_state(commits=[SHA], root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (1, "", ""),
        "rev-parse --verify --quiet origin/master": (0, "abc\n", ""),
        "merge-base --is-ancestor": (0, "", ""),
    }, log))
    assert answer["state"] == repo.MERGED
    assert "origin/master" in answer["evidence"]
    assert any("origin/HEAD" in line for line in log)   # tried it first


# --- the branch, which can prove merged and little else -----------------------

def test_a_branch_alone_without_gh_is_unknown_not_open(monkeypatch):
    """A squash merge deletes the branch and rewrites its commits, so "not an
    ancestor" does NOT mean "not merged". Without `gh` the honest answer is
    unknown, and the reason names the tool that would have settled it."""
    _have_gh(monkeypatch, False)
    answer = repo.merge_state(branch="feature/x", root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (0, "abc\n", ""),
        "merge-base --is-ancestor": (1, "", ""),
    }))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN, repo.NO_GH_CLI)


def test_a_branch_in_the_integration_ref_is_merged_without_asking_gh(monkeypatch):
    _have_gh(monkeypatch, False)
    answer = repo.merge_state(branch="feature/x", root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (0, "abc\n", ""),
        "merge-base --is-ancestor": (0, "", ""),
    }))
    assert answer["state"] == repo.MERGED


def test_a_branch_with_no_pr_and_no_ancestry_is_open(monkeypatch):
    _have_gh(monkeypatch)
    answer = repo.merge_state(branch="feature/x", root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (0, "abc\n", ""),
        "merge-base --is-ancestor": (1, "", ""),
        "remote -v": GH_REMOTE,
        "pr list": (0, "[]", ""),
    }))
    assert answer["state"] == repo.OPEN
    assert "no PR exists" in answer["evidence"]


def test_a_merged_pr_for_the_branch_outranks_an_open_one(monkeypatch):
    """Branches get reused. One merged PR is proof the work landed even when a
    later PR on the same head is still open."""
    _have_gh(monkeypatch)
    answer = repo.merge_state(branch="feature/x", root=ROOT, runner=_runner({
        "rev-parse --verify --quiet origin/HEAD": (0, "abc\n", ""),
        "merge-base --is-ancestor": (1, "", ""),
        "remote -v": GH_REMOTE,
        "pr list": (0, '[{"number": 1, "state": "OPEN", "url": "u1"}, '
                       '{"number": 2, "state": "MERGED", "url": "u2"}]', ""),
    }))
    assert (answer["state"], answer["evidence"]) == (repo.MERGED,
                                                     "u2 is merged")


# --- probe ordering and reason precedence ------------------------------------

def test_the_first_definite_answer_wins_over_a_later_unknown(monkeypatch):
    """PR first, commits second: a merged PR settles it and the commit probe
    never runs, which is what makes a squash merge readable at all."""
    _have_gh(monkeypatch)
    log = []
    answer = repo.merge_state(pr=103, commits=[SHA], branch="feature/x",
                              root=ROOT, runner=_runner({
                                  "remote -v": GH_REMOTE,
                                  "pr view": (0, '{"state": "MERGED", '
                                                 '"url": "u"}', ""),
                              }, log))
    assert answer["state"] == repo.MERGED
    assert not any("merge-base" in line for line in log)


def test_the_most_actionable_reason_wins_when_nothing_can_answer(monkeypatch):
    """`gh` missing AND no integration ref: the PR probe says no_gh_cli, the
    commit probe says probe_failed. The user is told about the failure that
    actually happened, not the tool they might install."""
    _have_gh(monkeypatch, False)
    answer = repo.merge_state(pr=103, commits=[SHA], root=ROOT, runner=_runner({
        "rev-parse --verify --quiet": (1, "", ""),
    }))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.PROBE_FAILED)
    assert "no integration ref" in answer["detail"]


# --- the runner itself --------------------------------------------------------

def test_the_default_runner_turns_a_missing_executable_into_a_return_code():
    """The one place a child process can raise, and it does not: every failure
    arrives as (non-zero, "", text) so no probe needs a try/except."""
    rc, out, err = repo._default_runner(
        ["mindhive-no-such-executable-42"], None, 5)
    assert rc != 0 and out == "" and err


def test_merge_state_tolerates_junk_inputs_without_raising():
    """Callers pass whatever a historical ledger record held."""
    for pr in (None, "", "not-a-number", True, -1):
        answer = repo.merge_state(pr=pr, root=ROOT, runner=_runner({}))
        assert answer["state"] == repo.UNKNOWN
    answer = repo.merge_state(commits=[None, "  ", ""], branch="   ", root=ROOT,
                              runner=_runner({}))
    assert answer["reason"] == repo.NO_WORK_LINK
