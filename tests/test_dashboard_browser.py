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

from mindhive import dashboard, dashboard_get, events  # noqa: E402

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


# Which document a mutation is cut out of. The shell is one of four pages: the
# three workspace views are full documents served by their own renderers, and a
# guard that lives in one of them (their poll loops do) cannot be found in the
# shell — a mutant that named it would fail the anchor check and read as "the
# guard moved" rather than "you aimed at the wrong page".
#
# A table of already-imported modules rather than an importlib lookup, so a typo
# is a KeyError in the test run instead of a mutation that patches nothing. The
# workspace renderers are patched on `dashboard_get`, which is where the server
# resolves them from — patching them on their defining module would leave the
# name `dashboard_get` imported at module load still bound to the real one.
_MUTABLE = {
    "shell": (dashboard, "render_page"),
    "parallel": (dashboard_get, "render_parallel"),
    "swarm": (dashboard_get, "render_swarm"),
    "research": (dashboard_get, "render_research"),
}


@contextlib.contextmanager
def serving(mutation=None):
    """Serve the real dashboard, optionally with one guard cut out of one page.

    `mutation` is `(old, new)` against the shell, or `(target, old, new)` where
    target is a key of `_MUTABLE`. The anchor is checked against a rendered page
    BEFORE the server starts: a mutation whose anchor has drifted would otherwise
    change nothing, the probe would pass, and the mutant case would report that
    as a failure to break — true, but for the wrong reason and with no hint
    which."""
    with contextlib.ExitStack() as stack:
        if mutation is not None:
            target, old, new = (("shell",) + tuple(mutation) if len(mutation) == 2
                                else tuple(mutation))
            module, attr = _MUTABLE[target]
            real = getattr(module, attr)
            if old not in real(TOKEN):
                raise RuntimeError(
                    f"mutation anchor is no longer in the {target} page, so this "
                    f"mutant tests nothing: {old!r}")

            def render(*a, _real=real, **k):
                return _real(*a, **k).replace(old, new)

            stack.enter_context(mock.patch.object(module, attr, render))

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
        # One path with one delay, or a sequence of (path, ms) pairs. The
        # boot-scope probe needs the PICKER's answer held back as well as the
        # section's, because the window it tests is the one between them; each
        # script wraps the fetch the previous one installed, so a request that
        # matches neither path still reaches the real fetch untouched.
        for path, ms in ([(delay_path, delay_ms)] if isinstance(delay_path, str)
                         else list(delay_path or [])):
            page.add_init_script(DELAY_FETCH.replace("__PATH__", path)
                                            .replace("__MS__", str(ms)))
        page.goto(f"{url}/?token={TOKEN}", wait_until="domcontentloaded")
        yield page
    finally:
        ctx.close()


def settled(page):
    """The section on screen is the one that finished loading.

    The status chip alone cannot say that. `setBusy(false)` writes 'ready' as
    its RESTING state and the shell is SERVED with that resting text already in
    place, so between `domcontentloaded` and boot's first `run()` the chip reads
    'ready' about a section nobody has asked for yet. Measured on Linux, three
    runs: 'ready' at 5-15 ms, the rail marked at 46-49 ms, the overview grid
    painted at 90-112 ms. Every probe that read the page straight after this
    helper was reading that 40-90 ms hole; on Windows they passed only because
    the next Playwright round-trip cost more than the render did, which is not a
    guarantee of anything and stopped being true on a Linux runner.

    `RUN_GEN` closes the window because it counts run() calls rather than
    describing a mood: `>= 1` says a section has been ASKED for, and 'ready'
    then says none is still in flight. Both halves are needed -- the counter
    alone would return while the first fetch is still out, and the chip alone is
    what was already wrong.
    """
    page.wait_for_function(
        """() => {
          const s = document.getElementById('status');
          return typeof RUN_GEN === 'number' && RUN_GEN >= 1
                 && !!s && s.textContent === 'ready';
        }""", timeout=30000)


# The guard on the guard. `settled()` is the helper 49 assertions stand behind,
# so a regression in it does not fail loudly -- it makes every probe read the
# page a little too early, which on a fast enough machine still passes.
SETTLE_HOLD_MS = 2000


def test_settled_waits_for_a_section_and_not_for_the_served_word_ready(browser, url):
    """The status chip is not a section signal, and the shell PROVES it.

    `dashboard_page.py` serves `<span id="status">ready</span>` in the markup,
    and boot is `await loadProjects(); if (!viewPicked) showOverview();` -- so
    across the whole projects fetch the chip already reads 'ready' about a
    section that has not been asked for yet. The old `settled()` waited for
    exactly that string and returned into the hole: measured on Linux, 'ready'
    at 5-15 ms, the rail marked at 46-49 ms, the overview grid painted at
    90-112 ms. Eight probes failed there on Linux and passed on Windows, where
    the next round-trip happened to cost more than the render did.

    The fixture is the defect, not a re-statement of the fix: `/api/projects`
    is held for two seconds, which widens that window from tens of milliseconds
    to a size no scheduler can close. The first half asserts the OLD condition
    is satisfied while `RUN_GEN` is still 0 and no grid exists -- if that ever
    stops being true this test is no longer reproducing anything and says so
    rather than passing quietly. The second half is what `settled()` now owes
    its callers.
    """
    with opened(browser, url, delay_path="/api/projects",
                delay_ms=SETTLE_HOLD_MS) as page:
        STATE = """() => ({
          gen: (typeof RUN_GEN === 'number' ? RUN_GEN : null),
          status: (document.getElementById('status') || {}).textContent,
          on: document.querySelectorAll('nav button.on').length,
          grid: !!document.querySelector('#view .cards[data-shape]'),
        })"""
        # The old condition, verbatim rather than described.
        page.wait_for_function(
            "document.getElementById('status').textContent === 'ready'",
            timeout=10000)
        early = page.evaluate(STATE)
        assert early["gen"] == 0 and not early["grid"] and early["on"] == 0, (
            f"the held projects fetch did not reproduce the window this test "
            f"exists for -- boot had already picked a section by the time the "
            f"old condition was met: {early}")

        settled(page)
        after = page.evaluate(STATE)
        assert after["gen"] >= 1, (
            f"settled() returned before any section had been asked for: {after}")
        assert after["on"] == 1 and after["grid"], (
            f"settled() returned before the section reached the screen: {after}")


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
    # The panel's section entrance. Injected as a detached `section.panel.swap`
    # rather than measured on the live `#panel`, for the same reason every row
    # above is injected: the real one wears `swap` for 220 ms after a click and
    # a probe that raced it would be flaky in the direction that reads as green.
    ("body", '<section class="panel swap" id="probe"></section>', "section.panel.swap"),
]


def probe_reduced_motion(browser, url):
    """The media query, executed, for every animation this page declares itself.

    The un-reduced context is the anti-vacuity half: without it, `animation-name:
    none` under reduce would also be reported by an element that never moved."""
    def animation_name(parent, html, **ctx_kw):
        # Inject, read and clean up in ONE evaluate — one JS task, so nothing
        # can run between the insert and the read. Two calls left a window: half
        # these rows hang off `#view`, `opened()` returns on domcontentloaded
        # without waiting for `settled`, and `showHTML` assigns `#view.innerHTML`
        # wholesale, which detaches every node inside it. Measured directly, a
        # node injected into `#view` is gone one poll window later in 9 runs out
        # of 40 (4/40 even after `settled`) — the same failure shape HOVER_MOTION
        # below already documents and pins to `body` because of. The two calls
        # here usually won the race; run-to-completion means this one cannot lose
        # it, whatever the poll is doing.
        with opened(browser, url, **ctx_kw) as page:
            return page.evaluate("""([sel, h]) => {
              const parent = document.querySelector(sel);
              if (!parent) throw new Error('no parent for the probe: ' + sel);
              parent.insertAdjacentHTML('beforeend', h);
              const probe = document.getElementById('probe');
              if (!probe) throw new Error('the probe markup carried no #probe');
              const name = getComputedStyle(probe).animationName;
              probe.remove();
              return name;
            }""", [parent, html])

    for parent, html, rule in MOTION:
        moving = animation_name(parent, html)
        still = animation_name(parent, html, reduced_motion="reduce")
        assert moving != "none", (
            f"`{rule}` is not animated even in a normal context, so the "
            f"reduced-motion half would pass for an element that never moved")
        assert still == "none", (
            f"`{rule}` keeps running `{still}` under prefers-reduced-motion — "
            f"repeating animation is a trigger for some people, not a preference")


def probe_reduced_motion_is_read_live(browser, url):
    """The preference, changed on a page that is ALREADY open.

    CSS re-evaluates a media query the instant the OS setting changes, so the
    stylesheet always keeps up. The scripted scrolls are the one piece of motion
    a stylesheet cannot reach, and they used to read the preference ONCE at load
    into a const — which meant a reader who switched reduce-motion on mid-session
    got a board where every animation had stopped and the scrolls still glided.
    This board stays open for days; a preference read once is a preference mostly
    ignored.

    `emulate_media` is applied AFTER `goto`, which is the whole point: a probe
    that set it in the context would be answered correctly by a snapshot too.
    """
    with opened(browser, url) as page:
        before = page.evaluate("() => reduceMotion()")
        page.emulate_media(reduced_motion="reduce")
        after = page.evaluate("() => reduceMotion()")
        # The anti-vacuity half: the media query itself must have moved, or a
        # `reduceMotion` hardwired to `true` would satisfy the assertion below.
        css_moved = page.evaluate(
            "() => matchMedia('(prefers-reduced-motion: reduce)').matches")

    assert before is False, (
        "the page already believed reduce-motion was on before it was turned "
        "on, so this probe cannot tell a live read from a snapshot")
    assert css_moved is True, (
        "the media query did not change under emulate_media — the probe never "
        "actually turned the preference on")
    assert after is True, (
        "reduceMotion() still reports the value it read at load: the scripted "
        "scrolls ignore a preference the stylesheet has already obeyed")


# The frame's own poll loops: 1 s in MindParallel (workspace_page.py) and
# Self-Reliant (research_page.py), 1.2 s in MindSwarm (swarm_page.py). The
# window is sized so a loop that is running produces several requests and one
# that is not produces exactly zero — a margin, not a coin flip.
WS_WINDOW_MS = 4000
WS_SETTLE_MS = 1500
WS_START_TIMEOUT_MS = 20000


def _ws_calls(seen):
    return [u for u in seen if "/api/workspace/" in u]


def probe_a_hidden_workspace_frame_stops_polling(browser, url):
    """Leaving a workspace section must stop it talking to the server.

    `#graph` hosts all three workspace pages, and each runs a self-scheduling
    poll loop of its own. Hiding an iframe does NOT stop its timers, and leaving
    the section only toggled a class — so the frame went on fetching
    `/api/workspace/agents` about once a second, indefinitely, for a section
    nobody was looking at, on top of this page's own 2.5 s poll. Measured on the
    real page before the fix: 5 requests in a 6 s window after clicking away, and
    6 more in the next one.

    Counted from the REQUEST side rather than from inside the frame, because
    what is wrong with the old behaviour is what the SERVER sees.

    The open window is the anti-vacuity half and it is load-bearing: zero
    requests after leaving is also what a frame that never started, or a
    workspace page that is simply broken, would produce.
    """
    seen = []
    with opened(browser, url) as page:
        page.on("request", lambda r: seen.append(r.url))
        settled(page)
        page.click('button[data-sec="ws-parallel"]')

        # The frame has to LOAD before it can poll; a window measured during
        # that load would report the quiet this test looks for, for the wrong
        # reason. Wait for the loop to prove itself first.
        waited = 0
        while not _ws_calls(seen) and waited < WS_START_TIMEOUT_MS:
            page.wait_for_timeout(250)
            waited += 250
        assert _ws_calls(seen), (
            f"the workspace frame made no request at all in {waited} ms — it "
            f"never loaded, so nothing below can distinguish a fix from a "
            f"broken section")

        seen.clear()
        page.wait_for_timeout(WS_WINDOW_MS)
        while_open = len(_ws_calls(seen))

        page.click('button[data-sec="overview"]')
        settled(page)
        page.wait_for_timeout(WS_SETTLE_MS)      # let the in-flight one land
        seen.clear()
        page.wait_for_timeout(WS_WINDOW_MS)
        after_leaving = len(_ws_calls(seen))

        # Reported on failure so a red reads as a diagnosis: a frame that is
        # still pointed at its page and still quiet is the fix working; one
        # pointed at about:blank would mean something unloaded it instead.
        still_at = page.evaluate(
            "() => (document.getElementById('graph').getAttribute('src') || '')")

    assert while_open >= 2, (
        f"the open section only made {while_open} workspace requests in "
        f"{WS_WINDOW_MS} ms, so a silent frame afterwards proves nothing")
    assert after_leaving == 0, (
        f"a hidden workspace frame made {after_leaving} requests in "
        f"{WS_WINDOW_MS} ms after the reader left the section — hiding an "
        f"iframe does not stop its timers, so the page has to skip its own "
        f"tick (src is {still_at!r})")


TYPED_INTO_PANE = "half-typed project name"


def probe_a_workspace_frame_keeps_what_was_typed_into_it(browser, url):
    """Leaving a workspace section must not throw away the reader's own text.

    The shell re-pointed `#graph` at its URL on EVERY visit, so a glance at
    Overview and back cost the whole document. MindParallel's project-name box
    is the clean witness: nothing but the reader and the save handler ever
    writes it, unlike MindSwarm's mission line, which the poll rewrites from the
    server every 1.2 s and would have gone empty for reasons of its own.

    The warm-up round-trip is load-bearing. `settled()` returns before
    loadProjects() has answered, so the FIRST visit points the frame at an
    unqualified URL and the next visit re-navigates to the project-qualified
    one — correctly, and it would look exactly like the defect.
    """
    with opened(browser, url) as page:
        settled(page)

        page.click('button[data-sec="ws-parallel"]')
        page.wait_for_timeout(WS_SETTLE_MS)
        page.click('button[data-sec="overview"]')
        page.wait_for_timeout(WS_SETTLE_MS)          # end of the warm-up

        page.click('button[data-sec="ws-parallel"]')
        page.wait_for_timeout(WS_SETTLE_MS)
        page.frame_locator("#graph").locator("#pname").fill(TYPED_INTO_PANE)
        page.wait_for_timeout(300)
        typed = page.frame_locator("#graph").locator("#pname").input_value()

        page.click('button[data-sec="overview"]')
        page.wait_for_timeout(WS_SETTLE_MS)
        page.click('button[data-sec="ws-parallel"]')
        page.wait_for_timeout(WS_SETTLE_MS)
        kept = page.frame_locator("#graph").locator("#pname").input_value()

    assert typed == TYPED_INTO_PANE, (
        f"the field never took the text ({typed!r}), so what it holds after the "
        f"round trip says nothing")
    assert kept == TYPED_INTO_PANE, (
        f"the reader glanced at Overview and came back to {kept!r}: the frame "
        f"was re-navigated, which discards the document and everything typed "
        f"into it")


