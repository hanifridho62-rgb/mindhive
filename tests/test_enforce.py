import json

import pytest

from mindhive import enforce


# --- pure matchers ------------------------------------------------------------

def test_grep_unscoped_denies():
    assert enforce._grep_unscoped("Grep", {"pattern": "foo"})


@pytest.mark.parametrize("scope", [{"path": "src"}, {"glob": "**/*.py"}, {"type": "py"}])
def test_grep_scoped_allows(scope):
    assert enforce._grep_unscoped("Grep", {"pattern": "foo", **scope}) is None


def test_grep_matcher_ignores_other_tools():
    assert enforce._grep_unscoped("Bash", {"command": "rg foo"}) is None


def test_worktree_remove_without_force_denies():
    assert enforce._worktree_no_force("Bash", {"command": "git worktree remove /tmp/wt"})


@pytest.mark.parametrize("cmd", [
    "git worktree remove --force /tmp/wt",
    "git worktree remove -f /tmp/wt",
])
def test_worktree_remove_with_force_allows(cmd):
    assert enforce._worktree_no_force("Bash", {"command": cmd}) is None


def test_worktree_matcher_ignores_unrelated_bash():
    assert enforce._worktree_no_force("Bash", {"command": "git status"}) is None


# --- evaluate honors the manifest (opt-in gating) -----------------------------

def test_evaluate_denies_only_when_enabled():
    man = {"tool:ripgrep-timeout": {"enabled": True}}
    assert enforce.evaluate("Grep", {"pattern": "x"}, manifest=man)


def test_evaluate_disabled_entry_allows():
    man = {"tool:ripgrep-timeout": {"enabled": False}}
    assert enforce.evaluate("Grep", {"pattern": "x"}, manifest=man) is None


def test_evaluate_empty_manifest_allows():
    assert enforce.evaluate("Grep", {"pattern": "x"}, manifest={}) is None


def test_evaluate_unknown_signature_in_manifest_is_ignored():
    man = {"raw:deadbeef": {"enabled": True}}          # no matcher for this sig
    assert enforce.evaluate("Grep", {"pattern": "x"}, manifest=man) is None


def test_evaluate_reason_override_wins():
    man = {"tool:ripgrep-timeout": {"enabled": True, "reason": "SCOPE IT"}}
    assert enforce.evaluate("Grep", {"pattern": "x"}, manifest=man) == "SCOPE IT"


def test_evaluate_is_fail_open_on_garbage_entry():
    man = {"tool:ripgrep-timeout": "not-a-dict"}
    assert enforce.evaluate("Grep", {"pattern": "x"}, manifest=man) is None


# --- hook_decision (PreToolUse output contract) -------------------------------

def test_hook_decision_builds_deny_json():
    enforce.set_enabled("test", "tool:ripgrep-timeout", True)
    raw = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "x"}})
    out = enforce.hook_decision(raw, project="test")
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "path" in hso["permissionDecisionReason"]


def test_hook_decision_allows_when_nothing_enabled():
    raw = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "x"}})
    assert enforce.hook_decision(raw, project="test") == {}


def test_hook_decision_allows_on_bad_json():
    assert enforce.hook_decision("{not json") == {}


def test_hook_decision_allows_on_non_dict_payload():
    assert enforce.hook_decision("[1,2,3]") == {}


def test_hook_decision_camelcase_fields():
    # enable enforcement on disk for project "test", then drive the full parse
    enforce.set_enabled("test", "tool:ripgrep-timeout", True)
    raw = json.dumps({"toolName": "Grep", "toolInput": {"pattern": "x"}})
    out = enforce.hook_decision(raw, project="test")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- manifest round-trip + suggest/enable/disable -----------------------------

def test_set_enabled_round_trips():
    enforce.set_enabled("test", "git:worktree-locked", True)
    man = enforce.load_manifest("test")
    assert man["git:worktree-locked"]["enabled"] is True
    enforce.set_enabled("test", "git:worktree-locked", False)
    assert enforce.load_manifest("test")["git:worktree-locked"]["enabled"] is False


def test_set_enabled_refuses_signature_without_matcher():
    res = enforce.set_enabled("test", "raw:nope", True)
    assert "error" in res
    assert enforce.load_manifest("test") == {}       # nothing written


def test_load_manifest_missing_is_empty():
    assert enforce.load_manifest("test") == {}


# --- static policy tier (GateGuard port) --------------------------------------

def _pre(tool, tool_input):
    return json.dumps({"tool_name": tool, "tool_input": tool_input})


def test_policy_destructive_command_asks():
    out = enforce.hook_decision(_pre("Bash", {"command": "rm -rf /tmp/x"}),
                                project="test")
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert hso["permissionDecisionReason"].startswith(enforce.ENFORCED_MARK)


