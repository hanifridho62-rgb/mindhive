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
from mindhive.mindnet import optin
from mindhive.mindnet import sidecar as s_mod
from mindhive.mindnet import tasks as t_mod
from mindhive.mindnet.controller import PhaseRecord


# --- helpers -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def armed(monkeypatch):
    """Arm MindNetwork enforcement for this whole file.

    MN-6 made the two mindnet policies OPT-IN (they now default dormant, see
    `optin.py` and `tests/test_mindnet_optin.py` for why: one sidecar governs
    every worktree of a repo, so an ON-by-default rule froze checkouts nobody
    had opted in). Every assertion below is about what enforcement DOES once
    somebody asked for it, so the whole file opts in — otherwise each of these
    "is denied" tests would pass or fail for the wrong reason, and each "is
    allowed" test would pass vacuously.

    The dormant half of the contract gets its own section at the foot of this
    file, where the flag is the only thing that differs between a deny and an
    allow."""
    monkeypatch.setenv(optin.ENV_VAR, "1")

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


# --- MN-8a: the content check through the real hook ---------------------------------
#
# The predicate tests in `test_mindnet_enforcer.py` prove the rule decides
# correctly. These prove the decision REACHES the surface that can act on it —
# the same gap `test_a_shell_redirect_violation_is_denied_at_the_hook` was
# written for, where a prefilter made a fully-tested rule dead code at the only
# layer that runs.

_JS = "function add(a, b) { return a + b; }"


def test_alien_syntax_in_a_python_file_is_denied_at_the_hook(spine):
    spine["sc"] = _sc(phase="execution", langs=("python",))
    out = enforce.hook_decision(_payload(file_path="src/x.py", content=_JS))
    assert _decision(out) == "deny"
    # Asserted on the SPECIFIC message, not merely on "a denial happened": the
    # extension rule can also deny this tool call, and a test satisfied by any
    # refusal would stay green with the content check deleted.
    assert enf_syntax_denial() in _reason(out)
    assert enforce.ENFORCED_MARK in _reason(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


def test_valid_python_content_is_allowed_at_the_hook(spine):
    """The sibling that stops the test above from being satisfied by a hook that
    denies every `.py` write."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    assert enforce.hook_decision(
        _payload(file_path="src/x.py",
                 content="def add(a, b):\n    return a + b\n")) == {}


def test_an_edit_fragment_is_not_denied_at_the_hook(spine):
    """The anti-wedge guard at the layer that would actually do the wedging.

    ``    return a + b`` is a correct mid-function edit and is not standalone-
    parseable. If this ever goes red, editing is broken inside any python-locked
    task and the escape hatches are themselves edits."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    assert enforce.hook_decision(
        _payload(tool="Edit", file_path="src/x.py", old_string="pass",
                 new_source="x", new_string="    return a + b")) == {}


def enf_syntax_denial():
    from mindhive.mindnet import enforcer
    return enforcer.SYNTAX_DENIAL


def test_a_shell_redirect_violation_is_denied_at_the_hook(spine):
    """The predicate now covers execute tools, but only if the prefilter in
    `_policy_mindnet_language` lets them reach it. That prefilter read
    `WRITE_TOOLS` alone, so the whole shell rule would have been dead code at
    the only layer that runs — passing unit tests over a policy that never
    fires."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    out = enforce.hook_decision(
        _payload(tool="Bash", command="cat > src/app.ts <<'EOF'\nx\nEOF"))
    assert _decision(out) == "deny"
    assert "Language Compatibility Lock violation" in _reason(out)
    assert enforce.ENFORCED_MARK in _reason(out)


def test_a_harmless_shell_command_is_allowed_at_the_hook(spine):
    """`Bash` is the most-called tool in the system. The sibling that keeps the
    test above from being satisfied by an enforcer that denies every command."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    assert enforce.hook_decision(_payload(tool="Bash",
                                          command="pytest -q")) == {}


