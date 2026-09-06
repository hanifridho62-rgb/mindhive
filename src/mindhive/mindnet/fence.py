# src/mindhive/mindnet/fence.py
"""The fenced block's defuser: every untrusted string passes through here.

Split out of `brief.py` when that module crossed the enforced 1000-line cap,
along the seam its own docstring already drew. Everything here is about ONE
string at a time and knows nothing about waves, phases, advice or stats.
`brief` composes; this module is what makes composing safe.

## Why a fence needs a defuser at all

Everything the block renders was written by people and models into the ledger,
and it reaches a `claude -p` child holding write access to a real worktree.
`inject.safe_text` stops a memo forging a new directive LINE or HEADER; it
cannot stop a well-formed imperative sentence, because that is indistinguishable
from prose. The labelled fence is what makes the boundary explicit -- so the one
thing a rendered line must never be able to look like is the fence itself.

Two rules do that, and they are deliberately different shapes:

* `_strip_fences` is the GAP rule: find the word CONTEXT, look a bounded
  distance either side for a rule run, and remove the whole banner when both
  gaps are short and unpunctuated. The bound exists to protect honest prose
  (`... this wave. Some context: the ...`).
* `_strip_terminal_rules` is the whole-LINE shape rule: a rule run at the START
  or END of a line that mentions CONTEXT is a banner whatever sits between them.
  It exists because the gap rule's bound is bypassable -- pad the forgery past
  `_BANNER_GAP` and the banner used to survive verbatim into the block AND
  report `dropped=0`, so a run under attack read as clean twice over.

The second runs only after the first has reached its fixed point. Interleaved,
the weaker pass gets there first and leaves a stub the stronger one can no
longer match; `_defuse_counted` carries the measurement.

## `dropped` and `defused` are different questions

`dropped` is "the defuser emptied this line". `defused` is the strictly wider
"the defuser changed this line at all", and it is what makes an attempt that was
merely DISARMED observable. Both ride out to the `mindnet_brief` payload.
"""
from __future__ import annotations

import re
import unicodedata

from . import inject as inj_mod

# The widths, here rather than in `brief`, because every function below takes one
# and `brief` imports them back. A constant that lives away from its only
# consumers is the kind that drifts.
MAX_LINE_CHARS = inj_mod.MAX_ADVICE_ITEM_CHARS
PHASE_CHARS = 60

# How many times `_defused` will try to reach a fixed point before giving up on a
# line entirely. Small on purpose: a line that still carries a fence marker after
# this many passes is adversarial, not unlucky, and dropping it is the only answer
# that cannot be gamed.
_DEFUSE_PASSES = 8

# What counts as the RULE in a banner: any run of these. Not just the ASCII
# hyphen — an agent reading `—— END CONTEXT ——`, `=== END CONTEXT ===` or
# `── END CONTEXT ──` treats the block as closed exactly as it would for `---`,
# and the em dash is this repo's own idiom in prose, so it is the likeliest form
# to appear rather than the least. A hyphen-only class let all three through
# (red team, 2026-08-29), which is what an earlier comment here wrongly called
# "any dashed banner".
# WORD-CATEGORY codepoints that nonetheless RENDER as a horizontal rule. This is
# the irreducible remainder of the enumeration: `\w` matches letters, and these
# ARE letters — `ㅡ` HANGUL LETTER EU is category Lo, `ー` KATAKANA-HIRAGANA
# PROLONGED SOUND MARK is Lm, `_` is Pc. A character that IS a letter and LOOKS
# like a rule cannot be identified by Unicode category, so it has to be named.
# Three entries instead of thirty, because everything category-shaped is handled
# by the class below rather than by this list.
_RULE_WORDLIKE = unicodedata.normalize(
    "NFKC", "_" + chr(0x3161) + chr(0x30FC))

# NFKC-normalized, and that is load-bearing rather than tidy. `_visible` runs NFKC
# BEFORE `_strip_fences` ever sees the text, so what reaches the rule below is the
# normalized form, not the codepoint an attacker typed. Written literally, the list
# above misses HANGUL LETTER EU entirely: U+3161 normalizes to U+1173 HANGUL
# JUNGSEONG EU, so a pattern built on U+3161 matches a character that can no longer
# be present. That was a real red test, not a hypothetical. Normalizing the SET the
# same way the TEXT is normalized makes the two agree by construction, and it also
# folds in the halfwidth and fullwidth variants for free — U+FF3F -> `_`,
# U+FF70 -> U+30FC — which is why those need no entry of their own.