# Both answers are held open, and the gap between them IS the window under
# test: the picker settles at PROJECTS_HOLD_MS, the section's answer lands at
# GRAPH_HOLD_MS, so the graph is asked for with no project and answered after
# one exists. Without the first delay the picker settles before a click can be
# made at all -- measured, and the reason the anti-vacuity guard below is a
# RuntimeError rather than an assert: the mutant driver reads an AssertionError
# as the probe discriminating, and a probe that never entered its own window
# would then look exactly like a probe that worked.
PROJECTS_HOLD_MS = 2500
GRAPH_HOLD_MS = 5000


def probe_a_section_clicked_at_boot_still_gets_its_answer(browser, url):
    """A section clicked before the project picker settles must still render.

    `apiGet` captures the project a question was asked of and refuses an answer
    that arrives after the picker has moved -- right, and it used to fire on the
    move from NO project to the first one, which is not a switch: `withProj`
    omits the parameter entirely while PROJECT is empty, so the server answered
    from `default_project()`, and loadProjects then sets PROJECT to exactly that
    project. Measured against the real store, clicking Graph at boot produced

        building graph -- failed
        a response for project "" arrived after the picker had already moved

    while the identical click three seconds later rendered 22.8 MB.

    Both vacuity guards are load-bearing and they bracket the click: the picker
    must be EMPTY when the section is chosen (or the run never entered the
    window) and PROJECT must be non-empty afterwards (or nothing moved and the
    guard was never asked anything).
    """
    # The isolated store starts EMPTY, and an empty store offers no project at
    # all -- `loadProjects` then leaves PROJECT at '', nothing ever moves, and
    # both halves of this case pass for the wrong reason. One event is what makes
    # the namespace real enough to be listed and settled on.
    events.append_event("decision", {"text": "a project for the picker to settle on"})

    with opened(browser, url, delay_path=[("/api/projects", PROJECTS_HOLD_MS),
                                          ("/api/graph", GRAPH_HOLD_MS)]) as page:
        page.wait_for_selector('button[data-sec="graph"]', timeout=15000)
        picker_empty = page.evaluate(
            "() => document.getElementById('proj').options.length === 0")

        page.click('button[data-sec="graph"]')
        page.wait_for_timeout(GRAPH_HOLD_MS + 6000)
        state = page.evaluate("""() => ({
          project: PROJECT,
          srcdoc: (document.getElementById('graph').getAttribute('srcdoc') || '').length,
          status: document.getElementById('status').textContent,
          panel: (document.getElementById('out').textContent || '').trim().slice(0, 160),
        })""")

    if not picker_empty:
        raise RuntimeError(
            "the picker was already populated when the section was clicked, so "
            "this run never entered the window the defect lives in")
    if not state["project"]:
        raise RuntimeError(
            "PROJECT never moved off '', so no transition happened and the guard "
            "under test was never asked anything -- reading the picker's option "
            "COUNT instead of PROJECT itself hid exactly this, because an empty "
            "store still renders one '(no projects)' option")
    assert state["srcdoc"] > 0, (
        f"a section clicked before the picker settled lost its own answer: "
        f"status {state['status']!r}, panel {state['panel']!r}")


# The skeleton fires 150 ms after a section load starts, so the click that has
# to land inside that window is made at 60 ms. /api/overview is held open for
# longer than both, which is what turns a 2-in-4 race into a fact: warm, the
# overview memo answers inside 150 ms and no skeleton is scheduled at all.
SKEL_RACE_MS = 60
OVERVIEW_HOLD_MS = 1500


def probe_a_workspace_click_survives_the_section_it_left(browser, url):
    """Choosing a workspace section must not be undone by the section left behind.

    showWorkspace was the one section entry that swapped the panel without going
    through run(), so it never bumped RUN_GEN and the departing section's run
    still passed its own current() check. That run's skeleton timer then painted
    into #view, and paintInto's only(view) hid the frame the reader had already
    chosen — leaving the ws-parallel button lit above Overview's placeholder and
    a section that never arrives.

    The assertion is on what the READER ends up looking at, not on the source
    shape: the frame visible, pointed at a workspace URL.
    """
    with opened(browser, url, delay_path="/api/overview",
                delay_ms=OVERVIEW_HOLD_MS) as page:
        settled(page)

        # Two separate section loads, and they have to be separate. Waiting for
        # the skeleton to APPEAR proves the window is real, but it also carries
        # the clock past it — do that in the same load as the trial and the
        # click lands after the timer has already fired, which is how the first
        # cut of this probe let the mutant through. So: arm on one load, click
        # inside the window on the next.
        page.click('button[data-sec="overview"]')
        page.wait_for_function(
            "() => document.getElementById('view').innerHTML.includes('skel')",
            timeout=10000)
        settled(page)

        page.click('button[data-sec="overview"]')
        page.wait_for_timeout(SKEL_RACE_MS)     # still inside the 150 ms window
        page.click('button[data-sec="ws-parallel"]')
        page.wait_for_timeout(4000)     # past the skeleton, past a poll tick
        state = page.evaluate("""() => {
          const g = document.getElementById('graph');
          const btn = document.querySelector('button[data-sec].on');
          return {hidden: g.classList.contains('hidden'),
                  src: (g.getAttribute('src') || '(none)').split('?')[0],
                  lit: btn ? btn.dataset.sec : '(none)',
                  status: document.getElementById('status').textContent};
        }""")

    assert state["lit"] == "ws-parallel", (
        f"the section button says {state['lit']!r}, so this run never got as far "
        f"as the behaviour under test")
    assert not state["hidden"] and "/api/workspace/" in state["src"], (
        f"the reader picked a workspace section and the panel went back to the "
        f"one they left: frame hidden={state['hidden']} src={state['src']!r} "
        f"while the lit button is {state['lit']!r}")
    # run() owns the chip now; a section that never clears it reads as stuck.
    assert state["status"] != "loading overview", (
        f"the departing run left the status chip on: {state['status']!r}")


# (parent, markup, the rule under test). One row per cat STATE, because the
# states are the whole difficulty: a lit cat already carries `mn-bob`, and the
# rule that puts it there out-specifies a bare sprite-plus-hover. A single idle
# row would pass against a stylesheet where every lit cat silently ignored the
# cursor — which is the version an operator would actually meet, since the lit
# cats are the ones worth pointing at.
#
# Pinned to `body` under fixed positioning rather than dropped into `#view`, for
# two reasons the first run of this probe found the hard way. A lit cat is
# ALREADY bobbing, and Playwright's `hover` waits for an element to hold still,
# which an infinite animation never does — it timed out after thirty seconds
# reporting `element is not stable`, which is the feature working. And `#view` is
# rewritten under the cats poll, which detached the probe mid-wait. Fixed
# position takes the node out of the repaint, and the cursor is moved by
# coordinate below rather than through actionability, which is what makes a
# permanently-moving target hoverable at all.
PINNED = "position:fixed;left:12px;top:240px;z-index:9999"
SPRITE = ('<svg class="mn-cat-sprite" id="probe" width="16" height="16"'
          ' viewBox="0 0 12 12"></svg>')

HOVER_MOTION = [
    ("body", f'<span class="mn-cat" style="{PINNED}">{SPRITE}</span>',
     ".mn-cat-sprite:hover"),
    ("body", f'<span class="mn-cat active" style="{PINNED}">{SPRITE}</span>',
     ".mn-cat.active svg:hover"),
    ("body", f'<span class="mn-cat dim" style="{PINNED}">{SPRITE}</span>',
     ".mn-cat.dim svg:hover"),
    ("body", f'<span class="mn-sess working" style="{PINNED}">{SPRITE}</span>',
     ".mn-sess.working .mn-cat-sprite:hover"),
    ("body", f'<span class="mn-inj verbatim" style="{PINNED}">{SPRITE}</span>',
     ".mn-inj.verbatim .mn-cat-sprite:hover"),
]

DANCE = "mn-dance-step, mn-dance-tilt, mn-dance-squash"
# The three tracks write these three properties, one each. Read back off the
# COMPUTED style while the cursor is down, they are what proves the tracks
# compose instead of overwriting one another.
DANCE_PROPS = ("translate", "rotate", "scale")


def probe_hover_motion(browser, url):
    """The hover dance, executed, in every cat state — and silenced under the
    preference like every other animation on this page.

    Asserting the animation NAMES rather than "something animates" is what makes
    this discriminating. Drop the qualified selectors and a lit cat still reports
    a running animation on hover — `mn-bob`, the one it was already running —
    so an is-it-moving check would stay green while the feature was gone for
    exactly the cats it was asked for.

    The second half asserts something the name check cannot reach: that the three
    tracks actually RUN AT ONCE. Two animations that both write the `transform`
    shorthand do not reliably combine — one entry takes the property — so the
    choreography is carried on the individual transform properties, which
    compose by construction. Rewrite any track back to the shorthand and the
    animation-name list is completely unchanged while its property stops moving,
    which is precisely the regression this samples for."""
    def animation_name(parent, html, *, hover, **ctx_kw):
        with opened(browser, url, **ctx_kw) as page:
            page.eval_on_selector(parent, "(el, h) => el.insertAdjacentHTML('beforeend', h)",
                                  html)
            if hover:
                # By coordinate, deliberately. `page.hover` waits for the target
                # to hold still and a lit cat never does; the centre of a sprite
                # bobbing two pixels is inside the sprite the whole time anyway.
                at = page.eval_on_selector("#probe", """el => {
                  const r = el.getBoundingClientRect();
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }""")
                page.mouse.move(at["x"], at["y"])
            return page.eval_on_selector("#probe",
                                         "el => getComputedStyle(el).animationName")

    for parent, html, rule in HOVER_MOTION:
        resting = animation_name(parent, html, hover=False)
        hovered = animation_name(parent, html, hover=True)
        still = animation_name(parent, html, hover=True, reduced_motion="reduce")
        assert hovered == DANCE, (
            f"`{rule}` computes `{hovered}` under the cursor, not `{DANCE}` — the "
            f"cat does not answer the pointer (at rest it runs `{resting}`, so a "
            f"rule that merely loses the cascade reports that name here)")
        assert still == "none", (
            f"`{rule}` keeps running `{still}` under prefers-reduced-motion — the "
            f"dance loops and is asked for, but the preference is about motion, "
            f"not about who asked. One `animation:none` has to clear all three "
            f"tracks, and it does because it clears the whole list")

    # The composition claim, sampled once. It is a property of the KEYFRAMES,
    # not of the selectors, so one state answers it for all five.
    parent, html, rule = HOVER_MOTION[0]
    with opened(browser, url) as page:
        page.eval_on_selector(parent, "(el, h) => el.insertAdjacentHTML('beforeend', h)",
                              html)
        at = page.eval_on_selector("#probe", """el => {
          const r = el.getBoundingClientRect();
          return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        }""")
        page.mouse.move(at["x"], at["y"])
        # Sampled across real frames rather than read once: a single reading
        # cannot tell a moving property from a parked one.
        seen = page.evaluate("""(props) => new Promise(res => {
          const el = document.getElementById('probe');
          const out = {}; props.forEach(p => out[p] = new Set());
          let n = 0;
          requestAnimationFrame(function tick() {
            const cs = getComputedStyle(el);
            props.forEach(p => out[p].add(cs[p]));
            if (++n < 8) requestAnimationFrame(tick);
            else res(Object.fromEntries(props.map(p => [p, [...out[p]]])));
          });
        })""", list(DANCE_PROPS))
    for prop in DANCE_PROPS:
        values = seen[prop]
        assert len(values) > 1, (
            f"`{prop}` held one value ({values!r}) across eight frames of the "
            f"dance, so its track is not reaching the element — the other tracks "
            f"still run and `animationName` still lists all three, which is why "
            f"this is asserted separately from the names above. The usual cause "
            f"is a track rewritten to the `transform` shorthand, where the last "
            f"list entry takes the property and the rest silently do nothing")


def _dance_settles(page, parent, html, *, settle_ms):
    """Hover the pinned sprite once it has stopped moving, wait `settle_ms` past
    the moment its dance actually starts, then report how many distinct values
    each dance property takes across eight frames. A parked track reports one; a
    running track reports several.

    Both waits are here because both of the premises they replace were false in
    practice; the comments below record what was measured."""
    page.eval_on_selector(parent, "(el, h) => el.insertAdjacentHTML('beforeend', h)", html)
    # Aim at a sprite that has stopped ARRIVING. `.mn-sess` carries a 240 ms
    # entrance that translates its row 4 px, and on a booting page that entrance
    # resolves whenever the main thread gets to it -- measured here still
    # travelling at 620 ms, long after the pointer had been placed. A pointer
    # aimed at the edge of a box that then slides 3 px falls outside it: that
    # fires `mouseout`, CANCELS all three tracks, and starts them again on the
    # way back in, so the read lands ~380 ms into a fresh first pass and reports
    # a correctly capped dance as an endless one. The stylesheet was right the
    # whole time -- `animation-iteration-count` read 1 throughout, and all three
    # grey states settle cleanly once the pointer is aimed at a still target.
    #
    # Stillness is asserted geometrically rather than by waiting on the entrance
    # by name: an arrival animation added later to any wrapper this helper is
    # handed would bring the same defect straight back, and five identical
    # frames say "not moving" without having to know what was moving.
    page.wait_for_function("""() => {
      const r = document.getElementById('probe').getBoundingClientRect();
      const key = [r.x, r.y, r.width, r.height].join(',');
      window.__still = (window.__rect === key) ? (window.__still || 0) + 1 : 0;
      window.__rect = key;
      return window.__still >= 5;
    }""", timeout=5000)
    at = page.eval_on_selector("#probe", """el => {
      const r = el.getBoundingClientRect();
      return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }""")
    # `settle_ms` is a duration past the longest TRACK, so it is counted from the
    # dance's own start -- and the dance begins when the browser next resolves
    # style for `:hover`, which on a booting page is up to half a second after
    # the pointer arrives (measured: 467 ms). The listener goes on BEFORE the
    # move and RECORDS the start rather than polling for a running animation: a
    # capped dance empties `getAnimations()` on the far side of its single pass,
    # so a late poll cannot tell "never started" from "already finished" --
    # opposite verdicts read off one empty list.
    page.eval_on_selector("#probe", """el => {
      window.__danceAt = null;
      el.addEventListener('animationstart', () => {
        if (window.__danceAt === null) window.__danceAt = performance.now();
      });
    }""")
    page.mouse.move(at["x"], at["y"])
    page.wait_for_function("() => window.__danceAt !== null", timeout=5000)
    page.wait_for_timeout(settle_ms)
    return page.evaluate("""(props) => new Promise(res => {
      const el = document.getElementById('probe');
      const out = {}; props.forEach(p => out[p] = new Set());
      let n = 0;
      requestAnimationFrame(function tick() {
        const cs = getComputedStyle(el);
        props.forEach(p => out[p].add(cs[p]));
        if (++n < 8) requestAnimationFrame(tick);
        else res(Object.fromEntries(props.map(p => [p, [...out[p]]])));
      });
    })""", list(DANCE_PROPS))


# Past the longest track (780ms) plus a frame or two of slack. Read BEFORE this
# and a dim cat is still mid-pass, which is the feature, not the defect.
SETTLE_MS = 900