def test_the_kill_switch_reaches_the_shell_path_too(spine, monkeypatch):
    """A rule that can refuse shell commands needs its off switch to work on
    them — the manifest entry is the operator's documented remedy."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    monkeypatch.setattr(enforce, "load_manifest", lambda project=None: {
        "policy:gate:mindnet-language": {"enabled": False}})
    assert enforce.hook_decision(
        _payload(tool="Bash", command="echo x > src/app.ts")) == {}


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


# --- MN-6: the blast-radius gate at the surface that actually runs ------------------
#
# The two tests below are ONE pair with ONE variable. Same payload, same sidecar,
# same manifest — only the opt-in flag differs. That is what makes them evidence
# rather than two unrelated observations.

def test_a_phase_violating_write_is_allowed_when_enforcement_is_dormant(
        spine, monkeypatch):
    """No opt-in anywhere: a `brainstorm` write that MN-4 would refuse must run.

    This is the whole blast-radius fix. `paths.detect_project` collapses every
    worktree of a repo onto one namespace, so the sidecar this reads is SHARED —
    an ON-by-default rule meant one forgotten `in_progress` task at a planning
    gate denied writes in every checkout at once."""
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_the_same_write_is_denied_once_enforcement_is_armed(spine, monkeypatch):
    """The other half of the pair, and the canary for the test above: the flag
    is the ONLY difference, so 'allowed' up there cannot be some unrelated
    allow."""
    monkeypatch.setenv(optin.ENV_VAR, "1")
    out = enforce.hook_decision(_payload(file_path="src/x.py"))
    assert _decision(out) == "deny"
    assert "MindNetwork State Machine violation" in _reason(out)


def test_a_language_violating_write_is_allowed_when_dormant(spine, monkeypatch):
    spine["sc"] = _sc(phase="execution", langs=("python",))
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    assert enforce.hook_decision(_payload(file_path="app.js")) == {}


def test_a_dormant_hook_never_reads_the_sidecar(spine, monkeypatch):
    """Dormant must be CHEAP as well as permissive. The opt-in test comes before
    the projection read, so a project that never opted in pays no file I/O for
    mindnet on any tool call."""
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    enforce.hook_decision(_payload(file_path="src/x.py"))
    assert spine["reads"] == 0


def test_a_dormant_bash_command_is_allowed(spine, monkeypatch):
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    assert enforce.hook_decision(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "py -m pytest -q"}})) \
        == {}


def test_the_env_kill_switch_beats_a_persisted_opt_in(spine, monkeypatch,
                                                      tmp_path):
    """MN-4's third P0 was a kill switch locked inside the room it opened. An
    operator whose repo is frozen must be able to disarm enforcement without
    writing a file — and the variable has to win against the record on disk,
    not merely in its absence."""
    root = tmp_path / "checkout"
    root.mkdir()
    optin.set_enabled(True, project=None, root=root)
    monkeypatch.chdir(root)

    monkeypatch.setenv(optin.ENV_VAR, "1")
    assert _decision(enforce.hook_decision(_payload(file_path="src/x.py"))) \
        == "deny", "canary: the persisted opt-in must be capable of denying"

    monkeypatch.setenv(optin.ENV_VAR, "0")
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_dormancy_does_not_disable_the_policies_that_were_here_first(
        spine, monkeypatch):
    """Composition, which is how MN-4's second P0 happened: two individually
    sound guards that broke each other. `gate:no-verify` and the destructive and
    config-protection gates are standing rules with their own adoption decision
    — the mindnet opt-in must not reach them."""
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    out = enforce.hook_decision(json.dumps(
        {"tool_name": "Bash",
         "tool_input": {"command": "git commit --no-verify -m x"}}))
    assert _decision(out) == "deny"
    assert "MindNetwork" not in _reason(out)


def test_the_manifest_switch_and_the_opt_in_are_independent(spine, monkeypatch):
    """Two switches, both real. An armed opt-in must still respect a manifest
    disable, or the documented kill switch MN-4 shipped stops working."""
    monkeypatch.setenv(optin.ENV_VAR, "1")
    monkeypatch.setattr(enforce, "load_manifest", lambda project=None: {
        "policy:gate:mindnet-phase": {"enabled": False},
        "policy:gate:mindnet-language": {"enabled": False}})
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_a_raising_optin_check_is_dormant_not_an_outage(spine, monkeypatch):
    """Fail-open, in the direction that matters: a bug in the gate must allow,
    never deny. Distinct from the sidecar's fail-open — this fault happens
    BEFORE the projection is read."""
    monkeypatch.setenv(optin.ENV_VAR, "1")

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(optin, "armed", boom)
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}


def test_the_opt_in_is_checked_once_per_hook_call(spine, monkeypatch):
    """Both mindnet checks need the answer and `PreToolUse` fires on every tool
    call — the same reason the sidecar is read once. Shares the per-call cache
    `evaluate_policies` already owns."""
    monkeypatch.setenv(optin.ENV_VAR, "1")
    spine["sc"] = _sc(phase="execution", langs=("python",))
    n = {"calls": 0}
    real = optin.armed

    def counting(*a, **k):
        n["calls"] += 1
        return real(*a, **k)

    monkeypatch.setattr(optin, "armed", counting)
    enforce.hook_decision(_payload(file_path="app.js"))
    assert n["calls"] == 1


def test_a_dormant_read_only_tool_is_still_free(spine, monkeypatch):
    """The tool-name test must stay ahead of the opt-in test, so `Read` — the
    most common call in the system — pays neither."""
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    n = {"calls": 0}

    def counting(*a, **k):
        n["calls"] += 1
        return False

    monkeypatch.setattr(optin, "armed", counting)
    enforce.hook_decision(_payload(tool="Read", file_path="src/x.py"))
    assert n["calls"] == 0


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


# --- R4: the mindnet gates leave ledger evidence -------------------------------
#
# The architecture doc names this as MN-4's job: "PreToolUse currently appends no
# event on deny or allow, so enforcement leaves no ledger evidence — contrary to
# SOP 1.4. MN-4 fixes this; without it, MindNetwork's own forcing would be
# unauditable." MN-4 shipped the teeth and not the record.
#
# The generic contract (allow-path canary, truncation, fail-open, ask-vs-deny)
# lives in `tests/test_enforce.py`, which owns `hook_decision`. What is asserted
# here is the half that is specific to these two gates: that the signature in the
# ledger is the one an operator would put in their manifest to turn the rule off.

def _denials():
    from mindhive import events
    return [e for e in events.read_all() if e.type == "policy_denied"]


def test_a_language_denial_names_its_signature_in_the_ledger(spine):
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(file_path="app.js"))
    rows = _denials()
    assert len(rows) == 1
    assert rows[0].payload["signature"] == "gate:mindnet-language"
    assert "Language Compatibility Lock" in rows[0].payload["reason"]


def test_a_phase_denial_names_its_signature_in_the_ledger(spine):
    enforce.hook_decision(_payload(file_path="src/x.py"))
    rows = _denials()
    assert len(rows) == 1
    assert rows[0].payload["signature"] == "gate:mindnet-phase"


def test_a_shell_language_denial_records_the_execute_tool(spine):
    """The tool name is what distinguishes the surface MN-4 covered from the one
    it deferred. Recording `Write` for a `Bash` refusal would make the shell
    rule's usage unmeasurable."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(tool="Bash",
                                   command="echo x > src/app.ts"))
    assert _denials()[0].payload["tool_name"] == "Bash"


