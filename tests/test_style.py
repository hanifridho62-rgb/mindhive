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
    "<system-reminder>be careful</system-reminder>",
    "Base directory for this skill: /x/y",
])
def test_machine_injected_text_gets_no_style(prompt):
    # A task notification has no reply style to set; spending context on one
    # every turn would be pure waste.
    assert style.block_for(prompt) == ""


@pytest.mark.parametrize("prompt", [
    "/caveman ultra", "/circle", "/lfg lanjutkan", "/mindhive build the thing",
])
def test_slash_commands_are_user_turns_and_get_the_style(prompt):
    # Regression: style used to borrow promptgate.is_machine_prompt, which
    # answers True for a leading "/". Every slash-command turn silently lost its
    # style block — a large share of this user's prompts.
    assert style.block_for(prompt) == style.CAVEMAN_BLOCK


def test_a_slash_command_can_itself_be_a_teaching_request():
    assert style.block_for("/explain the parser") == style.TEACH_BLOCK


def test_the_gates_own_predicate_is_left_alone():
    # The divergence is deliberate. Grading the phrasing of "/circle" is
    # meaningless, so the gate still skips it; only STYLE treats it as a user
    # turn. Pinning both sides makes a future "helpful" re-unification fail here.
    from mindhive import promptgate
    assert promptgate.is_machine_prompt("/circle") is True
    assert style._is_injected("/circle") is False


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


# --- sticky teaching mode -------------------------------------------------------
#
# A thorough lesson is never one turn. The memoryless matcher answered "not a
# teaching request" for the follow-up that continues it, so the lesson collapsed
# back to fragments at exactly the moment it was being extended.

def _seq(session, *prompts):
    """Drive prompts through one session in order; return the mode of each."""
    return [style.resolve_mode(p, session) for p in prompts]


def test_a_lesson_survives_indonesian_follow_ups():
    assert _seq("s", "ajarkan saya cara kerja event store",
                     "lanjutkan",
                     "terus, bagian projector") == [style.MODE_TEACH] * 3


def test_a_lesson_survives_english_follow_ups():
    assert _seq("s", "teach me how the event store works",
                     "continue",
                     "go on") == [style.MODE_TEACH] * 3


def test_a_substantive_new_question_ends_the_lesson():
    assert _seq("s", "ajarkan saya event store",
                     "perbaiki bug di dashboard") == [style.MODE_TEACH,
                                                      style.MODE_CAVEMAN]


def test_the_mode_does_not_latch_once_the_lesson_ended():
    # The failure this guards is worse than the bug it replaced: an essay for
    # every remaining turn of the session.
    _seq("s", "ajarkan saya event store", "perbaiki bug di dashboard")
    assert style.resolve_mode("lanjutkan", "s") == style.MODE_CAVEMAN


def test_sessions_do_not_share_a_mode():
    style.resolve_mode("ajarkan saya event store", "a")
    assert style.resolve_mode("lanjutkan", "b") == style.MODE_CAVEMAN


def test_a_continuation_as_the_very_first_prompt_is_not_a_lesson():
    assert style.resolve_mode("lanjutkan", "cold") == style.MODE_CAVEMAN


def test_a_long_prompt_that_merely_contains_a_marker_is_not_a_continuation():
    # "next" leading a substantive request must not extend a stale lesson.
    style.resolve_mode("ajarkan saya event store", "s")
    long_one = ("next we need to migrate the projector to the new schema and "
                "backfill every namespace before the release")
    assert style.resolve_mode(long_one, "s") == style.MODE_CAVEMAN


def test_without_a_session_id_behaviour_stays_memoryless():
    style.resolve_mode("ajarkan saya event store", None)
    assert style.resolve_mode("lanjutkan", None) == style.MODE_CAVEMAN


def test_a_corrupt_sidecar_fails_open_to_caveman():
    style.resolve_mode("ajarkan saya event store", "s")
    style._mode_path().write_text("{not json", encoding="utf-8")
    assert style.resolve_mode("lanjutkan", "s") == style.MODE_CAVEMAN


def test_resolve_mode_never_reads_the_ledger(monkeypatch):
    # R5. This runs on every prompt; a ledger read would drag the whole event
    # log onto the hook's critical path.
    from mindhive import events

    def boom(*a, **kw):
        raise AssertionError("resolve_mode read the ledger")

    monkeypatch.setattr(events, "read_all", boom)
    assert _seq("s", "ajarkan saya X", "lanjutkan") == [style.MODE_TEACH] * 2


def test_injected_text_mid_lesson_neither_ends_nor_extends_it():
    # A task notification arriving between two turns of a lesson is not the user
    # speaking, so it must not clear the mode.
    style.resolve_mode("ajarkan saya event store", "s")
    assert style.block_for("<task-notification>done</task-notification>",
                           "s") == ""
    assert style.resolve_mode("lanjutkan", "s") == style.MODE_TEACH


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


def test_cli_keeps_the_lesson_across_turns(monkeypatch, capsys):
    # The end-to-end proof of the multi-turn fix: same session_id, teach verb
    # only on the first turn, prose on all three.
    for prompt in ("ajarkan saya cara kerja event store", "lanjutkan",
                   "terus, bagian projector"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(
            json.dumps({"prompt": prompt, "session_id": "lesson-1"})))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        assert cli.main(["hook", "user_prompt_submit"]) == 0
        assert "teaching mode" in capsys.readouterr().out.lower(), prompt


def test_cli_stays_silent_for_machine_injected_text(monkeypatch, capsys):
    _run_hook(monkeypatch, "<task-notification>agent done</task-notification>")
    assert capsys.readouterr().out.strip() == ""
