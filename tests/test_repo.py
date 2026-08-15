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


# --- staleness: how far this checkout has drifted -----------------------------
#
# Same seam, same bargain. The pairing under test throughout: a commit count is
# only as good as the snapshot it was measured against, so the count NEVER ships
# without the age of that snapshot.

NOW = 1_700_000_000.0
REF_OK = {"rev-parse --verify --quiet origin/HEAD": (0, "sha\n", "")}


def _stale_runner(behind=0, ahead=0, git_dir=None, extra=None):
    responses = dict(REF_OK)
    responses["rev-list --left-right --count"] = (0, f"{ahead}\t{behind}\n", "")
    responses["rev-parse --git-common-dir"] = ((0, str(git_dir), "") if git_dir
                                              else (128, "", "no git dir"))
    responses.update(extra or {})
    return _runner(responses)


def test_a_checkout_behind_its_ref_says_so_with_both_counts():
    answer = repo.staleness(root=ROOT, runner=_stale_runner(behind=64, ahead=3),
                            now=NOW)
    assert answer["state"] == repo.BEHIND
    assert (answer["behind"], answer["ahead"]) == (64, 3)
    assert answer["ref"] == "origin/HEAD"


def test_a_current_checkout_is_current_even_when_ahead():
    """Ahead is normal mid-branch; only BEHIND means the base moved on."""
    answer = repo.staleness(root=ROOT, runner=_stale_runner(behind=0, ahead=9),
                            now=NOW)
    assert answer["state"] == repo.CURRENT and answer["behind"] == 0


def test_an_unreadable_fetch_age_counts_as_stale_not_as_fresh():
    """DISCRIMINATING: "0 behind" against a snapshot of unknown age is the
    misleading answer this pairing exists to prevent. An unknown age is not
    evidence of a recent one."""
    answer = repo.staleness(root=ROOT, runner=_stale_runner(), now=NOW)
    assert answer["fetch_age_hours"] is None
    assert answer["fetch_stale"] is True


def test_a_recent_fetch_is_not_stale(tmp_path):
    marker = tmp_path / "FETCH_HEAD"
    marker.write_text("x", encoding="utf-8")
    import os
    os.utime(marker, (NOW - 3600, NOW - 3600))          # one hour old
    answer = repo.staleness(root=ROOT, now=NOW,
                            runner=_stale_runner(git_dir=tmp_path))
    assert 0.9 < answer["fetch_age_hours"] < 1.1
    assert answer["fetch_stale"] is False


def test_the_freshness_window_is_the_caller_s_to_set(tmp_path):
    marker = tmp_path / "FETCH_HEAD"
    marker.write_text("x", encoding="utf-8")
    import os
    os.utime(marker, (NOW - 7200, NOW - 7200))          # two hours old
    runner = _stale_runner(git_dir=tmp_path)
    assert repo.staleness(root=ROOT, runner=runner, now=NOW,
                          max_age_hours=1.0)["fetch_stale"] is True
    assert repo.staleness(root=ROOT, runner=runner, now=NOW,
                          max_age_hours=8.0)["fetch_stale"] is False


def test_no_integration_ref_is_unknown_with_its_own_reason():
    """Nothing to compare against — no `origin/HEAD`, `origin/master` or
    `origin/main`, which `git clone --branch` and `git worktree add` both leave
    absent. Reporting "current" there would be an invention. (This is NOT the
    same condition as a branch with no upstream; see the tracking tests below,
    which exist because the two were once conflated.)"""
    answer = repo.staleness(root=ROOT, runner=_runner({}), now=NOW)
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.NO_INTEGRATION_REF)
    assert "origin/HEAD" in answer["detail"]            # says what it tried


def test_outside_a_checkout_is_unknown_not_current(monkeypatch):
    monkeypatch.setattr(repo, "worktree_root", lambda: None)
    answer = repo.staleness(runner=_runner({}), now=NOW)
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.NO_CHECKOUT)


def test_a_failed_rev_list_is_unknown_not_zero():
    answer = repo.staleness(root=ROOT, now=NOW, runner=_runner(dict(
        REF_OK, **{"rev-list": (128, "", "fatal: bad revision")})))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.PROBE_FAILED)
    assert "bad revision" in answer["detail"]


def test_unparseable_counts_are_unknown_not_guessed():
    answer = repo.staleness(root=ROOT, now=NOW, runner=_runner(dict(
        REF_OK, **{"rev-list": (0, "banana\n", "")})))
    assert (answer["state"], answer["reason"]) == (repo.UNKNOWN,
                                                  repo.PROBE_FAILED)


def test_staleness_never_fetches():
    """A probe that reached the network could not run on the prompt path, in a
    hook, or on a plane — and this one is meant to run in all three."""
    log = []
    repo.staleness(root=ROOT, now=NOW,
                   runner=_runner(dict(REF_OK, **{
                       "rev-list": (0, "0\t0\n", ""),
                       "rev-parse --git-common-dir": (128, "", "")}), log=log))
    assert log, "the probe ran no git commands at all"
    assert not any("fetch" in cmd or "pull" in cmd or "ls-remote" in cmd
                   for cmd in log), log


