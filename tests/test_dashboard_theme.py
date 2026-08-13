# tests/test_dashboard_theme.py
"""The palette (T26) and the loading skeletons (T28).

Both are presentation, and presentation is where a test is easiest to write
uselessly: asserting that a stylesheet contains a string proves the string is
there, not that the interface works. So every test here asserts a RELATIONSHIP
that a human cannot hold in their head across 2,400 lines of embedded CSS — every
token used is defined in the document that uses it, no colour is written twice, no
skeleton names a shape that does not exist, every shimmer is silenced under
reduced motion. Those are exactly the properties that decay silently.

The scanner has its own canary (`test_the_scanner_would_catch_a_pasted_literal`)
because a regex that matches nothing would make this whole file pass. It also has
to NOT match HTML entities: `&#9679;` is a bullet, not a colour, and a naive
`#[0-9a-f]{3,8}` scan reports eight of them in this page.
"""
import pathlib
import re

import pytest

from mindhive import (cat_board, dashboard_page, research_page, swarm_page,
                      taskview, theme, workspace_page)

# A hex colour, and not an HTML entity (`&#9679;`) or part of an identifier.
HEX = re.compile(r"(?<![&\w])#([0-9a-fA-F]{3,8})(?![0-9a-fA-F\w-])")
VAR_USE = re.compile(r"var\(--([a-z0-9-]+)\)")
VAR_DEF = re.compile(r"^\s*--([a-z0-9-]+):", re.M)

# The shell document's own stylesheet: these two render into ONE document and are
# written against the palette, so the palette's rules are theirs.
SHELL_SOURCES = (dashboard_page, taskview)

# Deliberately outside the palette:
#   cat_board  — a fragment rendered into TWO documents (the shell and the swarm
#                page) whose palettes differ, so it cannot depend on either one's
#                tokens; and its colours reach the browser as SVG `fill=`
#                presentation attributes, which do not substitute var() in Chrome
#                or WebKit. A token there paints black.
#   the iframe pages — separate documents with their own (GitHub-ish) palettes,
#                sharing only the three status colours, which they do take from
#                `theme`. Unifying the rest would be a redesign, not a refactor.
IFRAME_SOURCES = (workspace_page, swarm_page, research_page)


def _source(module) -> str:
    return pathlib.Path(module.__file__).read_text(encoding="utf-8")


def _literals(text) -> list:
    return [m.group(1).lower() for m in HEX.finditer(text)]


def _shell_document() -> str:
    return dashboard_page.render_page("test-token",
                                      tasks_css=taskview.CARDS_CSS,
                                      tasks_js=taskview.CARDS_JS)


DOCUMENTS = {
    "shell": _shell_document,
    "parallel": lambda: workspace_page.render_parallel("test-token"),
    "swarm": lambda: swarm_page.render_swarm("test-token"),
    "research": lambda: research_page.render_research("test-token"),
}


# --- T26: the palette --------------------------------------------------------

def test_no_colour_is_written_twice_as_a_literal():
    """The whole point of the refactor, as one assertion. A value used once, for
    one decorative purpose, is clearer as a literal — a token used once adds a
    lookup and removes no duplicate. A value used TWICE is two places to change
    and two chances to drift, which is how this page came to hold `#7fb2ff` and
    `#7fb4ff` as two spellings of the link blue."""
    counts = {}
    for module in SHELL_SOURCES:
        for value in _literals(_source(module)):
            counts.setdefault(value, []).append(module.__name__.split(".")[-1])
    repeats = {v: where for v, where in counts.items() if len(where) > 1}
    assert not repeats, "colours written more than once as literals: " + ", ".join(
        f"#{v} ({len(w)}x in {', '.join(sorted(set(w)))})"
        for v, w in sorted(repeats.items()))


