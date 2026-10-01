# SPDX-License-Identifier: Apache-2.0
"""The gateway interceptor (blueprint P1f; MEB ORCH-02).

Two hook stages, one fact. `pre_tool_use` sees the INTENT -- a `gh pr merge` or
a `git merge` about to run -- and writes it to a per-session sidecar.
`post_tool_use` sees the RESULT, reads that sidecar back, asks git and `gh` what
actually happened, and appends one `interceptor_signal` naming the actor
(`claude_code`, `ci_bot` or `human_manual`). Nothing else in the system can
answer "who landed this", because the tool result says only that a command ran.

Three properties are load-bearing and each has a test that fails without it:

* **It never blocks.** `allowed` is always True. This is an OBSERVER on the
  hook path; a bug in it must never stop a person's `git merge`.
* **It never writes to stdout.** `cmd_hook`'s stdout IS the hook protocol, so
  the interceptor's entire output is the ledger and its sidecar.
* **It is fail-open end to end.** Unparseable payload, missing `gh`, an
  exploded probe: the hook returns and the tool call proceeds.
"""
import json
import time

import pytest

from mindhive import events, paths
from mindhive.orchestrator import gateway as gw
from mindhive.orchestrator import interceptor as itc


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MINDHIVE_EVENT_BACKEND", raising=False)
    monkeypatch.delenv("MINDHIVE_INTERCEPTOR", raising=False)
    return tmp_path


SESSION = "39151937-fd17-48c0-bb12-fd5e596b1c1f"


def _pre(command, session=SESSION, tool="Bash", **extra):
    payload = {"session_id": session, "tool_name": tool,
               "tool_input": {"command": command}}
    payload.update(extra)
    return json.dumps(payload)


def _post(command, session=SESSION, tool="Bash", stdout="", stderr="", **extra):
    payload = {"session_id": session, "tool_name": tool,
               "tool_input": {"command": command},
               "tool_response": {"stdout": stdout, "stderr": stderr,
                                 "interrupted": False}}
    payload.update(extra)
    return json.dumps(payload)


def _signals(project=None):
    return [e for e in events.read_all(project) if e.type == "interceptor_signal"]


def _intent_path(session=SESSION, project=None):
    safe = events.segment_name(session)[:-6]
    return paths.state_dir(project) / f"interceptor_intent_{safe}.json"


class _Runner:
    """A fake `(argv, cwd, timeout) -> (rc, out, err)` that records its calls."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, cwd=None, timeout_s=None):
        self.calls.append(list(argv))
        for key, value in self.answers.items():
            if key in " ".join(argv):
                return value
        return 1, "", "unexpected argv"


# A real object name. `worktree._is_oid` takes 40 or 64 HEX characters, so a
# placeholder spelled `tree0000...` (38 characters, and `t` is not hex) makes
# the real probe answer `clean=None` -- "could not tell" -- not `clean=False`.
EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

MERGED_PR = json.dumps({"state": "MERGED", "mergedAt": "2026-09-20T10:00:00Z",
                        "mergedBy": {"login": "claude-code[bot]"},
                        "author": {"login": "hanif"},
                        "url": "https://github.com/o/r/pull/7"})


# --- the verdict is an observation, never a decision ------------------------------

def test_the_verdict_is_always_allowed(store):
    """The single most important property. An interceptor on the pre_tool_use
    path that can answer False is one bug away from blocking the operator's own
    `git merge`."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    for raw in (_pre("gh pr merge 7 --squash"), _pre("git merge lane"),
                _pre("rm -rf /"), "not json at all", "", "{}"):
        assert g.observe_intent(raw).allowed is True
    for raw in (_post("gh pr merge 7"), "garbage", ""):
        assert g.observe_result(raw).allowed is True


def test_every_verdict_is_the_dataclass(store):
    g = itc.GatewayInterceptor(repo_root=str(store))
    v = g.observe_intent(_pre("gh pr merge 7"))
    assert isinstance(v, itc.InterceptorVerdict)
    assert v.action_type == "pr_merge" and isinstance(v.reason, str)


def test_the_env_switch_turns_it_into_a_no_op(store, monkeypatch):
    monkeypatch.setenv("MINDHIVE_INTERCEPTOR", "0")
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g.observe_intent(_pre("gh pr merge 7")).allowed is True
    assert not _intent_path().exists()
    assert g.observe_result(_post("gh pr merge 7")).allowed is True
    assert _signals() == []


# --- what it recognises ------------------------------------------------------------

@pytest.mark.parametrize("command,action", [
    ("gh pr merge 7 --squash", "pr_merge"),
    ("gh  pr  merge  7", "pr_merge"),
    ("git merge lint/m/0", "git_merge"),
    ("git merge --no-ff lane", "git_merge"),
    ("gh pr close 7", "pr_close"),
    ("gh pr create --fill", "pr_create"),
])
def test_it_recognises_the_four_action_types(store, command, action):
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g.observe_intent(_pre(command)).action_type == action
    assert json.loads(_intent_path().read_text(encoding="utf-8"))["action_type"] == action


@pytest.mark.parametrize("command", [
    "echo gh pr merge",            # a mention, not a run
    "git merged-files",            # `merge` is not a whole word here
    # The three read-only plumbing commands whose names START with `merge`.
    # `\b` succeeds between `e` and `-`, so a word-boundary anchor matches all
    # three -- and `_branch_of` would then hand `_is_ancestor` the last token,
    # `HEAD`, which is trivially its own ancestor. The interceptor would append
    # `merged: true` to an append-only ledger for a question that merged
    # nothing.
    "git merge-base --is-ancestor lint/m/0 HEAD",
    "git merge-tree --write-tree HEAD lint/m/0",
    "git merge-file a.txt base.txt b.txt",
    "ls",
    "pytest -q",
    "git commit -m 'gh pr merge'",
    "grep -rn 'git merge' src/",
])
def test_it_ignores_a_command_that_only_mentions_one(store, command):
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g.observe_intent(_pre(command)).action_type == ""
    assert not _intent_path().exists()


