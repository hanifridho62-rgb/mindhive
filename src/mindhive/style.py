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
opposite instruction — and keeps it. The carve-out is STICKY: "menyeluruh" means
a lesson that spans turns, and a follow-up that only says "lanjutkan" carries no
teach verb, so per-turn detection collapsed the lesson at exactly the moment it
was being extended. `resolve_mode` holds it open and ends it on the next
substantive question.

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

from . import paths
from .promptgate import PROMPT_SCAN_CAP

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
    f"{STYLE_MARK} Reply style — teaching mode.\n"
    "The user asked to be taught or given a full explanation, so the standing "
    "caveman style is OFF. Write complete, ordinary prose: full sentences, the "
    "reasoning shown, worked examples, and the why behind each step — not "
    "fragments.\n"
    "This holds for the whole lesson, including follow-ups that only say keep "
    "going. Caveman returns on the next substantive question.\n"
    "Still reply in the user's own language."
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


def _is_injected(text) -> bool:
    """True for text the user did not type: harness tags and injected skill
    bodies.

    Deliberately a near-duplicate of `promptgate.is_machine_prompt` rather than a
    call to it, and the one difference is the entire point. That predicate
    answers True for a leading `/`, which is right for the prompt-quality GATE —
    grading the phrasing of `/circle` is meaningless — and wrong for STYLE: a
    slash command is the user typing, and the reply still owes them the standing
    style.

    Do not re-unify the two. Style borrowed the gate's predicate once, and every
    slash-command turn silently lost its style block; `test_style.py` pins the
    difference so a future merge fails loudly instead of quietly."""
    s = str(text or "").lstrip()
    if not s:
        return True
    if s.startswith("<"):                       # harness/system tags
        return True
    return s[:40].lower().startswith("base directory for this skill:")


def wants_teaching(text) -> bool:
    """True when the prompt asks to be taught or given a full explanation.

    Injected text — harness tags and injected skill bodies — answers False: it is
    not the user asking for anything. A slash command is NOT injected text here
    (see `_is_injected`); `/explain the parser` is a teaching request like any
    other. Past `PROMPT_SCAN_CAP` only the leading slice is scanned, so a pasted
    100KB log cannot turn this into a heavy regex sweep on the hook's critical
    path."""
    if _is_injected(text):
        return False
    low = " ".join(str(text or "").lower().split())[:PROMPT_SCAN_CAP]
    return any(rx.search(low) for rx in _TEACH_RES)


MODE_CAVEMAN = "caveman"
MODE_TEACH = "teach"

# A continuation prompt LEADS with a continuation marker and is short. Both
# halves carry weight: the `^` anchor keeps "...and then refactor the parser"
# from reading as a continuation, and the length bound keeps a substantive
# request that merely opens with "next" from reviving a lesson the user left.
CONTINUATION_MAX_WORDS = 6

_CONTINUATION_RES = (
    re.compile(r"^(?:lanjut|lanjutkan|teruskan|terus|lagi|berikutnya|"
               r"selanjutnya|sambung|sambungkan)\b"),
    re.compile(r"^(?:continue|carry on|go on|keep going|next|more|"
               r"and then|then|go ahead)\b"),
)


def is_continuation(text) -> bool:
    """True for a short prompt whose whole content is "keep going".

    Bounded by `PROMPT_SCAN_CAP` like `wants_teaching`, but by BAILING rather
    than truncating, and the difference is not stylistic. A teach verb can sit
    legitimately inside a long prompt, so that one scans a leading slice; a
    continuation is capped at `CONTINUATION_MAX_WORDS` by definition, so nothing
    past the cap can be one and none of it is worth normalizing. The expensive
    part below is the whole-string `lower().split()`, not the regexes — without
    this bail a pasted 100KB log gets swept on the hook's critical path.

    The one input this reclassifies is a marker followed by a single
    multi-kilobyte token, which now reads as caveman. Forgetting a lesson is the
    safe failure here; inventing one is not."""
    if _is_injected(text):
        return False
    t = str(text or "")
    if len(t) > PROMPT_SCAN_CAP:
        return False
    low = " ".join(t.lower().split())
    if len(low.split()) > CONTINUATION_MAX_WORDS:
        return False
    return any(rx.search(low) for rx in _CONTINUATION_RES)