# What counts as the RULE in a banner: a run of THREE OR MORE of one character
# that is not a letter, digit or space. By EFFECT, not by enumeration.
#
# The old version listed ~30 codepoints. A list is the wrong shape for this: the
# attacker picks the glyph, so every list is a list of the glyphs somebody thought
# of. Measured at the render layer, 8 of 13 rule-drawing glyphs walked straight
# through it — FULL BLOCK, BLACK RECTANGLE, both EIGHTH BLOCKs, MODIFIER LETTER
# MINUS, HANGUL LETTER EU, and two more picked at review time precisely because
# they were in no list (/autoplan, 2026-08-30).
#
# TWO or more, unchanged: a banner drawn in two em dashes is still a banner, and
# `test_a_fence_close_built_from_any_rule_glyph_cannot_end_the_block` pins that.
# The false positives a run of two invites are handled by _BANNER_GAP_RE below,
# which is a better lever than run length — raising the run to three bought prose
# safety by reopening the two-glyph hole, which is trading one leak for another.
_RULE_RE = re.compile("([^" + chr(92) + "w" + chr(92) + "s]|["
                      + re.escape(_RULE_WORDLIKE) + "])" + chr(92) + "1{1,}")
_CONTEXT_RE = re.compile(r"\bCONTEXT\b", re.IGNORECASE)

# How far either side of the word CONTEXT a rule may sit and still be one banner.
_FENCE_WINDOW = 160

# What may sit BETWEEN a rule and the word CONTEXT for the pair to read as a banner.
#
# This is the half that tells a forged fence apart from an ordinary sentence, and
# without it the rule above is a menace to honest text. Pre-fix, a constraint
# reading "the retry path -- see the note below -- reruns the wave. Some context:
# the loop breaks when nothing moved... and the ratio is 3--4x." rendered as "the
# retry path -- see the note below and the ratio is 3--4x." — a whole clause deleted
# out of the middle of a real memo, silently, because two unrelated dash runs
# happened to sit within 160 characters of the word "context". The author is never
# told; the agent just reads a sentence that no longer says what was written.
#
# A banner is `RULE  words  CONTEXT  words  RULE` with nothing between but the
# banner's own words. A sentence is not: it carries the punctuation that ends
# clauses. So the gap may hold anything EXCEPT `.,:;!?`, and at most
# `_BANNER_GAP` characters of it. Both honest examples above are rejected on the
# punctuation clause — `wave. Some ` and `context: the ` — while `--- END CONTEXT ---`,
# `-- END CONTEXT --` and `[END CONTEXT]` all pass it.
#
# Deliberately NOT a whitespace-only gap: `safe_text` collapses newlines before this
# runs, so a real fence and its surrounding prose end up on one line and "the banner
# is alone on its line" is not a property that survives to here.
_BANNER_GAP = 24
_BANNER_GAP_RE = re.compile(r"[^.,:;!?]{0,%d}\Z" % _BANNER_GAP)

# Blank-rendering characters that are NOT Unicode category `Cf`, so a category
# test alone misses them. `[mind<U+3164>net]` round-tripped through the whole
# defuser unchanged (red team, 2026-08-29) — invisible to a reader, invisible to
# `str.split`, and not the marker `inject._MARK_RE` matches.
_INVISIBLE = frozenset(
    "ᅟᅠㅤ"      # Hangul fillers (category Lo)
    "⠀"                  # braille pattern blank (So)
    "឴឵"            # Khmer inherent vowels (Mn/Cf depending on version)
    "ﾠ"                  # halfwidth hangul filler
)


