# tests/test_dashboard_browser.py
"""T12 — the tier every other dashboard test is honest about not being.

The node tier-1 suite EXECUTES the page's decisions, which is already a long way
past matching source strings, but it runs them against a hand-rolled DOM stub in
`node:vm`. That stub has no cascade, no media queries, no layout and no clock,
so three kinds of claim were unprovable anywhere in this repo:

  * **CSS precedence.** `header #live.stale:not(.paused)` exists because both
    rules are one id plus one class and the later one wins a tie. Whether the
    `:not()` actually wins is a question for a cascade, not for a substring.
  * **Media queries.** Every `prefers-reduced-motion` guard is asserted by
    reading the stylesheet and pairing selectors by hand — that can see somebody
    wrote a guard, not that the guard wins. A real reduced-motion context
    answers the question the guard exists for: does the animation stop.
  * **Real timers.** The loading ladder (nothing under SKEL_MS, skeleton past
    it) is a claim about wall time, and the node harness records timers and
    deliberately never lets one elapse.

Every probe below is paired with a MUTANT — the same probe against a page with
that one guard removed, REQUIRED to fail. A browser test that would pass against
the broken page proves the browser works, not the dashboard. The mutations are
in `MUTANTS`, and each names the defect it restores.

Deselected by default (`-m 'not node and not browser'`), for the same reason the
node tier is: `hive selftest` shells `pytest -q tests/` from inside the
dashboard's own POST handler (src/mindhive/verify.py:64), so nothing that
launches a browser may be in the default set. Run it with `pytest -m browser`
after `pip install -e .[browser]` and `playwright install chromium`.

THE TRAP, recorded before it costs anyone else an afternoon: an embedded browser
pane can report `document.hidden = true`, and BOTH pollers early-return on it.
A suite that never checked would drive a page whose live refresh never ran and
report a serene pass. `test_the_page_is_visible_to_its_own_pollers` asserts that
premise instead of assuming it.
"""
import contextlib
import json
import threading
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is not installed — these cases were REQUESTED (`-m browser`) "
           "and did not run; nothing here passed. Install with "
           "`pip install -e .[browser]` and `playwright install chromium`.")

from mindhive import dashboard  # noqa: E402

pytestmark = pytest.mark.browser

TOKEN = "browser-token-123"

# Delaying a response by sleeping inside a `page.route` handler would ALSO stall
# the sampling side: the sync API runs route handlers on the same greenlet loop
# the main call waits on, so a `time.sleep` there freezes `wait_for_timeout` too
# and every sample lands after the delay. The delay is therefore installed in the
# page, where the clock, the timers and the DOM are all real and only the
# response's arrival time is arranged.
DELAY_FETCH = """
  (() => {
    const real = window.fetch;
    window.fetch = function(...a){
      const url = String((a[0] && a[0].url) || a[0] || '');
      if (url.includes('__PATH__'))
        return new Promise(r => setTimeout(() => r(real.apply(this, a)), __MS__));
      return real.apply(this, a);
    };
  })();
"""


class _QuietServer(ThreadingHTTPServer):
    """Closing a browser context aborts whatever it still had in flight, and the
    cats probe deliberately leaves a 4 s response outstanding when it does. The
    stock handler prints a full traceback for each one, which would leave a
    PASSING CI job strewn with tracebacks — the fastest way to teach a reader
    that tracebacks in this job are normal. Only the aborted-peer errors are
    swallowed; anything else still prints."""

    def handle_error(self, request, client_address):
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionAbortedError,
                                              ConnectionResetError, BrokenPipeError)):
            super().handle_error(request, client_address)


@contextlib.contextmanager
def serving(mutation=None):
    """Serve the real dashboard, optionally with one guard cut out of the page.

    The anchor is checked against a rendered page BEFORE the server starts: a
    mutation whose anchor has drifted would otherwise change nothing, the probe
    would pass, and the mutant case would report that as a failure to break —
    true, but for the wrong reason and with no hint which."""
    render = dashboard.render_page
    if mutation is not None:
        old, new = mutation
        if old not in render(TOKEN):
            raise RuntimeError(
                f"mutation anchor is no longer in the served page, so this mutant "
                f"tests nothing: {old!r}")

        def render(*a, _real=dashboard.render_page, **k):
            return _real(*a, **k).replace(old, new)

    with mock.patch.object(dashboard, "render_page", render):
        srv = _QuietServer(("127.0.0.1", 0), dashboard.make_handler(TOKEN))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{srv.server_address[1]}"
        finally:
            srv.shutdown()