def test_an_allowed_call_leaves_no_denial_row(spine):
    """The canary, restated over the mindnet gates specifically: these two are
    the ones that evaluate a sidecar on every write, so an append that leaked to
    the allow path would land hardest here."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}
    assert _denials() == []


def test_a_dormant_checkout_records_nothing(spine, monkeypatch):
    """MN-6 made these policies opt-in. A dormant checkout does not deny, so it
    must not accumulate denial rows either — an audit trail that logs refusals
    that never happened is worse than none."""
    monkeypatch.delenv(optin.ENV_VAR, raising=False)
    monkeypatch.setattr(optin, "armed", lambda project=None: False)
    spine["sc"] = _sc(phase="execution", langs=("python",))
    assert enforce.hook_decision(_payload(file_path="app.js")) == {}
    assert _denials() == []


def test_the_learning_loop_does_not_ingest_our_own_denials(spine):
    """`failures.py` eating the enforcer's output has bitten this repo before —
    it is why `ENFORCED_MARK` exists at all (`enforce.py:186-190`). A new event
    type is a new way in, so assert the incident reader still sees nothing."""
    from mindhive import failures
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(file_path="app.js"))
    assert _denials(), "guard against a vacuous pass: the row must exist"
    assert failures._existing_failure_keys("test") == set()


# --- R5: the compatibility cat finally wakes -----------------------------------
#
# MN-4's Open Question 3 left this open: "`compatibility`, `history`, and
# `pr-green` may not have a natural [emit site] yet; those cats simply never
# wake." For `compatibility` there was always a natural site — the language lock
# IS that faculty — so the cat was permanently absent from a board whose defining
# claim (R16) is that a missing cat means a real bug. An always-missing cat makes
# that claim untestable.
#
# The emit lives at the `hook_decision` seam, never inside `enforcer.py`:
# `test_the_module_does_not_import_events_at_all` asserts over that module's own
# source that it touches no ledger, and `telemetry.emit` appends.

def _activity():
    from mindhive import events
    from mindhive.mindnet import telemetry as tel
    return [e for e in events.read_all() if e.type == tel.EVENT_TYPE]


def test_a_language_denial_wakes_the_compatibility_cat_and_closes_it(spine):
    """An `active` row AND its paired `idle`, in that order.

    The unpaired version parked the cat in `ui_cats`' dim band — which is the
    documented HANG signal — for six hours after a decision that took
    microseconds. `telemetry.busy` pairs every other faculty's rows for exactly
    this reason; work that is instantaneous has to close itself."""
    from mindhive.mindnet import telemetry as tel
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(file_path="app.js"))
    rows = _activity()
    assert [r.payload["status"] for r in rows] == [tel.STATUS_ACTIVE,
                                                   tel.STATUS_IDLE]
    assert {r.payload["faculty"] for r in rows} == {"compatibility"}


def test_the_woken_cat_names_the_task_that_locked_the_repo(spine):
    """A cat that cannot be joined back to a task is a light with no switch —
    the same reason the denial message itself carries the task id."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(file_path="app.js"))
    assert _activity()[0].payload["task_id"] == "t1"