def test_no_token_value_is_also_written_as_a_literal():
    """A half-applied migration is worse than none: the next reader cannot tell
    which form is authoritative, and changing the token leaves the literal
    behind — a colour that moves everywhere except one rule."""
    by_value = {v.lower(): name for name, v in theme.tokens().items()}
    leaked = []
    for module in SHELL_SOURCES:
        for value in set(_literals(_source(module))):
            if "#" + value in by_value:
                leaked.append(f"#{value} (= --{by_value['#' + value]}) in "
                              f"{module.__name__}")
    assert not leaked, "token values still written as literals: " + "; ".join(leaked)


def test_the_shared_status_colours_are_literal_nowhere():
    """The three values every document agrees on. They were spelled out in five
    files, which is the only kind of duplication that produces a *visible* bug:
    a green in the swarm page that no longer matches the green in the shell."""
    shared = {v.lower() for v in theme.tokens(theme.SHARED_GROUPS).values()}
    for module in SHELL_SOURCES + IFRAME_SOURCES:
        found = shared & {"#" + v for v in _literals(_source(module))}
        assert not found, f"{module.__name__} still spells out {sorted(found)}"


def test_cat_board_stays_independent_of_every_host_palette():
    """The exception, asserted as a property rather than left as a gap in the rule
    above. `cat_board` renders into the shell AND the swarm page, and its colours
    become SVG `fill=` attributes, which do not substitute var(). It must
    therefore contain no token reference at all — the failure mode of getting this
    wrong is a board of cats with black eyes, in one document only."""
    assert not VAR_USE.search(_source(cat_board)), \
        "cat_board references a host document's tokens; SVG fill= will not resolve it"


@pytest.mark.parametrize("name", list(DOCUMENTS))
def test_each_document_defines_exactly_the_tokens_it_uses(name):
    """The strongest of these, because it runs the real render. Catches a document
    that uses a token nobody defined (every rule using it silently drops, so the
    element paints with an inherited or default colour) AND one that defines
    tokens it never uses (a palette that is a list of colours instead of a
    description of the interface)."""
    html = DOCUMENTS[name]()
    used, defined = set(VAR_USE.findall(html)), set(VAR_DEF.findall(html))
    assert not (used - defined), \
        f"{name} uses undefined tokens: {sorted(used - defined)}"
    assert not (defined - used), \
        f"{name} defines tokens it never uses: {sorted(defined - used)}"
    assert used, f"{name} resolved no tokens at all — was the palette injected?"


@pytest.mark.parametrize("name", list(DOCUMENTS))
def test_no_palette_marker_survives_into_a_response(name):
    html = DOCUMENTS[name]()
    for marker in ("/*__PALETTE__*/", "/*__STATUS__*/"):
        assert marker not in html, f"{name} shipped with {marker} unsubstituted"


def test_the_two_folded_pairs_stay_folded():
    """`#7fb4ff` and `#3a4150` were each one channel step from a value already in
    use for the same purpose, and this refactor folded them in. A canary against
    the fold coming undone by a paste — which is exactly how it happened."""
    for value in ("#7fb4ff", "#3a4150"):
        for module in SHELL_SOURCES:
            assert value not in _source(module).lower(), (
                f"{value} is back in {module.__name__}; it is one step from a "
                f"token that already means the same thing")


def test_the_scanner_would_catch_a_pasted_literal():
    """CANARY for every test above: they all rest on `HEX`, and a pattern that
    matched nothing would make the file pass while the page filled with
    duplicates. It must also leave HTML entities alone — `&#9679;` is the bullet
    in the live chip, and a naive scan calls it a colour."""
    assert _literals("a { color:#2a2f3d; border-color:#2A2F3D; }") \
        == ["2a2f3d", "2a2f3d"], "the scanner is case-sensitive or blind"
    assert _literals("<span>&#9679;</span> live &#10005;") == [], \
        "the scanner reports HTML entities as colours"
    assert _literals("#mn-cats { color:var(--ok); }") == [], \
        "the scanner reports an id selector as a colour"