# Every grey state this page can paint, and what its greyness reports. Grey is
# the board's whole vocabulary for absence and it is spoken in three separate
# strips, so this is a census: a fourth grey state added later with no row here
# is exactly the regression the list is shaped to make visible.
GREY_STATES = [
    ("mn-cat dim", "a faculty has gone quiet"),
    ("mn-sess resting", "a Claude session is sitting idle"),
    ("mn-inj silent", "a turn's injection delivered nothing at all"),
]


def probe_dim_cat_dances_once(browser, url):
    """Every GREY cat answers the cursor once and then stops; a live one keeps
    going.

    Grey is how this board says "this did not happen" — each of the three states
    pairs `filter: grayscale(100%)` with reduced opacity for that reason — and a
    dance that never ends is the loudest available claim to be alive. So a grey
    cat gets exactly one pass: enough to say "seen", not enough to say "well".

    `.mn-inj.silent` is the row that makes this more than tidiness. That strip
    exists to report which injections reached the model and which did not, and a
    silent one is the failure case. A silent cat dancing on loop states the
    opposite of the only fact its row carries.

    Asserted by SAMPLING, not by reading `animation-iteration-count` back. The
    declared count is the mechanism; whether the cat has actually stopped moving
    at the far side of it is the behaviour, and only one of those is what an
    operator sees. `animationName` is no help here either — it still names all
    three tracks long after they have finished running.

    The live cat at the end is what stops this from passing against a stylesheet
    where the dance is broken for everyone."""
    for cls, reports in GREY_STATES:
        markup = f'<span class="{cls}" style="{PINNED}">{SPRITE}</span>'
        with opened(browser, url) as page:
            settled = _dance_settles(page, "body", markup, settle_ms=SETTLE_MS)
        for prop in DANCE_PROPS:
            assert len(settled[prop]) == 1, (
                f"`.{cls.replace(' ', '.')}` kept moving its `{prop}` {SETTLE_MS} ms "
                f"in ({settled[prop]!r}) — the dance is looping on a cat whose "
                f"greyness says {reports}. An endless dance overrides the "
                f"grayscale filter instead of agreeing with it")

    live = f'<span class="mn-cat" style="{PINNED}">{SPRITE}</span>'
    with opened(browser, url) as page:
        still_going = _dance_settles(page, "body", live, settle_ms=SETTLE_MS)
    moving = [p for p in DANCE_PROPS if len(still_going[p]) > 1]
    assert moving, (
        f"a LIVE cat had also stopped by {SETTLE_MS} ms ({still_going!r}), so the "
        f"grey assertions above prove nothing — they would pass just as well "
        f"against a page where the dance never loops for anybody")


# One faculty cat, and the SAME cat two and a half hours older. The age gap is
# chosen to cross `catAge`'s minutes/hours boundary, so a refreshed title reads
# "2.5h" and a frozen one still reads "6m" — a two-second bump would render
# identically and prove nothing.
CATS_A = [{"faculty": "task-graph", "display": "dim", "status": "idle",
           "age_secs": 400}]
CATS_B = [{"faculty": "task-graph", "display": "dim", "status": "idle",
           "age_secs": 9000}]
CATS_C = [{"faculty": "task-graph", "display": "active", "status": "active",
           "age_secs": 9000}]
SESS_A = [{"session_id": "s1", "display": "working", "kind": "chat",
           "title": "wire it up", "doing": "writing", "task": "T",
           "phase": "p", "skill": "k", "age_secs": 60}]
SESS_B = [dict(SESS_A[0], age_secs=9000)]


def probe_unchanged_poll_holds_structure_and_refreshes_the_clock(browser, url):
    """A poll that brings nothing new keeps every element AND still moves the
    clock. Both strips, because both had half of this wrong.

    `paintCats` and `paintSessions` clear their container and rebuild it, so
    without a guard every 2.5 s tick hands each cat a brand-new element — and a
    brand-new element restarts its animation from 0%. Measured on the live board
    before the faculty guard landed: a dim cat, whose hover dance is capped to a
    single pass, produced four movement bursts in eight seconds, spaced at the
    poll interval. It did not dance once. It pulsed.

    The guard then bought that with a second defect, which is the half this
    probe exists to keep fixed. `age_secs` has to stay out of the signature — it
    ticks every poll, and a signature holding it can never match — so a strip
    whose signature was unchanged got skipped ENTIRELY and its ages froze. Not
    "a poll behind", which is what the session guard's comment claimed, but
    frozen until some other field moved: driven from 400 s to 9000 s, the strip
    went on reporting "6m ago", and so did the open panel someone was reading.

    So the assertion is a conjunction, and neither half is safe alone. Node
    IDENTITY, because a rebuilt strip renders identically and only the objects
    decide whether an animation survives. Title TEXT, because holding the nodes
    is worthless if what they say is wrong.

    The third paint is the opposite failure: a guard can also suppress a repaint
    that mattered. `display` flips, and it must come through."""
    with opened(browser, url) as page:
        settled(page)
        got = page.evaluate("""([a, b, c, sa, sb]) => {
          const bar = document.getElementById('mn-cats');
          const sbar = document.getElementById('mn-sessions');
          if (!bar || !sbar) return {error: 'the page serves no cat strips'};
          paintCats(a);
          const first = bar.querySelector('.mn-cat');
          const titleA = first.title;
          first.click();                      // open the panel and read it too
          const panel = document.getElementById('mn-cat-panel');
          const panelA = panel.textContent;
          paintCats(b);                       // same cats, 2.5 h older
          const second = bar.querySelector('.mn-cat');
          // Read WHILE the faculty panel is still the one on screen. The
          // session click further down takes the panel over -- one panel serves
          // both strips -- so reading this at the end would compare the faculty
          // cat's "before" against the session cat's "after".
          const panelB = panel.textContent;
          paintCats(c);                       // display flips: MUST repaint
          const third = bar.querySelector('.mn-cat');

          // The session strip is driven the other way round on purpose: aged
          // FIRST and clicked open only afterwards, so what it opens with comes
          // from whatever the skipped poll left behind on the element.
          paintSessions({sessions: sa, sessions_hidden: 0, sessions_total: 1,
                         producers: {session: true}});
          const s1 = sbar.querySelector('.mn-sess');
          const sTitleA = s1.title;
          paintSessions({sessions: sb, sessions_hidden: 0, sessions_total: 1,
                         producers: {session: true}});
          const s2 = sbar.querySelector('.mn-sess');
          s2.click();
          const sessPanelAfter = panel.textContent;

          return {kept: first === second, titleA, titleB: second.title,
                  panelA, panelB,
                  redrew: third !== second,
                  cls: third ? third.className : null,
                  sig: !!bar.dataset.sig,
                  sessKept: s1 === s2, sTitleA, sTitleB: s2.title,
                  sessPanelAfter};
        }""", [CATS_A, CATS_B, CATS_C, SESS_A, SESS_B])

    assert not got.get("error"), got["error"]
    assert got["sig"], (
        "the faculty strip recorded no signature at all, so it cannot suppress "
        "anything — every poll will keep rebuilding it")
    for label, kept in (("faculty", got["kept"]), ("session", got["sessKept"])):
        assert kept, (
            f"an unchanged poll replaced the {label} cat's element. A new element "
            f"restarts its animation from 0%, so a dim cat's single pass begins "
            f"again every 2.5 s and reads as a pulse. `age_secs` ticking is not a "
            f"change worth a rebuild — it is why the signature excludes it")
    for label, before, after in (("faculty title", got["titleA"], got["titleB"]),
                                 ("open panel", got["panelA"], got["panelB"]),
                                 ("session title", got["sTitleA"], got["sTitleB"])):
        assert before != after, (
            f"the {label} still reads {after!r} after the cat aged from 400 s to "
            f"9000 s. Holding the nodes is only half the job: `age_secs` is out "
            f"of the signature by design, so a skipped poll must still re-apply "
            f"the text or the strip freezes on a number that was true once")
        assert "2.5h" in after, (
            f"the {label} changed but does not carry the new age: {after!r}")
    assert got["redrew"], (
        f"a poll that changed `display` was suppressed too (class still "
        f"{got['cls']!r}) — the guard is now hiding real state changes, which is "
        f"worse than the repaint it was added to stop")
    assert "active" in (got["cls"] or ""), (
        f"the strip repainted but did not take the new display: {got['cls']!r}")
    # The third way this can be wrong, and the subtlest: a click handler that
    # captured its payload in a closure keeps serving whatever the last
    # EXECUTED paint gave it. The element is fresh, the title is fresh, and the
    # panel that opens underneath disagrees with both.
    assert "2.5h" in got["sessPanelAfter"], (
        f"a session cat whose title reads {got['sTitleB']!r} opened a panel "
        f"reading {got['sessPanelAfter'][:90]!r}. The refresh moved the title but "
        f"the click handler is still rendering from the payload it closed over at "
        f"paint time — hover and click have to agree, or the newer number just "
        f"makes the older one look authoritative")


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


READ_RAIL = """() => {
  const on = [...document.querySelectorAll('nav button.on')];
  const dot = document.getElementById('navdot');
  const b = on[0];
  return {lit: on.length, label: b ? b.textContent : null,
          current: b ? b.getAttribute('aria-current') : null,
          claimed: [...document.querySelectorAll('nav button[aria-current]')].length,
          // The Y OFFSET, parsed, not the declaration text: the engine
          // normalises `0 16px` to `0px 16px` on the way back out, and a test
          // that compares the string is asserting a serialisation rule.
          at: parseFloat((dot.style.translate.split(' ')[1] || '0').replace('px', '')),
          want: b ? b.offsetTop : null,
          shown: dot.classList.contains('lit')};
}"""


def probe_active_section(browser, url):
    """The rail says which section is on screen, and the indicator goes there.

    A 26-button rail with no active state makes the user hold the answer in
    their head, and the board's whole job is to stop them having to. Three
    separate facts are checked because three separate things can rot: the
    class (what the eye reads), `aria-current` (what a screen reader reads --
    a colour alone is half a state), and where the travelling bar actually
    ENDED UP. The last one is the reason this is a browser test at all: the
    bar is positioned from `offsetTop`, which only exists once the page has
    laid itself out, and a rendered-source check cannot see it.

    Asserted after the FIRST section too, not only after the switch: the boot
    path marks the rail from inside `showOverview()` rather than from a click
    handler, so a rail that only ever lit on click would pass a switch-only
    test while arriving blank."""
    with opened(browser, url) as page:
        settled(page)
        first = page.evaluate(READ_RAIL)
        assert first["lit"] == 1 and first["label"] == "Overview", (
            f"the rail does not name the section the page lands on: {first}")
        assert first["shown"] and first["at"] == first["want"], (
            f"the indicator is not parked on the active button: {first}")

        page.click("nav button:text-is('Log')")
        settled(page)
        moved = page.evaluate(READ_RAIL)
        assert moved["lit"] == 1, (
            f"{moved['lit']} buttons claim to be the active section; exactly "
            f"one can be")
        assert moved["label"] == "Log", (
            f"clicking Log left the rail pointing at {moved['label']!r}, so it "
            f"is reporting a section the panel is no longer showing")
        assert moved["current"] == "page" and moved["claimed"] == 1, (
            f"the active section is said in colour but not to assistive tech: "
            f"{moved}")
        assert moved["at"] == moved["want"] and moved["at"] != first["at"], (
            f"the indicator did not travel to the new button: {moved}")


def probe_no_sideways_scroll(browser, url):
    """One unbreakable token must not drag the whole board sideways.

    The panel is a `1fr` grid item, so its track grows to its own min-content
    and the document grows with it: a single word that cannot break sets the
    width of everything. The row is manufactured here rather than read from the
    store, because the suite's ledger is a throwaway and an empty Log would pass
    this vacuously -- but the shape is not invented. `hive log --files` is
    space-separated, so an entry that passed a comma-joined string recorded one
    path of the form `a.py,b.py,c.py`, and the real ledger holds several,
    measured at 1209 px of unbreakable text.

    Asserted on `scrollWidth` vs `clientWidth` -- whether the page can be
    scrolled sideways at all -- rather than on the CSS declaration, because the
    declaration is the mechanism and the scrollbar is what the operator gets."""
    with opened(browser, url) as page:
        settled(page)
        page.evaluate("""() => {
          const path = Array.from({length: 14},
            (_, i) => 'src/mindhive/some_module_' + i + '.py').join(',');
          document.getElementById('view').insertAdjacentHTML('beforeend',
            '<ul class="hits"><li><div class="file">an entry</div>'
            + '<div class="snip">' + path + '</div></li></ul>');
        }""")
        page.wait_for_timeout(200)
        size = page.evaluate("""() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth,
          token: Math.round(document.querySelector('.hits .snip')
                            .getBoundingClientRect().width)})""")
        assert size["token"] > 0, "the manufactured row did not render"
        assert size["scroll"] <= size["client"], (
            f"one unbreakable token widened the document to {size['scroll']}px "
            f"inside a {size['client']}px viewport, so every section of the "
            f"board now scrolls sideways: {size}")


def probe_pointer_surfaces_react(browser, url):
    """A hand cursor is a promise; this is the payment.

    The source census (`test_every_pointer_cursor_surface_answers_the_cursor`)
    proves a `:hover` rule EXISTS for every surface that turns the cursor into a
    hand. It cannot prove the rule reaches the element -- a specificity loss, a
    token that resolves to the same colour it replaced, or a `transition` on a
    property nothing changes all leave the rule in the file and the surface
    dead. So this reads the computed background before and after the pointer
    arrives and requires it to MOVE.

    Four surfaces, chosen because they are four different ways of being
    clickable: a real `<button>` in the panel (which shipped wearing the OS
    default cursor and no hover at all), the header's live chip (a bare span
    that toggles polling), the project `<select>` (which opens a menu on click
    and was still showing the OS arrow, while changing what the whole board is
    looking at), and a roadmap step (a row whose glyph already spends its colour
    on state, so the ground is what is left to light). The step is manufactured
    rather than read from the store -- the suite's ledger is a throwaway and an
    empty roadmap would pass this vacuously.

    The pointer is parked off-element between surfaces: hovering the next one
    would release the previous one anyway, but the last surface's reading would
    then depend on the order of the list."""
    with opened(browser, url) as page:
        settled(page)
        page.click("nav button:text-is('Recall')")
        settled(page)
        # Into `#panel`, not `#view`: `showText` calls `only(out)`, which hides
        # `#view` outright -- an injected row would be present, invisible, and
        # would fail as a hover timeout rather than as a dead surface.
        page.evaluate("""() => {
          document.getElementById('panel').insertAdjacentHTML('beforeend',
            '<div class="rmstep"><span class="rmnode">*</span>a step</div>');
        }""")
        page.wait_for_timeout(50)

        # Ground AND edge. Three of these light their background; the project
        # selector lights its border instead, deliberately -- it holds a value,
        # and a ground tint on a filled control argues with the focus ring that
        # lands on the same element a moment later. A probe that read only
        # `backgroundColor` would call that surface dead.
        dead = []
        for sel, name in (("#controls button", "the Search button"),
                          ("header #live", "the live chip"),
                          ("#proj", "the project selector"),
                          (".rmstep", "a roadmap step")):
            read = ("el => { const c = getComputedStyle(el);"
                    " return c.backgroundColor + ' / ' + c.borderColor; }")
            before = page.eval_on_selector(sel, read)
            page.hover(sel)
            page.wait_for_timeout(300)       # the tracks are .15s
            after = page.eval_on_selector(sel, read)
            page.mouse.move(0, 0)
            page.wait_for_timeout(200)
            if before == after:
                dead.append(f"{name} (`{sel}`) held {before} under the pointer")
        assert not dead, (
            "these surfaces show a hand cursor and then do not change at all "
            "when the pointer is actually on them, so the page is promising a "
            "click and giving no confirmation the target is live: "
            + "; ".join(dead))


