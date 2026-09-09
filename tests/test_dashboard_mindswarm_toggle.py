# tests/test_dashboard_mindswarm_toggle.py
"""PR-5(b) — the MindSwarm door's soft-deprecation toggle.

The owner's recorded decision for both non-primary doors was "code kept,
reversible; a notice and a toggle, no removal". This file encodes the *and no
removal* half, which is the half a toggle silently breaks: it is easy to write a
flag that hides a button and equally easy, in the same commit, to delete the
route behind it. So every test here that asserts the button is GONE has a
sibling asserting the machinery behind it is still there.

The other property under test is that the flag is read PER REQUEST rather than
frozen into a module-level constant. Note what that does NOT buy, because the
first version of this file claimed it did: a running dashboard's environment is
fixed at launch, so an operator cannot flip the variable in a shell and reload.
The value has to be in the environment the process was STARTED with. What the
per-request read buys is that nothing is captured at import -- which happens
before argv is parsed -- so a future flag or an in-process setter can still
reach it, and the tests below can flip it against one live server.

`tests/conftest.py`'s autouse `isolate_store` keeps every request in this file
on a throwaway namespace.
"""
import pathlib
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, dashboard_page

SWARM_NAV = 'data-sec="ws-swarm"'


@pytest.fixture
def swarm_server():
    """One dashboard server for the whole test, so a flag flip between two
    requests is measured against the SAME process — which is the thing the
    per-request read has to be proven on."""
    token = "swarm-toggle-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _page(base) -> str:
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert r.status == 200
        return r.read().decode("utf-8")


# --- the renderer ---------------------------------------------------------------

def test_the_button_is_served_by_default():
    """The armed canary for every negative below. `swarm` defaults to True, so
    every call site that predates this parameter renders exactly what it
    rendered before the parameter existed. (An earlier draft counted those call
    sites here and got the number wrong -- thirteen, not fourteen. A count that
    rots with the next test file added is not worth the line it takes.)"""
    assert SWARM_NAV in dashboard_page.render_page("t")
    assert ">MindSwarm</button>" in dashboard_page.render_page("t")


def test_swarm_false_drops_the_nav_row_and_nothing_else():
    """The row goes; the two other Workspace doors and the marker do not."""
    off = dashboard_page.render_page("t", swarm=False)
    assert SWARM_NAV not in off
    assert "__SWARM_NAV__" not in off, "the splice marker leaked into the page"
    assert 'data-sec="ws-parallel"' in off
    assert 'data-sec="ws-research"' in off


def test_the_hidden_button_leaves_the_shell_js_standing():
    """`showWorkspace` is what a bookmark and an operator who flips the flag
    back both depend on, so its absence would make this change irreversible in
    the one direction the decision said it must not be.

    ON THE DEFINITIONS, not on the names. This test first asserted
    `"showWorkspace" in off` and `"/api/workspace/" in off`, and a second
    adversarial review proved both vacuous: emptying `dashboard_js_views.JS`
    entirely leaves the page still matching, because the MindParallel and
    Self-Reliant nav buttons carry `showWorkspace(` in an onclick,
    `SWARM_HIDDEN_NOTE` carries the route path, and two comments in
    `dashboard_js_core.py` carry both. The docstring that shipped with it
    asserted the opposite. These two anchors are the function's own definition
    and the fetch that builds the URL; each exists in exactly one place, and
    the mutant confirms it -- blank the JS module and this goes red.

    The HONEST LIMIT it does keep: this is the shell's COMPOSITION, not the
    route. The route is proven where a route can only be proven, against a live
    server, in `test_the_swarm_route_still_answers_with_the_row_hidden`."""
    from mindhive import dashboard_js_views

    defn = "function showWorkspace(view){"
    fetch = "/api/workspace/'+view"

    # The property, first: the hidden row did not take the shell's JS with it.
    off = dashboard_page.render_page("t", swarm=False)
    assert defn in off
    assert fetch in off
    # Then the provenance, so a future reader knows which module a red points
    # at -- and so the test says "re-aim me" rather than lying if the JS is
    # reworded rather than lost.
    assert defn in dashboard_js_views.JS, "the anchor moved; re-aim the test"
    assert fetch in dashboard_js_views.JS, "the anchor moved; re-aim the test"


def test_the_swarm_route_still_answers_with_the_row_hidden(swarm_server,
                                                          monkeypatch):
    """THE no-removal test, and the only one that can fail for a route removal.

    Its predecessor asserted `dashboard_get.render_swarm is
    swarm_page.render_swarm` -- an import alias, which stays true after the
    route is deleted from `_get_workspace`. Review called that out. This one
    dials the URL with the nav row hidden and reads the response body, so
    deleting the route, the renderer, or the 'swarm' branch all turn it red."""
    monkeypatch.setenv("MINDHIVE_MINDSWARM", "0")
    assert SWARM_NAV not in _page(swarm_server), "the canary is unarmed"

    with urllib.request.urlopen(
            swarm_server + "/api/workspace/swarm?token=swarm-toggle-token",
            timeout=5) as r:
        assert r.status == 200
        body = r.read().decode("utf-8")
    assert "MindSwarm" in body, body[:400]


