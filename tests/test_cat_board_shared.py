# tests/test_cat_board_shared.py
"""MN-13 — the cat board reaches the MAIN dashboard, from ONE implementation.

The founding ask put a cat on every running MindNetwork section so an operator can
see "apa saja yang menyala dan apa saja yang tidak menyala". It shipped on the swarm
page only — a page the operator rarely opens — so the signal was missing from the one
they actually watch.

The fix has a failure mode worse than the gap: paste the strip into the second page
and you get two copies of the palettes, sprites and labels, drifting silently until
one page shows six cats and the other seven, with nothing to say which is lying.
These tests pin the fix AND the single copy, because only the second is at risk from
a future edit that "just adds a colour here".
"""
import json
import re

import pytest

from mindhive import cat_board
from mindhive.dashboard_page import render_page
from mindhive.mindnet import telemetry as tel
from mindhive.swarm_page import render_swarm


@pytest.fixture(scope="module")
def dash():
    return render_page("tok")


@pytest.fixture(scope="module")
def swarm():
    return render_swarm("tok")


# --- the board is actually on the main dashboard ---------------------------

def test_the_main_dashboard_renders_the_cat_strip(dash):
    """The gap this unit closes. Before it, `mn-cat` appeared zero times in the
    dashboard page — the cats existed only on a page the user does not live on."""
    assert 'id="mn-cats"' in dash


def test_the_main_dashboard_carries_the_metadata_panel(dash):
    """Clicking a cat is how you find out WHICH task a faculty is stuck on. A strip
    without the panel is decoration."""
    assert 'id="mn-cat-panel"' in dash


def test_the_main_dashboard_polls_the_cat_endpoint(dash):
    assert "/api/mindnet/cats" in dash


def test_the_dashboard_board_paints_what_it_polls(dash):
    """The canary for the three above: markup plus an endpoint proves nothing if no
    call joins them."""
    assert "paintCats" in dash
    assert re.search(r"paintCats\(\s*\(await apiGet\('/api/mindnet/cats'\)\)", dash)


def test_the_dashboard_board_does_not_wait_on_a_section_being_live(dash):
    """`refreshLive` returns early when no section registered a liveFn, when the
    visible section is not refreshable, and while a manual operation runs. The board
    exists to be readable exactly then, so it must not be driven by that timer."""
    assert "setInterval(pollCats" in dash, (
        "the cat board needs its own timer; hanging it off refreshLive would blank "
        "it during the manual operations it is meant to report on")


def test_the_dashboard_board_paints_once_before_the_first_interval(dash):
    """A 2.5-second wait for the first paint would make a freshly-opened dashboard
    look like nothing is running."""
    assert re.search(r"showOverview\(\);\s*pollCats\(\);", dash)


def test_the_dashboard_board_is_scoped_to_the_project_picker(dash):
    """It calls apiGet, which routes through withProj — so switching project in the
    picker switches whose faculties you are watching. A raw fetch would silently
    show the default project's cats under another project's heading."""
    assert "apiGet('/api/mindnet/cats')" in dash


# --- exactly one implementation -------------------------------------------

# Definition syntax, not bare identifiers: `CAT_LABEL` is legitimately READ four
# times inside the board, and `catFor` in the swarm page legitimately reads
# `CAT_PALETTES.length` for its node cats. Counting references would fail on correct
# code — a first draft of these tests did exactly that.
CAT_DEFS = ["const CAT_GRID =", "const CAT_PALETTES =", "const CAT_LABEL =",
            "const FACULTY_ORDER =", "function catSVG(", "function catAge(",
            "function catPanel(", "function paintCats(", "function facultyCat("]


@pytest.mark.parametrize("definition", CAT_DEFS)
def test_each_page_carries_exactly_one_definition_of_each_piece(definition, dash,
                                                               swarm):
    """Invariant 10 applied to the board. Two definitions in one page is a bug the
    browser resolves by last-wins; two definitions across two pages is a bug nobody
    sees until the pages disagree."""
    for page, label in ((dash, "dashboard"), (swarm, "swarm")):
        got = page.count(definition)
        assert got == 1, (
            f"'{definition}' appears {got}x in the {label} page, expected 1")


@pytest.mark.parametrize("definition", CAT_DEFS)
def test_no_page_source_file_defines_the_board_itself(definition):
    """The real guard: the definitions must live in cat_board.py, not in either
    page module. A page that grows its own copy would still satisfy the count test
    above while re-creating the duplication this unit removed."""
    import pathlib
    root = pathlib.Path(cat_board.__file__).parent
    for mod in ("dashboard_page.py", "swarm_page.py"):
        body = (root / mod).read_text(encoding="utf-8")
        assert definition not in body, (
            f"{mod} defines '{definition}' itself — the board's one home is "
            f"cat_board.py")