# --- T28: the loading skeletons ---------------------------------------------

def _shapes() -> dict:
    """The shape names and markup, read out of the page source. Parsed rather
    than duplicated here: a copy in the test would keep passing after the page
    stopped agreeing with it."""
    body = re.search(r"const SKEL_SHAPES = \{(.+?)\n  \};",
                     _source(dashboard_page), re.S)
    assert body, "SKEL_SHAPES is no longer where this test can read it"
    # An entry's markup may be built across several lines, so entries are split on
    # where the NEXT one starts rather than on the first comma-newline.
    shapes = {}
    for entry in re.split(r"\n    (?=\w+:)", body.group(1)):
        named = re.match(r"\s*(\w+):\s*(.+)", entry, re.S)
        if named:
            shapes[named.group(1)] = named.group(2).strip().rstrip(",")
    return shapes


def test_the_skeleton_waits_only_as_long_as_a_flicker():
    """150 ms is the whole decision: long enough that a paint which was already
    instant shows nothing, short enough that it does not swallow the first sixth
    of a real 1.2-1.9 s section load — the part where the user is still deciding
    whether the click registered."""
    found = re.search(r"const SKEL_MS = (\d+)", _source(dashboard_page))
    assert found and int(found.group(1)) == 150, \
        f"SKEL_MS is {found and found.group(1)}, not the measured 150 ms"


def test_every_section_load_names_a_shape_that_exists():
    """DISCRIMINATING, and the reason `showSkeleton` may safely ignore an unknown
    name. A typo'd shape is invisible in review and in the browser — the section
    simply paints no placeholder — so it is caught HERE, statically, over every
    call site, rather than by whoever notices one panel behaving differently."""
    shapes = set(_shapes())
    assert shapes == {"cards", "rows", "block"}, shapes
    calls = re.findall(r"\brun\((?:'[^']*'|\w+\+'[^']*')\s*,.*?,\s*([^,()]+?)\);",
                       _source(dashboard_page), re.S)
    named = [c.strip() for c in calls if c.strip().startswith("'")]
    assert named, "no run() call sites found — this test stopped reading the page"
    for arg in named:
        assert arg.strip("'") in shapes, \
            f"run() asks for skeleton {arg}, which SKEL_SHAPES does not define"


def test_every_shape_paints_a_class_the_stylesheet_styles():
    """A shape whose markup names an unstyled class is an INVISIBLE skeleton: the
    div is there, it has no size and no shimmer, and the panel looks like it is
    doing nothing for the whole load."""
    css = _source(dashboard_page)
    for name, markup in _shapes().items():
        classes = set(re.findall(r'class="([a-z0-9 ]+)"', markup))
        for cls in sorted({c for group in classes for c in group.split()}):
            # Either a rule inside the skeleton (`.skel .line`, and compounds like
            # `.skel .bar.w40`), or a page class the shape reuses on purpose so it
            # inherits the real geometry — `.cards` is the grid Overview paints.
            styled = (re.search(rf"\.skel [^{{}}\n]*\.{cls}\b", css)
                      or re.search(rf"\n  \.{cls}[ ,.{{]", css))
            assert styled, f"shape {name!r} paints .{cls}, which has no rule"


def test_reduced_motion_silences_every_shimmer():
    """The guard has to name each animated class explicitly (a media query adds no
    specificity, so it cannot rely on a shorthand), which means adding a shape
    without adding it here ships motion to someone who asked for none."""
    css = _source(dashboard_page)
    guard = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.+?)\n  \}",
                      css, re.S)
    assert guard, "the reduced-motion guard is gone"
    animated = set(re.findall(r"\.skel \.(\w+)[ ,{]", css.split("@media")[0]))
    for cls in sorted(animated):
        assert f".skel .{cls}" in guard.group(1), \
            f".skel .{cls} shimmers with no reduced-motion guard"