def probe_panel_entrance(browser, url):
    """The panel announces a new section once, and only on arriving at one.

    Counted from real `animationstart` events rather than from the class,
    because the class is exactly what a broken re-trigger leaves looking right:
    remove-then-add inside one task is a no-op the browser never sees, so the
    element ends the turn wearing `swap` either way and only the SECOND section
    of a session goes silent. A class check would pass against that.

    Three facts, and the third is the reason the entrance is wired to
    `markSection` and not to `only()`. `only()` runs on every paint, and Log
    repaints its whole list every 2.5 s under the live poll -- an entrance wired
    there would make the board pulse at the reader indefinitely, which is the
    opposite of what a 220 ms arrival is for. So: exactly one per entry, again
    on the next entry, and never from a poll.

    The counter is zeroed immediately before each click and the assertion is on
    the DELTA, because `settled()` is not a boot gate: `#status` can already
    read `ready` before the first section has been asked for, so a run that
    counted from zero would fold the boot's own entrance into the first click's
    total and report a working page as broken.

    The gate is the boot's own entrance EVENT, and nothing earlier. Waiting for
    `nav button.on` looks equivalent -- `markSection` sets the class one line
    after it re-triggers the animation -- and it is not: the class lands
    synchronously and `animationstart` is dispatched at the next animation
    update, so the gate opened, the counter was zeroed, and the boot's event
    then arrived and was charged to the first click. That is what it looked
    like: two entrances 100 ms apart, green in isolation and red in a full-tier
    run, which is the shape of a test that races the thing it measures rather
    than of a page that misbehaves."""
    with opened(browser, url) as page:
        page.evaluate("""() => {
          window.__swaps = 0; window.__at = [];
          document.getElementById('panel').addEventListener('animationstart',
            e => { if (e.animationName === 'panelIn') {
              window.__swaps++; window.__at.push(Math.round(performance.now())); } });
        }""")
        page.wait_for_function("window.__swaps >= 1")   # the boot's own entrance
        settled(page)

        def entrances_from(click):
            page.evaluate("window.__swaps = 0; window.__at = [];")
            page.click(f"nav button:text-is('{click}')")
            page.wait_for_timeout(500)
            return page.evaluate("window.__swaps")

        first = entrances_from("Log")
        assert first == 1, (
            f"arriving at Log started the panel entrance {first} times, not "
            f"once, so it is running on paints rather than on arrivals; started "
            f"at {page.evaluate('window.__at')} ms")

        second = entrances_from("State")
        assert second == 1, (
            f"the next section arrived with {second} entrances: the one-shot "
            f"was never re-triggered, which is exactly what remove-then-add "
            f"with no layout read between them does -- the browser never sees "
            f"the intermediate state, so nothing restarts")

        # Now the poll, and LOG is the section it has to be measured on. Overview
        # would read as green whatever the answer: its poll compares a shape
        # signature and writes values in place, so it never repaints structure on
        # an unchanged tick and `only()` is not reached at all. Log rebuilds its
        # whole list every 2.5 s, so a repaint-driven entrance is visible there
        # and the mutant that wires one can actually be caught.
        entrances_from("Log")
        settled(page)
        page.evaluate("window.__swaps = 0")
        page.wait_for_timeout(3200)          # more than one LIVE_MS tick
        idle = page.evaluate("window.__swaps")
        assert idle == 0, (
            f"the panel re-announced itself {idle} times while simply sitting "
            f"on Log, so a live poll is being read as a section change and the "
            f"board never stops moving")


def probe_header_holds_its_height(browser, url):
    """The status line cannot become two lines, so the header cannot jump.

    `#status` is the one piece of text on the board that changes on every click
    -- "ready" becomes "loading change log" and back -- and as a shrinkable flex
    item it wrapped: measured on the REAL board at 1280 px, with a project name
    and two live session chips beside it, the header went 52 px -> 66 px and
    pushed every section down 14 px and back on every navigation.

    What is asserted here is the guard as APPLIED -- the computed `white-space`
    and `flex-shrink` on the element itself -- and then that the longest status
    text this page ever sets still occupies exactly one line. Stated plainly:
    the second half cannot fail on this server. The suite's store is a throwaway,
    so the served header carries no project name, no faculty cats and no session
    chips, and there is so much slack that nothing would wrap whatever the sheet
    said; widths from 360 px to 1200 px were measured with the guard removed and
    the header height was identical to the guarded page at every one of them.
    The computed-style half is the half with teeth, and it is not a source
    check: it fails on a specificity loss or an overriding rule, which is how
    this guard would realistically be lost."""
    with opened(browser, url) as page:
        settled(page)
        pinned = page.eval_on_selector("header #status", """el => {
          const cs = getComputedStyle(el);
          return {wrap: cs.whiteSpace, shrink: cs.flexShrink};
        }""")
        assert pinned["wrap"] == "nowrap", (
            f"the status line may wrap again (white-space: {pinned['wrap']}), so "
            f"a longer status than 'ready' takes a second line and the whole "
            f"board shifts down on every click")
        assert pinned["shrink"] == "0", (
            f"the status line is still shrinkable (flex-shrink: "
            f"{pinned['shrink']}), so a crowded header squeezes it first and "
            f"nowrap alone only turns the wrap into a clipped word")

        lines = page.eval_on_selector("header #status", """el => {
          const one = el.getBoundingClientRect().height;
          el.textContent = 'loading change log';
          const many = el.getBoundingClientRect().height;
          el.textContent = 'ready';
          return [Math.round(one), Math.round(many)];
        }""")
        assert lines[0] == lines[1], (
            f"the longest status text this page sets takes {lines[1]}px against "
            f"{lines[0]}px for the shortest, so the header grows while a section "
            f"loads: {lines}")


def probe_keyboard_rail(browser, url):
    """The rail answers the keyboard, and stops answering it mid-query.

    Sixteen sections and the only way in was a click. The shortcut is the easy
    half; the half that decides whether a shortcut is usable at all is that a
    bare key must stop being a shortcut the moment the reader is writing, and
    `/` is a character long before it is a command. Both halves are asserted
    here, and the second one is the reason this probe types into the query box
    rather than only pressing keys at the document.

    Escape is checked last and separately because it is the one key that has to
    work WHILE typing -- getting out of a field is what it is for, so it cannot
    sit behind the same guard as the rest."""
    with opened(browser, url) as page:
        settled(page)
        HERE = "() => (document.querySelector('nav button.on') || {}).textContent"
        start = page.evaluate(HERE)
        assert start == "Overview", f"the page did not land on Overview: {start!r}"

        page.keyboard.press("Alt+ArrowDown")
        settled(page)
        down = page.evaluate(HERE)
        assert down != start, (
            f"Alt+ArrowDown left the rail on {down!r}, so the keyboard cannot "
            f"move between sections at all")

        page.keyboard.press("Alt+ArrowUp")
        settled(page)
        back = page.evaluate(HERE)
        assert back == start, (
            f"Alt+ArrowUp went to {back!r} instead of back to {start!r}, so the "
            f"two directions do not undo each other")

        page.keyboard.press("/")
        settled(page)
        assert page.evaluate(HERE) == "Recall", (
            "`/` did not reach Recall, so the one shortcut most readers try "
            "first does nothing")
        assert page.evaluate("document.activeElement.id") == "q", (
            "`/` reached Recall and left the caret somewhere else, so the "
            "reader still has to click the box before typing")

        # The anti-case, and the whole reason the guard exists: the SAME key,
        # now that a field has focus, has to be a character.
        page.keyboard.press("/")
        page.wait_for_timeout(150)
        typed = page.evaluate("document.getElementById('q').value")
        assert typed == "/", (
            f"typing `/` into the query box produced {typed!r} instead of '/', "
            f"so a bare-key shortcut is eating characters out of a search field")

        page.keyboard.press("Alt+ArrowDown")
        page.wait_for_timeout(150)
        assert page.evaluate(HERE) == "Recall", (
            "Alt+ArrowDown moved section while the caret was in the query box; "
            "modified keys are still keys the reader may be using to edit")

        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        assert page.evaluate("document.activeElement.id") != "q", (
            "Escape did not release the query box, so the only way out of a "
            "field is the mouse")


def probe_card_is_a_door(browser, url):
    """The two numbers that have a section behind them can be opened.

    Both ways in, because a `<div role="button">` keeps neither promise on its
    own: the pointer half is the browser's, the Enter/Space half is ours, and
    shipping one without the other is how a board ends up mouse-only in exactly
    the place it looks most inviting. The other five cards are checked NOT to be
    doors -- a hand cursor over a hash-chain verdict that leads nowhere is the
    defect this whole design round exists to remove."""
    with opened(browser, url) as page:
        settled(page)
        HERE = "() => (document.querySelector('nav button.on') || {}).textContent"

        cards = page.evaluate(
            "() => [...document.querySelectorAll('#view .cards .card')]"
            ".map(c => [c.className, (c.querySelector('.lbl')||{}).textContent])")
        assert cards, "Overview painted no cards, so this probe measured nothing"
        go = sorted(lbl for cls, lbl in cards if "go" in cls.split())
        assert go == ["memory", "sessions"], (
            f"the cards that claim to lead somewhere are {go}, not "
            f"['memory', 'sessions']: either a card promises a section it has "
            f"none of, or one of the two lost its way in")

        # The KEY half runs first, on the Overview the page booted into, and the
        # ordering is the finding rather than a preference. Doing it second --
        # navigate away, come back, focus, press -- was green alone and red in a
        # full-tier run: coming back rebuilds the grid, `settled()` can return
        # while that rebuild is still to come, and focus lands on a node that is
        # then replaced. Enter arrives at `body`, `closest('.card.go')` is null,
        # and the page correctly does nothing. The CLICK half is immune to the
        # same race because Playwright's click re-resolves and waits for
        # stability, so it goes second.
        page.focus("#view .card.go:has(.lbl:text-is('memory'))")
        held = page.evaluate("() => document.activeElement.className")
        assert "card" in held, (
            f"the memory card did not keep focus (it is on {held!r}), so what "
            f"follows would be testing the wrong element, not the key")
        page.keyboard.press("Enter")
        settled(page)
        assert page.evaluate(HERE) == "Memory", (
            "Enter on the memory card did nothing: the card takes focus and "
            "says role=button, and then only the mouse can use it")

        page.click("nav button:text-is('Overview')")
        settled(page)
        page.click("#view .card.go:has(.lbl:text-is('sessions'))")
        settled(page)
        assert page.evaluate(HERE) == "Sessions", (
            "clicking the sessions card did not open the Sessions section")


def probe_back_to_top(browser, url):
    """A long section can be left without a flick of the wheel.

    The height is manufactured -- the suite's ledger is a throwaway and every
    section of it fits on one screen, so a probe that waited for real content
    would be measuring nothing. The real board reaches this on its own: 239
    change-log entries, and the board scrolls the DOCUMENT, so the rail goes
    with it.

    Asserted on `opacity`, on `pointer-events`, and on where the page actually
    ENDS UP -- not on the class, which is what a rule that lost its
    `pointer-events` still wears while being unclickable."""
    with opened(browser, url) as page:
        settled(page)
        page.evaluate("""() => {
          document.getElementById('panel').insertAdjacentHTML('beforeend',
            '<div id="tall" style="height:3000px"></div>');
        }""")
        page.wait_for_timeout(100)
        assert page.evaluate("getComputedStyle(document.getElementById('totop'))"
                             ".opacity") == "0", (
            "the way back is on screen before anything has been scrolled, so it "
            "sits over the board permanently")

        page.evaluate("scrollTo(0, 900)")
        page.wait_for_timeout(400)
        state = page.evaluate("""() => {
          const t = document.getElementById('totop'), cs = getComputedStyle(t);
          return {opacity: cs.opacity, events: cs.pointerEvents, y: window.scrollY};
        }""")
        assert state["y"] > 240, f"the page did not scroll at all: {state}"
        assert state["opacity"] == "1" and state["events"] != "none", (
            f"scrolling past the threshold did not bring the way back: {state}")

        page.click("#totop")
        page.wait_for_function("window.scrollY === 0", timeout=5000)
        parked = page.evaluate(
            "() => document.activeElement.getAttribute('data-sec')")
        assert parked, (
            "the page went back to the top and left focus on whatever was at "
            "the bottom, so the next Tab starts from a list the reader has left")


def probe_overview_comes_back_on_screen(browser, url):
    """Coming back to Overview puts Overview back on screen.

    Found by a probe that was measuring something else entirely, which is worth
    saying plainly: the click half of the card test timed out on an element that
    resolved and never became visible, and the reason was a two-click bug on the
    real board.

    The panel shows ONE of `#view`, `#out` and the graph frame at a time and
    `only()` HIDES the other two rather than emptying them. So after any section
    that renders through `showText` -- Memory with nothing in it, State,
    Briefing, and every error path -- `#view` still contains the last overview
    grid while `#out` is what the reader is looking at. The repaint guard asked
    `view.contains(prev)`, which is true of a hidden pane, so arriving back at
    Overview with the counts unchanged wrote the same numbers into an invisible
    grid and returned. Rail: Overview. Status: ready. On screen: "No memory
    yet".

    Asserted on measured geometry, not on the `hidden` class, because the class
    is the mechanism and what the reader gets is whether the grid has a box.
    Memory is the way in because the suite's ledger is a throwaway and therefore
    always empty -- which is exactly the branch that renders through
    `showText`."""
    with opened(browser, url) as page:
        settled(page)
        SHOWN = """() => {
          const box = el => { const r = el.getBoundingClientRect();
                              return r.width > 0 && r.height > 0; };
          const v = document.getElementById('view'), o = document.getElementById('out');
          const grid = document.querySelector('#view .cards[data-shape]');
          return {view: box(v), out: box(o), grid: !!grid && box(grid),
                  sec: (document.querySelector('nav button.on') || {}).textContent};
        }"""
        first = page.evaluate(SHOWN)
        assert first["grid"] and first["view"], (
            f"the board did not open on a visible overview grid: {first}")

        page.click("nav button:text-is('Memory')")
        settled(page)
        page.wait_for_timeout(200)
        away = page.evaluate(SHOWN)
        assert away["out"] and not away["view"], (
            f"Memory did not take the panel over through the text pane, so this "
            f"probe never reached the state it exists to test: {away}")

        page.click("nav button:text-is('Overview')")
        settled(page)
        page.wait_for_timeout(400)
        back = page.evaluate(SHOWN)
        assert back["sec"] == "Overview", f"the rail did not return: {back}"
        assert back["view"] and back["grid"], (
            f"the rail says Overview and the reader is still looking at the "
            f"previous section: the grid was refreshed while hidden and never "
            f"put back on screen: {back}")