def _mode_path(project=None):
    """Per-session reply-mode sidecar.

    Mode is derived, ephemeral, per-session state, so it lives in an
    overwrite-in-place JSON sidecar and never in the ledger — the same call
    `plugins._catalog_path` makes, for the same reason. The ledger is
    append-only and hash-chained; a row per prompt would bloat the chain
    forever with nothing prunable, since pruning would break the chain.

    Self-pruning in normal use: an entry exists only while a session sits
    mid-lesson, and the next substantive prompt deletes it. Only a session
    abandoned mid-lesson leaves one behind."""
    return paths.project_dir(project) / "style_mode.json"


def _read_modes(project=None) -> dict:
    """The sidecar as a dict, or `{}` for missing, unreadable, or corrupt.

    Forgetting the mode is always a safe failure: the user gets the standing
    caveman style, which is the default they asked for anyway."""
    try:
        p = _mode_path(project)
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                       # noqa: BLE001
        return {}


def _write_modes(modes, project=None) -> None:
    """Overwrite the sidecar with *modes*. Never raises.

    KNOWN RACE, accepted rather than fixed. `write_text_atomic` makes each write
    atomic, but the surrounding read → mutate → write in `resolve_mode` is not:
    the whole file is one JSON object keyed by session, so two sessions that
    write near-simultaneously each save a dict built from a snapshot taken
    before the other's write, and the later write drops the earlier session's
    entry.

    Blast radius is one session losing its live lesson and dropping back to
    caveman until the next teach verb — the same failure `resolve_mode` already
    treats as safe, reached by a rarer route. Fixing it properly needs per-file
    locking or a compare-and-swap retry around the read/write pair, which is a
    design change; a bare retry here would not help, since the lost update is
    invisible to the writer that caused it."""
    try:
        paths.write_text_atomic(_mode_path(project), json.dumps(modes))
    except Exception:                                       # noqa: BLE001
        pass                            # a style pass never breaks a prompt


def resolve_mode(text, session_id=None, project=None) -> str:
    """The reply mode for this turn — `MODE_TEACH` or `MODE_CAVEMAN`.

    A thorough lesson is never one turn, so teaching mode is sticky. Three
    transitions, checked in order:

    1. a teach/explain verb starts (or restarts) a lesson;
    2. a continuation prompt inside a live lesson keeps it alive;
    3. anything else ends it.

    Rule 3 is what stops the mode latching for the rest of the session: it
    expires on the next real question, with no timeout to tune and no exit
    command for the user to remember. Without a `session_id` there is nowhere
    to keep state, so behaviour degrades to the memoryless original rather than
    sharing one global mode across every session.

    Reads and writes only the sidecar — never the ledger. This runs on every
    prompt, which is also why rule 2 checks the already-loaded sidecar BEFORE
    scanning the text: outside a live lesson the scan cannot change the answer,
    and outside a live lesson is nearly every prompt."""
    if wants_teaching(text):
        if session_id:
            modes = _read_modes(project)
            modes[str(session_id)] = MODE_TEACH
            _write_modes(modes, project)
        return MODE_TEACH
    if not session_id:
        return MODE_CAVEMAN
    key = str(session_id)
    modes = _read_modes(project)
    if modes.get(key) == MODE_TEACH and is_continuation(text):
        return MODE_TEACH
    if key in modes:                                        # the lesson is over
        modes.pop(key)
        _write_modes(modes, project)
    return MODE_CAVEMAN


def block_for(text, session_id=None, project=None) -> str:
    """The style directive for one prompt: teaching mode, or the caveman default.

    Empty string for machine-injected text — a task notification has no reply
    style to set, and spending context on one every time would be pure waste.
    Injected text also short-circuits before `resolve_mode`, so a notification
    arriving mid-lesson neither ends nor extends it."""
    if _is_injected(text):
        return ""
    return (TEACH_BLOCK if resolve_mode(text, session_id, project) == MODE_TEACH
            else CAVEMAN_BLOCK)


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
        block = block_for(payload.get("prompt", ""),
                          payload.get("session_id"), project)
        if not block:
            return context
        hso = (context or {}).get("hookSpecificOutput") or {}
        existing = str(hso.get("additionalContext") or "")
        merged = f"{block}\n\n{existing}" if existing else block
        return dict(context or {}, hookSpecificOutput=dict(
            hso, hookEventName="UserPromptSubmit", additionalContext=merged))
    except Exception:                                       # noqa: BLE001
        return context