@pytest.fixture(scope="module")
def url():
    with serving() as u:
        yield u


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:                       # pragma: no cover
            pytest.skip(f"chromium is not installed for playwright ({e}) — these "
                        f"cases were REQUESTED and did not run; nothing passed. "
                        f"Fix with `playwright install chromium`.")
        try:
            yield b
        finally:
            b.close()


@contextlib.contextmanager
def opened(browser, url, delay_path=None, delay_ms=0, **context_kw):
    ctx = browser.new_context(**context_kw)
    try:
        page = ctx.new_page()
        if delay_path:
            page.add_init_script(DELAY_FETCH.replace("__PATH__", delay_path)
                                            .replace("__MS__", str(delay_ms)))
        page.goto(f"{url}/?token={TOKEN}", wait_until="domcontentloaded")
        yield page
    finally:
        ctx.close()


def settled(page):
    page.wait_for_function(
        "document.getElementById('status').textContent === 'ready'", timeout=30000)


# ---------------------------------------------------------------- the probes
# Each takes (browser, url) rather than a page fixture so the mutant cases can
# run the very same code against a differently-served page. A probe that only
# existed in the clean test could not be reused to prove it goes red.

def probe_live_region(browser, url):
    """`display:none` or `visibility:hidden` on a live region means it is never
    announced at all — the exact way an accessibility fix becomes decorative.
    `.sr-only` uses the clip technique precisely to avoid that, and only a real
    cascade can confirm it; the served CSS text cannot.

    The second half is the bug found by reasoning and fixed by moving the
    element: a live region nested inside `aria-busy="true"` may be coalesced
    away, and run() raises aria-busy around every announcement it makes."""
    with opened(browser, url) as page:
        style = page.evaluate("""() => {
          const el = document.getElementById('ann');
          const s = getComputedStyle(el);
          return { display: s.display, visibility: s.visibility,
                   role: el.getAttribute('role'),
                   inBusy: !!el.closest('[aria-busy]') };
        }""")
    assert style["display"] != "none", "the live region is display:none"
    assert style["visibility"] != "hidden", "the live region is visibility:hidden"
    assert style["role"] == "status"
    assert style["inBusy"] is False, (
        "the live region sits inside an aria-busy container, so assistive tech is "
        "entitled to drop everything it announces while a section loads")


def probe_live_colour(browser, url):
    """`header #live.paused` and `header #live.stale` are both one id plus one
    class, so the later rule wins a tie — and the later one was `.stale`. The fix
    is a `:not(.paused)`, and whether it works is a cascade question.

    Both states are set directly: reaching them for real costs three failed 2.5 s
    ticks plus a click, and the CASCADE is what is under test here, not the path
    that sets the classes — the node tier covers that."""
    with opened(browser, url) as page:
        def colour(classes):
            return page.evaluate("""(cls) => {
              const el = document.getElementById('live');
              el.className = cls;
              return getComputedStyle(el).color;
            }""", classes)

        paused_only, both, stale_only = (colour("paused"), colour("paused stale"),
                                         colour("stale"))
    assert stale_only != paused_only, (
        "stale and paused compute the same colour, so this probe cannot tell which "
        "rule won and proves nothing")
    assert both == paused_only, (
        f"a chip that is stale AND paused renders {both}, the stale colour, next to "
        f"the word 'paused' — and markLiveHealth is unreachable while paused, so "
        f"nothing would ever take it back (paused={paused_only} stale={stale_only})")