def test_both_pages_render_the_same_board_code(dash, swarm):
    """Not merely 'one definition each' — the SAME definition. Two pages could each
    hold a single distinct copy and still drift."""
    js = cat_board.script()
    for page, label in ((dash, "dashboard"), (swarm, "swarm")):
        assert js in page, f"the {label} page does not carry cat_board.script() verbatim"


def test_the_swarm_page_still_uses_the_shared_palettes_for_its_node_cats(swarm):
    """The swarm page draws cats on agent nodes too, from the same table at a
    different size. That consumer must read the shared table, not a private one."""
    assert "% CAT_PALETTES.length" in swarm
    assert "catSVG(catFor(a.id), 30)" in swarm


# --- the shared module's own invariants ------------------------------------

def test_there_is_at_least_one_palette_per_faculty():
    """Fewer palettes than faculties means two faculties render identically, which
    defeats a board whose entire purpose is telling them apart."""
    assert len(cat_board.CAT_PALETTES) >= len(tel.FACULTIES)


def test_every_palette_is_visually_distinct():
    triples = [(p["b"].lower(), p["p"].lower(), p["e"].lower())
               for p in cat_board.CAT_PALETTES]
    assert len(set(triples)) == len(triples), f"duplicate palettes: {triples}"


def test_every_faculty_has_a_human_label():
    """An unlabelled cat renders its raw faculty id above its head. That is not
    wrong, but the ask was words a person reads — so the legend must be complete."""
    missing = [f for f in tel.FACULTIES if f not in cat_board.CAT_LABEL]
    assert not missing, f"faculties with no label: {missing}"


def test_the_faculty_order_is_injected_not_restated():
    """The one legend. A hand-written faculty list in JavaScript is a place for the
    order to drift, and the drift direction is 'the new faculty gets index -1'."""
    assert json.dumps(list(tel.FACULTIES)) in cat_board.script()
    assert "__FACULTY_ORDER__" not in cat_board.script(), "placeholder left unfilled"


def test_the_shared_css_does_not_own_the_container_box():
    """The two pages place the strip differently — a full-width band under the swarm
    header, an inline cluster in the dashboard header. If the shared sheet declared
    `#mn-cats` itself, each page would need an override, which is the duplication
    again with extra steps."""
    assert not re.search(r"#mn-cats\s*\{", cat_board.CSS), (
        "cat_board.CSS declares the #mn-cats box; that belongs to each page")


def test_the_label_sits_above_the_head():
    """The original ask was a cat "yang diatas kepala nya bertulisan Skill and
    plugin" — words above the head, not beside it. `column-reverse` on a flex column
    is what puts the sprite below the caption while keeping the caption first in the
    DOM for a screen reader."""
    rule = cat_board.CSS[cat_board.CSS.index(".mn-cat {"):]
    rule = rule[:rule.index("}")]
    assert "column-reverse" in rule, f"the caption is not above the sprite: {rule}"


def test_every_cat_sprite_carries_the_shared_motion_hook():
    """`catSVG` is the ONE place a cat is drawn, on every page and at every size, so
    the class it stamps is the only hook guaranteed to be on all of them. The
    reduced-motion guard below keys on it; without the stamp that guard would silently
    cover nothing."""
    js = cat_board.script()
    body = js[js.index("function catSVG"):]
    body = body[:body.index("function catAge")]
    assert 'class="mn-cat-sprite"' in body, (
        "catSVG no longer stamps the shared sprite class; every rule keyed on it "
        "silently stops matching")


def test_the_board_honours_a_reduced_motion_preference():
    """Motion here is a signal, not decoration — which is exactly why it must be
    droppable. A user who has asked their OS for less motion still gets the colour,
    the dim band and the panel; they just do not get the bob."""
    assert "prefers-reduced-motion: reduce" in cat_board.CSS
    guard = cat_board.CSS[cat_board.CSS.index("prefers-reduced-motion"):]
    guard = guard[:guard.index("#mn-cat-panel")]
    assert ".mn-cat-sprite" in guard and "animation:none" in guard, (
        f"the reduced-motion block does not actually stop the motion: {guard}")
    assert "!important" in guard, (
        "the rules that switch each animation ON are more specific than this one, "
        "and a media query adds no specificity — without !important the guard loses")


def test_the_reduced_motion_guard_is_page_independent():
    """It lives in the shared sheet precisely because it is the one motion rule both
    cat systems need. Naming a page's own furniture here would make it a page rule in
    the wrong file — and would leave the OTHER page's cats still moving."""
    guard = cat_board.CSS[cat_board.CSS.index("prefers-reduced-motion"):]
    guard = guard[:guard.index("#mn-cat-panel")]
    assert ".chip" not in guard and "#mn-cats" not in guard, (
        f"the shared guard names a page-specific selector: {guard}")