def probe_idle_section_stops_repainting(browser, url):
    """An idle live section repaints ZERO times while the poll keeps running.

    The 2.5 s poll re-runs the whole renderer whether or not anything moved, and
    eight of the nine live sections end that render in showHTML/showText with a
    complete payload -- so the panel was re-parsed every tick to produce the
    pixels that were already there. Measured on the DOM rather than argued from
    the source: a MutationObserver on `#view` counts subtree mutations across
    three whole poll windows.

    The second assertion is what stops this passing for the wrong reason. Zero
    repaints is also what a DEAD board looks like, so the "updated HH:MM:SS"
    stamp must have moved in the same window -- the poll ran, it just had
    nothing to write. Without that half, pausing live updates would make this
    probe green.

    Log is the section, because its payload is fixed for the life of the server
    (the suite's ledger is a throwaway and nothing writes to it), so any
    mutation observed here is churn by definition rather than news."""
    with opened(browser, url) as page:
        settled(page)
        page.click("nav button:text-is('Log')")
        settled(page)
        page.wait_for_timeout(300)

        page.evaluate("""() => {
          window.__churn = 0;
          window.__obs = new MutationObserver(rs => { window.__churn += rs.length; });
          window.__obs.observe(document.getElementById('view'),
                               {childList: true, subtree: true, characterData: true});
          window.__syncAtStart = document.getElementById('synced').textContent;
        }""")
        # Three full poll windows plus slack. LIVE_MS is 2500 in
        # dashboard_js_core.py; this is deliberately wall-clock rather than a
        # wait_for_function, because the thing being measured is the ABSENCE of
        # an event and there is nothing to wait for.
        page.wait_for_timeout(8200)
        got = page.evaluate("""() => {
          window.__obs.disconnect();
          return {churn: window.__churn,
                  before: window.__syncAtStart,
                  after: document.getElementById('synced').textContent,
                  sec: (document.querySelector('nav button.on') || {}).textContent};
        }""")
        assert got["sec"] == "Log", f"the probe drifted off Log: {got}"
        assert got["after"] and got["after"] != got["before"], (
            f"the live stamp did not move in 8.2 s, so the poll was not running "
            f"and a churn count of {got['churn']} proves nothing: {got}")
        assert got["churn"] == 0, (
            f"an idle section mutated its panel {got['churn']} time(s) in three "
            f"poll windows -- the poll is re-parsing markup that did not change, "
            f"which is what drops the reader's selection and focus every 2.5 s")


def probe_a_selection_survives_the_poll(browser, url):
    """What the churn actually costs, stated as the thing a reader loses.

    Selecting text and having it vanish 2.5 s later is the whole defect in one
    gesture: `innerHTML = html` replaces the nodes the selection was anchored
    to, so the range collapses even when the markup is identical. Asserted on
    the selected STRING, not on `rangeCount` -- a collapsed range still counts
    as one."""
    with opened(browser, url) as page:
        settled(page)
        page.click("nav button:text-is('Log')")
        settled(page)
        page.wait_for_timeout(300)
        picked = page.evaluate("""() => {
          const h = document.querySelector('#view h2, #view .file, #view li');
          if (!h) return null;
          const r = document.createRange();
          r.selectNodeContents(h);
          const sel = window.getSelection();
          sel.removeAllRanges(); sel.addRange(r);
          return String(sel).trim();
        }""")
        assert picked, "nothing selectable was painted into #view, so this probe measured nothing"
        page.wait_for_timeout(5200)                    # two whole poll windows
        after = page.evaluate("() => String(window.getSelection()).trim()")
        assert after == picked, (
            f"the reader's selection did not survive the poll: selected "
            f"{picked!r}, {after!r} is left. The panel was rewritten with the "
            f"same markup and took the selection with it")


# --- Round F: the board at depth --------------------------------------------
#
# Everything below is about the part of a section that is NOT the first screen.
# The board's own Log is 270 rows and measured 42016 px tall in a 900 px window
# before this round -- 46 screens on which the rail, the section heading and any
# sense of position had all scrolled away, leaving one fixed button whose only
# offer was to go back to the top and start again.
#
# Each probe manufactures its own height rather than waiting for content: the
# suite's ledger is a throwaway and every section of it fits on one screen, so a
# probe that scrolled the real store here would be measuring nothing at all. The
# markup goes into `#panel` and not `#view`, deliberately -- `showHTML` assigns
# `#view.innerHTML` wholesale and this file already records injected nodes
# vanishing there in 9 runs out of 40.

def _tall(page, extra=""):
    """Give the document something to scroll, and return the header's height."""
    page.evaluate("""(extra) => {
      document.getElementById('panel').insertAdjacentHTML('beforeend',
        extra + '<div id="tall" style="height:6000px"></div>');
    }""", extra)
    page.wait_for_timeout(150)
    return page.evaluate(
        "() => Math.round(document.querySelector('header').getBoundingClientRect().height)")


def probe_the_keyboard_reaches_the_panel(browser, url):
    """One tab from the top of the document to the content.

    The rail is 26 buttons and it comes first in the source, so the keyboard's
    route to the panel was 27 stops long. Measured, not assumed: the count came
    off the served page.

    Both halves are asserted because both ship broken independently. A skip link
    that is `display:none` until focus can never BE focused, so the first Tab
    would land past it; and an anchor pointed at an element that is not
    focusable moves the viewport and leaves the keyboard exactly where it was,
    which is the standard way this feature is half-built."""
    with opened(browser, url) as page:
        settled(page)
        page.keyboard.press("Tab")
        # The link TRAVELS into view (`transition: top .15s`), so a reading taken
        # on the same tick as the keypress is a reading of where it started.
        # Measured, not assumed: the first version of this probe read y=-60 --
        # the resting position -- against a link that was working correctly.
        page.wait_for_timeout(300)
        first = page.evaluate("""() => {
          const a = document.activeElement, r = a.getBoundingClientRect();
          return {id: a.id, tag: a.tagName, y: Math.round(r.y),
                  h: Math.round(r.height)};
        }""")
        assert first["id"] == "skip", (
            f"the first thing the keyboard reaches is {first}, not the skip "
            f"link, so the way into the panel is still 27 tabs of rail")
        assert first["y"] >= 0 and first["h"] > 0, (
            f"the skip link takes focus while it is off-screen or unrendered "
            f"({first}) — a control nobody can see is not an offer")
        page.keyboard.press("Enter")
        page.wait_for_timeout(250)
        landed = page.evaluate("() => document.activeElement.id")
        assert landed == "panel", (
            f"following the skip link left the focus on {landed!r}: the target "
            f"is not focusable, so the page scrolled and the keyboard did not "
            f"move at all")


def probe_the_rail_and_heading_stay_at_depth(browser, url):
    """Navigation and context, at row 200 of a section.

    Three claims, and the third is the one that is easy to ship broken: the rail
    is where the header ends, the heading is where the header ends, and BOTH of
    those are the header's measured height rather than a constant -- it is 52 px
    empty and 68 px once the cat strips arrive, so a sheet that wrote 52 into a
    rule would tuck the rail under the header for the rest of the session.

    The header is GROWN before any of that is asserted, and the growth is the
    point. `--headh` is declared 52px in the sheet as a starting value, and in a
    plain test window the header measures 52px too -- so a board that never
    measured anything would satisfy every assertion below by coincidence, which
    is what the first version of this probe did. Making it 96px first means the
    numbers can only agree if the measurement is real. Growth, rather than a
    fixed second value, is also the real event: the cat strips arrive from a
    poll seconds after load and push the header down.

    The rail is then CLICKED, because being on screen and being usable are
    different claims: a sticky element under something else with a higher
    z-index is visible and inert."""
    with opened(browser, url) as page:
        settled(page)
        _tall(page, '<h2 class="h" id="probehead">a long section</h2>')
        head_h = page.evaluate("""() => {
          const h = document.querySelector('header');
          h.style.minHeight = '96px';
          return new Promise(done => requestAnimationFrame(() =>
            requestAnimationFrame(() =>
              done(Math.round(h.getBoundingClientRect().height)))));
        }""")
        assert head_h == 96, (
            f"the probe meant to grow the header to 96px and it measured "
            f"{head_h}px — it is not testing what it says it is")
        page.evaluate("scrollTo(0, 3000)")
        page.wait_for_timeout(300)
        at = page.evaluate("""() => ({
          y: Math.round(window.scrollY),
          nav: Math.round(document.querySelector('nav').getBoundingClientRect().y),
          head: Math.round(document.getElementById('probehead').getBoundingClientRect().y),
          headh: getComputedStyle(document.documentElement)
                   .getPropertyValue('--headh').trim(),
        })""")
        assert at["y"] > 2000, f"the page did not scroll at all: {at}"
        assert at["headh"] == f"{head_h}px", (
            f"the sheet is working from --headh: {at['headh']} while the header "
            f"is actually {head_h}px tall, so everything that starts below the "
            f"header starts in the wrong place")
        assert at["nav"] == head_h, (
            f"3000px down the section, the rail is at y={at['nav']} instead of "
            f"y={head_h}: it scrolled away with the content, which is what left "
            f"the reader no navigation at all past the first screen")
        assert at["head"] == head_h, (
            f"the section heading is at y={at['head']} instead of y={head_h}: "
            f"the only thing on screen that names the section is gone, and the "
            f"panel is an unlabelled wall of rows from here down")
        page.click("nav button:text-is('Log')")
        settled(page)
        assert page.evaluate(
            "() => document.querySelector('nav button.on').textContent") == "Log", (
            "the rail is on screen at depth and did not answer a click")


def probe_the_progress_bar_reports_the_position(browser, url):
    """Where the reader is, on a section long enough for that to be a question.

    The idle half first, and it is not a formality: a bar that is always there
    sits at 100% on every short section and says something false about all of
    them. It appears only past half a screen of travel.

    Asserted on the computed `scale`, which is what the compositor actually
    draws, rather than on the number the handler wrote."""
    def scale_x(page):
        return page.evaluate("""() => {
          const s = getComputedStyle(document.getElementById('prog')).scale;
          return s === 'none' ? 1 : parseFloat(String(s).split(' ')[0]);
        }""")

    with opened(browser, url) as page:
        settled(page)
        assert page.evaluate(
            "() => document.getElementById('prog').classList.contains('on')") is False, (
            "the progress bar is showing on a section that fits on one screen, "
            "where it can only ever report a position nobody needs")
        _tall(page)
        page.evaluate("""() => {
          const room = document.documentElement.scrollHeight - innerHeight;
          scrollTo(0, Math.round(room / 2));
        }""")
        page.wait_for_timeout(400)
        half = scale_x(page)
        assert page.evaluate(
            "() => document.getElementById('prog').classList.contains('on')") is True, (
            "the section is now several screens long and the bar never arrived")
        assert 0.45 <= half <= 0.55, (
            f"halfway down the section the bar reads {half:.3f} of its width")
        page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(400)
        end = scale_x(page)
        assert end > 0.98, (
            f"at the bottom of the section the bar reads {end:.3f}, so it never "
            f"reaches the end it is reporting on")


def probe_the_header_lifts_off_the_content(browser, url):
    """A sticky header with rows sliding under it, and something that says so.

    Both directions, because a class that is added and never removed leaves the
    shadow on at the top of every section, which is the same defect wearing the
    opposite sign."""
    with opened(browser, url) as page:
        settled(page)
        def shadow():
            return page.evaluate(
                "() => getComputedStyle(document.querySelector('header')).boxShadow")

        assert shadow() == "none", (
            "the header is carrying its scrolled shadow while the page is at "
            "the top, where there is nothing under it to be in front of")
        _tall(page)
        page.evaluate("scrollTo(0, 800)")
        page.wait_for_timeout(300)
        deep = shadow()
        assert deep != "none", (
            "the content is scrolling under the header and the header says "
            "nothing: at rest its 1px border reads as a seam, and in motion as "
            "nothing at all")
        page.evaluate("scrollTo(0, 0)")
        page.wait_for_timeout(300)
        assert shadow() == "none", (
            f"the shadow ({deep}) stayed after the page came back to the top")


def probe_a_door_card_wears_the_boards_focus_ring(browser, url):
    """The two cards that lead somewhere, from the keyboard.

    `role="button" tabindex="0"` on a `<div>` promises a button and inherits
    none of one: measured on the served page, a focused door card computed
    `outline-style: none` from this sheet and fell back to whatever ring the
    BROWSER draws -- which is `outline-style: auto`, and is the discriminator
    here. Reached by tabbing rather than by `.focus()`, because `:focus-visible`
    is exactly the distinction between the two."""
    with opened(browser, url) as page:
        settled(page)
        for _ in range(60):
            page.keyboard.press("Tab")
            if page.evaluate("() => !!(document.activeElement.closest"
                             "&& document.activeElement.closest('.card.go'))"):
                break
        else:
            raise AssertionError(
                "40+ tabs never reached a door card, so this probe measured "
                "nothing — either the cards lost tabindex or Overview is empty")
        ring = page.evaluate("""() => {
          const s = getComputedStyle(document.activeElement);
          return {style: s.outlineStyle, colour: s.outlineColor,
                  width: s.outlineWidth};
        }""")
        assert ring["style"] == "solid", (
            f"the focused door card is wearing {ring}, not the board's own "
            f"ring: `auto` is the browser's default, which is the state this "
            f"was in before — every other control on the page is `solid`")
        assert ring["colour"] == "rgb(127, 178, 255)", (
            f"the ring is {ring['colour']}, which is not --link — the card is "
            f"being focused by some other rule than the board's")


def probe_a_press_is_answered(browser, url):
    """The click, not just the pointer.

    Six surfaces answered a hover and then sat perfectly still while they were
    being pressed. The live chip is the one measured here because it is the one
    control that is on screen in every section, and because it is a bare span in
    the header, outside every `.panel`/`nav` rule in the sheet.

    The pointer is released and re-read: a press that never comes back up is a
    stuck state, and it looks identical in a single reading."""
    with opened(browser, url) as page:
        settled(page)
        box = page.evaluate("""() => {
          const r = document.getElementById('live').getBoundingClientRect();
          return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        }""")
        def translate():
            return page.evaluate(
                "() => getComputedStyle(document.getElementById('live')).translate")

        page.mouse.move(box["x"], box["y"])
        page.wait_for_timeout(120)
        resting = translate()
        page.mouse.down()
        page.wait_for_timeout(120)
        pressed = translate()
        page.mouse.up()
        page.wait_for_timeout(200)
        released = translate()
        assert pressed != resting, (
            f"the chip does not move while it is held down ({pressed}) — the "
            f"pointer is answered and the click is not")
        assert released == resting, (
            f"the chip stayed at {released} after the button came back up, so "
            f"the press is a state it cannot leave")


