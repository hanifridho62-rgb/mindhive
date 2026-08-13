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

from mindhive import outcomes
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


@pytest.fixture
def unsupervised(monkeypatch):
    """Rotation on, nobody waiting — a user who switched the feature on by hand.

    Explicit rather than assumed: the marker is inherited, so a test suite run
    from inside `hive session run` would otherwise get the supervised advice and
    every assertion below would silently invert."""
    monkeypatch.delenv(hook.SUPERVISED_ENV, raising=False)


@pytest.fixture
def supervised(monkeypatch):
    monkeypatch.setenv(hook.SUPERVISED_ENV, "1")


def _advice(decision) -> str:
    return decision["hookSpecificOutput"]["additionalContext"]


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


# --- what the advice is allowed to claim -----------------------------------
#
# The advice is the only part of this feature a human or an agent ever reads, so
# each sentence in it is a promise the feature has to keep. "A successor session
# is queued" used to be unconditional, and it is false in every session that
# merely has rotation switched on: the artifact is written and the process that
# would read it does not exist.

def test_supervised_advice_names_who_is_waiting(tmp_path, enabled, supervised,
                                               monkeypatch):
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    advice = _advice(hook.hook_decision(_payload(_transcript(tmp_path))))
    assert "hive session run" in advice
    assert "waiting on this session" in advice
    assert "successor" in advice


def test_unsupervised_advice_does_not_promise_a_successor(tmp_path, enabled,
                                                          unsupervised,
                                                          monkeypatch):
    """DISCRIMINATING. Same crossing, same artifact, no supervisor — so the one
    thing the message must not do is describe what a supervisor would do next."""
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    advice = _advice(hook.hook_decision(_payload(_transcript(tmp_path))))
    assert "Nothing is queued" in advice
    assert "no successor starts" in advice
    assert "A successor session is queued" not in advice


def test_unsupervised_advice_names_the_file_it_actually_wrote(tmp_path, enabled,
                                                              unsupervised,
                                                              monkeypatch):
    """The honest remainder has to be checkable: the path in the message is the
    path that was written, so the reader can go and look rather than take
    "recorded" on trust. Rotation is still RECORDED unsupervised — only the
    claim about a successor changes."""
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    advice = _advice(hook.hook_decision(_payload(_transcript(tmp_path))))
    assert str(artifact.path()) in advice
    assert artifact.read() is not None
    assert "hive session run" in advice          # the command that would queue one


def test_the_supervised_marker_alone_does_not_switch_the_hook_on(tmp_path,
                                                                 monkeypatch):
    """Opt-in is the property that makes a global Stop hook safe, and adding a
    second variable is exactly how one would leak away. The marker chooses WORDS;
    `MINDHIVE_ROTATION` alone decides whether there are any."""
    monkeypatch.delenv("MINDHIVE_ROTATION", raising=False)
    monkeypatch.setenv(hook.SUPERVISED_ENV, "1")
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    assert hook.hook_decision(_payload(_transcript(tmp_path))) is None
    assert artifact.read() is None


def test_canary_the_same_payload_fires_once_rotation_is_also_on(tmp_path,
                                                                monkeypatch):
    """CANARY for the test above."""
    monkeypatch.setenv("MINDHIVE_ROTATION", "1")
    monkeypatch.setenv(hook.SUPERVISED_ENV, "1")
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    assert hook.hook_decision(_payload(_transcript(tmp_path))) is not None


@pytest.mark.parametrize("marker", ["1", ""])
def test_neither_advice_mode_emits_a_block_decision(tmp_path, enabled,
                                                    monkeypatch, marker):
    """The inverted-mechanism guard, now that there are two messages. A branch
    is exactly where the forbidden decision would get reintroduced on one side
    only."""
    monkeypatch.setenv(hook.SUPERVISED_ENV, marker)
    monkeypatch.setenv("MINDHIVE_ROTATION_CAP", "1000")
    decision = hook.hook_decision(_payload(_transcript(tmp_path)))
    assert "decision" not in decision
    assert "block" not in json.dumps(decision).lower()


@pytest.mark.parametrize("value,is_supervised", [
    ("1", True), ("true", True), ("on", True), ("yes", True),
    ("0", False), ("off", False), ("", False),
])
def test_supervised_reads_the_same_spellings_as_enabled(value, is_supervised):
    assert hook.supervised({hook.SUPERVISED_ENV: value}) is is_supervised


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


# --- the outcomes-registry probe -----------------------------------------------

def test_probe_reports_silence_as_the_honest_zero(monkeypatch):
    """Rotation off is a success, not a fault. CLAUDE.md keeps registration and
    activation separate on purpose, so a probe that called the silent hook
    degraded would be reporting the design as a defect."""
    monkeypatch.delenv("MINDHIVE_ROTATION", raising=False)
    report = hook._probe()
    assert report.outcome == outcomes.NOTHING_TO_DO
    assert report.worked
    assert "hive session run" in report.summary


def test_probe_distinguishes_supervised_from_unsupervised(monkeypatch):
    """The two modes must not report identically — that conflation is the whole
    reason the supervised marker exists."""
    monkeypatch.setenv("MINDHIVE_ROTATION", "1")
    monkeypatch.setenv(hook.SUPERVISED_ENV, "1")
    supervised = hook._probe()
    monkeypatch.delenv(hook.SUPERVISED_ENV)
    unsupervised = hook._probe()
    assert supervised.outcome == outcomes.DID_WORK
    assert unsupervised.outcome == outcomes.DID_WORK
    assert supervised.summary != unsupervised.summary
    assert "under a supervisor" in supervised.summary
    assert "nothing is queued" in unsupervised.summary


def test_probe_is_degraded_when_the_two_advices_collapse_into_one(monkeypatch):
    """The regression this surface is here to catch: one text for both modes means
    one of the two sessions is told something untrue. Checked ahead of the enabled
    gate, because a collapsed advisory is broken whether or not it is switched on
    in the process running the probe."""
    monkeypatch.delenv("MINDHIVE_ROTATION", raising=False)
    monkeypatch.setattr(hook, "_ADVICE_UNSUPERVISED", hook._ADVICE_SUPERVISED)
    report = hook._probe()
    assert report.outcome == outcomes.DEGRADED
    assert not report.worked
    assert "same text" in report.summary


def test_the_probe_registers_on_import():
    """Two module-level lines run in every session on this machine. If the
    registration ever stops happening, this says so rather than the hook quietly
    dropping out of the registry."""
    surface = outcomes.get("rotation-advice")
    assert surface is not None
    assert surface.probe is hook._probe


def test_the_probe_never_reads_the_transcript_or_writes_an_artifact(tmp_path,
                                                                   monkeypatch):
    """A probe runs on every suite run. This one must not touch the two things the
    hook itself touches, or the registry would be writing rotation state as a side
    effect of being measured."""
    monkeypatch.setenv("MINDHIVE_ROTATION", "1")
    monkeypatch.setattr(hook.occupancy, "read_occupancy",
                        lambda *a, **k: pytest.fail("probe read the transcript"))
    monkeypatch.setattr(hook.artifact, "write",
                        lambda *a, **k: pytest.fail("probe wrote an artifact"))
    assert hook._probe().outcome in outcomes.SELF_REPORTABLE