# (parent selector, markup to inject, the rule under test). The stylesheet is
# what is in question and an injected node gets exactly the same cascade, so the
# markup is built here rather than driven to — most of these rules target
# elements that exist for only a second, mid-interaction.
MOTION = [
    ("header", '<span class="dot busy" id="probe"></span>', "header .dot.busy"),
    ("header #live", '<span class="blip" id="probe">x</span>', "header #live .blip"),
    ("#view", '<span class="ovspin" id="probe">x</span>', ".ovspin"),
    ("#view", '<span class="rmnode rmactive" id="probe">x</span>', ".rmnode.rmactive"),
    ("#view", '<div class="skel"><div class="bar" id="probe"></div></div>', ".skel .bar"),
]


def probe_reduced_motion(browser, url):
    """The media query, executed, for every animation this page declares itself.

    The un-reduced context is the anti-vacuity half: without it, `animation-name:
    none` under reduce would also be reported by an element that never moved."""
    def animation_name(parent, html, **ctx_kw):
        with opened(browser, url, **ctx_kw) as page:
            page.eval_on_selector(parent, "(el, h) => el.insertAdjacentHTML('beforeend', h)",
                                  html)
            return page.eval_on_selector("#probe",
                                         "el => getComputedStyle(el).animationName")

    for parent, html, rule in MOTION:
        moving = animation_name(parent, html)
        still = animation_name(parent, html, reduced_motion="reduce")
        assert moving != "none", (
            f"`{rule}` is not animated even in a normal context, so the "
            f"reduced-motion half would pass for an element that never moved")
        assert still == "none", (
            f"`{rule}` keeps running `{still}` under prefers-reduced-motion — "
            f"repeating animation is a trigger for some people, not a preference")


