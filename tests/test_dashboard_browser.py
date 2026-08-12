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
    """The staged loading feedback against a real clock: nothing under SKEL_MS
    (300 ms), a skeleton past it, and the skeleton gone once the answer lands.
    The node harness records timers and never fires them, on purpose — so this
    threshold has been asserted as source shape and never once observed."""
    with opened(browser, url, delay_path="/api/changelog", delay_ms=1500) as page:
        # Boot loads Overview, which is NOT delayed. Let it finish, so nothing
        # below is reading a placeholder left over from start-up.
        settled(page)
        assert page.query_selector(".skel") is None

        page.click("nav button:text-is('Log')")
        page.wait_for_timeout(150)              # inside the ladder's quiet band
        early = page.query_selector(".skel")
        assert early is None, (
            "a placeholder painted within 150 ms — below SKEL_MS the eye reads a "
            "section load as a flicker rather than as progress")

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

    ("the stale rule loses its :not(.paused) and wins the tie again",
     probe_live_colour,
     ("header #live.stale:not(.paused) { color:#e0b341; }",
      "header #live.stale { color:#e0b341; }")),

    ("this page's own reduced-motion guard is dropped (the cat sheet's stays)",
     probe_reduced_motion,
     ("    header .dot.busy, header #live .blip, .ovspin, .rmnode.rmactive,\n"
      "    .skel .bar { animation:none !important; }",
      "    .rmfill { animation:none !important; }")),

    ("pollCats loses its latch and queues a tick onto a slow one",
     probe_cats_poll_skips_rather_than_queues,
     ("    if (catsInFlight) return;   // skip this tick, never queue it\n", "")),

    ("SKEL_MS goes to 0 — every section load flickers a placeholder",
     probe_loading_ladder,
     ("const SKEL_MS = 300, SLOW_MS = 10000;", "const SKEL_MS = 0, SLOW_MS = 10000;")),

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
