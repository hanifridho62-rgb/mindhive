# tests/test_rotation_hook.py
"""Contract for the Stop-hook decision — unit U5.

Two things this file exists to prevent.

**A wedged machine.** This hook is registered GLOBALLY: it fires at the end of
every agent turn in every Claude Code session on this box. A hook that raises,
or that blocks, does not break one feature — it breaks all work everywhere. So
fail-open is tested directly, with an injected exception, not assumed from a
try/except being visible in the source.

**The inverted mechanism.** `{"decision": "block"}` on a Stop hook means "do NOT
stop, keep working" — the opposite of rotating. The originating request proposed
exactly that. `test_over_threshold_never_emits_a_block_decision` is the guard.
"""
import json

import pytest

from mindhive.rotation import artifact, hook


# --- fixture builders ------------------------------------------------------

def _transcript(tmp_path, *, input_tokens=2, cache_creation=4826,
                cache_read=198992, model="claude-opus-5", sidechain=False):
    p = tmp_path / "transcript.jsonl"
    p.write_text(json.dumps({
        "type": "assistant", "isSidechain": sidechain,
        "message": {"model": model, "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": 1,
        }},
    }) + "\n", encoding="utf-8")
    return p


def _payload(transcript, **overrides):
    body = {
        "session_id": "sess-1",
        "transcript_path": str(transcript),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
    body.update(overrides)
    return json.dumps(body)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("MINDHIVE_ROTATION", "1")


# --- the firing path -------------------------------------------------------

def test_over_threshold_fires_and_writes_the_artifact(tmp_path, enabled, monkeypatch):
    # Opus 5 threshold at ratio 0.5 is 500_000; force a low cap to cross it.
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    decision = hook.hook_decision(_payload(_transcript(tmp_path)))
    assert decision is not None
    assert decision["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert decision["hookSpecificOutput"]["additionalContext"]
    saved = artifact.read()
    assert saved["occupancy"] == 203820
    assert saved["threshold"] == 1000
    assert saved["session_id"] == "sess-1"


def test_over_threshold_never_emits_a_block_decision(tmp_path, enabled, monkeypatch):
    """DISCRIMINATING. For a Stop hook, decision:block means 'do not stop,
    continue the conversation' — it would prevent rotation, not cause it. The
    originating design proposed emitting exactly that."""
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    decision = hook.hook_decision(_payload(_transcript(tmp_path)))
    assert "decision" not in decision
    assert "block" not in json.dumps(decision).lower()


def test_under_threshold_is_silent_and_writes_nothing(tmp_path, enabled):
    small = _transcript(tmp_path, input_tokens=1, cache_creation=1, cache_read=1)
    assert hook.hook_decision(_payload(small)) is None
    assert artifact.read() is None


# --- the suppression rules, each with the canary that arms it --------------

def test_disabled_by_default_even_when_over_threshold(tmp_path, monkeypatch):
    """MINDHIVE_ROTATION unset. Registration and activation are separate steps
    because a global Stop hook that misbehaves takes every session with it."""
    monkeypatch.delenv("MINDHIVE_ROTATION", raising=False)
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    assert hook.hook_decision(_payload(_transcript(tmp_path))) is None
    assert artifact.read() is None


def test_canary_the_same_payload_fires_when_enabled(tmp_path, monkeypatch):
    """CANARY for the test above: proves the payload it declared silent is one
    the hook CAN speak on, so the negative is armed rather than vacuous."""
    monkeypatch.setenv("MINDHIVE_ROTATION", "1")
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    assert hook.hook_decision(_payload(_transcript(tmp_path))) is not None


def test_stop_hook_active_short_circuits(tmp_path, enabled, monkeypatch):
    """Another hook already blocked this stop. Re-entering would compound it."""
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    raw = _payload(_transcript(tmp_path), stop_hook_active=True)
    assert hook.hook_decision(raw) is None
    assert artifact.read() is None


def test_subagent_stop_is_ignored(tmp_path, enabled, monkeypatch):
    """`agent_id` is present only inside a subagent. A subagent has its own
    context window, so its Stop must never rotate the parent session."""
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    raw = _payload(_transcript(tmp_path), agent_id="agt-1")
    assert hook.hook_decision(raw) is None
    assert artifact.read() is None


def test_unknown_occupancy_is_silent(tmp_path, enabled, monkeypatch):
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    raw = _payload(tmp_path / "does-not-exist.jsonl")
    assert hook.hook_decision(raw) is None
    assert artifact.read() is None


# --- totality --------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "not json", "[1,2,3]", "null", '{"a":'])
def test_malformed_stdin_is_silent_not_a_raise(raw, enabled):
    assert hook.hook_decision(raw) is None


def test_fail_open_when_a_dependency_raises(tmp_path, enabled, monkeypatch):
    """CANARY-STYLE: inject a failure at the module seam and prove the hook
    still returns None. Fail-open proven, not inferred from reading the source."""
    def boom(*_a, **_k):
        raise RuntimeError("transcript subsystem exploded")

    monkeypatch.setattr(hook.occupancy, "read_occupancy", boom)
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    assert hook.hook_decision(_payload(_transcript(tmp_path))) is None


def test_fail_open_when_the_artifact_write_raises(tmp_path, enabled, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(hook.artifact, "write", boom)
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    assert hook.hook_decision(_payload(_transcript(tmp_path))) is None


# --- configuration ---------------------------------------------------------

def test_ratio_is_overridable_by_env(tmp_path, enabled, monkeypatch):
    # 203820 tokens against a 1M window: 0.1 -> 100_000 (over), 0.5 -> 500_000 (under).
    t = _transcript(tmp_path)
    monkeypatch.setenv("MINDHIVE_ROTATION_RATIO", "0.1")
    assert hook.hook_decision(_payload(t)) is not None
    artifact.clear()
    monkeypatch.setenv("MINDHIVE_ROTATION_RATIO", "0.5")
    assert hook.hook_decision(_payload(t)) is None


def test_garbage_env_values_fall_back_to_defaults(tmp_path, enabled, monkeypatch):
    monkeypatch.setenv("MINDHIVE_ROTATION_RATIO", "banana")
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "banana")
    # Default ratio 0.5 x 1M capped at 500k; 203820 is under, so: silence, no crash.
    assert hook.hook_decision(_payload(_transcript(tmp_path))) is None


@pytest.mark.parametrize("value,fires", [
    ("1", True), ("true", True), ("on", True), ("yes", True),
    ("0", False), ("off", False), ("", False),
])
def test_enable_flag_accepts_the_usual_spellings(tmp_path, monkeypatch, value, fires):
    monkeypatch.setenv("MINDHIVE_ROTATION", value)
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    got = hook.hook_decision(_payload(_transcript(tmp_path)))
    assert (got is not None) is fires


# --- CLI wiring ------------------------------------------------------------

def test_cli_hook_stop_exits_zero_when_firing(tmp_path, monkeypatch, capsys):
    from mindhive import __main__ as cli_mod
    monkeypatch.setenv("MINDHIVE_ROTATION", "1")
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    monkeypatch.setattr("sys.stdin", _FakeStdin(_payload(_transcript(tmp_path))))
    assert cli_mod.main(["hook", "stop"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["hookSpecificOutput"]["hookEventName"] == "Stop"


def test_cli_hook_stop_prints_nothing_when_silent(tmp_path, monkeypatch, capsys):
    from mindhive import __main__ as cli_mod
    monkeypatch.delenv("MINDHIVE_ROTATION", raising=False)
    monkeypatch.setattr("sys.stdin", _FakeStdin(_payload(_transcript(tmp_path))))
    assert cli_mod.main(["hook", "stop"]) == 0
    assert capsys.readouterr().out.strip() == ""


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def isatty(self):
        return False

    def read(self):
        return self._text