def probe_loading_ladder(browser, url):
    """The staged loading feedback against a real clock: nothing under SKEL_MS, a
    skeleton past it, and the skeleton gone once the answer lands. The node
    harness records timers and never fires them, on purpose — so this threshold
    has been asserted as source shape and never once observed.

    The quiet band is read from the PAGE's own `SKEL_MS` rather than written here
    as a number. A hardcoded band silently stopped testing anything the moment the
    threshold moved (300 → 150): the probe was still waiting the old half-band,
    which had become the whole one.
    """
    with opened(browser, url, delay_path="/api/changelog", delay_ms=1500) as page:
        # Boot loads Overview, which is NOT delayed. Let it finish, so nothing
        # below is reading a placeholder left over from start-up.
        settled(page)
        assert page.query_selector(".skel") is None

        # `const` at the top level of a classic script lives in the global
        # LEXICAL scope, not on `window` — an evaluate in the same realm sees it.
        skel_ms = page.evaluate("SKEL_MS")
        assert isinstance(skel_ms, (int, float)), (
            f"the page's SKEL_MS is not readable ({skel_ms!r}), so the band below "
            f"would be a guess")
        # Half the shipped band, floored so the SKEL_MS=0 mutant still gets a real
        # window in which its immediate placeholder can appear and be caught.
        band = max(40, int(skel_ms) // 2)

        page.click("nav button:text-is('Log')")
        page.wait_for_timeout(band)             # inside the ladder's quiet band
        early = page.query_selector(".skel")
        assert early is None, (
            f"a placeholder painted within {band} ms — below SKEL_MS "
            f"({skel_ms} ms) the eye reads a section load as a flicker rather "
            f"than as progress")

        page.wait_for_selector(".skel", timeout=5000)
        assert page.get_attribute("#panel", "aria-busy") == "true"

        page.wait_for_selector(".skel", state="detached", timeout=30000)
        assert page.get_attribute("#panel", "aria-busy") == "false"
        assert page.inner_text("#status").strip() == "ready"


# Counts the cats poll the way the SERVER would see it — how many of its
# requests are outstanding at once — and holds each response past the poll
# interval so a tick necessarily lands on an unfinished one. `max` is the whole
# measurement: the latch's claim is that a slow tick is SKIPPED, and a skipped
# tick is exactly one that never becomes a second concurrent request.
COUNT_CATS = """
  (() => {
    const real = window.fetch;
    const s = window.__cats = { inflight: 0, max: 0, done: 0 };
    window.fetch = function(...a){
      const url = String((a[0] && a[0].url) || a[0] || '');
      if (!url.includes('/api/mindnet/cats')) return real.apply(this, a);
      s.inflight++;
      if (s.inflight > s.max) s.max = s.inflight;
      const settle = () => { s.inflight--; s.done++; };
      return new Promise(r => setTimeout(() => r(real.apply(this, a)), __MS__))
        .then(res => { settle(); return res; },
              err => { settle(); throw err; });
    };
  })();
"""
CATS_DELAY_MS = 4000            # LIVE_MS is 2500, so a tick always lands mid-flight
CATS_WATCH_MS = 7000            # three ticks' worth of chances to queue one


def probe_cats_poll_skips_rather_than_queues(browser, url):
    """The finding this whole PR started from, measured instead of read.

    `pollCats` is on a 2.5 s interval against an endpoint that cost 0.87-0.92 s
    idle and far more under load; before the latch, a tick that landed on an
    unfinished request simply started another, and the folds are pure Python so
    the GIL serialises them into a queue every click then waits behind. Until
    now the latch was covered only by asserting `if (catsInFlight) return;`
    appears in the source — which is true of a latch that is never reached, or
    one whose flag is cleared in the wrong place.

    Here the response is held for 4 s against a 2.5 s interval, so at least one
    tick is GUARANTEED to arrive while a request is outstanding, and the only
    thing asserted is what the server would see: never two at once.

    (`refreshLive`'s own latch is the node tier's job — tier 5 drives it under a
    fake clock, which can fire a timer in zero wall time. This one is here
    because the poll is registered by `setInterval` at load and driving it needs
    a real clock.)"""
    with opened(browser, url) as page:
        page.add_init_script(COUNT_CATS.replace("__MS__", str(CATS_DELAY_MS)))
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(CATS_WATCH_MS)
        s = page.evaluate("window.__cats")

    assert s["done"] + s["inflight"] >= 1, (
        f"the cats poll never fired at all in {CATS_WATCH_MS} ms, so `max` is 0 "
        f"for the one reason that proves nothing: {s}")
    assert s["max"] == 1, (
        f"{s['max']} cats requests were outstanding at once — a tick that landed "
        f"on an unfinished poll queued another instead of skipping, which is the "
        f"amplification this PR exists to remove: {s}")


def probe_nav_clickable(browser, url):
    """T9 took the nav disable out — being unable to leave a section while it
    loads is what made a 1-2 s fetch feel like a hang. `aria-busy` says the same
    thing without taking the controls away. Asserted mid-load, which is the only
    moment the old behaviour was visible."""
    with opened(browser, url, delay_path="/api/changelog", delay_ms=1500) as page:
        settled(page)
        page.click("nav button:text-is('Log')")

        assert page.get_attribute("#panel", "aria-busy") == "true", (
            "the panel is not marked busy during a load, so the signal that "
            "REPLACED the nav disable is not being given either")
        disabled = page.evaluate(
            "Array.from(document.querySelectorAll('nav button'))"
            ".filter(b => b.disabled).map(b => b.textContent)")
        assert disabled == [], f"nav buttons disabled mid-load: {disabled}"

        # And the click actually lands: leaving a half-loaded section is the
        # thing the disable made impossible.
        page.click("nav button:text-is('Overview')")
        settled(page)
        assert page.evaluate("liveFn === renderOverview"), (
            "clicking away from a loading section did not change the live "
            "section, so the nav is merely undisabled rather than working")


# ------------------------------------------------------------- the real page

def test_the_page_is_visible_to_its_own_pollers(browser, url):
    """The premise of everything else here, asserted rather than assumed.

    `refreshLive` and `pollCats` both return early on `document.hidden`. In an
    embedded pane that flag can be true while the page is plainly on screen, and
    a suite that skipped this check would drive a dashboard with no live refresh
    at all and call it green. No mutant: this is a fact about the HARNESS, not a
    guard in the page, and there is nothing in the page to remove."""
    with opened(browser, url) as page:
        assert page.evaluate("document.hidden") is False, (
            "the page reports itself hidden, so both pollers early-return and "
            "every live-refresh assertion here would pass vacuously")
        assert page.evaluate("typeof refreshLive === 'function'")
        assert page.evaluate("typeof pollCats === 'function'")


def test_the_live_region_is_announced_rather_than_hidden(browser, url):
    probe_live_region(browser, url)


def test_pausing_wins_the_colour_over_a_stale_chip(browser, url):
    probe_live_colour(browser, url)


def test_reduced_motion_actually_stops_every_animation(browser, url):
    probe_reduced_motion(browser, url)


def test_a_slow_section_paints_a_skeleton_and_then_replaces_it(browser, url):
    probe_loading_ladder(browser, url)


def test_the_cats_poll_skips_a_slow_tick_instead_of_queueing_it(browser, url):
    probe_cats_poll_skips_rather_than_queues(browser, url)


def test_the_nav_stays_clickable_while_a_section_loads(browser, url):
    probe_nav_clickable(browser, url)


# ----------------------------------------------------------------- the mutants
# Each row restores one defect this PR fixed and requires the matching probe to
# go red. Anchors are verified against a freshly rendered page before the mutant
# server starts, so a drifted anchor is reported as such instead of quietly
# turning the row into a no-op.
MUTANTS = [
    ("sr-only becomes display:none — the region exists and is never announced",
     probe_live_region,
     ("  .sr-only { position:absolute;", "  .sr-only { display:none; position:absolute;")),

    ("the live region moves back inside the aria-busy panel",
     probe_live_region,
     ('  <div id="ann" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>\n'
      '  <section class="panel" id="panel" aria-busy="false">\n'
      '    <div id="controls"></div>',
      '  <section class="panel" id="panel" aria-busy="false">\n'
      '    <div id="ann" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>\n'
      '    <div id="controls"></div>')),

    # The colour is a token, not a literal: the palette moved into `theme.py`, so
    # the mutation has to keep whatever the shipped rule declares and remove only
    # the `:not(.paused)` this row is about.
    ("the stale rule loses its :not(.paused) and wins the tie again",
     probe_live_colour,
     ("header #live.stale:not(.paused) { color:var(--warn); }",
      "header #live.stale { color:var(--warn); }")),

    ("this page's own reduced-motion guard is dropped (the cat sheet's stays)",
     probe_reduced_motion,
     ("    header .dot.busy, header #live .blip, .ovspin, .rmnode.rmactive,\n"
      "    .skel .bar, .skel .tile, .skel .line, .skel .pane"
      " { animation:none !important; }",
      "    .rmfill { animation:none !important; }")),

    ("pollCats loses its latch and queues a tick onto a slow one",
     probe_cats_poll_skips_rather_than_queues,
     ("    if (catsInFlight) return;   // skip this tick, never queue it\n", "")),

    ("SKEL_MS goes to 0 — every section load flickers a placeholder",
     probe_loading_ladder,
     ("const SKEL_MS = 150, SLOW_MS = 10000;", "const SKEL_MS = 0, SLOW_MS = 10000;")),

    ("setBusy disables the whole nav again, the way it did before T9",
     probe_nav_clickable,
     ("    if (panelEl) panelEl.setAttribute('aria-busy', on ? 'true' : 'false');",
      "    if (panelEl) panelEl.setAttribute('aria-busy', on ? 'true' : 'false');\n"
      "    document.querySelectorAll('nav button').forEach(b => { b.disabled = on; });")),
]


@pytest.mark.parametrize("why,probe,mutation", MUTANTS, ids=[m[0][:44] for m in MUTANTS])
def test_the_probe_fails_against_a_page_with_that_guard_removed(browser, why, probe,
                                                                mutation):
    with serving(mutation) as mutant_url:
        with pytest.raises(AssertionError):
            probe(browser, mutant_url)


# --- the session cat answers the four questions on click ---------------------
#
# The endpoint is covered over real HTTP in `test_mindnet_cat_board.py` and the
# provider as a pure function in `test_mindnet_ui_sessions.py`. What only a
# browser can show is the part the user actually does: clicking the cat and
# reading the panel. The payload is stubbed rather than seeded, so this probe
# tests the PAGE and nothing behind it -- a strip that renders the right cat
# from the right JSON is the claim, and a ledger fixture would drag two folds
# and a memo into a question that is neither.

CATS_PAYLOAD = {
    "cats": [],
    "sessions": [{
        "session_id": "chat-probe-1",
        "display": "working",
        "kind": "chat",
        "title": "wire the cats up",
        "doing": "writing the panel",
        "task": "Answer the four questions",
        "phase": "step 2/3: render the panel",
        "skill": "compound-engineering:ce-plan",
        "age_secs": 12,
    }],
    "sessions_total": 1,
    "sessions_hidden": 0,
    "producers": {"session": True},
    "dim_after_secs": 180,
    "hide_after_secs": 21600,
    "idle_vanish_secs": 15,
    "online_after_secs": 180,
}


def _stub_cats(page):
    """Serve one known session cat, whatever the local ledger holds."""
    page.route("**/api/mindnet/cats*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(CATS_PAYLOAD)))


def probe_session_cat_panel(browser, url):
    with opened(browser, url) as page:
        _stub_cats(page)
        settled(page)
        cat = page.locator("#mn-sessions .mn-sess").first
        cat.wait_for(state="visible", timeout=15000)
        # Before the click there must be no panel: a panel that is always open
        # would make every assertion below pass without a click happening.
        assert page.locator("#mn-cat-panel").is_visible() is False
        cat.click()
        panel = page.locator("#mn-cat-panel")
        panel.wait_for(state="visible", timeout=5000)
        rows = page.evaluate("""() => Array.from(
          document.querySelectorAll('#mn-cat-panel .kv'))
            .map(d => [d.children[0].textContent, d.children[1].textContent])""")
        heading = page.locator("#mn-cat-panel h3").text_content()
        # A second click closes it -- the cat is a toggle, like the faculty cat.
        cat.click()
        closed = page.locator("#mn-cat-panel").is_visible()
    fields = dict(rows)
    assert heading == "wire the cats up", heading
    assert fields["task"] == "Answer the four questions", fields
    assert fields["phase"] == "step 2/3: render the panel", fields
    assert fields["skill"] == "compound-engineering:ce-plan", fields
    assert fields["state"] == "working", fields
    assert closed is False, "clicking the open cat again did not close its panel"


def probe_session_cat_panel_dashes_what_it_does_not_know(browser, url):
    """A session with no plan and no skill yet. The panel must still open and
    must say so with a dash -- rendering the string "undefined" into the DOM is
    the failure this repo has already shipped once for `origin`."""
    with opened(browser, url) as page:
        page.route("**/api/mindnet/cats*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(dict(CATS_PAYLOAD, sessions=[
                dict(CATS_PAYLOAD["sessions"][0], task="", phase="", skill="")]))))
        settled(page)
        cat = page.locator("#mn-sessions .mn-sess").first
        cat.wait_for(state="visible", timeout=15000)
        cat.click()
        page.locator("#mn-cat-panel").wait_for(state="visible", timeout=5000)
        values = page.evaluate("""() => Array.from(
          document.querySelectorAll('#mn-cat-panel .kv'))
            .map(d => d.children[1].textContent)""")
    assert "undefined" not in values, values
    assert "null" not in values, values
    assert values.count("\u2014") >= 3, values