def test_a_shell_denial_wakes_the_cat_too(spine):
    """And names the same task the refusal named. Asserting only the row COUNT
    let a wrong or absent `task_id` pass — the shell path had no equivalent of
    the task-id assertion the write path got."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(tool="Bash",
                                   command="echo x > src/app.ts"))
    rows = _activity()
    assert len(rows) == 2
    assert {r.payload.get("task_id") for r in rows} == {"t1"}
    assert {r.payload.get("phase") for r in rows} == {"execution"}


def test_a_phase_denial_does_not_wake_the_compatibility_cat(spine):
    """Wiring `compatibility` to every mindnet refusal would make the board lie
    about which faculty acted — the phase spine is `task-graph`'s business."""
    enforce.hook_decision(_payload(file_path="src/x.py"))
    assert _activity() == []


def test_an_allowed_write_wakes_nothing(spine):
    spine["sc"] = _sc(phase="execution", langs=("python",))
    assert enforce.hook_decision(_payload(file_path="src/x.py")) == {}
    assert _activity() == []


def test_a_malformed_task_id_still_emits_the_event(spine):
    """`telemetry._payload` drops decoration it cannot validate rather than
    dropping the row. The cat must appear even when the join key does not — an
    absent cat is read as a bug, and 'the id was malformed' is not one."""
    spine["sc"] = _sc(phase="execution", langs=("python",))
    spine["sc"].tasks["t1"].__dict__["task_id"] = "NOT A VALID ID"
    enforce.hook_decision(_payload(file_path="app.js"))
    rows = _activity()
    assert len(rows) == 2
    assert all("task_id" not in r.payload for r in rows)