def test_the_reduced_motion_guard_reaches_both_pages(dash, swarm):
    """The canary for the two above: a guard that is correct in `cat_board.CSS` and
    absent from a rendered page protects nobody. Same shape as the strip tests — prove
    it arrives, not merely that it was written."""
    for page, label in ((dash, "dashboard"), (swarm, "swarm")):
        assert "prefers-reduced-motion: reduce" in page, (
            f"the {label} page ships cats with no reduced-motion guard")


def test_a_dim_cat_is_visibly_dim():
    """The dim band IS the hang signal. The sprites are per-faculty coloured, so
    opacity alone leaves a stalled cat looking alive."""
    assert "filter:grayscale(100%)" in cat_board.CSS


def test_the_board_does_not_respell_the_threshold_numbers():
    """`ui_cats.board` decides every band server-side and ships the thresholds in
    the payload. A number restated here could disagree with the tests that pin it."""
    from mindhive.mindnet import ui_cats as ui
    js = cat_board.script()
    for n in (ui.HIDE_AFTER_SECS, ui.DIM_AFTER_SECS, ui.IDLE_VANISH_SECS):
        assert str(n) not in js, f"threshold {n} is hardcoded in the board's JS"


def test_the_panel_is_clamped_into_the_viewport():
    """A real regression, found by opening the page rather than by a unit test.

    Left-aligning the panel to its cat is fine on the swarm page, where the strip
    starts at the left edge. The dashboard puts the strip at the END of its header,
    so the rightmost cats opened a 210px panel past the window and the metadata —
    which task is stuck — was unreadable for exactly the faculties at the end of the
    legend.

    This pins the shape, not the geometry: no string comparison can see a rectangle
    leave the window, so the actual proof was measuring all seven panels in a
    browser. What this test defends is that the clamp is not quietly deleted."""
    js = cat_board.script()
    body = js[js.index("function catPanel"):]
    body = body[:body.index("function paintCats")]
    assert "clientWidth" in body, "the panel no longer clamps to the viewport width"
    assert "Math.min" in body and "Math.max" in body, (
        "the clamp needs both bounds: min keeps it inside the right edge, max keeps "
        "a cat near the left edge from pushing it off the other side")
    # Order matters: a display:none element measures zero wide, so clamping against
    # its own width has to happen after it is shown.
    assert body.index("p.style.display = 'block'") < body.index("clientWidth"), (
        "the panel is measured while still hidden — its width reads 0 and the "
        "clamp silently does nothing")


def test_a_page_without_the_strip_does_not_crash_the_board():
    """`paintCats` is injected wherever the script is. A page that carries the script
    but not the markup must paint nothing rather than throw — a TypeError here would
    surface as the CALLER's poll dying, which is a hard bug to trace back."""
    js = cat_board.script()
    head = js[js.index("function paintCats"):]
    head = head[:head.index("bar.innerHTML")]
    assert "if (!bar) return;" in head, (
        "paintCats dereferences #mn-cats without checking it exists")


# --- the pages the user already relies on still work ----------------------

def test_the_existing_dashboard_hooks_survive(dash):
    """CLAUDE.md records a real incident: a served-code change made the whole Oversee
    section vanish from this page, and from the user's side it looked like their work
    had been deleted. Adding a header element is exactly the kind of edit that could
    do it again, so the sections they rely on are pinned here."""
    for hook in ("showOversee", "renderOversee", "showOverview", "showMemory",
                 "showSessions", "showImprovements", "showLog", "showSops",
                 "loadProjects", "toggleLive", "refreshLive"):
        assert hook in dash, f"the dashboard lost '{hook}'"


def test_no_substitution_marker_survives_into_either_page(dash, swarm):
    """A marker left in the output is a fragment that silently did not render."""
    for page, label in ((dash, "dashboard"), (swarm, "swarm")):
        for marker in ("__CAT_CSS__", "__CAT_HTML__", "__CAT_JS__",
                       "__TOKEN__", "__FACULTIES__", "__FACULTY_ORDER__"):
            assert marker not in page, f"{label} page still contains {marker}"


def test_the_token_is_substituted_after_the_board(dash, swarm):
    """Order matters: the board fragments are page text, and substituting them after
    the token would let a crafted token place its own marker and inject the board's
    HTML into an attribute. Verified by giving the token a marker's exact text and
    checking it survives as data."""
    hostile = "/*__CAT_JS__*/"
    for render in (render_page, render_swarm):
        page = render(hostile)
        assert json.dumps(hostile) in page or hostile in json.dumps(hostile), (
            f"{render.__name__} did not carry the token through as a JSON string")
        # One board, not two: the marker inside the token must not have been
        # expanded into a second copy of the script.
        assert page.count("function paintCats") == 1, (
            f"{render.__name__} expanded a marker supplied via the token")
