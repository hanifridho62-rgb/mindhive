# tests/test_style.py
"""The standing reply-style pass: caveman on every human-typed prompt, normal
prose on the turns that ask to be taught.

Two properties carry the whole feature and are worth guarding hard: it must fire
UNCONDITIONALLY (a style that only shows up when the prompt gate also fired is
not a standing style), and its one carve-out must key on the explain/teach VERB
rather than on thoroughness words — otherwise "review ini secara menyeluruh"
silently turns a work request into a lecture."""
import io
import json
import sys

import pytest

from mindhive import __main__ as cli
from mindhive import style


def _raw(prompt):
    return json.dumps({"prompt": prompt})


# --- the default -----------------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "fix the auth middleware",
    "perbaiki middleware auth",
    "apa kabar hari ini",
    "what did we decide last week",
])
def test_caveman_is_the_default_for_any_human_prompt(prompt):
    assert style.block_for(prompt) == style.CAVEMAN_BLOCK


def test_default_block_preserves_the_users_language():
    # The property that makes the style usable for an Indonesian speaker at all.
    assert "compress the style, not the language" in style.CAVEMAN_BLOCK.lower()


def test_default_block_protects_technical_content_and_safety():
    low = style.CAVEMAN_BLOCK.lower()
    for must in ("code blocks", "error string", "security warnings",
                 "irreversible-action confirmations"):
        assert must in low, f"caveman block dropped: {must}"


@pytest.mark.parametrize("prompt", [
    "", "   ",
    "<task-notification>done</task-notification>",
    "/caveman ultra",
    "Base directory for this skill: /x/y",
])
def test_machine_injected_text_gets_no_style(prompt):
    # A task notification has no reply style to set; spending context on one
    # every turn would be pure waste.
    assert style.block_for(prompt) == ""


# --- the teaching carve-out --------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    # English
    "explain how the event chain works",
    "can you teach me this codebase",
    "walk me through the recall pipeline",
    "help me understand why the hash chain matters",
    "give me a tutorial on projections",
    "break this down for me",
    "elaborate on the dedup window",
    # Indonesian
    "jelaskan cara kerja hash chain",
    "tolong jelasin ini",
    "ajarkan saya cara pakai hive recall",
    "ajari saya bagian projector",
    "terangkan alur event store",
    "uraikan kenapa desainnya begitu",
    "saya mau penjelasan lengkap soal ledger",
    "berikan edukasi soal cara kerja graph",
])
def test_teaching_requests_switch_to_full_prose(prompt):
    assert style.wants_teaching(prompt), prompt
    assert style.block_for(prompt) == style.TEACH_BLOCK


def test_teach_block_asks_for_reasoning_not_fragments():
    low = style.TEACH_BLOCK.lower()
    assert "not\nfragments" in low or "not fragments" in low.replace("\n", " ")
    assert "own language" in low          # teaching mode is still bilingual


@pytest.mark.parametrize("prompt", [
    "review ini secara menyeluruh",
    "check this thoroughly",
    "audit the migration in detail",
    "periksa semua file dengan rinci",
    "buat laporan yang komprehensif",
])
def test_thoroughness_alone_is_not_a_teaching_request(prompt):
    # Asking for thorough WORK is not asking for a lecture. If this ever flips,
    # the user gets an essay where they wanted a review.
    assert not style.wants_teaching(prompt), prompt
    assert style.block_for(prompt) == style.CAVEMAN_BLOCK


def test_teaching_detection_ignores_machine_text():
    assert not style.wants_teaching("<reminder>explain the rules</reminder>")


def test_long_prompt_is_scanned_but_bounded():
    from mindhive.promptgate import PROMPT_SCAN_CAP
    # A teach verb inside the scanned prefix still counts...
    assert style.wants_teaching("jelaskan ini. " + "x " * 20_000)
    # ...one buried past the cap does not drag the hook into a huge scan.
    assert not style.wants_teaching("y " * PROMPT_SCAN_CAP + " jelaskan ini")


# --- hook composition ---------------------------------------------------------------

def test_extend_creates_context_when_the_gate_stayed_silent():
    # The common case: a clear prompt the gate has nothing to say about. The
    # style still has to be delivered, so this pass builds the payload itself.
    out = style.extend_hook_context({}, _raw("apa kabar"))
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert hso["additionalContext"] == style.CAVEMAN_BLOCK


def test_extend_prepends_and_keeps_existing_context():
    prior = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                    "additionalContext": "GATE SAYS SOMETHING"}}
    out = style.extend_hook_context(prior, _raw("fix the parser"))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(style.CAVEMAN_BLOCK)      # style first
    assert "GATE SAYS SOMETHING" in ctx             # nothing dropped


def test_extend_leaves_other_keys_untouched():
    prior = {"hookSpecificOutput": {"additionalContext": "x", "extra": 1},
             "topLevel": "keep"}
    out = style.extend_hook_context(prior, _raw("fix it"))
    assert out["topLevel"] == "keep"
    assert out["hookSpecificOutput"]["extra"] == 1


@pytest.mark.parametrize("raw", ["not json{{", "", json.dumps(["list"]), None])
def test_extend_is_fail_open_on_garbage(raw):
    # A style pass that can break a prompt is worse than one that says nothing.
    sentinel = {"hookSpecificOutput": {"additionalContext": "keep me"}}
    assert style.extend_hook_context(sentinel, raw) == sentinel


@pytest.mark.parametrize("prompt", [{"a": 1}, ["x"], 7, None])
def test_extend_tolerates_a_non_string_prompt(prompt):
    assert isinstance(style.extend_hook_context({}, _raw(prompt)), dict)


# --- the real CLI flow ----------------------------------------------------------------

def _run_hook(monkeypatch, prompt):
    monkeypatch.setattr(sys, "stdin", io.StringIO(_raw(prompt)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["hook", "user_prompt_submit"]) == 0


def test_cli_emits_the_style_on_a_prompt_no_other_pass_answers(monkeypatch, capsys):
    # The end-to-end proof of "setiap saat": a chatty, gate-clear, non-code
    # prompt used to produce total silence. It must now carry the style.
    _run_hook(monkeypatch, "what did we decide last week")
    out = capsys.readouterr().out
    assert "UserPromptSubmit" in out
    assert "caveman" in out
    assert "underspecified" not in out and "catalog consulted" not in out


def test_cli_swaps_in_teaching_mode_in_both_languages(monkeypatch, capsys):
    for prompt in ("explain how the ledger works",
                   "jelaskan cara kerja ledger"):
        _run_hook(monkeypatch, prompt)
        out = capsys.readouterr().out
        assert "teaching mode" in out.lower(), prompt
        assert "caveman, standing default" not in out, prompt


def test_cli_stays_silent_for_machine_injected_text(monkeypatch, capsys):
    _run_hook(monkeypatch, "<task-notification>agent done</task-notification>")
    assert capsys.readouterr().out.strip() == ""