def test_the_decision_survives_a_telemetry_failure(spine, monkeypatch):
    """A refusal that succeeded must not be reported as anything else because a
    cat animation could not be logged — `telemetry.emit`'s own contract, held at
    the call site too."""
    from mindhive.mindnet import telemetry as tel
    monkeypatch.setattr(tel, "emit", lambda *a, **kw: 1 / 0)
    spine["sc"] = _sc(phase="execution", langs=("python",))
    out = enforce.hook_decision(_payload(file_path="app.js"))
    assert _decision(out) == "deny"
    assert "Language Compatibility Lock violation" in _reason(out)


def test_the_woken_cat_reaches_the_board_then_leaves_it(spine):
    """End to end: the emit is only worth anything if `ui_cats` renders it, and
    only correct if it stops rendering it.

    The first version of this test asserted `DISPLAY_DIM` past 180 s as the
    expected end state — encoding the hang signal as the desired outcome. With
    the paired `idle`, the cat is on the board briefly and then gone, which is
    what `ui_cats` means by "it finished"."""
    from mindhive import events
    from mindhive.mindnet import telemetry as tel
    from mindhive.mindnet import ui_cats
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(file_path="app.js"))
    state = tel.fold(events.read_all())
    stamp = state["compatibility"].ts
    assert "compatibility" in {c["faculty"]
                               for c in ui_cats.board(state, now=stamp)}
    gone = ui_cats.shift(stamp, ui_cats.IDLE_VANISH_SECS + 1)
    assert "compatibility" not in {c["faculty"]
                                   for c in ui_cats.board(state, now=gone)}, (
        "an instantaneous decision must not leave a cat parked in the dim band, "
        "which ui_cats defines as the hang signal")


def test_the_new_cat_takes_its_canonical_slot_rather_than_arrival_order(spine):
    """`telemetry.FACULTIES` is the one canonical order (invariant 10), and
    `compatibility` sits sixth — after `task-graph`, before `pr-green`. A board
    that ordered by arrival would put the newest emitter last and reshuffle
    every time a rule fires, which is a board an operator cannot scan.

    `pr-green` is emitted FIRST here on purpose: it is canonically after
    `compatibility`, so arrival order and canonical order disagree and the
    assertion can tell them apart."""
    from mindhive import events
    from mindhive.mindnet import telemetry as tel
    from mindhive.mindnet import ui_cats
    tel.emit("pr-green", tel.STATUS_ACTIVE)
    spine["sc"] = _sc(phase="execution", langs=("python",))
    enforce.hook_decision(_payload(file_path="app.js"))
    state = tel.fold(events.read_all())
    order = [c["faculty"] for c in
             ui_cats.board(state, now=state["compatibility"].ts)]
    assert order == ["compatibility", "pr-green"]


