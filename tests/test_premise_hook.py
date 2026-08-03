# tests/test_premise_hook.py
"""The premise gate at its real surface: the UserPromptSubmit hook.

A passing `check()` unit does not prove the hook wires it. This file exercises
the decision the harness actually sees.
"""
import json

from mindhive import premise, promptgate
from mindhive import __main__ as cli_mod

from tests.test_premise import REAL_DIRECTIVE


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "mindhive").mkdir(parents=True)
    (tmp_path / "src" / "mindhive" / "advice.py").write_text("x", encoding="utf-8")
    return tmp_path


def _raw(prompt):
    return json.dumps({"prompt": prompt, "session_id": "s1"})


def _ctx(context):
    return str(((context or {}).get("hookSpecificOutput") or {})
               .get("additionalContext") or "")


def test_hook_reports_the_real_incidents_contradictions(tmp_path, monkeypatch):
    """The end-to-end regression. CANARY: this asserts the specific claim text,
    not merely that "some context was added" — another pass's injection must
    not be able to satisfy it."""
    monkeypatch.chdir(_repo(tmp_path))
    out = premise.extend_hook_context({}, _raw(REAL_DIRECTIVE))
    body = _ctx(out)
    assert premise.PREMISE_MARK in body
    assert "src/mindhive/history.py" in body
    assert "3518" in body
    assert "interrupted roadmap" in body.lower()


def test_hook_fires_past_promptgates_blind_spot(tmp_path, monkeypatch):
    """THE blind-spot test. `promptgate.assess` returns `clear` and
    `is_code_work` returns False past PROMPT_SCAN_CAP, which is why a 20KB
    directive got no scrutiny at all. This pass must not be gated behind
    either of them."""
    monkeypatch.chdir(_repo(tmp_path))
    padded = ("filler word " * 400) + REAL_DIRECTIVE
    assert len(padded) > promptgate.PROMPT_SCAN_CAP
    assert promptgate.assess(padded)["clear"] is True      # the gate says nothing
    assert promptgate.is_code_work(padded) is False        # and the catalog stays shut

    body = _ctx(premise.extend_hook_context({}, _raw(padded)))
    assert "src/mindhive/history.py" in body


def test_hook_is_silent_when_every_claim_is_true(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    ctx = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                  "additionalContext": "earlier pass"}}
    out = premise.extend_hook_context(ctx, _raw("edit src/mindhive/advice.py"))
    assert _ctx(out) == "earlier pass"


def test_hook_is_silent_when_only_unknowns(tmp_path, monkeypatch):
    """Pinned separately from the all-true case. Confirmations and unknowns
    alone say nothing; a gate that speaks on every prompt gets tuned out."""
    monkeypatch.chdir(_repo(tmp_path))
    out = premise.extend_hook_context({}, _raw("protecting a 3518-test baseline"))
    assert _ctx(out) == ""


def test_hook_preserves_an_earlier_passes_text_verbatim(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    ctx = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                  "additionalContext": "STYLE BLOCK"}}
    body = _ctx(premise.extend_hook_context(ctx, _raw(REAL_DIRECTIVE)))
    assert "STYLE BLOCK" in body
    assert premise.PREMISE_MARK in body


def test_hook_names_its_own_limits(tmp_path, monkeypatch):
    """The block must not read as a clean bill of health for the rest of the
    prompt."""
    monkeypatch.chdir(_repo(tmp_path))
    body = _ctx(premise.extend_hook_context({}, _raw(REAL_DIRECTIVE)))
    assert "not a clean bill of health" in body.lower()


def test_hook_is_total_on_malformed_payloads(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    for raw in ("", "{", "null", "[]", '{"no_prompt": 1}', None):
        assert premise.extend_hook_context({}, raw) == {}


def test_hook_is_silent_when_check_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(premise, "check", boom)
    ctx = {"hookSpecificOutput": {"additionalContext": "keep me"}}
    assert premise.extend_hook_context(ctx, _raw(REAL_DIRECTIVE)) is ctx


def test_hook_does_not_read_the_ledger_for_path_only_claims(tmp_path,
                                                            monkeypatch):
    """CANARY on the hot path. `promptgate`'s docstring records the number this
    guards: the live store is 22.8k events and `events.read_all` costs 1.09 s.
    Ordinary code prompts name files constantly, so reading the ledger for a
    path claim would put a second on most prompts to answer a question none of
    them asked. Path claims are a `Path.exists` call and nothing more."""
    monkeypatch.chdir(_repo(tmp_path))
    from mindhive import events as ev_mod

    def boom(*a, **k):
        raise AssertionError("read_all called for a path-only prompt")

    monkeypatch.setattr(ev_mod, "read_all", boom)
    premise.extend_hook_context({}, _raw("please fix src/mindhive/history.py"))


def test_hook_does_read_the_ledger_when_a_claim_needs_it(tmp_path, monkeypatch):
    """The other half: the guard must not starve the checks that DO need
    evidence."""
    monkeypatch.chdir(_repo(tmp_path))
    from mindhive import events as ev_mod
    seen = []
    monkeypatch.setattr(ev_mod, "read_all", lambda *a, **k: seen.append(1) or [])
    premise.extend_hook_context({}, _raw("resume the interrupted plan"))
    assert seen, "a roadmap claim must consult the ledger"


def test_hook_skips_machine_injected_text(tmp_path, monkeypatch):
    """Same rule promptgate uses: a task notification or an injected skill body
    is not the user asserting anything."""
    monkeypatch.chdir(_repo(tmp_path))
    for machine in ("<task-notification>src/mindhive/history.py</task-notification>",
                    "/ce-work src/mindhive/history.py",
                    "Base directory for this skill: src/mindhive/history.py"):
        assert premise.extend_hook_context({}, _raw(machine)) == {}


def test_the_hook_stage_actually_calls_the_pass(tmp_path, monkeypatch, capsys):
    """Proves the wiring in __main__, not just the function. Without the call
    site in the user_prompt_submit stage this fails while every test above
    still passes."""
    monkeypatch.chdir(_repo(tmp_path))
    monkeypatch.setattr("sys.stdin", _Stdin(_raw(REAL_DIRECTIVE)))
    cli_mod.main(["hook", "user_prompt_submit"])
    out = capsys.readouterr().out
    assert premise.PREMISE_MARK in out
    assert "src/mindhive/history.py" in out


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text

    def isatty(self):
        return False