def test_only_the_bash_tool_is_watched(store):
    """DISCRIMINATING. `Edit` on a file whose text contains `gh pr merge` is
    not a merge."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    raw = json.dumps({"session_id": SESSION, "tool_name": "Edit",
                      "tool_input": {"new_string": "gh pr merge 7"}})
    assert g.observe_intent(raw).action_type == ""
    assert not _intent_path().exists()


def test_the_pr_number_is_carried_to_the_result(store):
    g = itc.GatewayInterceptor(repo_root=str(store))
    g.observe_intent(_pre("gh pr merge 7 --squash"))
    assert json.loads(_intent_path().read_text(encoding="utf-8"))["pr_number"] == 7


# --- the intent sidecar ------------------------------------------------------------

def test_the_intent_lands_where_the_plan_says_and_survives_a_hostile_id(store):
    """The file name goes through `events.segment_name`, so a session id with
    a traversal in it cannot escape the state directory."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    g.observe_intent(_pre("gh pr merge 7", session="../../etc/passwd"))
    hits = list(paths.state_dir(None).glob("interceptor_intent_*.json"))
    assert len(hits) == 1
    assert hits[0].parent == paths.state_dir(None)
    assert ".." not in hits[0].name


def test_the_intent_write_is_atomic(store, monkeypatch):
    seen = []
    real = paths.write_text_atomic
    monkeypatch.setattr(paths, "write_text_atomic",
                        lambda p, t: (seen.append(str(p)), real(p, t))[1])
    itc.GatewayInterceptor(repo_root=str(store)).observe_intent(
        _pre("gh pr merge 7"))
    assert any("interceptor_intent_" in s for s in seen)


def test_no_session_id_means_no_intent_and_no_probe(store):
    """Without a session there is nowhere to put the intent and no way to pair
    it with a result, so the interceptor does nothing at all."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    raw = json.dumps({"tool_name": "Bash",
                      "tool_input": {"command": "gh pr merge 7"}})
    assert g.observe_intent(raw).allowed is True
    assert not list(paths.state_dir(None).glob("interceptor_intent_*.json"))
    runner = _Runner({})
    g2 = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g2.observe_result(raw)
    assert runner.calls == [] and _signals() == []


def test_two_sessions_keep_separate_intents(store):
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    g = itc.GatewayInterceptor(repo_root=str(store))
    g.observe_intent(_pre("gh pr merge 7"))
    g.observe_intent(_pre("gh pr merge 9", session=other))
    assert json.loads(_intent_path().read_text(encoding="utf-8"))["pr_number"] == 7
    assert json.loads(_intent_path(other).read_text(encoding="utf-8"))["pr_number"] == 9


def test_a_stale_intent_is_ignored_and_cleared(store):
    """Ten minutes. A pre_tool_use whose post never arrived (the session died,
    the tool hung) must not attach itself to an unrelated later command."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    g.observe_intent(_pre("gh pr merge 7"))
    stale = json.loads(_intent_path().read_text(encoding="utf-8"))
    stale["ts"] = time.time() - 601
    _intent_path().write_text(json.dumps(stale), encoding="utf-8")
    runner = _Runner({"gh": (0, MERGED_PR, "")})
    g2 = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    v = g2.observe_result(_post("gh pr merge 7"))
    assert runner.calls == []
    assert _signals() == []
    assert v.reason and not _intent_path().exists()


