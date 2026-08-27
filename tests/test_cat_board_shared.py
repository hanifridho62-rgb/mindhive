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
    call joins them.

    Pinned as the PROPERTY rather than one spelling of the call. It used to assert
    `paintCats((await apiGet(...)))` inline, which broke the moment the poll grew a
    second painter — a correct change failing a test that had pinned the shape
    instead of the contract."""
    assert "paintCats" in dash
    body = dash[dash.index("async function pollCats"):]
    body = body[:body.index("}")+1]
    assert "apiGet('/api/mindnet/cats')" in body, (
        "pollCats no longer fetches the cat endpoint")
    assert "paintCats(" in body, "the fetched board never reaches paintCats"


def test_one_fetch_feeds_both_strips(dash, swarm):
    """Two answers in one payload. A second request would double the poll rate of
    a strip that already fires every 1.2 s against a full ledger read."""
    for page, label in ((dash, "dashboard"), (swarm, "swarm")):
        body = page[page.index("async function pollCats"):]
        body = body[:body.index("\n  }") + 1]
        assert body.count("/api/mindnet/cats") == 1, (
            f"the {label} page fetches the cat endpoint more than once per poll")
        assert "paintCats(" in body and "paintSessions(" in body, (
            f"the {label} page does not feed both strips from that one fetch")


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
    # Sliced at `mnPanel`, which is where the clamp lives now that both
    # strips share one panel renderer. That move is the reason this gate
    # matters MORE than it did: the session strip sits at the end of the
    # same header, so its rightmost cat opens the same panel past the same
    # edge, and it inherits the fix only by going through this function.
    body = js[js.index("function mnPanel"):]
    body = body[:body.index("function catPanel")]
    assert "clientWidth" in body, "the panel no longer clamps to the viewport width"
    assert "Math.min" in body and "Math.max" in body, (
        "the clamp needs both bounds: min keeps it inside the right edge, max keeps "
        "a cat near the left edge from pushing it off the other side")
    # Order matters: a display:none element measures zero wide, so clamping against
    # its own width has to happen after it is shown.
    assert body.index("p.style.display = 'block'") < body.index("clientWidth"), (
        "the panel is measured while still hidden — its width reads 0 and the "
        "clamp silently does nothing")


def test_both_strips_render_through_the_one_clamped_panel():
    """The clamp above is inherited, not copied. A second panel renderer is
    how the faculty cat and the session cat would start describing the same
    four fields differently -- and how one of them would quietly lose the
    viewport clamp again."""
    js = cat_board.script()
    assert js.count("function mnPanel(") == 1
    for caller in ("function catPanel(", "function sessPanel("):
        body = js[js.index(caller):]
        body = body[:body.index("}", body.index("mnPanel("))]
        assert "mnPanel(" in body, caller + " no longer uses the shared panel"
        assert "clientWidth" not in body, (
            caller + ' grew its own clamp — there must be exactly one')


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


# --- MN-19: the session strip ------------------------------------------------

def test_both_pages_carry_the_session_strip(dash, swarm):
    """The operator lives on the dashboard and commands from the swarm page. A
    signal that only reaches one of them is the gap MN-13 already closed once for
    the faculty strip."""
    for page, label in ((dash, "dashboard"), (swarm, "swarm")):
        assert 'id="mn-sessions"' in page, f"{label} has no session strip"
        assert "function paintSessions(" in page, f"{label} cannot paint it"


def test_the_shared_sheet_does_not_own_the_session_container_either():
    """Same split the faculty strip keeps: the dashboard wants an inline cluster in
    its header, the swarm page a band under it. Only the box differs, so only the
    box belongs to each page."""
    assert not re.search(r"#mn-sessions\s*\{", cat_board.CSS), (
        "cat_board.CSS declares the #mn-sessions box; that belongs to each page")


def test_a_working_session_is_distinguishable_without_motion():
    """Motion cannot be the only encoding — under `prefers-reduced-motion` it is
    gone entirely, and a row of identical pills would tell the operator nothing.
    Colour and a state dot carry it too."""
    working = cat_board.CSS[cat_board.CSS.index(".mn-sess.working {"):]
    working = working[:working.index("}")]
    assert "color:" in working, f"working state is motion-only: {working}"


def test_only_a_working_session_cat_animates():
    # The rule must actually DECLARE an animation whose name resolves to real
    # keyframes — a substring match passed for a rule that declared nothing, and
    # would pass for `mn-bob2`, which every browser silently ignores.
    m = re.search(r"\.mn-sess\.working \.mn-cat-sprite\s*\{[^}]*"
                  r"animation:\s*([A-Za-z0-9_-]+)", cat_board.CSS)
    assert m, "the working session cat declares no animation"
    declared = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)", cat_board.CSS))
    assert m.group(1) in declared, (
        f"session cat animates with '{m.group(1)}', which no @keyframes declares")
    assert ".mn-sess.working .mn-cat-sprite" in cat_board.CSS
    assert not re.search(r"\.mn-sess\s+\.mn-cat-sprite\s*\{[^}]*animation:",
                         cat_board.CSS), (
        "every session cat animates regardless of state; motion then means nothing")


def test_the_entrance_animates_only_transform_and_opacity():
    """Animating layout properties janks. The reference is explicit and the
    existing `mn-bob` already follows it."""
    frames = cat_board.CSS[cat_board.CSS.index("@keyframes mn-sess-in"):]
    frames = frames[:frames.index("}\n")]
    for banned in ("width", "height", "top:", "left:", "margin"):
        assert banned not in frames, f"the entrance animates {banned}: {frames}"
    assert "opacity" in frames and "transform" in frames


def test_the_entrance_is_in_the_small_transition_band():
    """200-300ms for a small transition — fast enough not to feel laggy, slow
    enough to read as arriving rather than popping."""
    # Find the rule that DECLARES the entrance, not the reduced-motion override
    # that also matches `.mn-sess {` and merely switches it off.
    m = re.search(r"animation:mn-sess-in (\d+)ms", cat_board.CSS)
    assert m, "no entrance animation is declared on the session pill"
    assert 200 <= int(m.group(1)) <= 300


def test_the_reduced_motion_guard_covers_the_entrance_too():
    """The entrance rides the container, not the sprite, so the sprite-keyed rule
    does not reach it. Without this a reduced-motion viewer still gets every
    session cat sliding in."""
    guard = cat_board.CSS[cat_board.CSS.index("prefers-reduced-motion"):]
    guard = guard[:guard.index("/* --- MN-19")]
    assert ".mn-sess" in guard and ".mn-cat-sprite" in guard


def test_the_bob_phase_is_desynced_per_session():
    """N cats bobbing in lockstep read as one strobing bar, not N workers — the
    over-animation fatigue the interaction-design reference warns about. Negative
    so a cat starts mid-bob rather than standing still first."""
    js = cat_board.script()
    body = js[js.index("function sessPhase("):]
    body = body[:body.index("}")]
    assert "idSeed" in body and "1600" in body
    assert "-" in body, "the phase offset is positive; cats stand still on arrival"
    assert "animationDelay" in js, "the phase is computed but never applied"


def test_the_id_derivation_is_shared_not_respelled():
    """`idSeed` feeds the palette pick, the bob phase, and the swarm page's node
    cats. Three copies of one parseInt is three places for it to drift."""
    js = cat_board.script()
    assert js.count("function idSeed(") == 1
    assert js.count("function idCat(") == 1
    # `catFor` on the swarm page must delegate rather than hold a second copy.
    from mindhive.swarm_page import render_swarm
    assert "function catFor(id){ return idCat(id); }" in render_swarm("tok")


def test_the_session_strip_does_not_rebuild_when_nothing_changed():
    """`paintSessions` rebuilds its container, and rebuilding every 1.2 s restarts
    a 1.6 s bob before it can finish — the defect already fixed for the swarm
    chips. `age_secs` is deliberately outside the signature: it ticks every poll
    and would defeat the guard entirely."""
    js = cat_board.script()
    body = js[js.index("function paintSessions("):]
    # The guard's CONSEQUENT is a block, not a bare return: an unchanged poll
    # still has to refresh the ages (the clock ticks even when the strip does
    # not), so what this pins is the comparison and the early exit, not the
    # one-line shape the guard happened to have before the clock split.
    assert "SESS_SIG" in body and "if (sig === SESS_SIG){" in body         and "return;" in body[body.index("if (sig === SESS_SIG){"):]
    sig = body[body.index("const sig ="):body.index("if (sig === SESS_SIG)")]
    assert "age_secs" not in sig, (
        "age is in the repaint signature, so the guard can never fire")


def _painters():
    """Every `paint*` function in the shared script, name -> body.

    Split on the declarations rather than brace-matched, which is enough here
    and honest about it: these are top-level `function paint…(){}` declarations
    in one generated string, so the text between two of them is exactly one
    body. A painter nested inside another would break this, and there are none.
    """
    js = cat_board.script()
    starts = [(m.start(), m.group(1))
              for m in re.finditer(r"\bfunction (paint\w+)\s*\(", js)]
    out = {}
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(js)
        out[name] = js[pos:end]
    return out


def test_every_strip_that_rebuilds_itself_guards_against_an_unchanged_poll():
    """A census, not a list: computed from the script so a FOURTH painter added
    later cannot ship without a guard.

    The rule it enforces is one this file already states twice, once per strip
    that learned it the hard way. A painter that clears its container and
    rebuilds it hands every child a new element on every poll, and a new element
    restarts its animation from 0%. `paintSessions` carries `SESS_SIG` for that
    reason and `paintInjections` carries `dataset.sig` for the same one — and
    `paintCats` carried neither for as long as nobody looked. Measured on the
    live board: a dim cat, whose hover dance is capped to a single pass, moved
    in four bursts across eight seconds, spaced at the 2.5 s poll interval.

    Two halves, because a guard can fail in both directions. It has to EXIST,
    and it has to be able to fire — a signature that folds in `age_secs`, which
    changes on every single poll, is a guard that is never once true.

    What this cannot see is whether the guard wins at runtime; the browser tier
    asserts that by node identity. This asserts that nobody forgot."""
    rebuilders = {name: body for name, body in _painters().items()
                  if re.search(r"innerHTML\s*=\s*''", body)}
    assert len(rebuilders) >= 3, (
        f"only {sorted(rebuilders)} rebuild their container — this census was "
        f"written against three and a painter it cannot see is a painter it "
        f"cannot check")

    for name, body in sorted(rebuilders.items()):
        m = re.search(r"const sig = (.+?);\n", body, re.S)
        assert m, (
            f"{name} clears and rebuilds its container with no repaint "
            f"signature, so every poll hands its cats new elements and restarts "
            f"their animations mid-pass")
        # Both operand orders, deliberately. `paintSessions` writes
        # `sig === SESS_SIG` and the two dataset-keyed painters write
        # `bar.dataset.sig === sig`; the direction of an equality is not a
        # contract, and a census that insisted on one would be testing house
        # style while claiming to test the guard.
        held = r"(?:\w+\.dataset\.sig|[A-Z_]+SIG)"
        # Both consequent shapes, for the same reason both operand orders are
        # allowed. `paintInjections` exits on a bare `return;`; the two strips
        # that also refresh their clock on a held poll run a short block and
        # return out of it. The contract is "compares, then leaves without
        # rebuilding" — a block that falls through would still reach
        # `innerHTML = ''`, so `return;` inside it is the half that matters.
        assert re.search(rf"if \(\s*(?:sig\s*===\s*{held}|{held}\s*===\s*sig)\s*\)"
                         rf"\s*(?:return;|\{{[^{{}}]*return;)", body), (
            f"{name} computes a signature and never compares it — the guard is "
            f"present in the source and absent at runtime")
        assert "age_secs" not in m.group(1), (
            f"{name}'s repaint signature includes `age_secs`, which changes on "
            f"every poll — the comparison can never be true, so the guard is "
            f"decorative and the strip still rebuilds every tick")


def test_the_strip_reports_what_it_could_not_show():
    js = cat_board.script()
    body = js[js.index("function paintSessions("):]
    assert "sessions_hidden" in body and "more" in body


# --- MN-19 / U5: the board admits what it cannot see -------------------------

def test_an_empty_strip_distinguishes_quiet_from_blind():
    """The distinction whose absence cost this repo an entire investigation: a
    checkout whose branch predated the producer rendered EXACTLY like a quiet one,
    so "nothing is running" and "I cannot know" were indistinguishable."""
    js = cat_board.script()
    body = js[js.index("function paintSessions("):]
    assert "producers.session !== true" in body, (
        "the empty state must treat a MISSING producer map as blind, not quiet — "
        "`=== false` renders the confident answer when the payload has no key at "
        "all, which is exactly when the page cannot know")
    assert "no Claude running" in body
    blind = body[body.index("producers.session !== true"):]
    blind = blind[:blind.index("} else {")]
    assert "not built into this dashboard" in blind


def test_the_blind_state_is_visually_distinct_from_the_quiet_one():
    """Same words in the same grey would defeat the point.

    This asserted `".blind" in CSS or "blind" in script()` — satisfied by the JS
    string alone, so it passed while NO stylesheet declared the class and both
    empty states rendered identically. The whole distinction shipped invisible."""
    rules = _rule_for(".blind")
    assert rules, "no stylesheet declares .blind; the two empty states look alike"
    assert "color:" in rules, f".blind carries no colour of its own: {rules}"
    quiet = _rule_for(".none")
    assert quiet and "color:" in quiet
    def _colour(block):
        import re as _re
        m = _re.search(r"color:(#[0-9a-fA-F]+)", block)
        return m.group(1).lower() if m else None
    assert _colour(rules) != _colour(quiet), (
        "blind and quiet render in the same colour")


def _rule_for(selector):
    """The declaration block of the rule that styles *selector* on BOTH strips."""
    import re as _re
    m = _re.search(r"[^{}]*" + _re.escape(selector) + r"[^{}]*\{([^}]*)\}",
                   cat_board.CSS)
    return m.group(1) if m else ""


def test_the_session_strips_own_furniture_is_actually_styled():
    """A `#mn-cats`-scoped descendant selector cannot reach `#mn-sessions`. These
    were exactly that, so the session strip's label and empty states rendered in
    default body type — the mechanism shipped with none of its visible effect."""
    for cls in (".lbl", ".none", ".blind", ".more"):
        assert "#mn-sessions " + cls in cat_board.CSS, (
            f"{cls} is never styled inside #mn-sessions")