def _visible(text: str) -> str:
    """*text* with the characters a reader cannot see removed, and folded to NFKC.

    Three jobs, and each closes a bypass the other two miss:

    - **Category `Cf`** — zero-width space/joiner/non-joiner, the bidi overrides,
      the soft hyphen. `[mind<ZWSP>net]` is not the marker `inject._MARK_RE`
      matches and is not whitespace `str.split` collapses, so it reached the agent
      looking exactly like the header both controls exist to forbid.
    - **`_INVISIBLE`** — the blank-rendering characters that are not `Cf` at all.
      Category alone is not the boundary; "renders as nothing" is.
    - **NFKC** — so compatibility forms fold onto the shapes the matchers know.
      Without it the fullwidth `［ｍｉｎｄｎｅｔ］` is a different string from
      `[mindnet]` to every control here.

    The trade this makes deliberately: NFKC rewrites some legitimate text
    (ligatures, superscripts), and dropping all of `Cf` drops a handful of marks
    that carry meaning in Arabic and Syriac rather than hiding anything. Both are
    accepted, because the alternative is enumerating every invisible codepoint
    and being wrong about one — and this text is going to an autonomous agent
    holding write access to a checkout."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text
                   if unicodedata.category(c) != "Cf" and c not in _INVISIBLE)


def _strip_fences(text: str) -> str:
    """*text* with every rule-CONTEXT-rule banner removed.

    Scans for the word CONTEXT and looks a bounded distance either side for a
    rule, rather than matching a rule and searching forward for CONTEXT. That
    ordering is the whole point and it is a performance FIX, not a style choice:
    the regex this replaces was `-{2,}[^\\n]{0,160}?\\bCONTEXT\\b[^\\n]{0,160}?-{2,}`,
    which backtracks quadratically on a run of rule characters with no CONTEXT in
    it — measured 6.5 s for one 2,000-character line and 54 s at
    `MAX_SYMBOL_CHARS`, in the parent process, holding the GIL, for every advice
    line of every task (red team, 2026-08-29). A hostile ledger memo was a denial
    of service against the orchestrator.

    CONTEXT is the rare literal, so anchoring on it makes the work proportional
    to how often it appears, and each side scan is capped at `_FENCE_WINDOW`."""
    out: list = []
    i, n = 0, len(text)
    while True:
        m = _CONTEXT_RE.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        left = None
        for r in _RULE_RE.finditer(text, max(i, m.start() - _FENCE_WINDOW),
                                   m.start()):
            left = r                         # the LAST rule run before CONTEXT
        right = _RULE_RE.search(text, m.end(), min(n, m.end() + _FENCE_WINDOW))
        # Both gaps must be banner-shaped. Only the NEAREST rule on each side is
        # tested and that is sufficient: an earlier left rule spans everything this
        # one does plus a rule run, which is punctuation, so it cannot pass a gap
        # test this one failed. Same argument mirrored on the right.
        if left is not None and not _BANNER_GAP_RE.match(text, left.end(),
                                                         m.start()):
            left = None
        if right is not None and not _BANNER_GAP_RE.match(text, m.end(),
                                                          right.start()):
            right = None
        if left is None or right is None:
            # A bare mention of the word, not a banner. Keep it and move past.
            out.append(text[i:m.end()])
            i = m.end()
            continue
        out.append(text[i:left.start()])
        i = right.end()


def _strip_terminal_rules(text: str) -> str:
    """*text* with a leading and/or trailing rule run collapsed to one hyphen,
    when the line mentions CONTEXT at all.

    THE GAP RULE'S BYPASS, closed. `_strip_fences` above only removes a banner
    whose rule sits within `_BANNER_GAP` (24) characters of the word CONTEXT --
    a bound that exists to stop honest prose like `... this wave. Some context:
    the ...` being eaten. Pad the forgery past that bound and the whole banner
    survived verbatim INTO the fenced block, which is the one place a line must
    not be able to look like the fence.

    A whole-LINE shape rule is what the gap rule cannot express, and it is
    available here for a reason that is not true one level down: `safe_text` has
    already collapsed the item to a single line, so "at the start of the line"
    and "at the end of the line" are real, bounded positions. A rule run in one
    of those two positions on a line that mentions CONTEXT is a banner whatever
    sits between them -- there is no honest prose that opens with `=====` and
    closes with `=====` around the word.

    Collapsed to a single hyphen rather than deleted: deletion splices the
    neighbours together, which is the second-order attack `_defused`'s docstring
    records (`[mind{FENCE_CLOSE}net]`). One hyphen cannot re-form a rule run,
    and `_RULE_RE` needs two characters to match, so this is a fixed point on
    its second pass.

    Untouched when the line does not mention CONTEXT: a `---` bullet or an
    `=== heading ===` in a real memo is not a fence and deleting it would cost
    the reader information for nothing."""
    if not _CONTEXT_RE.search(text):
        return text
    lead = _RULE_RE.match(text)
    if lead is not None and lead.end() < len(text):
        text = "-" + text[lead.end():]
    tail = None
    for r in _RULE_RE.finditer(text):
        tail = r
    if tail is not None and tail.end() == len(text) and tail.start() > 0:
        text = text[:tail.start()] + "-"
    return text


def _defused(value, width=MAX_LINE_CHARS) -> str:
    """`inject.safe_text`, plus this module's own fence markers removed.

    `safe_text` strips the `[mindnet]` marker, because that is the header a
    projection-supplied string could forge in ITS block. It knows nothing about
    this one — so a ledger memo containing the closing marker verbatim would end
    the fence early and everything after it would read to the agent as
    instruction. `test_a_forged_fence_in_the_data_cannot_close_the_real_one`
    found exactly that in the first version of this module.

    Removal is iterated, because a single pass is defeatable: deleting one
    occurrence from `--- END --- END CONTEXT --- CONTEXT ---` re-forms another.

    `safe_text` is re-applied after EVERY removal, and that ordering is the fix
    for a second-order version of the same attack (`/review`, 2026-08-29). The
    first version ran `safe_text` once, up front, and stripped fences afterwards
    — so `[mind{FENCE_CLOSE}net]` passed the marker strip untouched, because the
    marker genuinely was not there yet, and the fence removal then spliced the
    halves into a real `[mindnet]`. The pass meant to defuse the line was what
    armed it. Marker and fence have to converge together, not in sequence.

    A line that has not reached a fixed point within `_DEFUSE_PASSES` is dropped
    whole. The exhaustion case is what covers every marker form at once: the
    final text is only trusted because `safe_text` has just seen it and changed
    nothing, so a line still mutating when the budget runs out is never returned
    on the strength of a containment check.

    Three passes compose here and all three must run on every iteration, because
    each one can create what another removes: `_visible` deletes the invisibles
    that hide a marker from the other two, `safe_text` collapses whitespace and
    strips `[mindnet]`, and `_FENCE_RE` removes the fence banners case- and
    spacing-insensitively. Running any of them once, up front, is what made the
    two earlier versions of this function defeatable."""
    return _defuse_counted(value, width)[0]


# `_defuse_counted` returns a TRIPLE: (text, dropped, defused). `dropped` is the
# line the defuser emptied; `defused` is the strictly wider "the defuser changed
# this line at all", which is what makes an attempt that was merely DISARMED
# observable. A forged banner padded past `_BANNER_GAP` used to survive whole
# AND report `dropped=0`, so a run under attack reported clean twice over: the
# line rendered, and nothing counted it. Collapsing its rules fixes the first
# half; this counter is the second.


def _defuse_counted(value, width=MAX_LINE_CHARS):
    """``(text, dropped)`` — `_defused`'s implementation, plus whether it ate the
    line.

    Split out rather than widening `_defused`, which has four callers and is the
    most security-critical function in this module: changing its arity edits
    every one of them and every test that pins them. One implementation, two
    entry points, so the counted and uncounted paths cannot disagree about what
    they defused — the same shape `payoff._score` took for the same reason.

    **``dropped`` is a property of the INPUT, not of the reason.** True when a
    non-empty input produced an empty output, whatever emptied it: the
    exhaustion drop, a whole line stripped as a forged banner, a marker
    collapsing to nothing. False when the input was empty or whitespace to begin
    with — counting those would make `dropped` rise whenever a resolver returns a
    blank row, which is noise where the whole point is signal.

    Why it exists: `_section` filters falsy results, `safe_block` drops lines,
    and `Stats` counts the resolver's ROWS rather than the rendered ones. So five
    hostile lines that defuse to nothing rendered as `skills=3 chars=0` plus
    "every source resolved empty for this task" — the one observable signal that
    an injection attempt reached the composer was discarded, and the feature
    could be under active attack with every run reporting success (TODOS 25).

    ``had_input`` uses `safe_text`'s OWN coercion rather than a second opinion
    about what counts as empty; `str(value or "")` blanks every falsy value. The
    `except` answers True because `safe_text` answers ``"?"`` there — a non-empty
    rendering, which survives, so nothing was dropped."""
    try:
        had_input = bool(" ".join(str(value or "").split()))
    except (ValueError, TypeError):
        had_input = True
    first = text = _visible(inj_mod.safe_text(value, width))
    settled = False
    for _ in range(_DEFUSE_PASSES):
        cleaned = _visible(inj_mod.safe_text(_strip_fences(text), width))
        if cleaned == text:
            settled = True
            break
        text = cleaned
    # The terminal-rule pass runs AFTER the fence loop has reached its fixed
    # point, never inside it, and the ordering is load-bearing rather than
    # tidiness. `_strip_fences` removes a banner WHOLE; `_strip_terminal_rules`
    # only collapses its rules to a hyphen. Interleaved, the weaker pass gets
    # there first and leaves a stub the stronger one can no longer match:
    # `--- END --- END CONTEXT --- CONTEXT ---` settles to `""` under the fence
    # loop alone and to `- END CONTEXT -` when the collapse runs on every
    # iteration. Measured, both ways, on that exact string.
    if settled:
        for _ in range(_DEFUSE_PASSES):
            cleaned = _visible(inj_mod.safe_text(
                _strip_terminal_rules(_strip_fences(text)), width))
            if cleaned == text:
                break
            text = cleaned
        else:
            settled = False
    # BOTH containment checks, not just the fence one. A line the terminal-rule
    # pass would still change is a line still carrying a banner shape, and
    # returning it on the strength of only the older check would reopen the
    # bypass this pass exists to close.
    if (not settled or _strip_fences(text) != text
            or _strip_terminal_rules(text) != text):
        return "", had_input, had_input
    out = text.strip()
    return out, (had_input and not out), (had_input and out != first.strip())


def safe_phase(value) -> str:
    """A phase name, defused for rendering OUTSIDE the fence.

    `render` already defuses the phase it puts inside the block. `agent.prompt_for`
    puts the SAME value in the prompt's header, above the fence, where nothing
    marks it as background — and it was shipping the raw sidecar string with its
    newlines intact, which is enough to forge both a fence close and a `[mindnet]`
    directive header (`/review`, 2026-08-29, measured: 19 injected newlines).

    One function because the failure was two consumers of one untrusted value
    disagreeing about whether it needed sanitising. When they disagree, the safe
    one eventually gets deleted as redundant."""
    return _defused(value, PHASE_CHARS)


def safe_line(value) -> str:
    """A one-line ledger-supplied string, defused at the standard width.

    For the fields the prompt renders ABOVE the fence — where the surrounding
    text is machine-authored, so an injected line inherits that authority. The
    fence protects what is inside it; this is what protects what is not."""
    return _defused(value, MAX_LINE_CHARS)


def safe_block(value) -> str:
    """A MULTI-LINE ledger-supplied string, defused without being flattened.

    A task description is legitimately several lines and collapsing it to one —
    which `safe_line` would do — makes the prompt worse to read for every honest
    task in order to constrain a hostile one. So the line structure survives and
    each line is defused on its own.

    Blank lines are kept (paragraphs are meaning); lines that defuse to nothing
    are dropped, which is what an emptied-out hostile line should look like."""
    return block_counted(value)[0]


def block_counted(value):
    """``(text, dropped)`` — `safe_block`'s implementation, plus the drop count.

    A counting sibling rather than a widened `safe_block`, because that one has
    callers outside this module (`agent.prompt_for`, `decompose`,
    `resilience.defused_stderr`) and none of them wants a pair. Same trade
    `_defuse_counted` makes one level down; see its docstring for what `dropped`
    means and what it deliberately does not count."""
    try:
        raw = str(value or "")
    except (ValueError, TypeError):
        return "", False, False
    out = []
    dropped = defused = 0
    for line in raw.splitlines():
        if not line.strip():
            out.append("")
            continue
        cleaned, gone, changed = _defuse_counted(line)
        dropped += 1 if gone else 0
        defused += 1 if changed else 0
        if cleaned:
            out.append(cleaned)
    # No leading/trailing blank runs, and never more than one blank in a row —
    # a description of pure whitespace must not render as a wall of gap.
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), dropped, defused
