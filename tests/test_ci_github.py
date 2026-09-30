# SPDX-License-Identifier: Apache-2.0
"""`mindhive.ci.github` -- every `gh` call the pre-merge CI makes.

The merge is the one irreversible call, so its argv is pinned whole: a merge
commit pinned to the tested head, and not one flag more. Statuses never raise:
a PR that could not be annotated still has a verdict.
"""
import json

import pytest

from mindhive.ci import github

HEAD = "3" * 40
PR = {"number": 151, "state": "OPEN", "isDraft": False,
      "headRefName": "claude/x", "headRefOid": HEAD, "baseRefName": "master",
      "title": "t", "url": "https://example.invalid/pull/151"}
SLUG = ("gh", "repo", "view")


class Fake:
    """Answers `gh` by argv prefix; records every call and where it ran."""

    def __init__(self, *rules):
        self.rules = list(rules)
        self.calls = []

    def __call__(self, argv, cwd=None, env=None, timeout_s=None, stdin=None, log=None):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, "cwd": cwd})
        for prefix, answer in self.rules:
            if tuple(argv[:len(prefix)]) == prefix:
                return answer
        return 1, "", f"not scripted: {argv[:3]}"


def _fields(argv) -> dict:
    return dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-f")


def test_the_merge_is_a_merge_commit_pinned_to_the_tested_head_and_nothing_more():
    fake = Fake((("gh", "pr", "merge"), (0, "Merged", "")))
    assert github.GitHub("R", run=fake).merge(151, HEAD) == (0, "Merged")
    assert [c["argv"] for c in fake.calls] == [
        ["gh", "pr", "merge", "151", "--merge", "--match-head-commit", HEAD]]


def test_a_status_goes_to_the_statuses_api_clipped_to_what_github_takes():
    fake = Fake((SLUG, (0, "owner/repo\n", "")), (("gh", "api"), (0, "{}", "")))
    gh = github.GitHub("R", run=fake)
    assert gh.post_status("a" * 40, "success", "mindhive-ci/windows-py3.12",
                          "x" * 300) == ""
    api = fake.calls[-1]["argv"]
    assert api[:5] == ["gh", "api", "-X", "POST",
                       "repos/owner/repo/statuses/" + "a" * 40]
    fields = _fields(api)
    assert fields["state"] == "success"
    assert fields["context"] == "mindhive-ci/windows-py3.12"
    assert len(fields["description"]) == github.DESCRIPTION_MAX
    assert fields["description"].endswith("...")


def test_the_repository_slug_is_asked_once():
    fake = Fake((SLUG, (0, "owner/repo", "")), (("gh", "api"), (0, "{}", "")))
    gh = github.GitHub("R", run=fake)
    gh.post_status("a" * 40, "pending", "mindhive-ci", "one")
    gh.post_status("a" * 40, "success", "mindhive-ci", "two")
    assert sum(c["argv"][:3] == list(SLUG) for c in fake.calls) == 1


def test_a_status_that_fails_is_a_reason_never_a_raise():
    fake = Fake((SLUG, (0, "owner/repo", "")),
                (("gh", "api"), (1, "", "HTTP 403: Resource not accessible")))
    assert "403" in github.GitHub("R", run=fake).post_status(
        "a" * 40, "failure", "mindhive-ci", "red")
    fake = Fake((SLUG, (1, "", "gh: To get started with GitHub CLI, please run: gh auth login")))
    assert "gh auth login" in github.GitHub("R", run=fake).post_status(
        "a" * 40, "pending", "mindhive-ci", "")


def test_an_unknown_state_is_refused_before_anything_is_asked():
    fake = Fake()
    assert "not a commit status state" in github.GitHub("R", run=fake).post_status(
        "a" * 40, "neutral", "mindhive-ci", "")
    assert fake.calls == []


def test_pr_reads_its_fields_and_open_prs_come_back_in_number_order():
    fake = Fake((("gh", "pr", "view"), (0, json.dumps(PR), "")),
                (("gh", "pr", "list"),
                 (0, json.dumps([{**PR, "number": 9}, {**PR, "number": 3}]), "")))
    gh = github.GitHub("R", run=fake)
    assert gh.pr(151)["headRefOid"] == HEAD
    assert fake.calls[0]["argv"] == ["gh", "pr", "view", "151", "--json",
                                     github.PR_FIELDS]
    assert [p["number"] for p in gh.open_prs()] == [3, 9]
    assert fake.calls[1]["argv"] == ["gh", "pr", "list", "--state", "open",
                                     "--limit", "100", "--json", github.PR_FIELDS]


def test_a_read_that_fails_raises_with_ghs_own_words():
    fake = Fake((("gh", "pr", "view"),
                 (1, "", "GraphQL: Could not resolve to a PullRequest with the number of 999.")))
    with pytest.raises(github.GitHubError, match="Could not resolve"):
        github.GitHub("R", run=fake).pr(999)
    fake = Fake((("gh", "pr", "view"), (0, "not json", "")))
    with pytest.raises(github.GitHubError, match="not JSON"):
        github.GitHub("R", run=fake).pr(1)


def test_merged_reads_the_state_and_the_merge_commit():
    fake = Fake((("gh", "pr", "view"),
                 (0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": "m" * 40}}), "")))
    assert github.GitHub("R", run=fake).merged(151) == ("MERGED", "m" * 40)
    assert fake.calls[0]["argv"] == ["gh", "pr", "view", "151", "--json",
                                     "state,mergeCommit"]
    fake = Fake((("gh", "pr", "view"),
                 (0, json.dumps({"state": "OPEN", "mergeCommit": None}), "")))
    assert github.GitHub("R", run=fake).merged(151) == ("OPEN", "")


def test_ready_says_why_not():
    assert github.GitHub("R", run=Fake((("gh", "auth", "status"),
                                        (0, "", "Logged in")))).ready() == ""
    why = github.GitHub("R", run=Fake((("gh", "auth", "status"),
                                       (1, "", "You are not logged into any GitHub hosts.")))).ready()
    assert "not logged into" in why


def test_every_call_runs_in_the_repository():
    fake = Fake((SLUG, (0, "owner/repo", "")), (("gh", "api"), (0, "{}", "")),
                (("gh", "pr", "view"), (0, json.dumps(PR), "")))
    gh = github.GitHub("C:/repo", run=fake)
    gh.pr(1)
    gh.post_status("a" * 40, "pending", "mindhive-ci", "")
    assert {c["cwd"] for c in fake.calls} == {"C:/repo"}
