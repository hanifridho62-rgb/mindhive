# SPDX-License-Identifier: Apache-2.0
# src/mindhive/theme.py
"""The dashboard's colour palette, defined once.

Before this module the shell document carried 194 hex literals, 152 of them
repeats of a value used somewhere else — `#2a2f3d` alone appeared 19 times across
two files. Nothing was wrong with any single one; the cost was that no colour had
a *name*, so "the border grey" was a value you had to recognise, changing it meant
finding every copy, and two near-identical values could drift apart unnoticed.
Two pairs already had: `#7fb2ff` and `#7fb4ff` (one channel step apart) were both
"the link blue", and `#3a4054` and `#3a4150` were both "an indicator that is off".

**A token is a name for a decision, not a place to put every colour.** A value
used once, in one rule, for one decorative purpose is clearer as a literal — a
token used once adds a lookup without removing a duplicate. So the rule this
module is held to (and `tests/test_dashboard_theme.py` enforces) is narrow:

1. No hex value may appear more than once as a literal in the styled surface.
   Repeats become tokens; single uses may stay literal.
2. No token's value may also appear as a literal, so a migration cannot be
   half-applied and leave the next reader unsure which form is authoritative.
3. Every token is referenced at least once — a palette with dead entries is a
   list of colours, not a description of the interface.

Some single-use values are still tokens here, deliberately: they complete a
FAMILY. The surface scale (`--bg` → `--inset`) and the change-log category scale
are read as scales, and a scale with a hole in it is worse than either all-tokens
or all-literals — the reader cannot tell whether the gap is a decision.

`--text-strong` and `--paper` hold the same value today and are still two tokens:
one is the brightest text in a dark interface, the other is the paper the graph
iframe draws on. They coincide; they are not the same decision. `--dim` and
`--muted` coincide the same way since blueprint K12 lifted the furniture grey
to AA: the tier survives as a name, so a later retune can part them again
without touching a rule.

Every text token clears WCAG AA for normal text (4.5:1) on `--surface` and on
`--bg`, measured by `contrast_ratio` below. `tests/test_theme_contrast.py`
holds every token to that threshold and pins, to two places, each ratio a
document quotes (6.87 and 7.22 for `--dim`, 4.51 and 4.60 for `--dimmest`);
the MEB's flat palette is `BLUEPRINT_PALETTE`, a view of the same values.

The iframe pages (`workspace_page`, `swarm_page`, `research_page`) are separate
DOCUMENTS, so custom properties do not reach them from the shell — a `:root` in
the parent is invisible inside an iframe. Their own palettes are their own
(GitHub-ish greys, deliberately distinct), but the three STATUS colours are the
same three everywhere, which is exactly the kind of value that drifts. They get
`STATUS_CSS`, built from the same constants as the full palette.
"""

# {group: {token name (without the leading --): (value, what it is for)}}
# Grouped because the groups are the reason the names make sense; the rendered
# CSS keeps them, so the palette reads as an interface description.
PALETTE = {
    "surfaces": {
        "bg": ("#12151c", "the page behind everything"),
        "surface": ("#171a23", "the header band"),
        "rail": ("#141722", "the nav rail"),
        "sunken": ("#0c0e14", "recessed: inputs, cards, pre#out, list rows"),
        "raised": ("#1d2230", "a control face: nav buttons, pills, mini buttons"),
        "inset": ("#141824", "a panel inside a panel: roadmap, enrichment"),
        "hover": ("#262d3d", "a control under the pointer"),
        "hover-tint": ("#1b2540", "a chip or legend row under the pointer"),
    },
    "lines": {
        "line": ("#2a2f3d", "every border in the shell"),
        "dot-idle": ("#3a4054", "an indicator that is off (header dot, status dot)"),
    },
    "text": {
        "text": ("#e2e8f0", "body text"),
        "text-strong": ("#fff", "the brightest text: a count inside a pill"),
        "field-text": ("#dfe6f3", "text the user typed, in an input or textarea"),
        "muted": ("#9aa3b8", "labels and secondary lines"),
        "faint": ("#8a93a6", "present but inactive: dismissed, pending, duplicate"),
        # K12: was #6b7488, 3.71:1 on --surface -- under AA for text.
        "dim": ("#9aa3b8", "furniture: group headings, timestamps"),
        # K12: was #555c6e, 2.60:1 on --surface and 2.65:1 on the --inset it is
        # drawn on. The same grey at a higher lightness -- hue and saturation
        # kept -- the darkest one that clears 4.5:1 on both, and still under
        # `faint`: a skipped step reads quieter than a pending one beside it.
        "dimmest": ("#798297", "skipped — the quietest thing still rendered"),
    },
    # The three that every document agrees on — see SHARED_GROUPS.
    "status": {
        "ok": ("#46c46a", "done, online, resolved"),
        "warn": ("#e0b341", "working, watched, stale"),
        "bad": ("#ff8a8a", "failed, unresolved, an error message"),
    },
    # Refinements of the three above that only the shell needs. Kept in their own
    # group so the shared block stays exactly the shared decisions: a document
    # that declares tokens it never uses is telling its reader something untrue
    # about what it draws.
    "indicators": {
        "ok-edge": ("#2a5", "the border of an ok-tinted control"),
        "ok-strong": ("#3fb950", "a saturated ok, for a dot that must read at 11px"),
        "bad-strong": ("#f85149", "a saturated bad, same reason"),
    },
    "accents": {
        "link": ("#7fb2ff", "a file path, a name that can be clicked"),
        "link-bright": ("#9ecbff", "a skill chip, which sits on a tinted ground"),
        "accent-edge": ("#3d6", "the outline of something switched on"),
    },
    "skeleton": {
        "shimmer-base": ("#161a25", "the resting tone of a loading placeholder"),
        "shimmer-sheen": ("#20263a", "the band that travels across it"),
    },
    "graph": {
        "paper": ("#fff", "the graph iframe's own background, a light document"),
    },
    # The change-log categories are a DATA-driven scale: `logBadge` maps an event
    # payload's category onto one of these, so a missing entry is a badge with no
    # colour. Complete on purpose, including the values used once.
    "categories": {
        "cat-update": ("#4a90d9", "update"),
        "cat-minor": ("#6aa84f", "minor_change"),
        "cat-deletion": ("#d94a4a", "deletion"),
        "cat-reduction": ("#d9a44a", "reduction"),
        "cat-major": ("#b04ad9", "major_change"),
        "cat-replacement": ("#4ad9c8", "replacement"),
        "cat-other": ("#888", "a category this build does not know"),
    },
    # The MEB palette (blueprint K12), token by token: each lands with the first
    # rule that draws it, because a declared-but-unused token fails the audit.
    "blueprint": {
        "accent": ("#3182ce", "the context-usage fill and initiative flags (MEB accent)"),
        "border": ("#282d3d", "the line between the sections and the commands"),
        "success": ("#38a169", "a run that completed, on an Oversee card"),
        "warning": ("#dd6b20", "a tripped circuit breaker, on an Oversee card"),
        "danger": ("#e53e3e", "a run a CI check failed, on an Oversee card"),
    },
}