def test_cli_repo_stale_reports_and_does_not_gate(capsys):
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["repo", "stale"]) == 0
    out = capsys.readouterr().out
    assert "checkout staleness:" in out
    assert "fetched" in out or "never fetched" in out


# --- staleness: can this checkout even pull? ----------------------------------
#
# The second pairing, and the one this repo learned the hard way. `hive repo
# stale` reported `current — 0 behind / 0 ahead of origin/HEAD` on a `master`
# where `git pull` was failing outright with "There is no tracking information
# for the current branch". Both halves were true and the pair was misleading:
# the counts are measured against the ref a MERGE lands on, which resolves on
# any clone, while pulling needs a ref THIS BRANCH tracks. So the probe reported
# no drift for a checkout that had no mechanism to notice drift at all — the
# 64-commit drift in the comment at the top of this section is what that
# produces if left alone.

def _tracking(branch="master", upstream="origin/master"):
    """Stubs for the two tracking questions. An empty *upstream* is git's own
    failure for a branch that tracks nothing."""
    return {"rev-parse --abbrev-ref HEAD": (0, f"{branch}\n", ""),
            "@{upstream}": ((0, f"{upstream}\n", "") if upstream
                            else (128, "", "fatal: no upstream configured"))}


def test_the_integration_ref_is_not_read_as_an_upstream():
    """DISCRIMINATING, and the exact live defect: `origin/HEAD` resolves, the
    counts come back zero, and the branch still tracks nothing. Both facts
    ship."""
    answer = repo.staleness(root=ROOT, now=NOW, runner=_stale_runner(
        extra=_tracking(upstream="")))
    assert answer["state"] == repo.CURRENT       # the drift answer is unchanged
    assert answer["ref"] == "origin/HEAD"        # measured against the merge ref
    assert answer["upstream"] == ""              # …which is not what it pulls
    assert answer["upstream_missing"] is True


def test_a_tracked_branch_reports_the_ref_it_actually_pulls():
    answer = repo.staleness(root=ROOT, now=NOW,
                            runner=_stale_runner(extra=_tracking()))
    assert answer["upstream"] == "origin/master"
    assert answer["upstream_missing"] is False
    assert answer["branch"] == "master"


def test_a_detached_head_reports_no_branch_rather_than_git_s_literal_HEAD():
    """`git rev-parse --abbrev-ref HEAD` answers the string "HEAD" when
    detached. Passing that through would name a branch that does not exist and
    invite a set-upstream command that cannot work."""
    answer = repo.staleness(root=ROOT, now=NOW, runner=_stale_runner(
        extra=_tracking(branch="HEAD", upstream="")))
    assert answer["branch"] == ""
    assert answer["upstream_missing"] is True


def test_the_tracking_question_is_asked_even_when_the_drift_one_cannot_be():
    """An unknown state is where a broken pull is MOST likely, so the probe
    still asks. `git pull` being broken is the actionable half."""
    answer = repo.staleness(root=ROOT, now=NOW,
                            runner=_runner(_tracking(upstream="")))
    assert answer["reason"] == repo.NO_INTEGRATION_REF
    assert (answer["branch"], answer["upstream_missing"]) == ("master", True)


def test_an_unreadable_tracking_probe_is_missing_not_present():
    """Same rule as the fetch age: "we could not ask" is not evidence that
    `git pull` works."""
    answer = repo.staleness(root=ROOT, now=NOW, runner=_stale_runner())
    assert answer["upstream_missing"] is True and answer["upstream"] == ""


def test_tracking_answers_on_its_own_without_running_the_drift_probe():
    """It is split out to be cheap: two git calls against staleness's five, so
    the orient path can afford to ask every session."""
    log = []
    answer = repo.tracking(root=ROOT, runner=_runner(_tracking(), log=log))
    assert answer == {"branch": "master", "upstream": "origin/master",
                      "upstream_missing": False}
    assert not any("rev-list" in cmd for cmd in log), log
    assert len(log) == 2, log


def test_tracking_outside_a_checkout_is_missing_not_present(monkeypatch):
    monkeypatch.setattr(repo, "worktree_root", lambda: None)
    answer = repo.tracking(runner=_runner({}))
    assert answer["upstream_missing"] is True and answer["branch"] == ""


def test_cli_repo_stale_names_a_missing_upstream_and_how_to_fix_it(capsys,
                                                                   monkeypatch):
    monkeypatch.setattr(repo, "staleness", lambda **kw: repo._stale_answer(
        repo.CURRENT, ref="origin/HEAD", branch="master", upstream="",
        fetch_age_hours=0.1))
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["repo", "stale"]) == 0
    out = capsys.readouterr().out
    assert "current — 0 behind" in out           # the old answer still shows
    assert "no upstream" in out                  # …and no longer stands alone
    assert "--set-upstream-to=origin/master master" in out


def test_cli_repo_stale_does_not_add_an_upstream_line_outside_a_checkout(
        capsys, monkeypatch):
    """`no_checkout` already said it. A second line about tracking refs would
    bury the reason the user can act on."""
    monkeypatch.setattr(repo, "staleness", lambda **kw: repo._stale_answer(
        repo.UNKNOWN, reason=repo.NO_CHECKOUT, detail="not inside a git checkout"))
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["repo", "stale"]) == 0
    out = capsys.readouterr().out
    assert "no_checkout" in out and "no upstream" not in out
