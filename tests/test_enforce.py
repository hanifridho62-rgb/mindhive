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