def test_a_session_cat_opens_a_panel_naming_task_phase_and_skill(browser, url):
    probe_session_cat_panel(browser, url)


def test_a_session_cat_panel_dashes_the_unknown(browser, url):
    probe_session_cat_panel_dashes_what_it_does_not_know(browser, url)


# --- one panel, two strips: who owns it --------------------------------------
#
# `paintCats` and `paintSessions` run back to back off ONE 2.5 s poll, and every
# bug in this area was one of them reaching into the other's open panel:
#
#   F11  the faculty strip's empty-board branch hid the panel unconditionally,
#        and an empty faculty board is the COMMON case -- most sessions emit no
#        faculty telemetry at all -- so a session panel closed itself roughly
#        one poll after every click, which reads as the click not working.
#   F9   the faculty cat's own handler swept only `#mn-cats .mn-cat.sel`, so
#        clicking a faculty cat after a session cat left BOTH marked selected
#        while the panel described one of them.
#
# Neither is visible without a real DOM, two real strips and a real click: the
# race is between two paint functions and a user, and none of the three exists
# in a unit test.

FACULTY_ROW = {"faculty": "task-graph", "display": "active", "status": "active",
               "task_id": "t1", "phase": "plan", "skill": "mindhive-python-tdd",
               "age_secs": 5}