def probe_the_reading_measure_is_capped(browser, url):
    """How wide the prose is allowed to get, in characters.

    Measured the way a reader meets it -- the rendered width of the element
    divided by the width of one character in its own computed font -- rather
    than from the declaration, because the cap is written in `ch` on the ROW and
    the text inside it is a different font size, which is a way to be wrong by a
    third while the CSS looks right.

    90 is the ceiling, not the target: the band anyone sets prose in is roughly
    45-85, the board came in at 152, and a probe that pinned the exact number
    would fail on a font change that made it better.

    BOTH row shapes, because the cap is written twice and they are separate
    guards. A plain row is held by `max-width` on the row; a two-column row
    (Log, where the badge and the text share a line with the file path) sets
    `max-width:none` and is held instead by its first grid TRACK. Measuring
    only the two-column row leaves the `max-width` unproven — it was, and
    cutting the declaration changed nothing this file could see.

    Deliberately a WIDE window. A cap can only be observed where something is
    pushing against it: in a default test viewport an uncapped prose track
    lands around 84 characters on its own, under this probe's own ceiling, so
    the measurement passes either way and says nothing. The board is a window
    the reader drags as wide as their screen, which is where the complaint came
    from — 152 characters — so that is the window to measure in."""
    with opened(browser, url, viewport={"width": 1600, "height": 900}) as page:
        settled(page)
        page.evaluate("""() => {
          const word = 'measurement ';
          document.getElementById('panel').insertAdjacentHTML('beforeend',
            '<ul class="hits">'
            + '<li class="two"><div class="file" id="probetwo">'
            + word.repeat(120) + '</div><div class="snip">a/path.py</div></li>'
            + '<li><div class="file" id="probeflat">'
            + word.repeat(120) + '</div></li></ul>');
        }""")
        page.wait_for_timeout(200)
        read = page.evaluate("""() => {
          const chars = id => {
            const el = document.getElementById(id);
            const cv = document.createElement('canvas').getContext('2d');
            cv.font = getComputedStyle(el).font;
            return Math.round(el.getBoundingClientRect().width
                              / cv.measureText('0').width);
          };
          return {two: chars('probetwo'), flat: chars('probeflat'),
                  panel: Math.round(document.getElementById('panel')
                                    .getBoundingClientRect().width)};
        }""")
        for shape, ch in (("two-column", read["two"]), ("plain", read["flat"])):
            assert ch > 20, (
                f"the prose column of a {shape} row measured {ch} characters, "
                f"which is too narrow to be a cap — this probe is measuring "
                f"something else")
            assert ch <= 90, (
                f"a line of change-log prose in a {shape} row runs {ch} "
                f"characters inside a {read['panel']}px panel; the eye loses "
                f"the start of the next line well before that, and this is the "
                f"section with 270 rows in it")


def probe_the_board_reflows_for_a_narrow_window(browser, url):
    """A 220px rail beside a `1fr` panel, in a window that is not wide.

    The board is a resizable local window, and below the breakpoint the panel
    had less room than the rail it sat next to. The rail becomes a band across
    the top rather than a drawer behind a button -- a drawer would be a second
    interaction to reach navigation that is one click away at every other
    width -- so the assertion is that it is FULL WIDTH and still there, not that
    it is gone."""
    with opened(browser, url, viewport={"width": 760, "height": 900}) as page:
        settled(page)
        laid = page.evaluate("""() => {
          const nav = document.querySelector('nav').getBoundingClientRect();
          const panel = document.getElementById('panel').getBoundingClientRect();
          return {navw: Math.round(nav.width), navx: Math.round(nav.x),
                  panelx: Math.round(panel.x), panelw: Math.round(panel.width),
                  buttons: document.querySelectorAll('nav button[data-sec]').length,
                  sw: document.documentElement.scrollWidth,
                  cw: document.documentElement.clientWidth};
        }""")
        assert laid["sw"] <= laid["cw"], (
            f"the narrow window scrolls sideways: {laid}")
        assert laid["navw"] > 700 and laid["panelx"] == 0, (
            f"the rail is still a 220px column at 760px wide, so the panel gets "
            f"{laid['panelw']}px of a 760px window: {laid}")
        assert laid["buttons"] >= 16, (
            "the rail lost its section buttons at the narrow width, which is "
            "not a reflow, it is a removal")


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


def test_reduced_motion_is_obeyed_when_it_is_turned_on_mid_session(browser, url):
    probe_reduced_motion_is_read_live(browser, url)


def test_leaving_a_workspace_section_stops_its_frame_polling(browser, url):
    probe_a_hidden_workspace_frame_stops_polling(browser, url)


def test_a_workspace_click_is_not_undone_by_the_section_it_left(browser, url):
    probe_a_workspace_click_survives_the_section_it_left(browser, url)


def test_a_workspace_section_keeps_its_document_across_a_visit_elsewhere(browser, url):
    probe_a_workspace_frame_keeps_what_was_typed_into_it(browser, url)


def test_a_section_clicked_during_boot_is_not_discarded_by_the_project_guard(browser, url):
    probe_a_section_clicked_at_boot_still_gets_its_answer(browser, url)


def test_a_cat_answers_the_cursor_in_every_state(browser, url):
    probe_hover_motion(browser, url)


def test_a_dim_cat_answers_the_cursor_once_and_a_live_one_keeps_dancing(browser, url):
    probe_dim_cat_dances_once(browser, url)


def test_an_unchanged_poll_holds_the_strips_and_still_moves_the_clock(browser, url):
    probe_unchanged_poll_holds_structure_and_refreshes_the_clock(browser, url)


def test_a_slow_section_paints_a_skeleton_and_then_replaces_it(browser, url):
    probe_loading_ladder(browser, url)


def test_the_cats_poll_skips_a_slow_tick_instead_of_queueing_it(browser, url):
    probe_cats_poll_skips_rather_than_queues(browser, url)


def test_the_nav_stays_clickable_while_a_section_loads(browser, url):
    probe_nav_clickable(browser, url)


def test_the_rail_names_the_section_on_screen_and_the_marker_travels(browser, url):
    probe_active_section(browser, url)


def test_one_unbreakable_token_does_not_widen_the_whole_board(browser, url):
    probe_no_sideways_scroll(browser, url)


def test_the_keyboard_reaches_the_panel_in_one_tab(browser, url):
    probe_the_keyboard_reaches_the_panel(browser, url)


def test_the_rail_and_the_heading_are_still_there_at_row_two_hundred(browser, url):
    probe_the_rail_and_heading_stay_at_depth(browser, url)


def test_a_long_section_says_where_the_reader_is(browser, url):
    probe_the_progress_bar_reports_the_position(browser, url)


def test_the_header_says_when_it_is_over_scrolled_content(browser, url):
    probe_the_header_lifts_off_the_content(browser, url)


def test_a_card_that_is_a_door_wears_the_boards_own_focus_ring(browser, url):
    probe_a_door_card_wears_the_boards_focus_ring(browser, url)


def test_a_held_control_moves_under_the_press(browser, url):
    probe_a_press_is_answered(browser, url)


def test_a_line_of_prose_stays_inside_a_readable_measure(browser, url):
    probe_the_reading_measure_is_capped(browser, url)


def test_a_narrow_window_gets_the_rail_across_the_top(browser, url):
    probe_the_board_reflows_for_a_narrow_window(browser, url)


def test_every_clickable_surface_lights_under_the_pointer(browser, url):
    probe_pointer_surfaces_react(browser, url)


def test_the_panel_announces_a_new_section_once_and_not_on_a_poll(browser, url):
    probe_panel_entrance(browser, url)


def test_the_status_line_cannot_wrap_and_take_the_header_with_it(browser, url):
    probe_header_holds_its_height(browser, url)


def test_the_rail_and_the_query_box_share_the_keyboard_without_fighting(browser, url):
    probe_keyboard_rail(browser, url)


def test_a_card_with_a_section_behind_it_opens_by_click_and_by_key(browser, url):
    probe_card_is_a_door(browser, url)


def test_a_long_section_can_be_left_from_the_bottom(browser, url):
    probe_back_to_top(browser, url)


def test_returning_to_overview_puts_overview_back_on_screen(browser, url):
    probe_overview_comes_back_on_screen(browser, url)


def test_an_idle_section_is_not_repainted_by_the_live_poll(browser, url):
    probe_idle_section_stops_repainting(browser, url)


def test_a_reader_can_keep_a_selection_while_the_board_polls(browser, url):
    probe_a_selection_survives_the_poll(browser, url)




# ------------------------------------------------- T13: the injection row
# The row is one cat per TURN, on the Oversee section, and hover and click were
# both asked for by name. Nothing in the default or node tiers can answer either
# — a `title` attribute that is never rendered and a click handler that never
# reaches a laid-out element both read as present in a string comparison.
#
# Seeded through the REAL sidecar and served by the REAL `/api/injection`, not a
# stubbed fetch: the route is one O(1) file read, so there is no reason to fake
# it, and faking it would leave the probe unable to notice the payload shape
# changing underneath the renderer.

INJ_SEED_TURNS = 9          # enough cats that the last one sits near the edge


def _inj_pass(name, verdict, chars=120):
    return {"name": name, "verdict": verdict, "chars": chars,
            "tokens": chars // 4, "ms": 1,
            "reason": "delivered" if verdict != "silent" else "nothing to say"}


def _seed_injection_row(project=None):
    """Three DISTINCT turn verdicts plus filler, written where the route reads.

    `turnVerdict` is worst-wins over the passes, so the three turns are built
    from pass mixtures rather than by setting a verdict directly — that way the
    probe exercises the reduction as well as the rendering."""
    from mindhive import injectors as inj

    def turn(sig, passes, ts):
        return {"ts": ts, "session_id": "s-probe", "signature": sig,
                "total_chars": sum(p["chars"] for p in passes),
                "total_tokens": sum(p["tokens"] for p in passes),
                "repeats": 1, "injectors": passes}

    ring = [
        # partial WINS over verbatim in the same turn — a degraded pass has to
        # be visible from the strip without opening anything.
        turn("sig-partial", [_inj_pass("sopgate", "partial"),
                             _inj_pass("style", "verbatim")],
             "2026-08-27T03:00:00Z"),
        turn("sig-verbatim", [_inj_pass("promptgate", "verbatim"),
                              _inj_pass("style", "verbatim")],
             "2026-08-27T02:00:00Z"),
        turn("sig-silent", [_inj_pass("premise", "silent", chars=0),
                            _inj_pass("symbols", "silent", chars=0)],
             "2026-08-27T01:00:00Z"),
    ]
    ring += [turn("sig-fill-%d" % i,
                  [_inj_pass("mindnet", "verbatim")],
                  "2026-08-26T0%d:00:00Z" % (i % 10))
             for i in range(INJ_SEED_TURNS - len(ring))]
    inj._write_sidecar({"session_id": "s-probe", "signature": "sig-partial",
                        "injectors": ring[0]["injectors"],
                        "total_chars": ring[0]["total_chars"],
                        "total_tokens": ring[0]["total_tokens"],
                        "block": "THE COMPOSED BLOCK FOR THE CURRENT TURN",
                        "unchanged_turns": 0, "recent": ring},
                       project=project)
    return ring


def _open_injection_row(page):
    """Oversee, painted, with the injection cats on screen."""
    settled(page)
    page.click("nav button:text-is('Oversee')")
    page.wait_for_selector("#mn-inject .mn-inj", timeout=30000)
    page.wait_for_timeout(200)


def probe_the_injection_row_shows_three_distinct_states(browser, url):
    """The three states are three APPEARANCES, not three class names.

    A class nothing answers is a state nobody can see, and that is the failure
    mode this whole feature exists to end reproduced in CSS: `silent` means the
    turn delivered nothing, and a silent cat that looks exactly like a verbatim
    one tells the operator the opposite of the truth.

    Asserted on computed style — what the compositor draws — rather than on the
    declaration, because a rule can be present and overridden."""
    _seed_injection_row()
    with opened(browser, url, viewport={"width": 1500, "height": 900}) as page:
        _open_injection_row(page)
        seen = page.evaluate("""() => {
          const out = {};
          for (const el of document.querySelectorAll('#mn-inject .mn-inj')){
            const s = getComputedStyle(el);
            const k = ['verbatim', 'partial', 'silent']
                        .find(c => el.classList.contains(c)) || 'none';
            if (!(k in out)) out[k] = {filter: s.filter, opacity: s.opacity,
                                       border: s.borderTopColor};
          }
          return out;
        }""")

    assert set(seen) >= {"verbatim", "partial", "silent"}, (
        f"the strip rendered {sorted(seen)} — turnVerdict did not reduce the "
        f"three seeded turns to three different states")
    assert "grayscale" in seen["silent"]["filter"], (
        f"a silent turn is drawn in full colour ({seen['silent']}) — it looks "
        f"exactly like one that delivered")
    assert float(seen["silent"]["opacity"]) < 1.0, seen["silent"]
    assert seen["verbatim"]["border"] != seen["partial"]["border"], (
        f"verbatim and partial share a border colour ({seen['verbatim']}) — a "
        f"degraded pass is then invisible from the strip, which is the one "
        f"thing the strip is for")
    assert "grayscale" not in seen["verbatim"]["filter"], (
        "the canary: a sheet that greyed EVERY cat would satisfy the silent "
        "assertion above without distinguishing anything")


def probe_hovering_an_injection_cat_names_the_turn(browser, url):
    """Hover was asked for by name, and it is a native `title`.

    Its content is the claim: which turn, how it went, how many passes actually
    spoke, and how big it was. "N of M spoke" is the load-bearing half — a cat
    whose title says only `verbatim` cannot tell an operator that four of seven
    passes stayed silent inside a turn the strip calls healthy."""
    _seed_injection_row()
    with opened(browser, url, viewport={"width": 1500, "height": 900}) as page:
        _open_injection_row(page)
        first = page.locator("#mn-inject .mn-inj").first
        first.hover()
        title = first.get_attribute("title") or ""
        page.wait_for_timeout(100)

    assert "2026-08-27T03:00:00Z" in title, f"the title names no turn: {title!r}"
    assert "partial" in title, f"the title does not say how it went: {title!r}"
    assert "2 of 2 spoke" in title, (
        f"the title does not say how many passes spoke: {title!r}")
    assert "240 chars" in title, f"the title carries no size: {title!r}"


def probe_clicking_an_injection_cat_opens_and_closes_the_whole_turn(browser, url):
    """Click was the other half of the ask, and the panel is the answer.

    Four claims, and the last two are the ones that ship broken: it opens, it
    names every pass in that turn, exactly ONE cat anywhere reads as selected,
    and clicking the same cat again closes it. A toggle that only opens is the
    normal way this is half-built."""
    _seed_injection_row()
    with opened(browser, url, viewport={"width": 1500, "height": 900}) as page:
        _open_injection_row(page)
        first = page.locator("#mn-inject .mn-inj").first
        first.click()
        page.wait_for_timeout(200)
        opened_state = page.evaluate("""() => {
          const p = document.getElementById('mn-cat-panel');
          return {shown: getComputedStyle(p).display,
                  text: p.innerText,
                  selected: document.querySelectorAll(
                      '.mn-cat.sel, .mn-sess.sel, .mn-inj.sel').length};
        }""")
        first.click()
        page.wait_for_timeout(200)
        closed_state = page.evaluate("""() => ({
          shown: getComputedStyle(document.getElementById('mn-cat-panel')).display,
          selected: document.querySelectorAll(
              '.mn-cat.sel, .mn-sess.sel, .mn-inj.sel').length,
        })""")

    assert opened_state["shown"] == "block", (
        f"the click did not open the panel: {opened_state}")
    for name in ("sopgate", "style"):
        assert name in opened_state["text"], (
            f"the panel does not name {name}, a pass in the clicked turn — the "
            f"strip says the turn was degraded and the panel is what says "
            f"WHICH pass degraded: {opened_state['text']!r}")
    assert "THE COMPOSED BLOCK FOR THE CURRENT TURN" in opened_state["text"], (
        "the current turn's composed text is not replayed — what Claude READ "
        "is the operator's actual question")
    assert opened_state["selected"] == 1, (
        f"{opened_state['selected']} cats read as selected across all three "
        f"strips; mnOpen exists so exactly one can")
    assert closed_state["shown"] == "none", (
        f"clicking the open cat again left the panel up: {closed_state}")
    assert closed_state["selected"] == 0, closed_state