def test_the_tooltip_says_kept_not_going_away():
    """The dashboard operator never sees the CLI notice, so this attribute is
    the only place the deprecation reaches them -- and it has to carry the same
    promise the CLI notice makes, not a harsher one."""
    # On the SERVED page, not on the constant: a tooltip spliced out of the
    # shell by a broken replace chain would leave the constant test green. The
    # negative half ran on the constant while the comment above it claimed
    # otherwise -- review caught that. Both halves are on the page now, and the
    # hidden-row note is held to the same promise, since it is the only wording
    # an operator meets once the row is gone.
    harsh = ("will be removed", "deprecated", "unsupported")
    served = dashboard_page.render_page("t").lower()
    assert "kept and still supported" in served
    assert "mindhive_mindswarm=0" in served
    for gone in harsh:
        assert gone not in served, gone
    for gone in harsh:
        assert gone not in dashboard_page.SWARM_HIDDEN_NOTE.lower(), gone


def test_hiding_the_row_leaves_the_way_to_unhide_it_on_the_page():
    """The instruction naming MINDHIVE_MINDSWARM lived INSIDE the button, so
    turning the row off also removed the only in-product statement of how to
    turn it back on. Review found it; `SWARM_HIDDEN_NOTE` is the answer, and
    this test is what keeps the two from being deleted together."""
    off = dashboard_page.render_page("t", swarm=False)
    assert "MINDHIVE_MINDSWARM=0" in off
    assert "/api/workspace/swarm still answers" in off


# --- the flag -------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("0", False), ("false", False), ("FALSE", False), ("off", False),
    ("OFF", False), ("no", False),
    ("1", True), ("yes", True), ("on", True), ("", True),
])
def test_the_off_vocabulary_matches_the_autoharvest_flag(monkeypatch, value,
                                                         expected):
    """The same four spellings of "off" `_auto_harvest_loop` accepts, case
    folded. A second vocabulary in one process is how an operator learns not to
    trust either -- and the empty string reads as ON deliberately: `VAR=` is a
    variable someone set to nothing, not a request to hide anything."""
    monkeypatch.setenv("MINDHIVE_MINDSWARM", value)
    assert dashboard.mindswarm_enabled() is expected


def test_unset_means_on(monkeypatch):
    """Default-on is the whole content of the word *soft*: an operator who does
    nothing keeps the door they have."""
    monkeypatch.delenv("MINDHIVE_MINDSWARM", raising=False)
    assert dashboard.mindswarm_enabled() is True


# --- the wiring -----------------------------------------------------------------

def test_the_served_page_is_not_frozen_at_import_or_at_server_start(
        swarm_server, monkeypatch):
    """THE discriminating test. A flag read once at import or at server start
    passes every renderer test above and fails this one: both requests below hit
    the same live process, and only a per-request read can answer them
    differently.

    It was called `..._without_a_restart`, and that name overclaimed: the flips
    below are in-process `monkeypatch.setenv` calls, which no operator can make
    against a running dashboard. What is proven here is the absence of a frozen
    capture, not an operator workflow."""
    monkeypatch.delenv("MINDHIVE_MINDSWARM", raising=False)
    assert SWARM_NAV in _page(swarm_server)

    monkeypatch.setenv("MINDHIVE_MINDSWARM", "0")
    assert SWARM_NAV not in _page(swarm_server)

    monkeypatch.setenv("MINDHIVE_MINDSWARM", "1")
    assert SWARM_NAV in _page(swarm_server), "the toggle did not flip back"


def test_hiding_the_row_does_not_cost_the_sections_the_operator_relies_on(
        swarm_server, monkeypatch):
    """CLAUDE.md's own recovery grep, run as a test. The recorded incident was a
    dashboard that answered 200 while serving code with the whole Oversee
    section missing, and it looked exactly like data loss. A flag that hides one
    nav row must not be able to reproduce that, so the check that caught it once
    runs here on the flag's off path.

    Stated plainly, because a review asked: the flag cannot reach these hooks,
    so this is a COMPOSITION guard on the off path, not a discriminating test of
    the flag. Its value is that the off path is the one nobody looks at, and the
    canary on the line below is armed -- if the row is still there, the guard
    was never exercised and it says so."""
    monkeypatch.setenv("MINDHIVE_MINDSWARM", "0")
    body = _page(swarm_server)
    assert SWARM_NAV not in body, "the canary is unarmed -- the row is still here"
    for hook in ("showOversee", "renderOversee", "showState", "showBriefing",
                 "showGraph"):
        assert hook in body, hook


def test_the_off_vocabulary_really_matches_the_flag_it_claims_to_match():
    """`mindswarm_enabled`'s docstring promises parity with the vocabulary
    `_auto_harvest_loop` reads for MINDHIVE_AUTOHARVEST. Until an adversarial
    review asked, that promise was prose sitting over two independent literal
    tuples in one module, with nothing to notice when one of them was edited.

    Compared on the SOURCE, with whitespace flattened so the wrapped copy and
    the single-line copy read alike. `_auto_harvest_loop` exposes no predicate
    to call -- its check is inline in the loop body -- so a behavioural
    comparison would need a refactor this change has no business making.
    Discriminating: reword either tuple and the count falls to 1.

    `>= 2`, not `== 2`: a third honest user of the same vocabulary is parity,
    not a failure. The cost this test still carries is that extracting the two
    literals into ONE shared constant -- the refactor that would actually
    guarantee what the docstring promises -- drops the count to 1 and turns it
    red. Whoever does that refactor should delete this test, not appease it."""
    src = pathlib.Path(dashboard.__file__).read_text(encoding="utf-8")
    flat = " ".join(src.split())
    # The members, not the parenthesised tuple: one site wraps after the open
    # paren, so flattening leaves it as `( "0", ...` and only the inner list is
    # common to both spellings.
    off = '"0", "false", "off", "no"'
    assert flat.count(off) >= 2, (
        f"expected the autoharvest flag and the mindswarm flag to share one "
        f"off-vocabulary; found {flat.count(off)} copies of [{off}]")