# The groups every document needs, whether or not it is the shell.
SHARED_GROUPS = ("status",)


def tokens(groups=None) -> dict:
    """{token name: value} for *groups* (default: all). Flat, because a caller
    that wants to look up one colour should not have to know its group."""
    names = PALETTE.keys() if groups is None else groups
    out = {}
    for group in names:
        for name, (value, _why) in PALETTE[group].items():
            out[name] = value
    return out


def render(groups=None, indent="  ") -> str:
    """A `:root` block for *groups*, one token per line with its purpose.

    The comments are the point of shipping this to the browser rather than
    inlining the values: the palette is the first thing anyone reads in the page
    source, and a list of hex values would teach them nothing."""
    names = list(PALETTE.keys() if groups is None else groups)
    decls = {group: [(f"--{name}: {value};", why)
                     for name, (value, why) in PALETTE[group].items()]
             for group in names}
    width = max((len(decl) for rows in decls.values() for decl, _ in rows),
                default=0)
    lines = [f"{indent}:root {{", f"{indent}  color-scheme: dark;"]
    for group in names:
        lines.append(f"{indent}  /* {group} */")
        for decl, why in decls[group]:
            lines.append(f"{indent}  {decl.ljust(width)}  /* {why} */")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


# The shell document's palette (dashboard_page + cat_board + taskview all render
# into it), and the status-only block for the iframe documents.
PALETTE_CSS = render()
STATUS_CSS = render(SHARED_GROUPS)

# The MEB's flat palette (blueprint K12), verbatim and in its order. A VIEW of
# `PALETTE`, never a second copy: `tests/test_theme_contrast.py` holds it to
# the MEB's values and to what `tokens()` resolves.
BLUEPRINT_PALETTE = {name: tokens()[name] for name in (
    "bg", "surface", "border", "text", "dim", "accent", "success", "warning", "danger")}


def relative_luminance(hex_colour: str) -> float:
    """WCAG 2.x relative luminance of `#rgb` or `#rrggbb`, 0.0 (black) to
    1.0 (white). Anything else -- an alpha channel included, which no ratio
    can answer without a backdrop, and a value that is not a str at all -- is
    a ValueError."""
    if not isinstance(hex_colour, str):
        raise ValueError(f"not a #rgb or #rrggbb colour: {hex_colour!r}")
    digits = hex_colour[1:] if hex_colour.startswith("#") else ""
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) != 6 or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise ValueError(f"not a #rgb or #rrggbb colour: {hex_colour!r}")
    lin = []
    for i in (0, 2, 4):
        c = int(digits[i:i + 2], 16) / 255
        lin.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio, lighter over darker: 1.0 to 21.0, whichever
    colour is named first. AA wants 4.5 for text and 3.0 for other marks."""
    a, b = relative_luminance(fg), relative_luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)
