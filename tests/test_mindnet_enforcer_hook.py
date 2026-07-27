# tests/test_mindnet_enforcer_hook.py
"""MN-4 U7 — the enforcer reaches the real `PreToolUse` surface.

Written TEST-FIRST against
``docs/plans/2026-07-27-001-feat-mindnet-mn3-mn4-mn5-mechanical-enforcement-plan.md``
U7. U6 built two pure predicates; nothing called them. This is the wiring, and
the wiring has failure modes the predicates do not.

## Why these register as POLICY_CHECKS rather than as their own hook path

`enforce.evaluate_policies` already provides ordered evaluation, the
per-signature manifest disable, the reason override, and the fail-open wrapper.
A bespoke path would have had to reimplement all four, and the manifest entry is
the operator's documented kill switch for a rule that can stop them working —
`test_the_manifest_kill_switch_restores_allow` is the test that proves the
switch is real.

## The three wiring-specific properties

1. **One sidecar read per hook call, not one per check.** Two checks each doing
   their own `sidecar.read` doubles the file I/O on a path that fires on every
   tool call. `test_one_hook_call_reads_the_sidecar_once` pins it.
2. **The denial carries `ENFORCED_MARK`.** Without it, `failures.py` re-ingests
   our own refusal text as a brand-new incident — the learning loop eating its
   own output. That has happened before in this repo (`enforce.py:186-190`).
3. **Existing policies keep their precedence.** `gate:no-verify` and the
   destructive-command gate were here first and must not be shadowed.
"""
import json

import pytest

from mindhive import enforce
from mindhive.mindnet import sidecar as s_mod
from mindhive.mindnet import tasks as t_mod
from mindhive.mindnet.controller import PhaseRecord


# --- helpers -------------------------------------------------------------------

def _sc(phase="brainstorm", status=t_mod.STATUS_IN_PROGRESS, langs=(),
        fresh=True):
    task = t_mod.Task(task_id="t1", title="a task", status=status,
                      language_context=tuple(langs))
    return s_mod.Sidecar(
        state=s_mod.PhaseState(
            {"t1": PhaseRecord(task_id="t1", phase=phase, epoch=0)}, {}),
        fresh=fresh, reason="", stamp={}, tasks={"t1": task}, advice={})


def _payload(tool="Write", **tool_input):
    return json.dumps({"tool_name": tool, "tool_input": tool_input})


@pytest.fixture
def spine(monkeypatch):
    """Install a sidecar without touching disk, and count the reads."""
    state = {"sc": _sc(), "reads": 0}

    def fake_read(project=None):
        state["reads"] += 1
        return state["sc"]

    monkeypatch.setattr(s_mod, "read", fake_read)
    return state


def _decision(out):
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def _reason(out):
    return (out.get("hookSpecificOutput") or {}).get(
        "permissionDecisionReason", "")


# --- R6 through the real hook -------------------------------------------------------