# --- review fix: the audit append must not age the projection it audits ---------------
#
# THE regression this section exists for, and it was invisible to every other
# test in this file. `sidecar._staleness` compares each segment's
# `[st_size, st_mtime_ns]`; `_record_denial` appends a row to exactly that
# directory. So the first refusal aged the sidecar, the next `sidecar.read`
# answered `fresh=False`, and `locked_task`/`blocking_task`/`active_task` all
# open with `if not sc.fresh: return None, None` — ALLOW. Re-running the refused
# command succeeded, and the phase block went dormant with it.
#
# The repo already states the rule this broke, at `__main__.py`: "No ledger
# append. `sidecar._staleness` counts every event, so a row written here without
# also rewriting the sidecar would read STALE." The audit was the first ledger
# writer on the hook path itself.
#
# These tests use the REAL `sidecar.read`, deliberately: the `spine` fixture
# monkeypatches it with a closure returning a fixed `fresh=True` sidecar, so no
# test that uses `spine` can observe this class of bug at all.

def _real_sidecar():
    from mindhive.mindnet import sidecar as sc_mod
    return sc_mod


def test_a_denial_does_not_age_the_sidecar(monkeypatch):
    """The refusal is recorded AND the projection stays current. Without the
    re-stamp these are mutually exclusive."""
    sc_mod = _real_sidecar()
    sc_mod.rebuild()
    assert sc_mod.read().fresh, "precondition: a fresh sidecar to age"
    out = enforce.hook_decision(
        _payload(tool="Bash", command="git commit --no-verify -m 'x'"))
    assert _decision(out) == "deny", "precondition: the append must have run"
    assert _denials(), "precondition: a row must have been written"
    assert sc_mod.read().fresh, (
        "the audit append aged the sidecar — the next tool call reads a stale "
        "projection and BOTH mindnet gates go dormant")


def test_a_language_denial_does_not_age_the_sidecar_despite_three_appends(
        spine):
    """A language denial writes three rows (`policy_denied` plus the cat's
    `active`/`idle` pair). The re-stamp has to happen after the LAST of them, or
    a later append re-ages what an earlier re-stamp restored."""
    sc_mod = _real_sidecar()
    sc_mod.rebuild()
    spine["sc"] = _sc(phase="execution", langs=("python",))
    out = enforce.hook_decision(_payload(file_path="app.js"))
    assert _decision(out) == "deny"
    assert len(_denials()) == 1 and len(_activity()) == 2, "precondition"
    assert sc_mod.read().fresh


def test_an_already_stale_sidecar_is_never_certified_fresh(monkeypatch):
    """The guard on the fix. Re-stamping is only ever allowed to preserve a
    freshness that already held — writing a current stamp onto a projection that
    genuinely predates the ledger would assert a correspondence nobody
    established, which is worse than the staleness it hides."""
    from mindhive import events
    sc_mod = _real_sidecar()
    sc_mod.rebuild()
    events.append_event("caveat", {"text": "something else moved the ledger"})
    assert not sc_mod.read().fresh, "precondition: genuinely stale"
    enforce.hook_decision(
        _payload(tool="Bash", command="git commit --no-verify -m 'x'"))
    assert not sc_mod.read().fresh, (
        "an audit append re-stamped a sidecar that was already stale")


def test_a_concurrent_writer_leaves_the_sidecar_stale(monkeypatch):
    """The narrow race the re-stamp must not paper over. If another process
    appended in the same window, the ledger really has moved past what this
    sidecar projects, and only OUR OWN append is safe to absorb."""
    from mindhive import events
    sc_mod = _real_sidecar()
    sc_mod.rebuild()
    assert sc_mod.read().fresh, "precondition"

    real_append = events.append_event

    def append_and_race(*a, **kw):
        out = real_append(*a, **kw)
        # A second segment appears between our append and the re-stat.
        real_append("caveat", {"text": "another session"},
                    session_id="other-session")
        return out

    monkeypatch.setattr(enforce_events_module(), "append_event", append_and_race)
    enforce.hook_decision(
        _payload(tool="Bash", command="git commit --no-verify -m 'x'"))
    assert not sc_mod.read().fresh, (
        "a third party's append was absorbed into our stamp")


def enforce_events_module():
    from mindhive import events
    return events