def test_policy_no_verify_denies():
    out = enforce.hook_decision(
        _pre("Bash", {"command": "git commit --no-verify -m 'x'"}),
        project="test")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_policy_no_verify_wins_over_destructive_ask():
    # A command that trips both gates must DENY (the stronger decision), not ask.
    out = enforce.hook_decision(
        _pre("Bash", {"command": "git push --force --no-verify"}),
        project="test")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_policy_config_protection_denies_existing(tmp_path):
    target = tmp_path / ".eslintrc.json"
    target.write_text("{}", encoding="utf-8")
    out = enforce.hook_decision(
        _pre("Edit", {"file_path": str(target)}), project="test")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_policy_disabled_via_manifest():
    pol = enforce.evaluate_policies(
        "Bash", {"command": "rm -rf /tmp/x"},
        manifest={"policy:gate:destructive-command": {"enabled": False}})
    assert pol is None


def test_policy_reason_override_wins():
    pol = enforce.evaluate_policies(
        "Bash", {"command": "rm -rf /tmp/x"},
        manifest={"policy:gate:destructive-command": {"reason": "NOPE"}})
    assert pol == ("gate:destructive-command", "ask", "NOPE")


def test_policy_ignores_non_command_tools():
    assert enforce.evaluate_policies("Grep", {"pattern": "rm -rf x"},
                                     manifest={}) is None


def test_policy_safe_command_allows():
    assert enforce.evaluate_policies("Bash", {"command": "git status"},
                                     manifest={}) is None


def test_policy_is_fail_open(monkeypatch):
    from mindhive import gateguard
    monkeypatch.setattr(gateguard, "is_destructive_command",
                        lambda cmd: 1 / 0)
    assert enforce.evaluate_policies("Bash", {"command": "rm -rf x"},
                                     manifest={}) is None


def test_learned_matcher_still_fires_alongside_policies():
    enforce.set_enabled("test", "tool:ripgrep-timeout", True)
    out = enforce.hook_decision(_pre("Grep", {"pattern": "x"}), project="test")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_set_enabled_accepts_policy_ids():
    res = enforce.set_enabled("test", "policy:gate:no-verify", False)
    assert res.get("enabled") is False
    man = enforce.load_manifest("test")
    assert man["policy:gate:no-verify"]["enabled"] is False
    # and the disabled policy no longer fires through hook_decision
    out = enforce.hook_decision(
        _pre("Bash", {"command": "git commit --no-verify -m 'x'"}),
        project="test")
    assert out == {}


def test_suggest_adds_disabled_stub_for_ruled_recurring_signature(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda project=None: [
        {"signature": "tool:ripgrep-timeout", "status": "active",
         "rule_source": "human", "occurrences": enforce.ENFORCE_THRESHOLD},
        # excluded: no active rule
        {"signature": "git:worktree-locked", "status": "active",
         "rule_source": "none", "occurrences": 99},
        # excluded: no matcher
        {"signature": "raw:abc", "status": "active",
         "rule_source": "human", "occurrences": 99},
    ])
    added = enforce.suggest("test")
    assert added == ["tool:ripgrep-timeout"]
    man = enforce.load_manifest("test")
    assert man["tool:ripgrep-timeout"]["enabled"] is False    # opt-in, not auto-on


def test_suggest_skips_below_threshold(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda project=None: [
        {"signature": "tool:ripgrep-timeout", "status": "active",
         "rule_source": "human", "occurrences": enforce.ENFORCE_THRESHOLD - 1},
    ])
    assert enforce.suggest("test") == []
    assert enforce.load_manifest("test") == {}


def test_suggest_is_idempotent(monkeypatch):
    from mindhive import incidents
    monkeypatch.setattr(incidents, "summaries", lambda project=None: [
        {"signature": "git:worktree-locked", "status": "active",
         "rule_source": "auto", "occurrences": 10},
    ])
    assert enforce.suggest("test") == ["git:worktree-locked"]
    assert enforce.suggest("test") == []              # already present, not re-added


def test_hook_decision_reason_carries_enforced_mark():
    """Every deny reason is tagged so the failure capture can recognize our own
    enforcement echo and not re-ingest it as a new raw:* incident."""
    enforce.set_enabled("test", "tool:ripgrep-timeout", True)
    raw = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "x"}})
    out = enforce.hook_decision(raw, project="test")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith(enforce.ENFORCED_MARK)