def probe_the_injection_panel_stays_inside_the_window(browser, url):
    """The plan's own verify line, and a bug found by opening the real page.

    The panel left-aligns to the cat it belongs to, so a cat near the right edge
    put it off screen entirely -- visible in a rectangle and in nothing a string
    comparison can see.

    Two things here are measured rather than assumed, both because the first
    version of this probe assumed them and proved nothing. The window is 900px
    because the clamp DOES NOT BITE at 1500: the strip is nine cats inside a
    panel that starts at x=242, the rightmost sits at x=790, and a ~210px panel
    ends at 1000 -- comfortably inside a 1500px window, so the guard could be
    cut with the probe still green. And the cat clicked is the RIGHTMOST, found
    by measuring, not the last in the DOM: the row wraps, so on a narrow board
    the last cat is the first one on a new line and reproduces nothing at all.

    The first assertion is the one that keeps this honest -- it says the
    unclamped panel WOULD have overflowed. Without it, a layout change that
    moved the cats left would leave this passing while testing nothing."""
    _seed_injection_row()
    with opened(browser, url, viewport={"width": 900, "height": 900}) as page:
        _open_injection_row(page)
        cats = page.locator("#mn-inject .mn-inj")
        assert cats.count() >= 4, (
            f"only {cats.count()} cats -- too few for any of them to sit near "
            f"the right edge, so this probe would pass on an unclamped panel")
        idx = page.evaluate("""() => {
          const cats = [...document.querySelectorAll('#mn-inject .mn-inj')];
          let best = 0;
          cats.forEach((c, i) => {
            if (c.getBoundingClientRect().left
                > cats[best].getBoundingClientRect().left) best = i;
          });
          return best;
        }""")
        cats.nth(idx).click()
        page.wait_for_timeout(200)
        box = page.evaluate("""(i) => {
          const cat = document.querySelectorAll('#mn-inject .mn-inj')[i];
          const r = document.getElementById('mn-cat-panel').getBoundingClientRect();
          const c = cat.getBoundingClientRect();
          return {left: Math.round(r.left), right: Math.round(r.right),
                  width: Math.round(r.width), cat: Math.round(c.left),
                  view: document.documentElement.clientWidth};
        }""", idx)

    assert box["width"] > 0, f"the panel has no width to clamp: {box}"
    assert box["cat"] + box["width"] > box["view"], (
        f"this probe is not reproducing the bug: a panel left-aligned to the "
        f"cat at x={box['cat']} would end at {box['cat'] + box['width']}, "
        f"inside the {box['view']}px window, so the clamp is never asked to do "
        f"anything. Seed more cats or narrow the window: {box}")
    assert box["right"] <= box["view"], (
        f"the panel runs to x={box['right']} in a {box['view']}px window -- it "
        f"is off screen, which is what left-aligning to a cat near the right "
        f"edge does: {box}")
    assert box["left"] >= 0, f"the panel is off the LEFT edge instead: {box}"


def test_the_injection_row_shows_three_distinct_states(browser, url):
    probe_the_injection_row_shows_three_distinct_states(browser, url)


def test_hovering_an_injection_cat_names_the_turn(browser, url):
    probe_hovering_an_injection_cat_names_the_turn(browser, url)


def test_clicking_an_injection_cat_opens_and_closes_the_whole_turn(browser, url):
    probe_clicking_an_injection_cat_opens_and_closes_the_whole_turn(browser, url)