def probe_one_panel_one_selection(browser, url):
    with opened(browser, url) as page:
        page.route("**/api/mindnet/cats*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(dict(CATS_PAYLOAD, cats=[FACULTY_ROW],
                                 producers={"session": True, "faculty": True}))))
        settled(page)
        sess = page.locator("#mn-sessions .mn-sess").first
        fac = page.locator("#mn-cats .mn-cat").first
        sess.wait_for(state="visible", timeout=15000)
        fac.wait_for(state="visible", timeout=15000)
        sess.click()
        page.locator("#mn-cat-panel").wait_for(state="visible", timeout=5000)
        after_session = page.evaluate(
            "() => document.querySelectorAll('.mn-cat.sel, .mn-sess.sel').length")
        fac.click()
        page.locator("#mn-cat-panel").wait_for(state="visible", timeout=5000)
        after_faculty = page.evaluate(
            "() => document.querySelectorAll('.mn-cat.sel, .mn-sess.sel').length")
        heading = page.locator("#mn-cat-panel h3").text_content()
    assert after_session == 1, f"{after_session} cats selected after one click"
    assert after_faculty == 1, (
        f"{after_faculty} cats read as selected while one panel describes one "
        "of them")
    assert heading != "wire the cats up", (
        "the panel still describes the session cat after a faculty click")