def test_a_brainstorm_phase_write_is_denied_at_the_hook(spine):
    out = enforce.hook_decision(_payload(file_path="src/x.py"))
    assert _decision(out) == "deny"
    assert "MindNetwork State Machine violation" in _reason(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_an_execution_phase_write_is_allowed_at_the_hook(spine):
    spine["sc"] = _sc(phase="execution")
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_a_language_violation_is_denied_at_the_hook(spine):
    spine["sc"] = _sc(phase="execution", langs=("python",))
    out = enforce.hook_decision(_payload(file_path="app.js"))
    assert _decision(out) == "deny"
    assert "Language Compatibility Lock violation" in _reason(out)


def test_a_permitted_language_is_allowed_at_the_hook(spine):
    spine["sc"] = _sc(phase="execution", langs=("python",))
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_the_evidence_directory_exemption_survives_the_wiring(spine):
    """The anti-deadlock guard has to hold at the hook, not just in the
    predicate — this is the path that actually runs."""
    assert enforce.hook_decision(
        _payload(file_path="docs/superpowers/specs/design.md")) == {}


def test_a_pending_task_does_not_deny_at_the_hook(spine):
    """The anti-brick guard, end to end."""
    spine["sc"] = _sc(status=t_mod.STATUS_PENDING)
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


# --- the denial is shaped so the learning loop does not eat it ------------------------

def test_the_denial_carries_the_enforced_mark(spine):
    """`failures.py` treats the marker as non-failure. Without it our own
    refusal text comes back as a brand-new raw:* incident — this exact loop bit
    the repo once already (`enforce.py:186-190`)."""
    out = enforce.hook_decision(_payload(file_path="src/x.py"))
    assert _reason(out).startswith(enforce.ENFORCED_MARK)


# --- the kill switch is real ----------------------------------------------------------

@pytest.mark.parametrize("sig", ["policy:gate:mindnet-phase",
                                 "policy:gate:mindnet-language"])
def test_the_manifest_kill_switch_restores_allow(spine, monkeypatch, sig):
    """A rule that can stop an operator working must have a documented off
    switch, and it must be the SAME switch every other policy uses."""
    spine["sc"] = _sc(phase="brainstorm", langs=("python",))
    monkeypatch.setattr(enforce, "load_manifest",
                        lambda project=None: {sig: {"enabled": False}})
    out = enforce.hook_decision(_payload(file_path="app.js"))
    other = ("Language" if "phase" in sig else "State Machine")
    assert other in _reason(out), (
        "disabling one mindnet policy must not disable the other")


def test_disabling_both_restores_a_full_allow(spine, monkeypatch):
    spine["sc"] = _sc(phase="brainstorm", langs=("python",))
    monkeypatch.setattr(enforce, "load_manifest", lambda project=None: {
        "policy:gate:mindnet-phase": {"enabled": False},
        "policy:gate:mindnet-language": {"enabled": False}})
    assert enforce.hook_decision(_payload(file_path="app.js")) == {}


def test_a_manifest_reason_override_replaces_the_message(spine, monkeypatch):
    monkeypatch.setattr(enforce, "load_manifest", lambda project=None: {
        "policy:gate:mindnet-phase": {"enabled": True,
                                      "reason": "custom operator text"}})
    out = enforce.hook_decision(_payload(file_path="src/x.py"))
    assert "custom operator text" in _reason(out)


# --- the read budget --------------------------------------------------------------

def test_one_hook_call_reads_the_sidecar_once(spine):
    """Two checks, one read. `PreToolUse` fires on every tool call, so a second
    read here is doubled file I/O on the hottest path in the system."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(file_path="app.js"))
    assert spine["reads"] == 1


def test_a_read_only_tool_does_not_read_the_sidecar_at_all(spine):
    """Nothing about `Read` can ever be denied by either rule, so paying a file
    read to discover that is pure waste on the most common tool call there is."""
    enforce.hook_decision(_payload(tool="Read", file_path="src/x.py"))
    assert spine["reads"] == 0


def test_the_hook_never_reads_the_ledger(spine, monkeypatch):
    """R8, at the surface that actually runs."""
    from mindhive import events as ev_mod

    def boom(*a, **k):
        raise AssertionError("PreToolUse must never read the ledger")

    monkeypatch.setattr(ev_mod, "read_all", boom)
    assert _decision(enforce.hook_decision(_payload(file_path="src/x.py"))) \
        == "deny"


# --- fail-open and non-interference ---------------------------------------------------

def test_no_sidecar_on_disk_allows():
    """No monkeypatch — the real `sidecar.read` against an empty store."""
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_a_raising_sidecar_read_allows(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(s_mod, "read", boom)
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_a_stale_sidecar_allows_at_the_hook(spine):
    spine["sc"] = _sc(fresh=False)
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_malformed_json_is_still_an_allow(spine):
    assert enforce.hook_decision("{not json") == {}


def test_the_no_verify_gate_still_fires_and_is_not_shadowed(spine):
    """A pre-existing standing rule. The mindnet checks are appended, so an
    ineffective `--no-verify` commit is still refused by ITS message."""
    out = enforce.hook_decision(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "git commit --no-verify -m x"}}))
    assert _decision(out) == "deny"
    assert "MindNetwork" not in _reason(out)


def test_existing_policies_keep_their_precedence(spine):
    """`POLICY_CHECKS` is ordered and first match wins. The two mindnet entries
    must come after the ones that were here first."""
    ids = [pid for pid, _d, _f in enforce.POLICY_CHECKS]
    assert ids.index("gate:no-verify") < ids.index("gate:mindnet-phase")
    assert ids.index("gate:config-protection") < ids.index("gate:mindnet-phase")


def test_both_mindnet_policies_are_registered():
    ids = [pid for pid, _d, _f in enforce.POLICY_CHECKS]
    assert "gate:mindnet-phase" in ids
    assert "gate:mindnet-language" in ids


def test_both_mindnet_policies_deny_rather_than_ask():
    """`ask` escalates to the human, which is right for a destructive command
    that is sometimes legitimate. A state-machine violation is not sometimes
    legitimate — the phase says the work is out of order."""
    for pid, decision, _fn in enforce.POLICY_CHECKS:
        if pid.startswith("gate:mindnet-"):
            assert decision == "deny"


def test_enforce_does_not_import_mindnet_at_module_level():
    """`enforce.py` is imported by the hook shim in every project, including
    ones with no mindnet state. The dependency is lazy, matching how
    `_policy_destructive` reaches `gateguard`."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(enforce))
    for node in tree.body:            # module level only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            text = getattr(node, "module", "") or ""
            names = {a.name for a in node.names}
            assert "mindnet" not in text and "mindnet" not in names
