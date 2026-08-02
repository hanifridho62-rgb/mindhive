# src/mindhive/style.py
"""The standing reply-style pass: caveman by default, normal prose when the user
asks to be taught.

Why this exists as a hook and not just a skill. The vendored `caveman` skill
opens with "ACTIVE EVERY RESPONSE. No revert after many turns" — an instruction
read once, at session start, about a behaviour that has to hold at turn 80. That
is precisely the instruction that decays: later context crowds out earlier
context, and the style quietly reverts to normal prose. Upstream ships a
per-prompt hook for exactly this reason. This module is that hook in MindHive's
own idiom, re-asserting the style on every human-typed prompt so the last turn
of a session reads like the first.

It rides the same `extend_hook_context(context, raw)` seam that MN-1's phase
spine and `enrich` use, and for the same stated reason: the reach is
UNCONDITIONAL, so it sits outside the prompt-gate's decision instead of riding
it. `promptgate.hook_context` stays a conditional gate whose silence is a
correct answer; this pass stays unconditional. Fusing the two would make "the
gate found nothing" indistinguishable from "the hook said nothing".

## The one carve-out

The user's own standing exception, verbatim: "kecuali saya ingin anda
menjelaskan dan mengajarkan kepada saya dengan menyeluruh". Terse fragments are
the wrong shape for a lesson, so a prompt that asks to be taught gets the
opposite instruction for that turn only.

Detection keys on the explain/teach VERB, never on thoroughness words alone.
"review ini secara menyeluruh" asks for thorough WORK, not a thorough lecture;
flipping style there would answer a request the user did not make. Words like
`menyeluruh` / `thoroughly` therefore modify the carve-out, they do not trigger
it. Anything the verb list misses is still covered in prose by the skill body —
the matcher is the deterministic floor, not the whole rule.

Bilingual for the same reason `promptgate._CODE_INTENT_RES` is: this user writes
prompts in Indonesian and English interchangeably, and an English-only matcher
would answer "not a teaching request" for most of them.
"""
from __future__ import annotations

import json
import re

from .promptgate import PROMPT_SCAN_CAP, is_machine_prompt

# Marker so an injected style block is greppable in a transcript and tellable
# apart from the gate's and the spine's blocks.
STYLE_MARK = "◆"

CAVEMAN_BLOCK = (
    f"{STYLE_MARK} Reply style — caveman, standing default.\n"
    "Answer terse like a smart caveman: drop articles, filler, pleasantries, "
    "hedging. Fragments fine. Short synonyms. No tool-call narration, no "
    "decorative tables or emoji.\n"
    "Reply in the user's own language — compress the style, not the language. "
    "Indonesian question gets an Indonesian caveman answer, never a switch to "
    "English.\n"
    "Verbatim always: code blocks, CLI commands, file paths, identifiers, error "
    "strings. Never invent abbreviations. Never name or announce the style.\n"
    "Drop it — full prose — for security warnings, irreversible-action "
    "confirmations, and anywhere compression would leave step order ambiguous. "
    "Resume straight after."
)

TEACH_BLOCK = (
    f"{STYLE_MARK} Reply style — teaching mode, THIS turn only.\n"
    "This prompt asks to be taught or given a full explanation, so the standing "
    "caveman style is OFF here. Write complete, ordinary prose: full sentences, "
    "the reasoning shown, worked examples, and the why behind each step — not "
    "fragments.\n"
    "Still reply in the user's own language. Caveman resumes on the next turn "
    "that does not ask to be taught."
)

# The teach/explain VERB list. Multiword English phrases live in their own
# pattern because `\b`-joined alternation over spaces reads badly.
_TEACH_RES = (
    re.compile(r"\b(?:explain|explanation|explanatory|elaborate|teach|teaches|"
               r"teaching|taught|tutorial|lesson|educate)\b"),
    re.compile(r"walk\s+me\s+through|help\s+me\s+understand|deep[\s-]?dive|"
               r"break\s+(?:it|this|that)\s+down"),
    # Indonesian. Root + the common prefixed/suffixed and colloquial forms, since
    # this user types `jelasin` as readily as `jelaskan`.
    re.compile(r"\b(?:jelaskan|jelasin|dijelaskan|menjelaskan|penjelasan|"
               r"terangkan|jabarkan|uraikan|ajarkan|ajari|ajarin|mengajarkan|"
               r"diajarkan|pengajaran|pelajaran|edukasi)\b"),
)


def wants_teaching(text) -> bool:
    """True when the prompt asks to be taught or given a full explanation.

    Machine-injected text (task notifications, slash-command envelopes, injected
    skill bodies) answers False for the same reason the prompt gate skips it: it
    is not the user asking for anything. Past `PROMPT_SCAN_CAP` only the leading
    slice is scanned, so a pasted 100KB log cannot turn this into a heavy regex
    sweep on the hook's critical path."""
    if is_machine_prompt(text):
        return False
    low = " ".join(str(text or "").lower().split())[:PROMPT_SCAN_CAP]
    return any(rx.search(low) for rx in _TEACH_RES)


def block_for(text) -> str:
    """The style directive for one prompt: teaching mode, or the caveman default.

    Empty string for machine-injected text — a task notification has no reply
    style to set, and spending context on one every time would be pure waste."""
    if is_machine_prompt(text):
        return ""
    return TEACH_BLOCK if wants_teaching(text) else CAVEMAN_BLOCK


def extend_hook_context(context, raw, project=None) -> dict:
    """Prepend the standing style directive to a UserPromptSubmit context.

    Unlike `enrich.extend_hook_context`, this one CREATES the context when the
    gate stayed silent — a clear, well-phrased prompt is the common case and it
    still needs the style. Prepended rather than appended so the style survives
    any later truncation of a long injected block.

    Fail-open in the project's house style: any error returns `context`
    unchanged, because a style pass that can break a prompt is far worse than
    one that occasionally says nothing."""
    try:
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return context
        block = block_for(payload.get("prompt", ""))
        if not block:
            return context
        hso = (context or {}).get("hookSpecificOutput") or {}
        existing = str(hso.get("additionalContext") or "")
        merged = f"{block}\n\n{existing}" if existing else block
        return dict(context or {}, hookSpecificOutput=dict(
            hso, hookEventName="UserPromptSubmit", additionalContext=merged))
    except Exception:                                       # noqa: BLE001
        return context