def test_the_injection_panel_stays_inside_the_window(browser, url):
    probe_the_injection_panel_stays_inside_the_window(browser, url)


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
     # The anchor spans the comment that sits between the two, because the
     # mutation has to REMOVE the region from outside and ADD it inside in one
     # replace, and there is exactly one span of text containing both places.
     ('  <div id="ann" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>\n'
      '  <!-- `tabindex="-1"` is what makes the skip link above move the focus and not\n'
      '       just the scroll position. -1 keeps it out of the tab ORDER: it is a\n'
      '       target, never a stop. -->\n'
      '  <section class="panel" id="panel" aria-busy="false" tabindex="-1">\n'
      '    <div id="controls"></div>',
      '  <section class="panel" id="panel" aria-busy="false" tabindex="-1">\n'
      '    <div id="ann" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>\n'
      '    <div id="controls"></div>')),

    # The colour is a token, not a literal: the palette moved into `theme.py`, so
    # the mutation has to keep whatever the shipped rule declares and remove only
    # the `:not(.paused)` this row is about.
    ("the stale rule loses its :not(.paused) and wins the tie again",
     probe_live_colour,
     ("header #live.stale:not(.paused) { color:var(--warn); }",
      "header #live.stale { color:var(--warn); }")),

    # Restores the shape the fix replaced: the body still runs, and still swaps
    # the panel, but outside run() — so nothing retires the departing section's
    # run and its skeleton timer still believes it is current.
    #
    # One LINE of anchor, not the whole function. The first cut of this mutant
    # pinned every line of showWorkspace and stopped matching the moment that
    # function grew its frame-reuse branch; the anchor check turned that into a
    # loud failure rather than a silent pass, which is what it is for, but a
    # mutant that only names the thing it is about does not drift in the first
    # place. `(fn => fn())` balances against the same `}));` tail.
    ("a workspace click swaps the panel without taking a generation",
     probe_a_workspace_click_survives_the_section_it_left,
     ("run('loading ' + view + ' workspace', () => new Promise(resolve => {",
      "(fn => fn())(() => new Promise(resolve => {")),

    # Restores the shape the fix replaced: the page polls whether or not anyone
    # is looking at it. Cut out of MINDPARALLEL, not the shell — the guard lives
    # in the workspace document, which is the page the probe drives.
    ("a hidden workspace frame is left polling",
     probe_a_hidden_workspace_frame_stops_polling,
     ("parallel",
      """  (async function loop(){ if (!paneHidden()) { await poll(); POLLED = true; }
                          setTimeout(loop, 1000); })();""",
      """  (async function loop(){ await poll(); POLLED = true;
                          setTimeout(loop, 1000); })();""")),

    # Restores the guard that treated "no project yet" as a project, so the
    # first answer of the session is thrown away for having been asked before
    # the picker existed.
    ("no project yet counts as a different project",
     probe_a_section_clicked_at_boot_still_gets_its_answer,
     ("    if (scope !== '' && PROJECT !== scope) throw StaleScope(scope);",
      "    if (PROJECT !== scope) throw StaleScope(scope);")),

    # The frame is kept BECAUSE it is worth keeping; this restores the visit
    # that threw it away, which is what the reuse check replaced.
    ("a return to a workspace section reloads the document",
     probe_a_workspace_frame_keeps_what_was_typed_into_it,
     ("""      if (!graph.hasAttribute('srcdoc') && graph.getAttribute('src') === want)
        return resolve();""",
      """      if (false)
        return resolve();""")),

    # Restores the shape the fix replaced: one read, at load, into a constant.
    # The page still answers `reduceMotion()`, so nothing throws — it just
    # answers with a number from before the reader changed their mind.
    ("the reduced-motion check goes back to a value read once at load",
     probe_reduced_motion_is_read_live,
     ("""  function reduceMotion(){
    return !!(window.matchMedia
              && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }""",
      """  const REDUCE_MOTION_SNAPSHOT = !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  function reduceMotion(){ return REDUCE_MOTION_SNAPSHOT; }""")),

    ("this page's own reduced-motion guard is dropped (the cat sheet's stays)",
     probe_reduced_motion,
     ("    header .dot.busy, header #live .blip, .pill.wsphase .blip, .ovspin, .rmnode.rmactive,\n"
      "    .skel .bar, .skel .tile, .skel .line, .skel .pane,\n"
      "    .cards.enter .card, .cards .big.bump,\n"
      "    .types .pill b.bump, section.panel.swap { animation:none !important; }",
      "    .rmfill { animation:none !important; }")),

    # The rail's canary. A section that stops marking itself is invisible from
    # the outside -- the panel still loads, the button still works, and the rail
    # simply goes on naming whatever was showing before. Removing ONE call is
    # the realistic regression, because the call is one line at the top of a
    # function whose job is plainly something else.
    ("Log stops marking itself and the rail keeps naming the last section",
     probe_active_section,
     ("function showLog(){ markSection('log'); ",
      "function showLog(){ ")),

    # The wrapping canary. Without a last-resort break the token cannot break at
    # all, so it overflows whatever box it is in and the board gains a horizontal
    # scrollbar under every section -- which is how this was found in the first
    # place.
    ("the snippet loses its last-resort break and one path widens the board",
     probe_no_sideways_scroll,
     ("  .hits .snip { white-space:pre-wrap; margin-top:4px; overflow-wrap:break-word; }",
      "  .hits .snip { white-space:pre-wrap; margin-top:4px; }")),

    # The OTHER half of the same defect, and the half that only became load-
    # bearing in Round F. `break-word` leaves the element's min-content at the
    # width of its longest token, so with the panel's automatic minimum size back
    # in force the `1fr` track grows to the token and takes the document with it.
    # One declaration, in a rule that reads as pure padding.
    ("the panel takes its automatic minimum size back and one token widens it",
     probe_no_sideways_scroll,
     ("  section.panel { padding:18px 22px; min-width:0; }",
      "  section.panel { padding:18px 22px; }")),

    # The dance's own canary. Removing ONE qualified selector is the realistic
    # regression — a later tidy-up reads five selectors for one effect as
    # duplication and collapses them — and it is invisible to every check that
    # only asks whether the cat moves: the working cat goes on running `mn-bob`
    # under the cursor and reports a running animation either way.
    ("the dance loses a qualified selector and a lit cat ignores the cursor",
     probe_hover_motion,
     ("  .mn-sess.working .mn-cat-sprite:hover,\n", "")),

    # The composition canary, and the reason the probe samples properties at all.
    # This mutant rewrites one track to the `transform` shorthand — the exact
    # edit a reader who has not met the individual transform properties would
    # make, thinking it equivalent. `animationName` still reports all three
    # tracks, so every name-based assertion stays green; the tilt simply stops
    # arriving, because the shorthand and the individual properties are
    # different properties and only one track is writing this one.
    ("a dance track is rewritten to the transform shorthand and stops composing",
     probe_hover_motion,
     ("""  @keyframes mn-dance-tilt { 0% { rotate:0deg }
                             20% { rotate:-9deg }
                             50% { rotate:2deg }
                             75% { rotate:9deg }
                             100% { rotate:0deg } }""",
      """  @keyframes mn-dance-tilt { 0% { transform:rotate(0deg) }
                             20% { transform:rotate(-9deg) }
                             50% { transform:rotate(2deg) }
                             75% { transform:rotate(9deg) }
                             100% { transform:rotate(0deg) } }""")),

    # The dim cat's canary. Deleting this one declaration is the realistic
    # regression — it reads as a stray override of the rule above it — and the
    # cat goes on moving under the cursor either way, so every is-it-animated
    # check stays green while the hang signal quietly stops meaning anything.
    ("the grey cats' iteration cap is dropped and absence dances forever",
     probe_dim_cat_dances_once,
     ("  .mn-cat.dim svg:hover,\n"
      "  .mn-sess.resting .mn-cat-sprite:hover,\n"
      "  .mn-inj.silent .mn-cat-sprite:hover { animation-iteration-count:1; }\n", "")),

    # The narrower mutant, and the reason the probe walks all three states
    # instead of one. Keeping the rule but losing a single selector is what a
    # tidy-up actually does, and the two surviving states go on stopping — so a
    # probe that sampled only `.mn-cat.dim` would stay green while a silent
    # injection, the failure case the whole strip exists to report, danced.
    ("only the silent-injection selector is lost from the grey cap",
     probe_dim_cat_dances_once,
     ("  .mn-inj.silent .mn-cat-sprite:hover { animation-iteration-count:1; }",
      "  .mn-cat.dim svg:hover { animation-iteration-count:1; }")),

    # The faculty strip's repaint guard, removed. This is the state the board
    # actually shipped in until the dance made it visible: the strip rebuilt on
    # every 2.5 s poll, and no test anywhere noticed, because a rebuilt strip
    # renders identically. Only node identity tells them apart.
    ("the faculty strip's repaint guard is dropped and every poll rebuilds it",
     probe_unchanged_poll_holds_structure_and_refreshes_the_clock,
     ("    if (bar.dataset.sig === sig){\n", "    if (false){\n")),

    # The other half, and the defect the guard itself introduced. Losing the
    # refresh call leaves the structural guard perfectly intact — nodes are held,
    # animations stay whole, every node-identity assertion passes — while the
    # strip and the open panel freeze on an age that was true once.
    ("the faculty strip holds its nodes but stops refreshing the clock",
     probe_unchanged_poll_holds_structure_and_refreshes_the_clock,
     ("      mnRefreshAges(bar, cats, '.mn-cat', 'faculty', c => c.faculty,\n"
      "                    catTitle, catPanel);\n", "")),

    # The same omission on the other strip. It is a separate mutant because it
    # is a separate call site: the session strip carried this defect first and
    # for longer, and a probe that only walked the faculty strip would have
    # called the pair fixed while half of it still froze.
    ("the session strip holds its nodes but stops refreshing the clock",
     probe_unchanged_poll_holds_structure_and_refreshes_the_clock,
     ("      mnRefreshAges(bar, rows, '.mn-sess', 'session', s => s.session_id,\n"
      "                    sessTitle, sessPanel);\n", "")),

    # The click handler goes back to closing over its payload. Everything
    # visible stays correct — the element is held, the title refreshes to
    # "2.5h ago" — and the panel that opens under it renders from the payload
    # captured at paint time. The disagreement only exists at the moment
    # someone clicks, which is why no amount of inspecting the strip finds it.
    ("the session click handler closes over its payload again",
     probe_unchanged_poll_holds_structure_and_refreshes_the_clock,
     ("else { mnOpen('sess', el, () => sessPanel(el._row, el)); }",
      "else { mnOpen('sess', el, () => sessPanel(s, el)); }")),

    # The opposite failure, and the reason the probe paints a third time. A
    # signature that swallows `display` suppresses the repaint that carries a
    # faculty going quiet or waking up — the strip's entire job — while every
    # is-it-rebuilding check stays green.
    ("the signature swallows display and the strip freezes on stale state",
     probe_unchanged_poll_holds_structure_and_refreshes_the_clock,
     ("JSON.stringify(cats.map(c => [c.faculty, c.display, c.status]))",
      "JSON.stringify(cats.map(c => [c.faculty]))")),

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

    # The button's canary. Panel buttons shipped with the OS arrow cursor and no
    # hover of any kind; the fix gave `button` a control face AND a reaction.
    # Dropping just the reaction is the realistic regression -- the face alone
    # looks finished in a screenshot -- and nothing else in the sheet lights a
    # panel button, so the surface goes back to dead silence.
    ("panel buttons keep the control face and stop reacting to the pointer",
     probe_pointer_surfaces_react,
     ("  button:hover { background:var(--hover); border-color:var(--accent-edge); }\n", "")),

    # The chip's canary, and the reason the probe walks three surfaces rather
    # than one. The live chip is a bare span in the header, outside every
    # `.panel`/`nav` rule in the sheet, so it is the surface a sweep is most
    # likely to miss -- and it is the one control on the board that is always
    # on screen whatever section is showing.
    ("the live chip goes back to a hand cursor over a surface that never lights",
     probe_pointer_surfaces_react,
     ("  header #live:hover { background:var(--hover-tint); }\n", "")),

    # The re-trigger's canary, and the reason the probe counts events instead of
    # reading the class. Removing the layout read looks like deleting a stray
    # no-op statement -- `void panelEl.offsetWidth;` computes nothing and is
    # discarded -- and the page goes on working in every visible way except that
    # every section after the first arrives with no entrance.
    ("the one-shot loses its layout read and only the first section animates",
     probe_panel_entrance,
     ("      void panelEl.offsetWidth;\n", "")),

    # The other half: the entrance moved to the obvious place. `only()` is where
    # a reader looks for "the panel changed", and it is wrong -- it runs on every
    # paint, so Log's 2.5 s rebuild would re-announce the section forever.
    ("the entrance is wired to every paint instead of every arrival",
     probe_panel_entrance,
     ("""  function only(el){
    viewPicked = true;""",
      """  function only(el){
    viewPicked = true;
    panelEl.classList.remove('swap'); void panelEl.offsetWidth;
    panelEl.classList.add('swap');""")),

    # The jitter's canary. `white-space:nowrap` on a status line reads as pure
    # typography and is the obvious thing to drop in a tidy-up; what it actually
    # holds is the height of the header, and with it the vertical position of
    # every section on the board.
    ("the status line is shrinkable and wrappable again",
     probe_header_holds_its_height,
     ("  header #status { font-size:12px; color:var(--muted); white-space:nowrap; flex:0 0 auto; }",
      "  header #status { font-size:12px; color:var(--muted); }")),

    # The form controls' canary. `input, select, textarea` were dressed early
    # and never given a reaction at all; this rule is the reaction, and it is a
    # single line that reads as optional polish. Losing it puts the project
    # selector -- the control that changes what the entire board is looking at
    # -- back to showing a hand over a surface that never answers.
    ("the value-carrying controls stop lighting their edge",
     probe_pointer_surfaces_react,
     ("  input:hover, select:hover, textarea:hover { border-color:var(--accent-edge); }",
      "  input:hover, select:hover, textarea:hover { }")),

    # The shortcut's canary. Alt+arrow is four lines that read as a convenience,
    # and the rail goes back to being mouse-only without anything else changing.
    ("Alt+arrow stops moving between sections",
     probe_keyboard_rail,
     ("""    if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowUp')){
      e.preventDefault(); stepSection(e.key === 'ArrowDown' ? 1 : -1); return;
    }""", "")),

    # The guard's canary, and the more dangerous half. Without it every bare-key
    # shortcut fires while the reader is writing: `/` stops reaching the field it
    # is typed into, so the search box silently eats one character out of every
    # query containing a slash -- which every file path does.
    ("bare-key shortcuts fire while the reader is typing",
     probe_keyboard_rail,
     ("    if (typingInto(document.activeElement) || e.ctrlKey || e.metaKey) return;\n", "")),

    # The keyboard half of a card. The click half goes on working, so a mouse
    # sweep reports the feature as landed while `role="button"` is a lie to
    # anybody who cannot use one.
    ("a card that says role=button answers only the mouse",
     probe_card_is_a_door,
     ("""    const card = e.target.closest && e.target.closest('.card.go');
    if (card && (e.key === 'Enter' || e.key === ' ')){
      e.preventDefault(); card.click(); return;
    }""", "")),

    # The way back never arrives. The listener still runs and the threshold is
    # still crossed; only the class is not written, which is exactly what a
    # refactor that "simplified" the handler would leave behind.
    ("the scroll threshold is crossed and nothing appears",
     probe_back_to_top,
     ("{ toTopUp = up; if (toTopEl) toTopEl.classList.toggle('up', up); }",
      "{ toTopUp = up; }")),

    # The two-click blank board. `contains` is true of a hidden pane, so the
    # refresh branch wrote fresh numbers into an invisible grid and returned --
    # and every section that renders through `showText` leaves `#view` in exactly
    # that state. This is the shipped bug, restored verbatim.
    ("the refresh branch calls a hidden grid live and leaves it hidden",
     probe_overview_comes_back_on_screen,
     ("    const live = prev && view.contains(prev) && !view.classList.contains('hidden');",
      "    const live = prev && view.contains(prev);")),

    # The repaint guard's canary. One expression, and the board goes back to
    # re-parsing every panel on every tick -- with nothing on screen to say so,
    # which is exactly why this needs a measured probe and not a review.
    ("the panel is repainted on every poll whether or not it changed",
     probe_idle_section_stops_repainting,
     ("    if (!wasHidden && el.dataset.paint === sig) return false;", "")),

    # The same removal, aimed at what it costs rather than at how often it
    # happens: the two probes fail for different reasons and a fix that only
    # satisfied one of them would be visible here.
    ("the poll takes the reader's selection with it",
     probe_a_selection_survives_the_poll,
     ("    if (!wasHidden && el.dataset.paint === sig) return false;", "")),

    # --- Round F ------------------------------------------------------------
    # The skip link, gone. The board still works in every way a mouse can see,
    # and the keyboard is back to 27 tab stops of rail before the content.
    ("the keyboard is back to tabbing the whole rail to reach the panel",
     probe_the_keyboard_reaches_the_panel,
     ('<a id="skip" href="#panel">skip to content</a>\n', "")),

    # The half that ships broken far more often than the link itself: the link
    # is there, it is focusable, it scrolls the panel into view -- and the focus
    # never moves, because an anchor cannot focus a target that is not
    # focusable. One attribute, and it reads like noise.
    ("the skip link's target stops being focusable",
     probe_the_keyboard_reaches_the_panel,
     ('aria-busy="false" tabindex="-1"', 'aria-busy="false"')),

    # The rail scrolls away again. `position:relative` is what was there before
    # and is still what `#navdot` needs an offsetParent for, so this is the
    # realistic regression: someone restores the line the indicator depends on
    # and drops the three beside it.
    ("the rail goes back to scrolling away with the content",
     probe_the_rail_and_heading_stay_at_depth,
     ("    position:sticky; top:var(--headh); align-self:start;\n"
      "    max-height:calc(100vh - var(--headh)); overflow-y:auto; }",
      "    position:relative; }")),

    # The heading scrolls away, which is the same defect on the other axis of
    # orientation: the reader keeps the navigation and loses the name of the
    # place they are in.
    ("the section heading stops holding its place",
     probe_the_rail_and_heading_stay_at_depth,
     ("  h2.h { position:sticky; top:var(--headh); z-index:4;",
      "  h2.h { z-index:4;")),

    # The measurement, gone -- and this one is invisible for the first second of
    # every session, which is the interesting part. `--headh` keeps its declared
    # 52px, the rail sticks 16px too high the moment the cat strips arrive, and
    # nothing anywhere reports it.
    #
    # The body, not the `measureHeader();` call: cutting the call alone is a
    # NO-OP, because `observe()` delivers its first callback synchronously with
    # the element's current size. That version of this row was written, ran
    # green against the mutant, and proved nothing — which is the whole reason
    # the mutant half of this file exists.
    ("the header's height stops being measured and the sheet uses the default",
     probe_the_rail_and_heading_stay_at_depth,
     ("    document.documentElement.style.setProperty(\n"
      "      '--headh', Math.round(headEl.getBoundingClientRect().height) + 'px');\n", "")),

    # The other half of the same measurement, and the defect the probe's
    # docstring is actually about: it IS measured, once, at parse time while the
    # header is still 52px empty, and never again. The cat strips then arrive,
    # the header grows to 68, and the rail spends the rest of the session
    # tucked 16px underneath it.
    ("the header is measured once, while it is empty, and never again",
     probe_the_rail_and_heading_stay_at_depth,
     ("  if (headEl && window.ResizeObserver) new ResizeObserver(measureHeader)"
      ".observe(headEl);\n", "")),

    # The bar never moves. The element is there, the class is written, the
    # section is long -- it simply always reads zero, which in a screenshot at
    # the top of a section is indistinguishable from correct.
    ("the progress bar arrives and never reports a position",
     probe_the_progress_bar_reports_the_position,
     ("    progEl.style.scale = at + ' 1';", "")),

    # The opposite: it reports a position on every section, including the ones
    # that fit on a screen, where it can only ever sit at 100%.
    ("the progress bar claims a position on a section that does not scroll",
     probe_the_progress_bar_reports_the_position,
     ("    progEl.classList.toggle('on', worth);",
      "    progEl.classList.toggle('on', true);")),

    # The header stops saying it is in front. Two lines inside a handler that
    # goes on doing its two other jobs, so nothing else changes.
    ("the header sits flat on the content it is covering",
     probe_the_header_lifts_off_the_content,
     ("    const deep = scrollY > DEEP_AT;\n"
      "    if (deep !== headDeep){ headDeep = deep; if (headEl) headEl.classList.toggle('deep', deep); }\n",
      "")),

    # The door cards fall back to the browser's ring. Every card still works,
    # from the mouse and from the keyboard; it is only the one control on the
    # board wearing a focus ring the design never chose -- which is what it was
    # doing before, unnoticed, because a focus ring is the one state nobody
    # screenshots.
    ("a door card goes back to whatever ring the browser draws",
     probe_a_door_card_wears_the_boards_focus_ring,
     ("  textarea:focus-visible, .card.go:focus-visible,",
      "  textarea:focus-visible,")),

    # The press stops being answered. The hover still is, so the surface looks
    # alive right up to the moment it is used.
    ("the chip answers the pointer and goes still under the click",
     probe_a_press_is_answered,
     ("  .skchip:active, .legitem:active, .callbtn:active, .rmstep:active,\n"
      "  header #live:active, .card.go:active { translate:0 1px; }\n", "")),

    # The measure cap, gone: one declaration, and prose in a plain row goes back
    # to the width of the window.
    ("the prose column goes back to the width of the panel",
     probe_the_reading_measure_is_capped,
     ("  .hits li { max-width:min(100%, 68ch);\n", "  .hits li {\n")),

    # The same cap for the OTHER row shape, which does not use `max-width` at
    # all. `1fr` is the shape this regresses into in practice — it is what you
    # reach for when a column looks too narrow, and it silently removes the cap
    # while still reading like a deliberate two-column layout.
    ("the two-column row lets its prose track grow to fill the panel",
     probe_the_reading_measure_is_capped,
     ("    grid-template-columns:minmax(0, 68ch) minmax(0, 26em);",
      "    grid-template-columns:1fr minmax(0, 26em);")),

    # The breakpoint, disarmed rather than deleted, so the rules are still in
    # the sheet and still never apply -- which is how a media query regresses in
    # practice, by having its condition edited.
    ("the narrow window keeps the wide layout",
     probe_the_board_reflows_for_a_narrow_window,
     ("  @media (max-width: 860px) {", "  @media (max-width: 1px) {")),

    # T13 --------------------------------------------------------------------
    # The silent cat goes back to full colour. The class is still written, the
    # legend still lists it, and the strip still has three kinds of cat on it --
    # they simply all look the same, which is the shape a visual state regresses
    # in: nothing throws, nothing is missing, and the operator is told the
    # opposite of the truth by a cat that looks healthy.
    ("a turn that delivered nothing looks exactly like one that delivered",
     probe_the_injection_row_shows_three_distinct_states,
     ("  .mn-inj.silent { filter:grayscale(100%); opacity:.5; }\n", "")),

    # The reduction, not the rendering: `partial` stops winning over `verbatim`
    # inside one turn, so a turn carrying a degraded pass renders as healthy.
    # Every cat still draws, in a colour the sheet defines, from a class the
    # legend documents -- the wrong one.
    ("a degraded pass stops colouring the turn it degraded",
     probe_the_injection_row_shows_three_distinct_states,
     ("    if (vs.indexOf('partial') >= 0) return 'partial';\n", "")),

    # Hover stops answering. The cat is still there, still coloured, still
    # clickable -- it has simply gone mute on the one interaction that costs the
    # reader nothing.
    ("the cat says nothing on hover",
     probe_hovering_an_injection_cat_names_the_turn,
     ("      el.title = 'injection ' + (t.ts || '') + CAT_DOT + turnVerdict(t)\n"
      "        + CAT_DOT + spoke + ' of ' + total + ' spoke' + CAT_DOT\n"
      "        + t.total_chars + ' chars';\n", "")),

    # The half of the title that a colour cannot carry: how many passes actually
    # spoke. A turn the strip calls verbatim can still have had four of seven
    # stay silent, and this is the only place that number appears.
    ("the title keeps the verdict and drops how many passes spoke",
     probe_hovering_an_injection_cat_names_the_turn,
     ("        + CAT_DOT + spoke + ' of ' + total + ' spoke' + CAT_DOT\n"
      "        + t.total_chars + ' chars';\n",
      "        + CAT_DOT + t.total_chars + ' chars';\n")),

    # The click, gone. Hover still works, so the row looks entirely alive right
    # up to the moment somebody tries to use it.
    ("the cat is decorative -- clicking it does nothing at all",
     probe_clicking_an_injection_cat_opens_and_closes_the_whole_turn,
     ("      el.addEventListener('click', ev => {\n"
      "        ev.stopPropagation();\n"
      "        const already = el.classList.contains('sel');\n"
      "        if (already){ mnClosePanel('inj'); }\n"
      "        else { mnOpen('inj', el, () => injPanel(t, el, block)); }\n"
      "      });\n", "")),

    # A toggle that only opens -- the standard way this is half-built, and
    # invisible to anyone who tests the opening and stops there.
    ("the panel opens and can never be closed from the cat that opened it",
     probe_clicking_an_injection_cat_opens_and_closes_the_whole_turn,
     ("        if (already){ mnClosePanel('inj'); }\n",
      "        if (false){ mnClosePanel('inj'); }\n")),

    # The clamp, gone: back to left-aligning on the cat. Found by opening the
    # real page, and it can only ever be found that way -- nothing in a string
    # comparison can see a rectangle leave the window.
    ("the panel left-aligns to its cat and walks off the right edge",
     probe_the_injection_panel_stays_inside_the_window,
     ("    p.style.left = Math.round(Math.min(Math.max(4, r.left), max)) + 'px';\n",
      "    p.style.left = Math.round(r.left) + 'px';\n")),
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