def test_an_intent_just_inside_the_window_is_honoured(store):
    """DISCRIMINATING boundary: 599 s is live, 601 s is stale."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    g.observe_intent(_pre("gh pr merge 7"))
    rec = json.loads(_intent_path().read_text(encoding="utf-8"))
    rec["ts"] = time.time() - 599
    _intent_path().write_text(json.dumps(rec), encoding="utf-8")
    runner = _Runner({"gh": (0, MERGED_PR, ""), "merge-base": (0, "", "")})
    itc.GatewayInterceptor(repo_root=str(store),
                           runner=runner).observe_result(_post("gh pr merge 7"))
    assert _signals()


def test_a_result_with_no_intent_probes_nothing(store):
    """The pairing is what makes the probe cheap: a post_tool_use for any of
    the thousands of other tool calls must not shell out to `gh`."""
    runner = _Runner({"gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_result(_post("pytest -q"))
    assert runner.calls == [] and _signals() == []


# --- the probe ---------------------------------------------------------------------

def test_a_merged_pr_emits_one_signal_naming_the_actor(store):
    runner = _Runner({"merge-base": (0, "", ""), "gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge 7 --squash"))
    v = g.observe_result(_post("gh pr merge 7 --squash"))
    (sig,) = _signals()
    assert sig.payload["merged"] is True
    assert sig.payload["merge_actor"] == "claude_code"
    assert sig.payload["exit_code"] == 0
    assert sig.payload["pr_number"] == 7
    # By value, on both surfaces. `in ("", "MERGED")` accepted the field being
    # blanked everywhere, which would leave every recorded signal unable to
    # answer "was this merge against a CLOSED PR".
    assert v.merge_actor == "claude_code" and v.ci_status == "MERGED"
    assert sig.payload["ci_status"] == "MERGED"


@pytest.mark.parametrize("login,actor", [
    ("claude-code[bot]", "claude_code"),
    ("Claude", "claude_code"),
    ("claude_code", "claude_code"),
    ("some-ci[bot]", "ci_bot"),
    ("dependabot[bot]", "ci_bot"),
    ("ci-bot", "ci_bot"),
    ("hanif", "human_manual"),
    # `gh` answers `mergedBy: null` for any PR it will not name the merger of
    # -- a PR merged before the field existed, a token without the scope, a
    # merge queue. "" is the login that arrives then, and "human_manual" is a
    # claim about a PERSON that nothing observed. MERGE_ACTORS carries
    # "unknown" for exactly this.
    ("", "unknown"),
    # Substring matching reads these five as bots or as claude. They are
    # ordinary surnames and logins.
    ("abbott", "human_manual"),
    ("talbot", "human_manual"),
    ("botond", "human_manual"),
    ("claudel", "human_manual"),
    ("mclaude", "human_manual"),
])
def test_the_actor_rule_prefers_claude_then_bot_then_human(store, login, actor):
    """Order matters: `claude-code[bot]` is BOTH a bot and claude, and it must
    read as claude -- the whole point is telling the agent's merges from
    everyone else's."""
    record = json.dumps({"state": "MERGED", "mergedAt": "2026-09-20T10:00:00Z",
                         "mergedBy": {"login": login} if login else None,
                         "author": {"login": "x"}, "url": "u"})
    runner = _Runner({"merge-base": (1, "", ""), "gh": (0, record, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge 7"))
    g.observe_result(_post("gh pr merge 7"))
    assert _signals()[0].payload["merge_actor"] == actor


def test_git_ancestry_alone_can_prove_a_merge(store):
    """`gh` may be absent. `git merge-base --is-ancestor` still answers for a
    non-squash merge, and the signal says so with an unknown actor rather than
    claiming a human did it."""
    runner = _Runner({"merge-base": (0, "", ""), "gh": (1, "", "gh: not found")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("git merge lint/m/0"))
    g.observe_result(_post("git merge lint/m/0", stdout="Fast-forward"))
    (sig,) = _signals()
    assert sig.payload["merged"] is True
    # EXACTLY "unknown". The old disjunction `in ("", "unknown",
    # "human_manual")` accepted the one answer MERGE_ACTORS forbids here: git
    # ancestry proves a merge happened and says nothing whatever about who.
    assert sig.payload["merge_actor"] == "unknown"


def test_the_ancestry_probe_asks_git_the_question_in_the_right_order(store):
    """`git merge-base --is-ancestor <branch> HEAD` asks "is <branch> already
    contained in HEAD". Swap the operands and it asks the opposite -- and HEAD
    is an ancestor of any branch cut from it, so EVERY unmerged lane would be
    recorded as merged. The operand order IS the semantics, so it is pinned by
    value rather than by the fake's substring match."""
    runner = _Runner({"merge-base": (0, "", ""), "gh": (1, "", "gh: not found")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("git merge lint/m/0"))
    g.observe_result(_post("git merge lint/m/0", stdout="Fast-forward"))
    (probe,) = [c for c in runner.calls if "merge-base" in c]
    assert probe == ["git", "merge-base", "--is-ancestor", "lint/m/0", "HEAD"]


def test_the_pr_probe_asks_gh_about_the_number_the_command_named(store):
    """The other half of the same gap: the fake answers on a substring of the
    joined argv, so `gh pr view 0` would satisfy it. The number is pinned."""
    runner = _Runner({"merge-base": (1, "", ""), "gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge 7 --squash"))
    g.observe_result(_post("gh pr merge 7 --squash"))
    (ask,) = [c for c in runner.calls if c[:3] == ["gh", "pr", "view"]]
    assert ask[3] == "7"
    assert "mergedBy" in ask[-1]


# --- one budget for the whole probe -------------------------------------------------

class _Ticking(_Runner):
    """A `_Runner` whose every call costs *cost* seconds on a fake clock and
    records the timeout it was handed -- the two things a budget is about."""

    def __init__(self, answers, cost):
        super().__init__(answers)
        self.now, self.cost, self.timeouts = 0.0, cost, []

    def __call__(self, argv, cwd=None, timeout_s=None):
        self.timeouts.append(timeout_s)
        self.now += self.cost
        return super().__call__(argv, cwd, timeout_s)


def _budgeted(store, runner, budget, timeout=20.0):
    return itc.GatewayInterceptor(repo_root=str(store), runner=runner,
                                  timeout_s=timeout, probe_budget_s=budget,
                                  monotonic=lambda: runner.now)


def test_the_probe_runs_under_one_budget_not_one_timeout_per_call(store):
    """A `git merge` is probed with up to two subprocesses, each bounded by
    `timeout_s`, so two 20 s timeouts stacked into 40 s inside a hook whose
    own runner allows 60. One deadline for the whole probe: a call gets no
    more than what is left of the budget, and a call nothing is left for is
    not made -- and is named in the reason, so a skipped question is never
    read as an answered one."""
    runner = _Ticking({"merge-base": (0, "", ""),
                       "rev-parse": (0, EMPTY_TREE_OID, "")}, cost=20.0)
    g = _budgeted(store, runner, budget=15.0)
    g.observe_intent(_pre("git merge lint/m/0"))
    v = g.observe_result(_post("git merge lint/m/0", stdout="Fast-forward"))
    assert runner.timeouts == [15.0]                 # the budget, not timeout_s
    assert [c[1] for c in runner.calls] == ["merge-base"]
    (sig,) = _signals()
    assert sig.payload["merged"] is True             # what arrived in time counts
    assert sig.payload["lane_commit_sha"] == ""      # rev-parse was not asked
    assert "budget" in sig.payload["reason"] and "budget" in v.reason


def test_a_budget_the_probe_can_afford_changes_nothing(store):
    """The canary for the test above: with room to spare every call is made
    with the full per-call timeout and the reason never mentions a budget."""
    runner = _Ticking({"merge-base": (0, "", ""),
                       "rev-parse": (0, EMPTY_TREE_OID, "")}, cost=1.0)
    g = _budgeted(store, runner, budget=30.0)
    g.observe_intent(_pre("git merge lint/m/0"))
    g.observe_result(_post("git merge lint/m/0", stdout="Fast-forward"))
    assert runner.timeouts == [20.0, 20.0]
    (sig,) = _signals()
    assert sig.payload["merged"] is True
    assert sig.payload["lane_commit_sha"] == EMPTY_TREE_OID
    assert "budget" not in sig.payload["reason"]


def test_the_second_git_call_gets_only_what_the_first_left(store):
    """The deadline is shared, not reset per call: after a 12 s merge-base
    under a 20 s budget, rev-parse is handed the 8 s that remain."""
    runner = _Ticking({"merge-base": (0, "", ""),
                       "rev-parse": (0, EMPTY_TREE_OID, "")}, cost=12.0)
    g = _budgeted(store, runner, budget=20.0)
    g.observe_intent(_pre("git merge lint/m/0"))
    g.observe_result(_post("git merge lint/m/0", stdout="Fast-forward"))
    assert runner.timeouts == [20.0, 8.0]


def test_a_spent_budget_asks_gh_nothing_and_claims_nothing(store):
    """A `pr_*` act with no budget left is recorded as unproven, the reason
    saying gh was not asked -- never as `merged: false` on gh's authority, and
    never as merged."""
    runner = _Ticking({"gh": (0, MERGED_PR, "")}, cost=0.0)
    g = _budgeted(store, runner, budget=0.0)
    g.observe_intent(_pre("gh pr merge 7 --squash"))
    g.observe_result(_post("gh pr merge 7 --squash"))
    assert runner.calls == []
    (sig,) = _signals()
    assert sig.payload["merged"] is False
    assert sig.payload["merge_actor"] == "unknown"
    assert "gh not asked" in sig.payload["reason"]


def test_the_default_budget_is_the_module_constant_and_fits_one_call():
    g = itc.GatewayInterceptor()
    assert g.probe_budget_s == itc.PROBE_BUDGET_S == 30.0
    assert itc.PROBE_BUDGET_S >= g.timeout_s


@pytest.mark.parametrize("command,branch", [
    ("git merge lint/m/0", "lint/m/0"),
    ("git merge --no-ff lane", "lane"),
    ("git merge -m 'msg' lane", "lane"),
    ("git merge", ""),
])
def test_the_branch_is_the_last_non_flag_token_after_the_subcommand(store,
                                                                   command,
                                                                   branch):
    """DISCRIMINATING. `command.split()[2:]` drops `git` and `merge`; the ref
    is the LAST non-flag token. Take `[1:]` instead and the ref becomes
    "merge"; take `refs[0]` and a `-m 'msg'` form yields "'msg'". Either way
    git is asked about a ref that does not exist, every `git merge` records as
    unmerged, and the interceptor's answer to "who landed this" is wrong for
    the whole non-squash path."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g._branch_of(command) == branch


def test_a_closed_pr_retires_the_plan_instead(store, monkeypatch):
    """A PR closed WITHOUT merging is abandoned work: the roadmap sweep is the
    thing that knows what to do with that, so the interceptor calls it rather
    than inventing a second retirement path."""
    called = []
    from mindhive import roadmap
    monkeypatch.setattr(roadmap, "retire_landed",
                        lambda **kw: called.append(kw) or [])
    record = json.dumps({"state": "CLOSED", "mergedAt": None,
                         "mergedBy": None, "author": {"login": "x"}, "url": "u"})
    runner = _Runner({"merge-base": (1, "", ""), "gh": (0, record, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr close 7"))
    g.observe_result(_post("gh pr close 7"))
    assert called, "a closed PR did not reach the roadmap sweep"
    assert _signals()[0].payload["merged"] is False


def test_a_merged_pr_does_not_retire_through_the_sweep(store, monkeypatch):
    """DISCRIMINATING. Only the CLOSED-without-merge branch sweeps."""
    called = []
    from mindhive import roadmap
    monkeypatch.setattr(roadmap, "retire_landed", lambda **kw: called.append(kw) or [])
    runner = _Runner({"merge-base": (0, "", ""), "gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge 7"))
    g.observe_result(_post("gh pr merge 7"))
    assert called == []


def test_a_failed_tool_is_not_probed_and_is_recorded_as_unmerged(store):
    """`gh pr merge` exiting non-zero merged nothing. Probing would cost a
    round trip to learn what the exit code already said."""
    runner = _Runner({"gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge 7"))
    raw = json.dumps({"session_id": SESSION, "tool_name": "Bash",
                      "tool_input": {"command": "gh pr merge 7"},
                      "tool_response": {"stdout": "", "stderr": "not mergeable",
                                        "interrupted": False}})
    v = g.observe_result(raw, failed=True)
    assert runner.calls == []
    (sig,) = _signals()
    assert sig.payload["merged"] is False
    assert sig.payload["exit_code"] != 0
    assert v.reason == "tool_failed"


def test_the_intent_is_deleted_once_the_result_is_recorded(store):
    """Exactly once: a second post_tool_use for the same command must not emit
    a second signal."""
    runner = _Runner({"merge-base": (0, "", ""), "gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge 7"))
    g.observe_result(_post("gh pr merge 7"))
    assert not _intent_path().exists()
    g.observe_result(_post("gh pr merge 7"))
    assert len(_signals()) == 1


def test_the_signal_lands_in_the_sessions_own_segment(store):
    runner = _Runner({"merge-base": (0, "", ""), "gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge 7"))
    g.observe_result(_post("gh pr merge 7"))
    assert _signals()[0].session_id == SESSION


# --- phase recognition -------------------------------------------------------------

def test_the_phase_comes_from_tool_metadata_first(store):
    g = itc.GatewayInterceptor(repo_root=str(store))
    raw = _pre("gh pr merge 7", phase="integration")
    assert g.observe_intent(raw).phase == "integration"


@pytest.mark.parametrize("stream,phase", [
    ("fase: P1f\nrest of the text", "P1f"),
    ("Phase: integration", "integration"),
    ("STATUS: red-green", "red-green"),
    ("step: 3a", "3a"),
    ("  fase: x", ""),                       # anchored at the line start
    ("nothing here", ""),
    ("fase: " + "x" * 200, ""),              # not a phase, a paragraph
])
def test_the_second_layer_reads_a_phase_line_from_the_stream(store, stream, phase):
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g._recognize_phase({}, stream) == phase


def test_the_phase_scan_is_bounded(store):
    """Only the first 500 characters: the stream is a tool's whole output and
    a linear scan of a 10 MB log on every hook call is not free."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g._recognize_phase({}, "x" * 600 + "\nfase: late") == ""


# --- the payload contract ----------------------------------------------------------

def test_the_payload_validator_refuses_what_it_should(store):
    good = {"tool_name": "Bash", "phase": "", "action_type": "pr_merge",
            "pr_number": 7, "lane_commit_sha": "", "merge_actor": "claude_code",
            "merged": True, "ci_status": "MERGED", "reason": "", "ts": 1.0}
    itc.validate_payload(good)
    for bad in ({}, {"tool_name": "Bash"}, dict(good, merged="yes"),
                dict(good, action_type="nonsense"),
                dict(good, merge_actor="me"), dict(good, pr_number=-1),
                "not a dict", None):
        with pytest.raises(ValueError):
            itc.validate_payload(bad)


def test_the_type_is_registered_the_way_the_data_model_requires(store):
    """DATA_MODEL section 6: a new type joins VALID_TYPES, the pg class map,
    and the payload contract registry -- three places, one change-set."""
    from mindhive import contracts, pgschema
    assert "interceptor_signal" in events.VALID_TYPES
    assert "interceptor_signal" in events.DETERMINISTIC_TYPES
    assert "interceptor_signal" not in events.SEMANTIC_TYPES
    assert pgschema.EVENT_TYPE_CLASSES["interceptor_signal"] == "deterministic"
    module, hint = contracts._PAYLOAD_CONTRACTS["interceptor_signal"]
    assert module == ".orchestrator.interceptor" and hint
    with pytest.raises(ValueError, match=r"\(use: "):
        contracts.validate_typed_payload("interceptor_signal", {})


# --- fail-open, end to end ---------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", "not json", "[]", '"a string"',
                                 '{"tool_input": null}', '{"tool_name": 7}'])
def test_every_malformed_payload_is_silent(store, raw, capsys):
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g.observe_intent(raw).allowed is True
    assert g.observe_result(raw).allowed is True
    assert capsys.readouterr().out == ""


def test_a_probe_that_explodes_does_not_reach_the_hook(store):
    def boom(*a, **k):
        raise RuntimeError("gh went up in smoke")
    g = itc.GatewayInterceptor(repo_root=str(store), runner=boom)
    g.observe_intent(_pre("gh pr merge 7"))
    v = g.observe_result(_post("gh pr merge 7"))
    assert v.allowed is True and v.reason


def test_an_unwritable_state_dir_does_not_break_the_hook(store, monkeypatch):
    monkeypatch.setattr(paths, "write_text_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g.observe_intent(_pre("gh pr merge 7")).allowed is True


def test_the_hook_stage_prints_exactly_what_it_printed_before(store, capsys,
                                                              monkeypatch):
    """cmd_hook's stdout IS the hook protocol. The interceptor is wired in
    beside the existing work, inside a try/except, and adds not one byte."""
    from mindhive.commands import ledger as ledger_mod

    class _Args:
        stage = "post_tool_use"
    raws = [_post("gh pr merge 7"), _post("pytest -q")]
    seen = []
    for raw in raws:
        monkey = iter([raw])
        # Through monkeypatch. `ledger_mod.sys` IS the real `sys` module, so a
        # bare assignment installs this stub process-wide for the rest of the
        # pytest session -- and its `read` raises StopIteration the second
        # time, in whichever unrelated file happens to be collected next.
        monkeypatch.setattr(ledger_mod.sys, "stdin", type("S", (), {
            "isatty": staticmethod(lambda: False),
            "read": staticmethod(lambda: next(monkey))})())
        ledger_mod.cmd_hook(_Args())
        out = capsys.readouterr().out
        seen.append(out)
        assert json.loads(out.strip())        # exactly one JSON line, as before
    assert all(set(json.loads(o.strip())) <= {"appended", "error"} for o in seen)


def test_the_interceptor_failing_does_not_fail_the_hook(store, monkeypatch, capsys):
    from mindhive.commands import ledger as ledger_mod
    monkeypatch.setattr(itc, "GatewayInterceptor",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    class _Args:
        stage = "post_tool_use"
    monkeypatch.setattr(ledger_mod.sys, "stdin", type("S", (), {
        "isatty": staticmethod(lambda: False),
        "read": staticmethod(lambda: _post("gh pr merge 7"))})())
    assert ledger_mod.cmd_hook(_Args()) is None
    assert json.loads(capsys.readouterr().out.strip())


# --- the production entry point ----------------------------------------------------

def _hook(ledger_mod, monkeypatch, stage, raw):
    """One `cmd_hook` call at *stage*, fed *raw* on stdin."""
    class _Args:
        pass
    _Args.stage = stage
    monkeypatch.setattr(ledger_mod.sys, "stdin", type("S", (), {
        "isatty": staticmethod(lambda: False),
        "read": staticmethod(lambda: raw)})())
    return ledger_mod.cmd_hook(_Args())


def test_both_hook_stages_really_reach_the_interceptor(store, monkeypatch,
                                                       capsys):
    """THE WIRING ITSELF. Every other test here drives `GatewayInterceptor`
    directly, so the three `_observe_gateway` call sites in `commands/ledger.py`
    could all be deleted with the whole suite green -- no intent would ever be
    written, every post would answer "no paired intent", and not one
    `interceptor_signal` would be appended in production.

    Drives the real `cmd_hook` at both stages and asserts the two observable
    effects: the sidecar after `pre_tool_use`, the signal after
    `post_tool_use`."""
    from mindhive import repo as repo_mod
    from mindhive.commands import ledger as ledger_mod

    # `_observe_gateway` builds its own interceptor, so the seam the test can
    # reach is the runner it falls back to when none was injected.
    monkeypatch.setattr(repo_mod, "_default_runner",
                        _Runner({"merge-base": (1, "", ""),
                                 "gh": (0, MERGED_PR, "")}))
    _hook(ledger_mod, monkeypatch, "pre_tool_use", _pre("gh pr merge 7"))
    capsys.readouterr()
    assert _intent_path().exists(), "pre_tool_use never reached the interceptor"

    _hook(ledger_mod, monkeypatch, "post_tool_use", _post("gh pr merge 7"))
    capsys.readouterr()
    (sig,) = _signals()
    assert sig.payload["pr_number"] == 7
    assert sig.payload["merged"] is True
    assert not _intent_path().exists(), "the paired sidecar was not consumed"


# --- the merge-probe event ---------------------------------------------------------

def test_a_conflicting_probe_leaves_a_merge_conflict_event(store, monkeypatch):
    """`mindnet/worktree.merge_probe` already computes the conflict list and
    throws it away when the caller does not ask. Recording it is what lets a
    person see that lane 0 has collided with the same file five times."""
    from mindhive.mindnet import worktree as mn_wt

    monkeypatch.setattr(mn_wt, "_git_rc", lambda *a, **k: (
        1, f"{EMPTY_TREE_OID}\nsrc/a.py\nsrc/b.py\n\ninfo", ""))
    probe = mn_wt.merge_probe(str(store), "lint/m/0")
    assert probe.clean is False
    rows = [e for e in events.read_all(None) if e.type == "merge_conflict_detected"]
    assert len(rows) == 1
    assert rows[0].payload["branch"] == "lint/m/0"
    assert rows[0].payload["conflicts"] == ["src/a.py", "src/b.py"]
    # By value. The tree is the object name `git merge-tree` wrote, and
    # DATA_MODEL calls it what separates a real collision from an unknown ref;
    # asserted only for truthiness, any constant 40-hex string would pass and
    # every recorded row would carry a fabricated oid.
    assert rows[0].payload["tree"] == EMPTY_TREE_OID


def test_a_clean_probe_records_nothing(store, monkeypatch):
    from mindhive.mindnet import worktree as mn_wt

    monkeypatch.setattr(mn_wt, "_git_rc",
                        lambda *a, **k: (0, f"{EMPTY_TREE_OID}\n", ""))
    assert mn_wt.merge_probe(str(store), "lint/m/0").clean is True
    assert [e for e in events.read_all(None)
            if e.type == "merge_conflict_detected"] == []


def test_the_conflict_event_is_best_effort(store, monkeypatch):
    """The probe's answer must survive an unwritable ledger: recording is a
    side benefit, answering "would this conflict" is the job."""
    from mindhive.mindnet import worktree as mn_wt

    monkeypatch.setattr(mn_wt, "_git_rc", lambda *a, **k: (
        1, f"{EMPTY_TREE_OID}\nsrc/a.py\n\ninfo", ""))
    monkeypatch.setattr(events, "append_event",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert mn_wt.merge_probe(str(store), "lint/m/0").clean is False


def test_a_payload_our_own_contract_rejects_is_not_swallowed_silently(store,
                                                                    monkeypatch):
    """The bare `except` exists for an UNWRITABLE LEDGER -- a condition outside
    this process. A payload `validate_payload` refuses is the opposite: our own
    bug, and swallowing it means the contract can drift with nothing to show
    for it. The probe must still ANSWER (that is the job), and the violation
    must leave a trace."""
    from mindhive.mindnet import worktree as mn_wt

    appended = []
    monkeypatch.setattr(events, "append_event",
                        lambda *a, **k: appended.append(a))
    mn_wt._record_conflict("lint/m/0", (), EMPTY_TREE_OID)
    assert appended == [], "an invalid payload must not reach the ledger"
    assert mn_wt.CONTRACT_VIOLATIONS, "the violation left no trace at all"
    assert "conflicts" in mn_wt.CONTRACT_VIOLATIONS[-1]


def test_an_unwritable_ledger_leaves_no_contract_violation(store, monkeypatch):
    """CANARY for the test above: the two failures are told apart."""
    from mindhive.mindnet import worktree as mn_wt

    del mn_wt.CONTRACT_VIOLATIONS[:]
    monkeypatch.setattr(events, "append_event",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    mn_wt._record_conflict("lint/m/0", ("src/a.py",), EMPTY_TREE_OID)
    assert mn_wt.CONTRACT_VIOLATIONS == []


def test_the_conflict_type_is_registered_too(store):
    from mindhive import contracts, pgschema
    assert "merge_conflict_detected" in events.DETERMINISTIC_TYPES
    assert pgschema.EVENT_TYPE_CLASSES["merge_conflict_detected"] == "deterministic"
    module, hint = contracts._PAYLOAD_CONTRACTS["merge_conflict_detected"]
    assert module == ".mindnet.worktree" and hint
    with pytest.raises(ValueError):
        contracts.validate_typed_payload("merge_conflict_detected", {"branch": 7})


# --- the PR probe gained two fields ------------------------------------------------

def _gh_on_path(monkeypatch):
    """`repo._probe_pr` asks `shutil.which("gh")` before it asks the runner:
    on a machine without the CLI the fake was never consulted, and the probe
    answered UNKNOWN."""
    from mindhive import repo

    monkeypatch.setattr(repo.shutil, "which",
                        lambda name, *a, **k: "gh" if name == "gh" else None)


def test_the_repo_probe_asks_for_the_merging_actor(store, monkeypatch):
    """`repo._probe_pr` already runs `gh pr view`; adding two fields to the
    same call is free, and the interceptor reuses the answer."""
    from mindhive import repo

    _gh_on_path(monkeypatch)
    runner = _Runner({"remote": (0, "origin https://github.com/o/r (fetch)", ""),
                      "gh": (0, MERGED_PR, "")})
    repo._probe_pr(7, runner, str(store), 5)
    argv = [c for c in runner.calls if "pr" in c and "view" in c][0]
    fields = argv[argv.index("--json") + 1]
    assert set(fields.split(",")) >= {"state", "mergedAt", "url",
                                      "mergedBy", "author"}


def test_the_old_probe_answer_is_unchanged(store, monkeypatch):
    """CANARY. `roadmap.retire_landed` reads `state`/`evidence` out of this;
    the two new fields must not change either."""
    from mindhive import repo

    _gh_on_path(monkeypatch)
    runner = _Runner({"remote": (0, "origin https://github.com/o/r (fetch)", ""),
                      "gh": (0, MERGED_PR, "")})
    answer = repo._probe_pr(7, runner, str(store), 5)
    assert answer["state"] == repo.MERGED
    assert "is merged" in answer["evidence"]
    assert answer["reason"] == ""


# --- the post hook stays off the disk unless it has something to pair --------------

def test_a_post_that_is_not_a_gateway_act_never_touches_the_filesystem(store, monkeypatch):
    """Round 2. `_result` read the sidecar before asking whether the post's own
    command was a gateway act at all, and `_intent_path` goes through
    `paths.state_dir`, which MKDIRS -- so every one of the thousands of Read,
    Edit and Grep calls in a session paid a mkdir plus a failed open, to answer
    a question a regex over the command answers without any syscall."""
    touched = []
    real = paths.state_dir

    def counting(project=None):
        touched.append(project)
        return real(project)
    monkeypatch.setattr(paths, "state_dir", counting)
    g = itc.GatewayInterceptor(repo_root=str(store),
                               runner=lambda argv, cwd=None, timeout_s=None: (1, "", ""))
    assert g.observe_result(_post("pytest -q")).reason == "not a gateway act"
    assert g.observe_result(_post("echo gh pr merge 5")).reason == "not a gateway act"
    assert g.observe_result(_post("", tool="Read")).reason == "not a gateway act"
    assert touched == []
    assert _signals() == []

    # A real gateway act still pairs through the sidecar, disk and all.
    g.observe_intent(_pre("gh pr merge 5"))
    touched.clear()
    g.observe_result(_post("gh pr merge 5"))
    assert touched, "a real gateway act must still read its intent"
    assert len(_signals()) == 1


# --- commands that do not begin with the program name ------------------------------

@pytest.mark.parametrize("command,action", [
    ("cd /repo && gh pr merge 7", "pr_merge"),
    ("cd /repo; git merge lane", "git_merge"),
    ("GH_TOKEN=xxx gh pr merge 7", "pr_merge"),
    ("env GH_TOKEN=xxx gh pr merge 7", "pr_merge"),
    ("/usr/bin/gh pr merge 7", "pr_merge"),
])
def test_a_real_invocation_is_recognised_behind_its_prefix(store, command,
                                                           action):
    """`cd <dir> && gh pr merge 7` is how a merge is actually run from a hook
    or a script. Anchoring the pattern at the very first character made every
    one of these invisible: no intent, no signal, and the gateway event the
    whole module exists to observe simply never happened."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g.observe_intent(_pre(command)).action_type == action


@pytest.mark.parametrize("command", [
    "echo gh pr merge",
    "echo 'cd /repo && gh pr merge 7'",
    "grep -rn 'git merge' src/",
    "git commit -m 'cd x && gh pr merge 7'",
])
def test_the_prefix_tolerance_does_not_admit_a_mention(store, command):
    """CANARY for the test above. Each segment is still anchored at ITS OWN
    start, so a quoted mention inside an `echo` or a commit message is not a
    run -- which is the property the original anchor was chosen for."""
    g = itc.GatewayInterceptor(repo_root=str(store))
    assert g.observe_intent(_pre(command)).action_type == ""
    assert not _intent_path().exists()


def test_a_merge_that_names_no_number_is_probed_by_branch(store):
    """`gh pr merge --squash` with no number merges the CURRENT BRANCH's PR --
    the commonest form there is. Recording `merged: false` for it, without
    asking anything, writes a false fact about a PR that WAS merged. `gh pr
    view` resolves the same branch, so the probe simply asks without a
    number."""
    runner = _Runner({"merge-base": (1, "", ""), "gh": (0, MERGED_PR, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge --squash"))
    g.observe_result(_post("gh pr merge --squash"))
    (sig,) = _signals()
    assert sig.payload["merged"] is True
    assert sig.payload["merge_actor"] == "claude_code"
    (ask,) = [c for c in runner.calls if c[:3] == ["gh", "pr", "view"]]
    assert "0" not in ask, "asked gh about PR 0"


def test_a_merge_that_names_no_number_and_cannot_be_resolved_claims_nothing(
        store):
    """CANARY. When `gh` cannot answer either, the signal must say so rather
    than record the same confident `merged: false`."""
    runner = _Runner({"merge-base": (1, "", ""), "gh": (1, "", "no PR found")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr merge --squash"))
    g.observe_result(_post("gh pr merge --squash"))
    (sig,) = _signals()
    assert sig.payload["merged"] is False
    assert sig.payload["merge_actor"] == "unknown"
    assert "gh could not answer" in sig.payload["reason"]


# --- the roadmap sweep must not run on the hook path -------------------------------

def test_the_closing_sweep_does_not_fold_the_whole_ledger_on_the_hook_path(
        store, monkeypatch):
    """`roadmap.retire_landed` reads the WHOLE ledger and probes every live
    plan with up to three subprocesses at `repo.PROBE_TIMEOUT_SECONDS` each.
    On `post_tool_use` that is a hook blocked for minutes and, past the tool's
    own patience, a signal lost. The sweep still runs -- the closed PR must
    still retire its plan -- but the signal is recorded FIRST, so a slow sweep
    can no longer cost the fact."""
    order = []
    from mindhive import roadmap
    monkeypatch.setattr(roadmap, "retire_landed",
                        lambda **kw: order.append("sweep") or [])
    real_append = events.append_event

    def _spy(event_type, *a, **k):
        if event_type == "interceptor_signal":
            order.append("signal")
        return real_append(event_type, *a, **k)

    monkeypatch.setattr(events, "append_event", _spy)
    record = json.dumps({"state": "CLOSED", "mergedAt": None, "mergedBy": None,
                         "author": {"login": "x"}, "url": "u"})
    runner = _Runner({"merge-base": (1, "", ""), "gh": (0, record, "")})
    g = itc.GatewayInterceptor(repo_root=str(store), runner=runner)
    g.observe_intent(_pre("gh pr close 7"))
    g.observe_result(_post("gh pr close 7"))
    assert order == ["signal", "sweep"], order
    assert _signals()[0].payload["merged"] is False


# --- the grammar is carved out, by identity ----------------------------------------

def test_the_carve_out_is_by_identity():
    """The command grammar lives in `gateway.py` so neither file passes the
    MEB's 500-line ceiling. Identity is what makes that a carve-out rather than
    a second copy that can drift -- and drift here means two answers to "is
    this a merge"."""
    assert itc.merge_actor_of is gw.merge_actor_of
    assert itc.action_of is gw.action_of
    assert itc.branch_of is gw.branch_of
    assert itc.ACTION_TYPES is gw.ACTION_TYPES
    assert itc.MERGE_ACTORS is gw.MERGE_ACTORS


def test_the_two_methods_are_the_module_functions():
    """The instance seam and the pure function answer the same thing."""
    g = itc.GatewayInterceptor()
    assert g._action_of("cd /r && gh pr merge 7") == gw.action_of("cd /r && gh pr merge 7")
    assert g._branch_of("git merge lane") == gw.branch_of("git merge lane")


def test_neither_import_order_breaks():
    """`gateway` imports nothing from `interceptor`, so there is no cycle."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    # `parents[2]` of `src/mindhive/orchestrator/interceptor.py` is `src/` --
    # one level deeper than a top-level module, where the same index is the
    # repo root. Checked before spawning: handed a directory with no package,
    # the child falls back to the editable install (the MAIN checkout) with
    # no error, and this test silently measures the wrong tree.
    root = Path(itc.__file__).resolve().parents[2]
    assert (root / "mindhive" / "__init__.py").is_file(), (
        f"{root} holds no mindhive package: the child would not import this "
        "checkout")
    for first in ("mindhive.orchestrator.gateway",
                  "mindhive.orchestrator.interceptor"):
        done = subprocess.run(
            [sys.executable, "-c", f"import {first}; print('ok')"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(root)})
        assert done.returncode == 0, f"{first}: {done.stderr}"


def test_the_grammar_half_touches_nothing(store):
    """It is pure text: no store, no subprocess, no clock. A child that
    imports it must not drag the ledger, `paths` or `subprocess` in behind it
    -- that is what keeps it cheap enough to answer on every Bash tool call."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(itc.__file__).resolve().parents[2]
    # Guarded for the reason given in `test_neither_import_order_breaks`.
    assert (root / "mindhive" / "__init__.py").is_file(), (
        f"{root} holds no mindhive package: the child would not import this "
        "checkout")
    # Measured as a DELTA over `mindhive.orchestrator` itself: the package's
    # own `__init__` is not this module's doing, and pinning the absolute set
    # would pin that instead of the thing under test.
    done = subprocess.run(
        [sys.executable, "-c",
         "import sys, mindhive.orchestrator;"
         "before = set(sys.modules);"
         "import mindhive.orchestrator.gateway as g;"
         "print(g.action_of('cd /r && gh pr merge 7'));"
         "print(sorted(m for m in set(sys.modules) - before"
         " if m.startswith('mindhive') or m == 'subprocess'))"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(root)})
    assert done.returncode == 0, done.stderr
    assert "('pr_merge', 7)" in done.stdout
    pulled = done.stdout.strip().splitlines()[-1]
    assert pulled == "['mindhive.orchestrator.gateway']", pulled