def probe_an_empty_faculty_board_leaves_the_session_panel_open(browser, url):
    """F11 head on. The payload has NO faculty cats, which is the normal case,
    and the faculty strip repaints on the same tick as the session strip."""
    with opened(browser, url) as page:
        _stub_cats(page)                        # cats: [] -- the common shape
        settled(page)
        cat = page.locator("#mn-sessions .mn-sess").first
        cat.wait_for(state="visible", timeout=15000)
        cat.click()
        panel = page.locator("#mn-cat-panel")
        panel.wait_for(state="visible", timeout=5000)
        # Sit through more than one full poll. The panel closing itself here is
        # the bug: nothing was clicked, and the user is still reading it.
        page.wait_for_timeout(6000)
        still_open = panel.is_visible()
        selected = page.evaluate(
            "() => document.querySelectorAll('.mn-sess.sel').length")
    assert still_open, (
        "the empty faculty board closed a panel the session strip owns")
    assert selected == 1, "the poll dropped the session cat's selection"


def probe_session_strip_does_not_execute_ledger_text(browser, url):
    """The session strip is the newest surface rendering text a person wrote,
    `esc()` covers `&<>` and NOT quotes, and the panel's values go through it.
    A payload shaped like markup must arrive as characters."""
    hostile = "<img src=x onerror=window.__pwned=1>"
    quoted = '" onmouseover="window.__pwned=1'
    with opened(browser, url) as page:
        page.route("**/api/mindnet/cats*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(dict(CATS_PAYLOAD, sessions=[dict(
                CATS_PAYLOAD["sessions"][0], title=hostile, task=hostile,
                phase=quoted, skill=hostile, doing=hostile)]))))
        settled(page)
        cat = page.locator("#mn-sessions .mn-sess").first
        cat.wait_for(state="visible", timeout=15000)
        cat.click()
        page.locator("#mn-cat-panel").wait_for(state="visible", timeout=5000)
        page.wait_for_timeout(500)
        pwned = page.evaluate("() => !!window.__pwned")
        injected = page.evaluate(
            "() => document.querySelectorAll('#mn-cat-panel img').length")
        heading = page.locator("#mn-cat-panel h3").text_content()
    assert pwned is False, "ledger text executed as script"
    assert injected == 0, "ledger text became a real element"
    assert heading == hostile, (
        f"the markup was not rendered as characters: {heading!r}")


def test_exactly_one_cat_is_selected_across_both_strips(browser, url):
    probe_one_panel_one_selection(browser, url)


def test_an_empty_faculty_board_does_not_close_the_session_panel(browser, url):
    probe_an_empty_faculty_board_leaves_the_session_panel_open(browser, url)


def test_the_session_panel_renders_hostile_ledger_text_as_characters(browser, url):
    probe_session_strip_does_not_execute_ledger_text(browser, url)
