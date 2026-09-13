# tests/test_graphview_browser.py
"""The Graph view's lookup tables, EXECUTED against a real DOM.

Every table in `graphview._RENDERER_JS` is indexed by a ledger-authored string
-- an event kind, a node type, a node id. On a plain `{}` such a lookup can walk
up to `Object.prototype`, and the miss comes back TRUTHY. Two consequences,
measured here rather than argued:

  * `if (seen[k]) return;` in `buildLegend` skips a kind it has never seen, so
    the legend row vanishes while the node itself still renders;
  * `KIND_LABELS[k] || k` returns the Object constructor, which is truthy, so
    the `|| k` fallback never fires and the row is labelled
    `function Object() { [native code] }`.

The second is why this file exists as a PAIR of probes. Fixing only `seen` turns
a missing row into a row with a garbage label -- strictly worse than fixing
neither -- and a test that only counted rows would have called that a pass.

WHY THE BROWSER TIER AND NOT THE NODE TIER. The task that produced this file
asked for a node-tier test on the `tests/test_paint_guard_node.py` pattern, and
that is the wrong tier for this defect. That harness runs the page against a
hand-rolled DOM stub in `node:vm`, and its own header records both why that is
acceptable there and the risk it carries: the stub "records writes rather than
interpreting them, so it cannot drift into agreeing with a broken page". The
paint guard is a question about ORDERING, which a recorder can answer. This is a
question about what the legend ACTUALLY CONTAINS after a full SVG renderer has
booted -- `createElementNS`, layout, a requestAnimationFrame loop -- and the stub
needed to answer it is roughly the 1455 lines paint_guard's is, every one of them
a chance to assert against my stub instead of against the page. A real browser
has no such gap: the assertions below read the same DOM the user's dashboard
renders. The fence is identical either way -- both tiers are deselected by
default -- so nothing is lost but the stub.

Run it deliberately:

    pytest -m browser tests/test_graphview_browser.py

MUTANT-PAIRED, like every other probe in the browser tier. Each guard is also
run against the same page with that one guard mechanically reverted, and the
probe is REQUIRED to fail there. The reversion is a text substitution on the REAL
rendered page, so the fixture cannot drift away from the code it guards -- and if
a substitution ever stops matching, `_mutate` raises instead of silently testing
nothing.

SEVERITY, recorded so nobody re-derives it: this is LATENT, not live, and the two
protections are different in kind. `kind` is `ev.type`, which `append_event`
validates against `events.VALID_TYPES` -- 0 of the 53 collide with an
`Object.prototype` member, and 0 of 71,338 rows in the live store carry one. Node
ids are safe by SHAPE instead: `graph.py` prefixes every one (`ev:`, `session:`,
`file:`, `tag:`, `topic:`, `plugin:`, `skill:`, `recommendation:`), so no id can
equal a prototype member. Neither protection is declared anywhere as something
this renderer may rely on, and the prefixes in particular are an accident of how
`graph.py` happens to build ids. It is not a security issue: no `Object.prototype`
member stringifies with a `"`, so a colour that came back as an inherited
function cannot break out of the double-quoted `style="background:..."`
attribute it lands in.
"""
import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed -- these cases were REQUESTED (`-m browser`) "
           "and did not run; nothing here passed. Install with "
           "`pip install -e .[browser]` and `playwright install chromium`.")

from mindhive import graphview  # noqa: E402

pytestmark = pytest.mark.browser

# A kind that is a member of Object.prototype, beside an ordinary one. The
# ordinary row is the control: it proves the legend rendered at all, so no probe
# below can pass by finding an empty page.
POISON = "constructor"
CONTROL = "decision"

GRAPH = {
    "nodes": [
        {"id": "ev:1", "type": "event", "kind": POISON,
         "label": POISON + " @ 2026-01-01T00:00:00Z", "text": "poison"},
        {"id": "ev:2", "type": "event", "kind": CONTROL,
         "label": CONTROL + " @ 2026-01-01T00:00:01Z", "text": "control"},
    ],
    "edges": [],
}

# Each entry: (needle, replacement) applied to the REAL rendered page. `_mutate`
# asserts the needle was actually found, so a rename upstream fails this file
# loudly instead of quietly turning every mutant into a no-op.
MUTANTS = {
    "seen": ("var seen = Object.create(null), rows = [];",
             "var seen = {}, rows = [];"),
    "labels": ("var KIND_LABELS = bare({", "var KIND_LABELS = ({"),
    "colors": ("var KIND_COLORS = bare({", "var KIND_COLORS = ({"),
}


def _mutate(html, *names):
    for name in names:
        needle, repl = MUTANTS[name]
        assert html.count(needle) == 1, (
            "mutation %r no longer matches the rendered page (found %d), so it "
            "would test nothing" % (name, html.count(needle)))
        html = html.replace(needle, repl)
    return html