# --- R4: a denial leaves ledger evidence --------------------------------------
#
# The architecture doc lists "PreToolUse auditability" as something MN-4 fixes:
# "PreToolUse currently appends no event on deny or allow, so enforcement leaves
# no ledger evidence — contrary to SOP 1.4." MN-4 shipped the teeth and not the
# audit trail, so every refusal this repo has ever issued is unreconstructable.
#
# The load-bearing test in this section is the ALLOW-path canary. `PreToolUse`
# fires on every tool call; an append on the allow path would put a file write on
# the hottest path in the system. A section that only asserted "deny writes an
# event" would pass just as happily against an enforcer that wrote one every
# time, so the canary comes first and is asserted over two distinct allow
# shapes: a tool no policy inspects, and a tool every policy inspected and
# declined.

def _denials(project="test"):
    from mindhive import events
    return [e for e in events.read_all(project) if e.type == "policy_denied"]


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "src/x.py"}),
    ("Bash", {"command": "pytest -q"}),
])
def test_an_allowed_tool_call_appends_nothing(tool, tool_input):
    """THE canary. Delete it and the section below still passes against an
    enforcer that appends on every call."""
    assert enforce.hook_decision(_pre(tool, tool_input), project="test") == {}
    assert _denials() == []


def test_a_denial_appends_exactly_one_event():
    enforce.hook_decision(
        _pre("Bash", {"command": "git commit --no-verify -m 'x'"}),
        project="test")
    rows = _denials()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["signature"] == "gate:no-verify"
    assert payload["tool_name"] == "Bash"
    assert payload["decision"] == "deny"
    assert payload["reason"]


def test_an_ask_decision_is_recorded_as_ask():
    """`gate:destructive-command` escalates rather than refusing. Recording it
    as a deny would misreport how often the enforcer actually stopped work."""
    enforce.hook_decision(_pre("Bash", {"command": "rm -rf /tmp/x"}),
                          project="test")
    rows = _denials()
    assert len(rows) == 1
    assert rows[0].payload["decision"] == "ask"


def test_the_stored_reason_omits_the_enforced_mark():
    """`ENFORCED_MARK` is a wire marker for the hook payload — it exists so
    `failures.py` does not re-ingest our own refusal as a fresh incident. The
    ledger wants the rule's own words, not the envelope."""
    enforce.hook_decision(
        _pre("Bash", {"command": "git commit --no-verify -m 'x'"}),
        project="test")
    assert enforce.ENFORCED_MARK not in _denials()[0].payload["reason"]


def test_an_oversized_reason_is_truncated_in_the_ledger_but_not_on_the_wire():
    """A manifest `reason` override is operator-supplied text of any length.
    The operator must still SEE all of it; the ledger must not carry a novel
    per denial."""
    huge = "x" * 10_000
    enforce.set_enabled("test", "tool:ripgrep-timeout", True)
    man = enforce.load_manifest("test")
    man["tool:ripgrep-timeout"] = {"enabled": True, "reason": huge}
    enforce._write_manifest("test", man)
    out = enforce.hook_decision(
        _pre("Grep", {"pattern": "x"}), project="test")
    stored = _denials()[0].payload["reason"]
    assert len(stored) < len(huge)
    assert huge in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_learned_matcher_denial_records_an_empty_signature():
    """The matcher tier returns a reason without a signature. Recording ``""``
    is the honest answer; inventing one would put a name in the ledger that no
    manifest entry matches."""
    enforce.set_enabled("test", "tool:ripgrep-timeout", True)
    enforce.hook_decision(_pre("Grep", {"pattern": "x"}), project="test")
    assert _denials()[0].payload["signature"] == ""


def test_the_decision_survives_a_ledger_that_cannot_be_written(monkeypatch):
    """Fail-open, and in the direction that matters. A full or locked ledger
    must not turn a deny into an allow — audit is a record OF the decision, not
    a participant in it."""
    from mindhive import events

    def boom(*a, **kw):
        raise OSError("ledger segment is read-only")

    monkeypatch.setattr(events, "append_event", boom)
    out = enforce.hook_decision(
        _pre("Bash", {"command": "git commit --no-verify -m 'x'"}),
        project="test")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "--no-verify" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_two_denials_append_two_events():
    for _ in range(2):
        enforce.hook_decision(
            _pre("Bash", {"command": "git commit --no-verify -m 'x'"}),
            project="test")
    assert len(_denials()) == 2


def test_policy_denied_is_a_registered_event_type():
    """An unregistered type is rejected by the append surfaces and would make
    the audit trail silently empty."""
    from mindhive import events
    assert "policy_denied" in events.VALID_TYPES
    assert "policy_denied" not in events.SEMANTIC_TYPES, (
        "a refusal is telemetry, not a curated memo — semantic membership would "
        "put one LEDGER markdown file and one recall-corpus entry on disk per "
        "denial")