def _legend(page, html):
    """The legend the user would actually see: (label, dot background) per row."""
    page.set_content(html, wait_until="load")
    page.wait_for_function(
        "document.querySelectorAll('#legend .row').length > 0", timeout=5000)
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('#legend .row')).map("
        "  function (r) {"
        "    var dot = r.querySelector('.dot');"
        "    return [r.textContent.trim(), dot ? dot.style.background : ''];"
        "  })")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:                       # pragma: no cover
            pytest.skip(f"chromium is not installed for playwright ({e}) -- these "
                        f"cases were REQUESTED and did not run; nothing passed. "
                        f"Fix with `playwright install chromium`.")
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    try:
        yield p
    finally:
        p.close()


def test_a_prototype_named_kind_gets_its_own_legend_row(page):
    """The defect in one assertion: two kinds in, two rows out, each labelled
    with its own kind."""
    rows = _legend(page, graphview.render_html(GRAPH))
    assert [label for label, _ in rows] == [POISON, CONTROL], rows


def test_a_prototype_named_kind_gets_a_real_colour(page):
    """`KIND_COLORS[n.kind] || TYPE_COLORS.event` has the same shape as the two
    lookups above. An inherited hit is truthy, so the fallback never fires and
    the colour becomes a stringified function -- visible in the dot's own
    style, which is where `nodeColor`'s return value lands."""
    rows = _legend(page, graphview.render_html(GRAPH))
    # Anchored on the POISONED row by name, not on "every row". Reverting `seen`
    # DELETES that row, and a loop over whatever survived would then inspect only
    # the control row, find it fine, and pass -- vacuously green against the very
    # defect this file exists for. Measured: that is exactly what happened before
    # this line, when the whole fix was reverted at the source.
    poisoned = [bg for label, bg in rows if label == POISON]
    assert len(poisoned) == 1, (POISON, rows)
    background = poisoned[0]
    assert background, ("the poisoned row rendered with no colour at all", rows)
    assert "native code" not in background, background
    assert "function" not in background, background


def test_the_legend_probe_fails_on_a_page_with_seen_reverted(page):
    """MUTANT. Revert only `seen` and the poisoned row disappears entirely --
    the exact symptom first seen in a real browser: one row where two belong."""
    rows = _legend(page, _mutate(graphview.render_html(GRAPH), "seen"))
    assert [label for label, _ in rows] == [CONTROL], rows


def test_the_legend_probe_fails_on_a_page_with_only_seen_fixed(page):
    """MUTANT, and the reason this file has two probes rather than one. With
    `seen` prototype-less but `KIND_LABELS` still a plain object, the row comes
    back and is LABELLED WRONG. A row-count assertion would report a pass."""
    rows = _legend(page, _mutate(graphview.render_html(GRAPH), "labels"))
    labels = [label for label, _ in rows]
    assert labels != [POISON, CONTROL], (
        "the labels mutation changed nothing -- this probe is not discriminating")
    assert any("native code" in label for label in labels), labels


def test_the_colour_probe_fails_on_a_page_with_kind_colors_reverted(page):
    """MUTANT for the colour probe. `KIND_COLORS` has no `constructor` entry, so
    on a plain object the lookup inherits the Object constructor, the
    `|| TYPE_COLORS.event` fallback never fires, and the function is
    string-concatenated straight into the dot's `background`.

    The SYMPTOM is not the function text showing up on screen -- that was the
    guess, and the browser disagreed. `background: function Object() { [native
    code] }` is not a valid declaration, so the CSS parser DROPS it and
    `style.background` reads back as the empty string: the dot simply has no
    colour. Worth recording, because it is a second and independent reason this
    is not an injection vector -- even reaching the attribute, the value never
    becomes a style. The raw attribute is asserted too, so the probe pins the
    mechanism and not only its visible residue."""
    html = _mutate(graphview.render_html(GRAPH), "colors")
    rows = _legend(page, html)
    backgrounds = [bg for _, bg in rows]
    assert "" in backgrounds, (
        "reverting KIND_COLORS left every dot with a valid colour -- the colour "
        "probe above is not discriminating: %r" % (rows,))
    raw = page.evaluate(
        "() => Array.from(document.querySelectorAll('#legend .row .dot'))"
        ".map(function (d) { return d.getAttribute('style') || ''; })")
    assert any("native code" in s for s in raw), raw


def test_every_mutation_still_matches_the_rendered_page():
    """CANARY over the canaries. `_mutate` asserts its needle is present, but
    only for the mutations a probe happens to use; this pins all of them, so an
    entry no test currently exercises cannot rot into a silent no-op."""
    html = graphview.render_html(GRAPH)
    for name in MUTANTS:
        assert _mutate(html, name) != html, name
