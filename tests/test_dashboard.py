# tests/test_dashboard.py
import json
import re
import socket
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from mindhive import dashboard, dashboard_page

# Store isolation is provided by the autouse `isolate_store` fixture in
# tests/conftest.py — every dashboard test (and its in-process server threads)
# runs against a throwaway ~/.mindhive so the live ledger is never touched.


@pytest.fixture
def dashboard_server():
    """Start a dashboard server on an ephemeral port; yield (base, token) and
    shut it down on teardown. Replaces the per-test _start() + try/finally."""
    token = "test-token-123"
    handler = dashboard.make_handler(token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", token
    finally:
        server.shutdown()


def _get(base, path, token=None):
    url = base + path
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def test_root_page_serves_html_without_token(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "<html" in body.lower()
    assert "MIND HIVE" in body


def test_api_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/state")  # no token
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_state_endpoint_returns_text(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/state", token)
    assert status == 200
    # Real STATE.md content (or the known empty sentinel) — not just "is a str".
    assert "# STATE" in body or "no STATE.md" in body


def test_verify_endpoint_returns_json(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/verify", token)
    assert status == 200
    obj = json.loads(body)
    # /api/verify wraps chain.verify() — assert its real contract key.
    assert "ok" in obj and isinstance(obj["ok"], bool)


def test_recall_endpoint_returns_list(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/recall?q=anything", token)
    assert status == 200
    obj = json.loads(body)
    assert isinstance(obj, list)


def test_graph_endpoint_returns_html(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/graph", token)
    assert status == 200
    # Real rendered graph page (the template emits these), not just any "<".
    assert "<html" in body.lower() and '<svg id="svg">' in body


def test_changelog_endpoint_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/changelog")  # no token
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_changelog_endpoint_returns_entries_and_pending(dashboard_server):
    from mindhive import changelog, events
    base, token = dashboard_server
    # empty store: no entries, no pending marker; categories always served
    # so the UI never hardcodes the taxonomy
    status, body = _get(base, "/api/changelog", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["entries"] == [] and obj["pending"] is None
    assert obj["categories"] == ["update", "minor_change", "deletion",
                                 "reduction", "major_change", "replacement"]
    # one recorded entry + a pending marker for an unlogged touch
    changelog.record("replacement", "swapped the exporter implementation",
                     files=["src/mindhive/export.py"], session_id="s1")
    events.append_event("file_touched", {"text": "/repo/src/mindhive/late.py"},
                        session_id="s2", ts="2026-07-15T11:00:00Z")
    changelog.queue(events.read_all(), session_id="s2")
    status, body = _get(base, "/api/changelog", token)
    obj = json.loads(body)
    assert len(obj["entries"]) == 1
    e = obj["entries"][0]
    assert e["category"] == "replacement"
    assert e["files"] == ["src/mindhive/export.py"]
    assert obj["pending"]["files"] == ["/repo/src/mindhive/late.py"]


def test_served_page_contains_log_view_hook(dashboard_server):
    # The CLAUDE.md restart check greps the served page for view hooks —
    # showLog must be present alongside showOversee.
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "showLog" in body
    assert "/api/changelog" in body


def test_boot_does_not_clobber_a_view_the_user_already_picked(dashboard_server):
    # loadProjects() takes seconds on a large ledger and the nav stays clickable
    # while it runs, so boot's own showOverview() used to overwrite whatever
    # section the user clicked in that window — silently, with no error.
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "await loadProjects(); if (!viewPicked) showOverview();" in body
    # and the unguarded form must be gone
    assert "await loadProjects(); showOverview();" not in body


def test_only_section_loads_arm_a_skeleton(dashboard_server):
    """A placeholder belongs to a run whose job is to replace the panel.

    run() is also how the dashboard does in-panel work -- dismissing an
    improvement, reflecting, resolving, enriching a prompt, deleting a session,
    appending a memory -- and how both Action buttons run. Those start from an
    already-painted view and do not replace it, so a skeleton there wipes what
    the user was reading in order to announce work that leaves it in place. It
    shipped firing for every caller of run(); it is now opt-in.
    """
    base, token = dashboard_server
    status, body = _get(base, "/")
    block = _js_block(body, "async function run(label, fn")
    assert "isSection" in block, "the skeleton is not gated on the caller's intent"
    skel_line = block[block.index("const skel"):block.index("const skel") + 140]
    assert "isSection" in skel_line, "the skeleton timer is armed regardless of caller"

    for call in ("run('loading overview', renderOverview, true)",
                 "run('loading memory', renderMemory, true)",
                 "run('loading oversee', renderOversee, true)",
                 "run('loading state', renderState, true)",
                 "run('loading change log', renderLog, true)",
                 "run('loading SOPs', renderSops, true)",
                 "run('loading sessions', renderSessions, true)",
                 "run('loading improvements', renderImprovements, true)",
                 "run('loading skill & plugin', renderPlugins, true)"):
        assert call in body, f"section load lost its skeleton: {call}"

    # The two Action helpers share this exact paint line; neither may opt in.
    assert body.count("if (current()) showText(r);") == 2, (
        "doAction/confirmAction changed shape -- re-check the assertion below")
    for i in _all_indexes(body, "if (current()) showText(r);"):
        assert ", true)" not in body[i:i + 120], (
            "an Action button arms a skeleton over the view it started from")


def _all_indexes(hay, needle):
    i, out = hay.find(needle), []
    while i != -1:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def test_run_marks_the_view_picked_before_it_awaits(dashboard_server):
    # The flag has to be set at click time. Setting it after the section's fetch
    # resolves would leave the same race open, because boot can win that race.
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    start = body.index("async function run(label, fn")   # prefix: the signature grows
    body_of_run = body[start:start + 400]
    picked = body_of_run.index("viewPicked = true;")
    busy = body_of_run.index("setBusy(true, label);")
    assert picked < busy, "viewPicked must be set before run() does anything else"
    # panel swaps that never reach run() count too
    assert "function only(el){ viewPicked = true;" in body


def test_both_pollers_skip_a_tick_instead_of_queueing(dashboard_server):
    # Measured 2026-08-10 against the 31k-event store: the section endpoints cost
    # 1.2-1.9 s of pure-Python fold and /api/mindnet/cats 0.87-0.92 s, against a
    # 2500 ms timer. Overlapping requests do not divide that work, they queue it --
    # four concurrent requests measured 4.99 s against 5.3 s run serially, so the GIL
    # serialises the folds -- and a user's click lands behind the queue at 6-12 s.
    # Each poller must skip its tick rather than stack another request on top.
    #
    # LIMIT, stated so nobody mistakes this for proof: this asserts the SHAPE of the
    # served source, not behaviour. No JavaScript executes here. It catches the guard
    # being deleted, set too late, or never released; it cannot observe two real
    # requests overlapping. The behavioural half now exists for the live poller --
    # `a_stuck_poll_never_issues_a_second_request` in the node tier-1 suite parks a
    # request and counts how many go out -- but only for that one; pollCats has no
    # executed cover, so this shape check is still the whole story there.
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    for fn, flag in (("refreshLive", "liveInFlight"), ("pollCats", "catsInFlight")):
        start = body.index("async function " + fn + "(){")
        block = body[start:start + 900]
        # `if (flag) return;` or a braced body that returns -- the live poller's
        # took a body when it grew the stuck-poll report. What must hold is that
        # the latch is read and the tick ends, not which of the two shapes it is.
        guard = block.index("if (" + flag + ")")
        assert "return;" in block[guard:guard + 200], \
            fn + " must skip while one of its own requests is in flight"
        # raised before the first await, or two ticks both pass the check and overlap
        raised = block.index(flag + " = true;")
        awaited = block.index("await ")
        assert raised < awaited, fn + " must set " + flag + " before it awaits"
        # released in finally, or one thrown error wedges the poller permanently
        assert "finally { " + flag + " = false; }" in block, \
            fn + " must clear " + flag + " in a finally block"


# --------------------------------------------------------------------------
# The stale-paint discard (T9): a render must never paint a section the user
# is no longer on.
#
# WHAT WAS ACTUALLY WRONG, so nobody re-derives it from the tests below: the
# live poller calls liveFn() DIRECTLY -- it does not go through run(), and it
# does not set the busy flag. So a poll of Oversee started 2 s ago is still in
# flight when Overview is clicked; /api/overview happens to answer first; the
# poller's showHTML() then lands LAST and repaints Oversee under a "ready"
# status. The user sees the section they picked appear and then get taken
# away. Disabling the nav during a fetch never touched this path, and a
# generation counter stamped inside run() cannot see the poller's renders at
# all -- which is why the fix compares the render function's own identity
# against liveFn at paint time.
#
# LIMIT of everything in this block, stated plainly: these assert the SHAPE of
# the served source. No JavaScript executes here. They catch a guard being
# deleted, placed before the await where it can catch nothing, placed after the
# paint where it is too late, or ordered after a throw (which reaches run()'s
# catch and paints an error over the new section instead). They cannot observe
# two real responses interleaving. The decision itself IS executed, in
# tests/test_paint_guard_node.py (T11, `pytest -m node`), against the exact
# source this page ships -- the seam is asserted by
# test_the_guard_node_executes_is_the_guard_the_page_ships below. Tier 2
# (Playwright, T12) does not exist yet.
# --------------------------------------------------------------------------

# Every renderer registered as a liveFn. renderPlugins is asserted separately:
# it predates this change and already carries the idiom in its positive form.
_LIVE_RENDERERS = ["renderOverview", "renderMemory", "renderImprovements",
                   "renderOversee", "renderLog", "renderSops", "renderState"]
# Renderers that raise on a bad payload -- the guard has to come first.
_THROWING_RENDERERS = ["renderImprovements", "renderOversee", "renderLog",
                       "renderSops"]
_PAINT_CALLS = ("showHTML(", "showText(", "paintMemory(", "paintImprovements(",
                "paintOversee(", "paintPlugins(")


def _js_block(page, signature):
    """Slice one top-level function out of the served page: from its signature
    to the first line that closes at the script's own two-space indent. Every
    nested block in this file is indented four or more, so `\\n  }` cannot match
    inside one (the same slice the poller test above relies on)."""
    start = page.index(signature)
    return page[start:page.index("\n  }", start)]


def _first_of(block, needles):
    hits = [block.index(n) for n in needles if n in block]
    return min(hits) if hits else None


@pytest.mark.parametrize("fn", _LIVE_RENDERERS)
def test_a_live_renderer_discards_a_paint_for_a_section_the_user_left(fn, dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    block = _js_block(body, "async function " + fn + "(){")
    guard = "if (!shouldPaint(liveFn, " + fn + ")) return;"
    assert guard in block, (
        fn + " can paint a section the user already navigated away from; it "
        "must re-read liveFn after its fetch and return when it no longer "
        "matches itself")
    where = block.index(guard)
    assert where > block.index("await "), (
        fn + " checks liveFn before it awaits, where nothing can have changed "
        "yet -- the check has to happen after the response lands")
    paint = _first_of(block, _PAINT_CALLS)
    assert paint is not None, fn + " no longer paints anything; fix this test"
    assert where < paint, fn + " checks liveFn only after it has already painted"


@pytest.mark.parametrize("fn", _THROWING_RENDERERS)
def test_a_discarded_render_does_not_raise_either(fn, dashboard_server):
    """Returning early is only half of it. A stale render that throws lands in
    run()'s catch, which calls fail() -- and fail() paints the error over
    whatever section the user is now looking at. Same clobber, different route."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    block = _js_block(body, "async function " + fn + "(){")
    assert block.index("if (!shouldPaint(liveFn, " + fn + ")) return;") < block.index("throw new Error"), (
        fn + " raises before it checks whether its section is still selected")


@pytest.mark.parametrize("fn,cache", [("renderMemory", "MEM_ITEMS = "),
                                      ("renderImprovements", "IMP_ITEMS = "),
                                      ("renderOversee", "OV_DATA = ")])
def test_a_discarded_render_writes_no_cache(fn, cache, dashboard_server):
    """A stale response that skips the paint but still overwrites the module
    cache leaves the visible section rendering another section's data on its
    next repaint -- the same bug, deferred by one tick."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    block = _js_block(body, "async function " + fn + "(){")
    assert block.index("if (!shouldPaint(liveFn, " + fn + ")) return;") < block.index(cache), (
        fn + " writes " + cache.strip() + " before it knows the response is "
        "still wanted")


def test_the_plugins_renderer_still_carries_the_same_idiom(dashboard_server):
    """renderPlugins had this guard before T9 (in its positive form, gating a
    paint rather than returning early). It is the reason the idiom was chosen
    rather than invented: seven existing call sites. Those sites now call
    shouldPaint too -- one decision, one home, one thing the node test covers."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert "if (shouldPaint(liveFn, renderPlugins)) paintPlugins();" in body
    assert body.count("shouldPaint(liveFn, renderPlugins)") >= 6
    assert "liveFn === renderPlugins" not in body, (
        "a raw identity comparison is back; the decision has to stay in "
        "shouldPaint, which is the only part of it any test can execute")
    assert "liveFn !== render" not in body, (
        "a raw identity comparison is back outside shouldPaint")


def test_the_guard_node_executes_is_the_guard_the_page_ships(dashboard_server):
    """SOURCE-SHAPE assertion, and stated as one -- but it is the seam that
    stops the tier-1 node test (tests/test_paint_guard_node.py) from proving
    something true about a copy. That test imports PAINT_GUARD_JS and executes
    it; this asserts the browser is served those same bytes. Without this pair,
    the page's guard could change and node would keep testing the old text."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert dashboard_page.PAINT_GUARD_JS in body, (
        "the served page no longer contains PAINT_GUARD_JS verbatim -- the node "
        "test is now exercising something the browser never receives")
    assert "/*__PAINT_GUARD__*/" not in body, "the placeholder was never substituted"
    # One definition only: a second copy is how the two would drift.
    assert body.count("function shouldPaint(") == 1
    assert "function shouldPaint(currentLiveFn, renderFn){" in \
        dashboard_page.PAINT_GUARD_JS
    # Pure, or node cannot import it: no globals, no DOM, no fetch.
    guard = _js_block(body, "function shouldPaint(currentLiveFn, renderFn){")
    for forbidden in ("document", "window", "liveFn =", "await", "fetch("):
        assert forbidden not in guard, (
            f"shouldPaint touches {forbidden!r}; it has to stay importable "
            "outside a browser or the tier-1 test cannot run it")


def test_the_nav_is_never_disabled_during_a_fetch(dashboard_server):
    """setBusy used to disable every nav button for the length of a fetch. It
    never covered the poller (which bypasses setBusy), it made the nav feel dead
    for the 1-2 s a section costs, and it is what made 'click A then B' feel
    impossible. The discard guards replace it; the server-side locks make it
    safe. Nothing may quietly reintroduce it."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    block = _js_block(body, "function setBusy(on, label){")
    assert "disabled" not in block, (
        "setBusy is disabling controls again: " + block)
    assert "navButtons" not in body, "the nav-locking helper is back"
    assert "nav button:disabled" not in body, (
        "dead CSS for a state nav buttons no longer enter")


def test_busy_is_announced_to_assistive_tech_instead_of_locking_the_nav(dashboard_server):
    """The accessibility half of dropping the nav lock. `disabled` on every nav
    button was the ONLY signal a screen reader got that the app was busy -- a
    side effect of a lock, not a statement of state. Removing it without
    replacing it would have been a regression for exactly the users who cannot
    see the spinner."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert 'id="panel" aria-busy="false"' in body, (
        "the panel does not declare an initial aria-busy state")
    block = _js_block(body, "function setBusy(on, label){")
    assert "aria-busy" in block, "aria-busy is never updated as work starts/ends"
    for needle in ('id="ann"', 'role="status"', 'aria-live="polite"',
                   'aria-atomic="true"', 'class="sr-only"'):
        assert needle in body, f"missing live-region attribute: {needle}"
    # display:none / hidden on a live region means it is never announced at all
    assert ".sr-only { position:absolute" in body
    assert "display:none" not in body[body.index(".sr-only {"):
                                      body.index(".sr-only {") + 200]


def test_run_announces_the_section_change_and_the_outcome(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    block = _js_block(body, "async function run(label, fn")   # prefix: the signature grows
    assert "announce(label);" in block, "no announcement when a section starts loading"
    # `await fn(` rather than `await fn()`: run() now hands the body its own
    # generation check, so the call carries an argument.
    assert block.index("announce(label);") < block.index("await fn(")
    assert block.count("announce(") == 3, (
        "expected three announcements: section change, success, failure -- got "
        + str(block.count("announce(")))
    assert "fail(e); announce(" in block, (
        "a failed operation ends silently for a screen reader")

    # The two OUTCOME announcements must be generation-guarded; the opening one
    # must not be. #ann is not the panel, so shouldPaint never covered it: a
    # stale run announced "loading oversee - ready" into the live region after
    # the user had already moved to Overview, and the node harness certified
    # those runs as clean because its panel probe watched only #view and #out.
    # Measured against the pre-guard page with the probe widened: 9 failures.
    after_await = block[block.index("await fn("):]
    assert after_await.count("if (current())") >= 2, (
        "the ready/failed announcements are not guarded by run()'s generation, "
        "so a stale section still speaks over the one the user picked")
    assert "if (current()) announce(label);" not in block, (
        "the OPENING announcement must fire unguarded -- at that instant this "
        "run IS the current one, and guarding it would silence every section "
        "change instead of only the stale ones")


def test_loading_feedback_is_staged_so_a_fast_section_never_flashes(dashboard_server):
    """Two thresholds, not one. Painting a placeholder immediately makes every
    fast section flicker; never painting one leaves the 1.2-1.9 s section loads
    looking frozen. And a 10 s wait under a bare 'working…' chip reads as a hang."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert "const SKEL_MS = 300, SLOW_MS = 10000;" in body
    block = _js_block(body, "async function run(label, fn")   # prefix: the signature grows
    assert "showSkeleton()" in block and "SKEL_MS)" in block, "no deferred skeleton"
    assert "SLOW_MS" in block, "nothing escalates the status text on a long wait"
    # Both timers belong to the run that armed them, and both now check the
    # generation before firing: a stale run's skeleton would otherwise repaint
    # over the section that replaced it, and its slow-status timer would clobber
    # the live run's status text 10 s in.
    skel_call = block[block.index("SKEL_MS)") - 120:block.index("SKEL_MS)")]
    assert "if (current())" in skel_call, (
        "the deferred skeleton is not generation-guarded")
    slow_call = block[block.index("const slow = setTimeout("):block.index("SLOW_MS);")]
    assert "if (current())" in slow_call, (
        "the slow-status timer is not generation-guarded, so a stale run "
        "rewrites the status text of the section that replaced it")
    # both timers cleared in finally, or a failed op leaves a skeleton painting
    # itself over the error the user needs to read; busy is released only by the
    # run that still owns the panel
    assert ("finally { clearTimeout(skel); clearTimeout(slow); "
            "if (current()) setBusy(false); }") in block
    skel = _js_block(body, "function showSkeleton(){")
    assert "document.activeElement" in skel and "closest('#view')" in skel, (
        "the skeleton can repaint the panel out from under a caret")


def test_every_page_module_guards_the_animations_it_declares():
    """D8 for the pages the census above cannot see.

    The census asserts per-selector coverage for `/`. It cannot cover
    `research_page`, `workspace_page` or `swarm_page`: those render into the
    dashboard's own iframes, so the host page's reduced-motion block never
    reaches them, and they are not in the served `/` stylesheet at all.
    `research_page` and `workspace_page` each shipped a pulsing status dot with
    no guard of their own -- not a partial gap, a total one.

    WEAKER THAN THE CENSUS, deliberately, and here is the limit: this asserts
    only that a module declaring motion also declares a reduced-motion block
    that stops something. It does NOT prove every selector in that module is
    covered, because two things make that undecidable from source text:

      * guards cross module boundaries -- `swarm_page`'s `.chip.running
        .mn-cat-sprite` is stopped by `cat_board.CSS`, which swarm_page injects
        at `/*__CAT_CSS__*/`, not by anything in swarm_page's own sheet; and
      * a guard can name a class the animating selector reaches through the DOM
        rather than through text -- `.mn-cat.active svg` IS `.mn-cat-sprite`,
        which is exactly why `_MOTION_GUARDS` above is a written map.

    A per-selector version of this sweep reported both of those as violations,
    and "fixing" the swarm one added a line already guarded with `!important`
    from cat_board. A check that manufactures work is worse than a narrow one
    that does not, so this stays narrow and the census stays the strict half.
    """
    import pathlib
    from mindhive import dashboard_page as _dp

    pkg = pathlib.Path(_dp.__file__).parent
    offenders = []
    for path in sorted(pkg.rglob("*.py")):
        # comments first: these files argue about their motion choices at
        # length, and prose containing `animation:` reads as a declaration.
        src = re.sub(r"/\*.*?\*/", " ",
                     path.read_text(encoding="utf-8", errors="replace"), flags=re.S)
        on = set(re.findall(r"animation:\s*([\w-]+)", src)) - {"none"}
        if not on:
            continue
        guards = re.findall(
            r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\s*\}", src, re.S)
        if not any("animation:" in g and "none" in g for g in guards):
            offenders.append(f"{path.name}: turns on {sorted(on)} and never "
                             f"stops it under prefers-reduced-motion")
    assert not offenders, (
        "a page module ships motion with no reduced-motion guard at all:\n  "
        + "\n  ".join(offenders) +
        "\nRepeating animation is a trigger for some people, not an annoyance, "
        "and an iframe page inherits nothing from the host page's guard.")


def test_the_pages_own_animations_honour_reduced_motion(dashboard_server):
    """`cat_board.CSS` ships the guard for the cat sprites and deliberately names
    nothing else. Without a page-local block, a reduced-motion user would keep
    every pulse this page declares itself and lose only the cats' bob."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    blocks = re.findall(r"@media \(prefers-reduced-motion: reduce\)\s*\{"
                        r"(.*?)\n  \}", body, re.S)
    assert blocks, "the dashboard page ships no reduced-motion guard at all"
    covered = " ".join(blocks)
    for sel in (".dot.busy", "#live .blip", ".ovspin", ".rmnode.rmactive",
                ".skel .bar"):
        assert sel in covered, (
            f"{sel} keeps animating under prefers-reduced-motion")
    assert "!important" in covered, (
        "the rules switching each animation on are at least as specific, and a "
        "media query adds no specificity -- without !important the guard loses")


# Every selector in the SERVED page that switches motion on, mapped to the
# selector of the rule that switches it back off under prefers-reduced-motion.
# Usually the same string. `.mn-cat.active svg` is the exception, and it is the
# reason this is a written map instead of a computed one: cat_board disables it
# through `.mn-cat-sprite`, a class it stamps on every sprite it draws, so the
# two are different text for the same elements. No CSS engine runs here, so that
# pairing is asserted rather than proved -- but the CENSUS below is computed
# from the page, which is the half that catches the regression that actually
# happens: an animation added later, in any of the three fragments, with nothing
# turning it off.
_MOTION_GUARDS = {
    # dashboard_page.py
    "header .dot.busy": "header .dot.busy",
    "header #live .blip": "header #live .blip",
    ".ovspin": ".ovspin",
    ".rmnode.rmactive": ".rmnode.rmactive",
    ".rmfill": ".rmfill",
    ".skel .bar": ".skel .bar",
    # cat_board.py
    ".mn-cat.active svg": ".mn-cat-sprite",
    ".mn-sess.working .mn-cat-sprite": ".mn-cat-sprite",
    ".mn-sess": ".mn-sess",
    # taskview.py
    ".tstat.ongoing": ".tstat.ongoing",
}

_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_MOTION_RE = re.compile(r"(?:animation|transition)\s*:\s*([^;}]+)")


def _css_rules(css):
    for sel, decls in _RULE_RE.findall(css):
        yield " ".join(sel.split()), decls


def test_every_animation_the_page_serves_is_stopped_under_reduced_motion(dashboard_server):
    """The census behind the checklist above. Three modules contribute CSS to
    this one page -- dashboard_page, cat_board and taskview -- and each guards
    its own motion, so "did anyone add a pulse and forget" is a question no
    single file can answer. This asks the assembled page.

    SOURCE-SHAPE, stated plainly: it reads the served stylesheet, it does not
    apply it. It cannot prove a guard wins the cascade; it proves every live
    animation has one, that the one it has is `!important` (a media query adds
    no specificity, so without it the guard loses), and that a newly added
    animation cannot ship without somebody deciding about it here."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert body.count("<style>") == 1, "the page grew a second stylesheet block"
    css = re.sub(r"/\*.*?\*/", "",
                 body[body.index("<style>"):body.index("</style>")], flags=re.S)

    reduced = re.findall(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n  \}",
                         css, re.S)
    assert reduced, "the page ships no reduced-motion guard at all"
    outside = css
    for block in reduced:
        outside = outside.replace(block, "")

    live = set()
    for sel, decls in _css_rules(outside):
        m = _MOTION_RE.search(decls)
        if m and not m.group(1).strip().startswith("none"):
            live.add(sel)
    assert live == set(_MOTION_GUARDS), (
        "the page's motion changed and this census was not updated.\n"
        f"  no guard decided for: {sorted(live - set(_MOTION_GUARDS))}\n"
        f"  listed but gone from the page: {sorted(set(_MOTION_GUARDS) - live)}\n"
        "Repeating animation is a trigger for some people, not an annoyance: a "
        "new one needs a prefers-reduced-motion rule that stops it, in the "
        "fragment that declares it.")

    stopped = {}
    for block in reduced:
        for sel, decls in _css_rules(block):
            if re.search(r"(?:animation|transition)\s*:\s*none", decls):
                for one in sel.split(","):
                    stopped.setdefault(" ".join(one.split()), []).append(decls)
    for on_sel, guard_sel in sorted(_MOTION_GUARDS.items()):
        assert guard_sel in stopped, (
            f"{on_sel} keeps moving under prefers-reduced-motion: no rule for "
            f"{guard_sel} inside a reduced-motion block")
        assert "!important" in " ".join(stopped[guard_sel]), (
            f"the guard for {on_sel} ({guard_sel}) is not !important, so the "
            "more specific rule that turns the animation on still wins")


def test_the_live_chip_stops_claiming_live_after_three_failed_ticks(dashboard_server):
    """Swallowing every poll failure was right for one blip and wrong for a dead
    server: the chip went on reading 'live' while nothing had refreshed for
    minutes. CONSECUTIVE is the load-bearing word -- a counter that never resets
    would flag a single recovered blip forever."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert "const LIVE_STALE_STRIKES = 3;" in body
    block = _js_block(body, "function markLiveHealth(ok){")
    assert "liveFails = ok ? 0 : liveFails + 1;" in block, (
        "one success must reset the streak, or a recovered blip flags forever")
    assert "liveFails >= LIVE_STALE_STRIKES" in block
    assert "classList.toggle('stale'" in block, "the state is never made visible"
    live = _js_block(body, "async function refreshLive(){")
    assert "markLiveHealth(true)" in live and "markLiveHealth(false)" in live, (
        "refreshLive records neither success nor failure")
    assert "header #live.stale" in body, "the stale class has no styling"


def test_overview_endpoint_returns_stats(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/overview", token)
    assert status == 200
    obj = json.loads(body)
    for key in ("project", "events", "by_type", "last_ts", "chain_ok"):
        assert key in obj, f"overview missing {key}"
    assert isinstance(obj["events"], int)
    assert isinstance(obj["by_type"], dict)
    assert isinstance(obj["chain_ok"], bool)


def test_overview_counts_an_appended_event(dashboard_server):
    base, token = dashboard_server
    before = json.loads(_get(base, "/api/overview", token)[1])["events"]
    _post(base, "/api/append", {"type": "decision", "payload": "ov note"}, token)
    after = json.loads(_get(base, "/api/overview", token)[1])
    assert after["events"] == before + 1
    assert after["by_type"].get("decision", 0) >= 1
    assert after["last_ts"]


def test_plugins_recommend_returns_json(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/plugins/recommend", token)
    assert status == 200
    obj = json.loads(body)
    assert isinstance(obj, dict)


def _post(base, path, payload, token):
    url = f"{base}{path}?token={token}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_plugins_autoscan_is_stale_gated(dashboard_server):
    base, token = dashboard_server
    # empty store -> first autoscan must actually scan
    status, obj = _post(base, "/api/plugins/autoscan", {}, token)
    assert status == 200 and obj["ok"] is True
    assert obj["scanned"] is True
    assert isinstance(obj["result"], dict)
    # second autoscan finds a fresh snapshot -> skips the scan
    status, obj2 = _post(base, "/api/plugins/autoscan", {}, token)
    assert obj2["scanned"] is False
    # force overrides the freshness gate
    status, obj3 = _post(base, "/api/plugins/autoscan", {"force": True}, token)
    assert obj3["scanned"] is True


def test_plugins_inventory_endpoint_returns_live_snapshot(dashboard_server, monkeypatch):
    """GET /api/plugins/inventory serves the signature-gated live inventory —
    the payload the Skills & plugins view polls every LIVE_MS."""
    from mindhive import plugins as plugins_mod
    fake = {"changed": True, "signature": "abc",
            "inventory": {"plugins": [], "local_skills": [],
                          "counts": {"plugins": 0, "plugin_skills": 0,
                                     "local_skills": 0, "skills_total": 0}}}
    monkeypatch.setattr(plugins_mod, "live_inventory", lambda: fake)
    base, token = dashboard_server
    status, body = _get(base, "/api/plugins/inventory", token)
    assert status == 200
    assert json.loads(body) == fake


def test_plugins_inventory_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/plugins/inventory")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_skill_and_plugin_view(dashboard_server):
    """The nav entry carries the Skill & Plugin panel wired to the live engine."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "Skill &amp; Plugin" in body
    for needle in ("renderPlugins", "showSkillPanel", "Recommended now",
                   "/api/plugins/inventory", "/api/skills/recommend"):
        assert needle in body, f"missing skill&plugin hook: {needle}"


def test_skills_detail_endpoint_returns_intended_use_body_and_date(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    detail = {"name": "one", "intended_use": "Use for testing.",
              "added_at": "2026-06-11T04:07:43.030Z", "body": "# One\n\nbody",
              "source": "alpha"}
    captured = {}
    monkeypatch.setattr(plugins_mod, "skill_detail",
                        lambda name, plugin=None: captured.update(name=name, plugin=plugin) or detail)
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/detail?name=one&plugin=alpha", token)
    assert status == 200
    assert json.loads(body) == detail
    assert captured == {"name": "one", "plugin": "alpha"}


def test_skills_detail_unknown_returns_404(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "skill_detail", lambda name, plugin=None: None)
    base, token = dashboard_server
    try:
        _get(base, "/api/skills/detail?name=nope", token)
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_skills_detail_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/skills/detail?name=one")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_skills_invoke_records_and_returns_ok(dashboard_server, monkeypatch):
    from mindhive import invoker as invoker_mod
    seen = {}
    monkeypatch.setattr(invoker_mod, "invoke_skill",
        lambda name, plugin=None, project=None: seen.update(name=name, plugin=plugin)
        or {"ok": True, "exit": 0, "excerpt": "done"})
    base, token = dashboard_server
    status, obj = _post(base, "/api/skills/invoke",
                        {"name": "brainstorming", "plugin": "superpowers"}, token)
    assert status == 200 and obj["ok"] is True
    assert seen == {"name": "brainstorming", "plugin": "superpowers"}


def test_skills_invoke_requires_name(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/skills/invoke", {}, token)
    assert obj["ok"] is False and "name" in obj["error"]


def test_skills_invoke_requires_token(dashboard_server):
    base, token = dashboard_server
    req = urllib.request.Request(f"{base}/api/skills/invoke", data=b"{}",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_skills_history_endpoint_returns_list(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    seen = {}
    monkeypatch.setattr(plugins_mod, "call_history",
        lambda name, project=None, limit=20, evs=None: seen.update(name=name, limit=limit)
        or [{"ts": "t", "ok": True, "text": "x", "session": "s"}])
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/history?name=foo&limit=7", token)
    assert status == 200
    assert json.loads(body) == [{"ts": "t", "ok": True, "text": "x", "session": "s"}]
    assert seen == {"name": "foo", "limit": 7}


def test_skills_history_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/skills/history?name=foo")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_call_history_hook(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("/api/skills/history", "Call history"):
        assert needle in body, f"missing call-history hook: {needle}"


def test_skills_status_endpoint_returns_status_and_counts(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "call_stats",
        lambda project=None, evs=None: {"status": {"a": "ok", "b": "fail"},
                                        "counts": {"a": 2, "b": 1}})
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/status", token)
    assert status == 200
    assert json.loads(body) == {"status": {"a": "ok", "b": "fail"},
                                "counts": {"a": 2, "b": 1}}


def test_page_has_invoke_count_hooks(dashboard_server):
    """Per-skill call telemetry is surfaced. The Skill & Plugin rebuild (U7)
    replaced the old name-adjacent count badge with a richer metrics line
    (calls / ok / fail / completion rate) rendered from PLUG_STATS via
    skMetrics — the invoke-count intent is preserved and enriched."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("PLUG_STATS", "skMetrics"):
        assert needle in body, f"missing per-skill telemetry hook: {needle}"


def test_page_has_clickable_improvements_legend(dashboard_server):
    """Improvements legend glyphs filter by resolution on click, like the
    Skills legend's one-click status filters."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("setImpRes", "IMP_RES"):
        assert needle in body, f"missing improvements legend filter hook: {needle}"


def test_skills_stats_endpoint_returns_stats_keyed_by_skill(dashboard_server):
    from mindhive import events
    events.append_event("invoke_result", {"name": "a", "ok": True, "text": ""},
                        session_id="s", ts="2026-07-01T10:00:00Z", project="test")
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/stats?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["stats"]["a"]["calls"] == 1
    assert obj["stats"]["a"]["completion_rate"] == 1.0


def test_skills_stats_endpoint_unknown_project_is_empty_not_500(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/stats?project=no-such-ns", token)
    assert status == 200
    assert json.loads(body) == {"stats": {}}


def test_skills_recommend_endpoint_returns_ranked_payload(dashboard_server, monkeypatch):
    from mindhive import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "live_inventory", lambda: {
        "changed": True, "signature": "x", "inventory": {
            "plugins": [{"name": "p", "skills": [
                {"name": "tester", "trigger": "Use when tests fail."}]}],
            "local_skills": [], "counts": {}}})
    base, token = dashboard_server
    status, body = _get(base, "/api/skills/recommend?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert "coverage" in obj and "recommendations" in obj
    assert obj["recommendations"][0]["name"] == "tester"
    assert obj["recommendations"][0]["provenance"] in ("measured", "estimated")


def test_page_has_invoke_and_status_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("invokeSkill", "/api/skills/invoke", "/api/skills/status", "statusDot"):
        assert needle in body, f"missing invoke/status hook: {needle}"


def test_skills_plugins_view_has_improvements_style_controls(dashboard_server):
    """The Skills & plugins view mirrors the Improvements mechanism: a fetch/paint
    split, persistent search + filter + sort controls, and a status legend."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("paintPlugins", "onPlugSearch", "plugstatus", "plugmarket",
                   "plugsort", "plugdir", "plugLegend", "PLUG_QUERY"):
        assert needle in body, f"missing plugins filter/sort hook: {needle}"


def test_skills_plugins_view_has_card_call_button_and_clickable_legend(dashboard_server):
    """Callable straight from the list (per-card ▶ call) and a legend whose
    green/red/grey items filter by status on click."""
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("callbtn", "setPlugStatus", "legitem"):
        assert needle in body, f"missing polish hook: {needle}"


def test_page_has_skill_detail_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("showSkillDetail", "/api/skills/detail", "Intended use"):
        assert needle in body, f"missing detail hook: {needle}"


def test_append_creates_event(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/append",
                        {"type": "decision", "payload": "dashboard test note",
                         "tags": "test"}, token)
    assert status == 200
    assert obj["ok"] is True
    assert obj["type"] == "decision"
    assert "ts" in obj


# --- registry contracts at the dashboard append surface ---------------------
# POST /api/append is the second surface that takes an arbitrary type+payload.
# It used to hardcode {"text": payload} — discarding any structured body, so it
# could not produce a valid registry payload at all — and it ran none of the
# per-type contracts `hive append` enforces. Both halves are covered here.
# Scope note: this is shape validation. A well-formed payload is accepted by
# design; refusing an illegitimate-but-well-formed one is the controller's job.

def test_append_keeps_structured_payload_for_registry_type(dashboard_server):
    from mindhive import events
    from mindhive.mindnet import tasks as tasks_mod
    base, token = dashboard_server
    payload = {"action": "declare", "task_id": "dash-u1",
               "title": "structured bodies survive the endpoint"}
    status, obj = _post(base, "/api/append",
                        {"type": "mindnet_task", "payload": payload,
                         "project": "p"}, token)
    assert status == 200 and obj["ok"] is True
    evs = events.read_all("p")
    assert len(evs) == 1
    # The real payload was stored — not {"text": …} — so it round-trips through
    # its own contract and folds into the graph.
    assert evs[0].payload == payload
    tasks_mod.validate_payload(evs[0].payload)
    assert tasks_mod.fold(evs)["dash-u1"].title == payload["title"]


def test_append_rejects_malformed_registry_payload(dashboard_server):
    from mindhive import events
    base, token = dashboard_server
    status, obj = _post(base, "/api/append",
                        {"type": "mindnet_task",
                         "payload": {"action": "declare"},   # no task_id
                         "project": "p"}, token)
    # An expected domain rejection, not a server fault.
    assert status == 200
    assert obj["ok"] is False
    assert "task_id" in obj["error"] and "hive mindnet" in obj["error"]
    assert events.read_all("p") == []       # the ledger is unchanged


def test_append_validates_the_other_registry_types_too(dashboard_server):
    """The endpoint now enforces every contract the CLI does, not just the new
    one — roadmap stands in for the four that predate mindnet_task."""
    from mindhive import events
    base, token = dashboard_server
    status, obj = _post(base, "/api/append",
                        {"type": "roadmap", "payload": {"action": "set"},
                         "project": "p"}, token)
    assert status == 200 and obj["ok"] is False
    assert "hive roadmap" in obj["error"]
    assert events.read_all("p") == []


def test_append_still_wraps_plain_text_for_free_text_types(dashboard_server):
    """Regression: the {"text": …} path must survive for uncontracted types —
    it is what the dashboard's own Append box sends."""
    from mindhive import events
    base, token = dashboard_server
    status, obj = _post(base, "/api/append",
                        {"type": "decision", "payload": "a plain string note",
                         "tags": "test", "project": "p"}, token)
    assert status == 200 and obj["ok"] is True and obj["type"] == "decision"
    evs = events.read_all("p")
    assert len(evs) == 1
    assert evs[0].payload == {"text": "a plain string note"}
    assert evs[0].tags == ["test"]


def test_post_requires_token(dashboard_server):
    base, token = dashboard_server
    url = f"{base}/api/project"
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


from mindhive import paths as hp


def test_embed_blocked_by_live_lock(dashboard_server):
    base, token = dashboard_server
    lock = hp.store_home() / "LIVE_LOCK"
    created = False
    if not lock.exists():
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("test", encoding="utf-8")
        created = True
    try:
        status, obj = _post(base, "/api/embed", {"force": False}, token)
        assert status == 200
        assert obj["ok"] is False
        assert "LIVE_LOCK" in obj["error"]
    finally:
        if created:
            lock.unlink()


def test_page_has_feature_controls(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    for needle in ["State", "Recall", "Graph", "Append", "Plugins", "Export"]:
        assert needle in body, f"missing control: {needle}"
    assert "fetch(" in body


# --------------------------------------------------------------------------
# Stable URL: fixed port + persisted token
# --------------------------------------------------------------------------

def test_default_port_is_fixed():
    assert dashboard.DEFAULT_PORT == 54461


def test_token_is_persisted_and_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("MINDHIVE_DASHBOARD_TOKEN", raising=False)
    t1 = dashboard.load_or_create_token()
    t2 = dashboard.load_or_create_token()
    assert t1 and t1 == t2                     # identical across launches
    # and it's actually on disk under the store home
    assert dashboard._token_path().read_text(encoding="utf-8").strip() == t1


def test_token_env_overrides_file(monkeypatch):
    monkeypatch.setenv("MINDHIVE_DASHBOARD_TOKEN", "fixed-env-token")
    assert dashboard.load_or_create_token() == "fixed-env-token"


# --------------------------------------------------------------------------
# Project / session / memory separation
# --------------------------------------------------------------------------

def test_projects_endpoint_lists_namespaces_with_stats(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "decision", "payload": "b", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "decision", "payload": "c", "project": "beta"}, token)
    status, obj = _get(base, "/api/projects", token)
    assert status == 200
    data = json.loads(obj)
    names = {p["name"]: p for p in data["projects"]}
    assert "alpha" in names and "beta" in names
    assert names["alpha"]["events"] == 2
    assert data["default"] == "alpha"          # richest wins


def test_overview_separates_memory_from_auto(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "intent", "project": "p"}, token)
    _post(base, "/api/append", {"type": "file_touched", "payload": "x.py", "project": "p"}, token)
    status, body = _get(base, "/api/overview?project=p", token)
    o = json.loads(body)
    assert o["events"] == 2
    assert o["memory_events"] == 1             # decision only
    assert o["auto_events"] == 1               # file_touched


def test_memory_endpoint_returns_semantic_only(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "caveat", "payload": "watch out", "project": "p"}, token)
    _post(base, "/api/append", {"type": "command_run", "payload": "ls", "project": "p"}, token)
    status, body = _get(base, "/api/memory?project=p", token)
    items = json.loads(body)
    assert len(items) == 1
    assert items[0]["type"] == "caveat"
    assert items[0]["text"] == "watch out"


def test_transcripts_fingerprint_advances_on_append(tmp_path):
    """The low-latency loop gates on this: it must change when a session file
    grows, so a new prompt triggers a harvest within one poll."""
    import os, time
    root = tmp_path / "projects"
    (root / "enc").mkdir(parents=True)
    f = root / "enc" / "s.jsonl"
    f.write_text("line1\n", encoding="utf-8")
    fp1 = dashboard._transcripts_fingerprint(root)
    assert fp1 > 0
    newer = time.time() + 5
    os.utime(f, (newer, newer))            # simulate an append a moment later
    fp2 = dashboard._transcripts_fingerprint(root)
    assert fp2 > fp1


def test_transcripts_fingerprint_empty_root_is_zero(tmp_path):
    assert dashboard._transcripts_fingerprint(tmp_path / "nope") == 0.0


def test_memory_endpoint_includes_user_prompts(dashboard_server):
    """Every captured prompt shows in the Memory view (a user_prompt event),
    while deterministic activity (command_run) still does not."""
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "user_prompt", "payload": "what is the plan?", "project": "p"}, token)
    _post(base, "/api/append", {"type": "command_run", "payload": "ls", "project": "p"}, token)
    status, body = _get(base, "/api/memory?project=p", token)
    items = json.loads(body)
    texts = {(m["type"], m["text"]) for m in items}
    assert ("user_prompt", "what is the plan?") in texts
    assert not any(m["type"] == "command_run" for m in items)


def test_memory_endpoint_collapses_near_duplicates(dashboard_server):
    base, token = dashboard_server
    # Two re-phrasings of the same caveat + one distinct memo.
    _post(base, "/api/append", {"type": "caveat",
          "payload": "prefer the browser for visual content like mockups and diagrams",
          "project": "p"}, token)
    _post(base, "/api/append", {"type": "caveat",
          "payload": "prefer browser for visual content like mockups and diagrams",
          "project": "p"}, token)
    _post(base, "/api/append", {"type": "caveat",
          "payload": "rebuild the exe with python 3.12 before launching", "project": "p"}, token)
    status, body = _get(base, "/api/memory?project=p", token)
    items = json.loads(body)
    assert len(items) == 2                      # the pair collapsed to one
    rep = next(m for m in items if "browser" in m["text"])
    assert rep["similar_count"] == 1            # absorbed one older duplicate
    # newest wording wins (the second append, without "the")
    assert rep["text"] == "prefer browser for visual content like mockups and diagrams"


def test_sessions_endpoint_groups_by_session(dashboard_server, monkeypatch):
    base, token = dashboard_server
    monkeypatch.setenv("MINDHIVE_SESSION", "sess-1")
    _post(base, "/api/append", {"type": "decision", "payload": "one", "project": "p"}, token)
    monkeypatch.setenv("MINDHIVE_SESSION", "sess-2")
    _post(base, "/api/append", {"type": "decision", "payload": "two", "project": "p"}, token)
    _post(base, "/api/append", {"type": "file_touched", "payload": "y.py", "project": "p"}, token)
    status, body = _get(base, "/api/sessions?project=p", token)
    sessions = {s["session_id"]: s for s in json.loads(body)}
    assert set(sessions) == {"sess-1", "sess-2"}
    assert sessions["sess-2"]["events"] == 2
    assert sessions["sess-2"]["memory"] == 1   # only the decision counts as memory


def test_project_scoping_isolates_overview(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "only-A", "project": "A"}, token)
    status, body = _get(base, "/api/overview?project=B", token)
    o = json.loads(body)
    assert o["project"] == "B"
    assert o["events"] == 0                     # A's event must not leak into B


def test_hivemind_overview_aggregates_all_projects(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "file_touched", "payload": "x.py", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "caveat", "payload": "b", "project": "beta"}, token)
    status, body = _get(base, "/api/overview?project=*", token)
    o = json.loads(body)
    assert o["events"] == 3                      # alpha(2) + beta(1)
    assert o["memory_events"] == 2               # decision + caveat
    assert o["auto_events"] == 1                 # file_touched
    assert o["projects"] == 2


def test_hivemind_memory_spans_projects_and_labels_them(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "from-alpha", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "caveat", "payload": "from-beta", "project": "beta"}, token)
    status, body = _get(base, "/api/memory?project=*", token)
    items = json.loads(body)
    projs = {m["project"] for m in items}
    assert projs == {"alpha", "beta"}
    assert {m["text"] for m in items} == {"from-alpha", "from-beta"}


def test_hivemind_projects_endpoint_exposes_unified_totals(dashboard_server):
    base, token = dashboard_server
    _post(base, "/api/append", {"type": "decision", "payload": "a", "project": "alpha"}, token)
    _post(base, "/api/append", {"type": "decision", "payload": "b", "project": "beta"}, token)
    status, body = _get(base, "/api/projects", token)
    data = json.loads(body)
    assert data["hivemind"]["events"] == 2
    assert data["hivemind"]["id"] == "*"


def test_hivemind_append_is_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/append",
                        {"type": "decision", "payload": "nope", "project": "*"}, token)
    assert obj["ok"] is False
    assert "HIVE MIND" in obj["error"]


def test_harvest_endpoint_runs_catch_up(dashboard_server, monkeypatch):
    import mindhive.harvest as h
    monkeypatch.setattr(h, "catch_up", lambda skip_captured=True: {"projects": {}, "total_new": 0})
    base, token = dashboard_server
    status, obj = _post(base, "/api/harvest", {}, token)
    assert obj["ok"] is True
    assert obj["result"]["total_new"] == 0


def test_harvest_endpoint_allowed_under_hivemind(dashboard_server, monkeypatch):
    import mindhive.harvest as h
    monkeypatch.setattr(h, "catch_up", lambda skip_captured=True: {"projects": {}, "total_new": 0})
    base, token = dashboard_server
    status, obj = _post(base, "/api/harvest", {"project": "*"}, token)
    assert obj["ok"] is True            # harvest is global, not blocked by HIVE MIND guard


def test_plugins_scan_allowed_under_hivemind(dashboard_server):
    # Plugins/skills are installed machine-wide, so scanning is global — the
    # HIVE MIND (all-projects) view must NOT reject it with the read-only guard.
    base, token = dashboard_server
    status, obj = _post(base, "/api/plugins/autoscan", {"project": "*"}, token)
    assert obj["ok"] is True
    assert "HIVE MIND" not in str(obj.get("error", ""))
    status, obj2 = _post(base, "/api/plugins/scan", {"project": "*"}, token)
    assert obj2["ok"] is True


def test_page_has_hivemind_picker(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert 'value="*"' in body or "hivemind" in body
    assert "all projects" in body.lower()


def test_page_has_project_and_memory_controls(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ['id="proj"', "onProjChange", "showMemory", "showSessions", "/api/projects"]:
        assert needle in body, f"missing: {needle}"


# --------------------------------------------------------------------------
# Token comparison (constant-time) + /api/sync folder resolution
# --------------------------------------------------------------------------

def test_wrong_token_is_rejected(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/state", token="totally-wrong-token")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_correct_token_is_accepted(dashboard_server):
    base, token = dashboard_server
    status, _ = _get(base, "/api/state", token)
    assert status == 200


def test_sync_uses_registered_project_folder(dashboard_server, monkeypatch):
    """/api/sync must write the selected project's OWN folder (from the autosync
    registry), not the directory the dashboard server was launched from."""
    from mindhive import autosync
    autosync.register("test", "C:/tx/test", "C:/code/test-project")
    captured = {}

    def spy_sync(folder, project=None):
        captured["folder"] = folder
        captured["project"] = project
        return "C:/code/test-project/MEMORY.md"

    monkeypatch.setattr(dashboard.sync_mod, "sync", spy_sync)
    base, token = dashboard_server
    status, obj = _post(base, "/api/sync", {"project": "test"}, token)
    assert status == 200 and obj["ok"] is True
    assert captured["folder"] == "C:/code/test-project"   # registry folder, not cwd
    assert captured["project"] == "test"
    assert obj["folder"] == "C:/code/test-project"


def test_sync_falls_back_to_cwd_when_unregistered(dashboard_server, monkeypatch):
    """An unregistered project has no folder mapping, so sync uses the cwd."""
    import os
    captured = {}
    monkeypatch.setattr(dashboard.sync_mod, "sync",
                        lambda folder, project=None: captured.update(folder=folder) or "x")
    base, token = dashboard_server
    _post(base, "/api/sync", {"project": "never-registered"}, token)
    assert captured["folder"] == os.getcwd()


# --- launch_background(): detached background launcher for the loop ----------

def test_stable_url_uses_default_port_and_token(monkeypatch):
    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "tok-xyz")
    url = dashboard.stable_url()
    assert url == f"http://127.0.0.1:{dashboard.DEFAULT_PORT}/?token=tok-xyz"


def test_launch_background_spawns_dashboard_subprocess(monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "tok-abc")
    monkeypatch.setattr(dashboard, "_instance_running", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.subprocess, "Popen", fake_popen)
    res = dashboard.launch_background()
    assert res["launched"] is True
    assert res["pid"] == 4321
    assert "tok-abc" in res["url"]
    assert captured["argv"][0] == dashboard.sys.executable
    assert captured["argv"][1:] == ["-m", "mindhive", "dashboard"]
    # child stdio is fully detached from the parent console (all three streams)
    assert captured["kwargs"]["stdin"] == dashboard.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] == dashboard.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == dashboard.subprocess.DEVNULL


def test_launch_background_passes_explicit_port(monkeypatch):
    captured = {}
    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "t")
    monkeypatch.setattr(dashboard.subprocess, "Popen",
                        lambda argv, **kw: captured.update(argv=argv) or type("P", (), {"pid": 1})())
    dashboard.launch_background(port=0)
    assert captured["argv"][-2:] == ["--port", "0"]


def test_launch_background_uses_platform_detach_flags(monkeypatch):
    import os as _os
    captured = {}
    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "t")
    monkeypatch.setattr(dashboard, "_instance_running", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.subprocess, "Popen",
                        lambda argv, **kw: captured.update(kw=kw) or type("P", (), {"pid": 1})())
    dashboard.launch_background()
    if _os.name == "nt":
        assert captured["kw"].get("creationflags", 0) != 0
    else:
        assert captured["kw"].get("start_new_session") is True


def test_launch_background_is_non_fatal_on_spawn_failure(monkeypatch):
    def boom(argv, **kw):
        raise OSError("cannot spawn")

    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "t")
    monkeypatch.setattr(dashboard, "_instance_running", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.subprocess, "Popen", boom)
    res = dashboard.launch_background()
    assert res["launched"] is False
    assert "cannot spawn" in res["reason"]

    # --- single-instance guard: only ever one dashboard pid ------------------


def test_instance_running_true_when_a_server_is_listening(dashboard_server):
    base, _ = dashboard_server
    port = int(base.rsplit(":", 1)[1])
    assert dashboard._instance_running(port) is True


def test_instance_running_false_when_nothing_listening():
    # bind an ephemeral port, close it, then probe: nothing is accepting there
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    assert dashboard._instance_running(free_port) is False


def test_instance_running_false_for_ephemeral_port_zero():
    assert dashboard._instance_running(0) is False


def test_launch_background_skips_spawn_when_already_running(monkeypatch, dashboard_server):
    base, _ = dashboard_server
    port = int(base.rsplit(":", 1)[1])
    spawned = {"called": False}

    def fake_popen(argv, **kw):
        spawned["called"] = True
        return type("P", (), {"pid": 1})()

    monkeypatch.setattr(dashboard, "load_or_create_token", lambda: "t")
    monkeypatch.setattr(dashboard.subprocess, "Popen", fake_popen)
    res = dashboard.launch_background(port=port)
    assert res["launched"] is False
    assert res["already_running"] is True
    assert spawned["called"] is False   # the whole point: no second pid


def test_serve_returns_early_without_binding_when_already_running(monkeypatch, dashboard_server):
    base, _ = dashboard_server
    port = int(base.rsplit(":", 1)[1])

    def boom(*a, **k):
        raise AssertionError("serve() must not bind a second server")

    # if the guard fails, serve() would try to bind and hit this
    monkeypatch.setattr(dashboard, "ThreadingHTTPServer", boom)
    dashboard.serve(port=port, open_browser=False)   # returns cleanly via the probe

    # --- open_panel(): the `mind hive` shortcut — launch-or-focus -------------


def test_open_panel_launches_when_nothing_running(monkeypatch):
    opened = {"called": False}
    monkeypatch.setattr(dashboard, "launch_background",
                        lambda port=None: {"launched": True, "url": "http://x/?token=t", "pid": 1})
    monkeypatch.setattr(dashboard.webbrowser, "open",
                        lambda *a, **k: opened.update(called=True))
    msg = dashboard.open_panel()
    assert msg == "MIND HIVE dashboard: http://x/?token=t"
    assert opened["called"] is False   # the detached child opens its own browser


def test_open_panel_opens_browser_when_already_running(monkeypatch):
    opened = {}
    monkeypatch.setattr(dashboard, "launch_background",
                        lambda port=None: {"launched": False, "already_running": True,
                                           "url": "http://x/?token=t", "reason": "already running"})
    monkeypatch.setattr(dashboard.webbrowser, "open",
                        lambda url, *a, **k: opened.update(url=url))
    msg = dashboard.open_panel()
    assert opened["url"] == "http://x/?token=t"     # seamless: browser lands on the live panel
    assert "already running" in msg and "http://x/?token=t" in msg


def test_open_panel_reports_failure_without_opening_browser(monkeypatch):
    opened = {"called": False}
    monkeypatch.setattr(dashboard, "launch_background",
                        lambda port=None: {"launched": False, "reason": "cannot spawn"})
    monkeypatch.setattr(dashboard.webbrowser, "open",
                        lambda *a, **k: opened.update(called=True))
    msg = dashboard.open_panel()
    assert msg == "dashboard: not launched (cannot spawn)"
    assert opened["called"] is False


# --------------------------------------------------------------------------
# /api/improvements: lessons learned (auto incidents + manual caveats)
# --------------------------------------------------------------------------
from mindhive import events as ev_mod, incidents as inc_mod


def _seed_incident(project, sig="pymodule:mindhive", n=2):
    for i in range(n):
        ev_mod.append_event("tool_failed",
                            {"text": f"err {sig}", "signature": sig,
                             "action": f"cmd{i}"},
                            session_id=f"s{i}", tags=["auto", "failure"],
                            project=project)
    inc_mod.audit(project=project)
    inc_mod.project(project=project)


def test_improvements_returns_auto_and_manual(dashboard_server):
    base, token = dashboard_server
    _seed_incident("test")
    ev_mod.append_event("caveat", {"text": "never run embed while V17 trades"},
                        session_id="s9", project="test")
    status, body = _get(base, "/api/improvements?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert len(obj["auto"]) == 1
    a = obj["auto"][0]
    assert a["signature"] == "pymodule:mindhive"
    assert a["occurrences"] == 2
    assert a["source"] == "auto" and a["project"] == "test"
    assert a["status"] == "active"
    assert a["lesson"] == ""                     # not yet reflected
    assert a["excerpt"].startswith("err ")
    assert len(obj["manual"]) == 1
    m = obj["manual"][0]
    assert m["source"] == "manual" and "V17" in m["text"]


def test_improvements_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/improvements")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_improvements_empty_store_returns_empty_lists(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/improvements?project=nothing-here", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["auto"] == [] and obj["manual"] == []
    # four tiles, all honest about having no data at all
    assert set(obj["stats"]) == {"day", "week", "month", "quarter"}
    for row in obj["stats"].values():
        assert row["disposition"] == "no_current_data"
        assert row["delta_pct"] is None


def test_improvements_hivemind_aggregates_projects(dashboard_server):
    base, token = dashboard_server
    ev_mod.append_event("caveat", {"text": "p1 lesson about flaky socket"},
                        session_id="a", project="p1")
    ev_mod.append_event("caveat", {"text": "p2 note on schema drift"},
                        session_id="b", project="p2")
    status, body = _get(base, "/api/improvements?project=*", token)
    assert status == 200
    obj = json.loads(body)
    projects = {m["project"] for m in obj["manual"]}
    assert {"p1", "p2"} <= projects


def test_improvements_unexpected_error_returns_500_json(dashboard_server, monkeypatch):
    base, token = dashboard_server

    def boom(project=None):
        raise RuntimeError("kaput")

    monkeypatch.setattr(inc_mod, "summaries", boom)
    try:
        _get(base, "/api/improvements?project=test", token)
        assert False, "expected 500"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "kaput" in obj["error"]


def test_page_has_improvements_view(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    for needle in ("Improvements", "renderImprovements", "paintImprovements",
                   "impstatus", "impcat"):
        assert needle in body, f"missing improvements hook: {needle}"


# --------------------------------------------------------------------------
# POST /api/rules/reflect: bounded, guarded reflection from the panel
# --------------------------------------------------------------------------
from mindhive import reflect as refl_mod, dashboard as dash_mod


def _write_verdict(project, sig):
    path = (hp.project_dir(project) / "rules" / "incidents"
            / f"{inc_mod._safe_name(sig)}.md")
    path.write_text(inc_mod._replace_section(
        path.read_text(encoding="utf-8"), "Auto Evaluation",
        "- Root cause: x\n- Lesson: y\n- Prevention rule: z\n- Category: tool_use"),
        encoding="utf-8")


def test_reflect_happy_path(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:one")
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert status == 200
    assert obj == {"ok": True, "reflected": 1, "failed": 0, "pending": 0}


def test_reflect_skips_already_reflected(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:done")
    _write_verdict("test", "sig:done")
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert obj["ok"] is True and obj["reflected"] == 0 and obj["pending"] == 0
    assert calls == []


def test_reflect_batch_cap_reports_pending(dashboard_server, monkeypatch):
    base, token = dashboard_server
    for i in range(5):
        _seed_incident("test", sig=f"sig:batch{i}")
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect",
                        {"project": "test", "limit": 3}, token)
    assert len(calls) == 3
    assert obj["reflected"] == 3 and obj["pending"] == 2


def test_reflect_excludes_candidates(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:single", n=1)      # below threshold
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert obj == {"ok": True, "reflected": 0, "failed": 0, "pending": 0}
    assert calls == []


def test_reflect_rejects_concurrent_run(dashboard_server):
    base, token = dashboard_server
    assert dash_mod._REFLECT_LOCK.acquire(blocking=False)
    try:
        status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
        assert status == 200
        assert obj["ok"] is False and "already running" in obj["error"]
    finally:
        dash_mod._REFLECT_LOCK.release()


def test_reflect_all_failed_missing_cli_is_domain_rejection(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:nocli")
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: {"reflected": False,
                                                   "reason": "empty verdict"})
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert status == 200
    assert obj["ok"] is False and "claude CLI unavailable" in obj["error"]


def test_reflect_noop_on_empty_store(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect", {"project": "bare"}, token)
    assert obj == {"ok": True, "reflected": 0, "failed": 0, "pending": 0}


def test_reflect_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect", {"project": "*"}, token)
    assert status == 200
    assert obj["ok"] is False and "read-only" in obj["error"]


# --------------------------------------------------------------------------
# POST /api/rules/resolve: active resolution over the red (unresolved) set
# --------------------------------------------------------------------------
_ROUTES_JSON = ('{"routes": [{"prevention_rule": "take the alternative route", '
                '"rationale": "different mechanism", "confidence": 0.9}]}')


def _seed_leaking_incident(project, sig="raw:leaky"):
    for s, ts in (("s1", "2026-07-01T10:00:00Z"), ("s2", "2026-07-01T11:00:00Z")):
        ev_mod.append_event("tool_failed",
                            {"text": "boom", "signature": sig, "action": "x"},
                            session_id=s, tags=["auto", "failure"], ts=ts,
                            project=project)
    inc_mod.audit(project=project)
    inc_mod.project(project=project)
    path = (hp.project_dir(project) / "rules" / "incidents"
            / f"{inc_mod._safe_name(sig)}.md")
    path.write_text(inc_mod._replace_section(
        path.read_text(encoding="utf-8"), "Auto Evaluation",
        "- Prevention rule: the failed rule"), encoding="utf-8")
    inc_mod.project(project=project)
    ev_mod.append_event("tool_failed",
                        {"text": "boom", "signature": sig, "action": "x"},
                        session_id="s3", tags=["auto", "failure"],
                        ts="2026-07-01T12:00:00Z", project=project)   # leak


def test_resolve_reroutes_leaking_rule(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_leaking_incident("test")
    monkeypatch.setattr(refl_mod, "_claude_runner",
                        lambda prompt, timeout_s=120: _ROUTES_JSON)
    status, obj = _post(base, "/api/rules/resolve", {"project": "test"}, token)
    assert status == 200
    assert obj["ok"] is True and obj["rerouted"] == 1 and obj["pending"] == 0
    rec = [r for r in inc_mod.summaries(project="test")
           if r["signature"] == "raw:leaky"][0]
    assert rec["active_rule"] == "take the alternative route"
    # the reroute re-anchors leak accounting: the lesson is no longer red
    st = inc_mod.signal_stats("test")["raw:leaky"]
    assert st["leaks"] == 0 and st["reroutes"] == 1


def test_resolve_rejects_concurrent_run(dashboard_server):
    base, token = dashboard_server
    assert dash_mod._REFLECT_LOCK.acquire(blocking=False)
    try:
        status, obj = _post(base, "/api/rules/resolve", {"project": "test"}, token)
        assert status == 200
        assert obj["ok"] is False and "already running" in obj["error"]
    finally:
        dash_mod._REFLECT_LOCK.release()


def test_resolve_noop_on_empty_store(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/resolve", {"project": "bare"}, token)
    assert obj["ok"] is True and obj["rerouted"] == 0 and obj["red"] == 0


def test_improvements_payload_carries_reroute_fields(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_leaking_incident("test", sig="raw:routed")
    monkeypatch.setattr(refl_mod, "_claude_runner",
                        lambda prompt, timeout_s=120: _ROUTES_JSON)
    from mindhive import resolve as resolve_mod
    assert resolve_mod.reroute_incident("raw:routed",
                                        project="test")["resolved"] is True
    status, body = _get(base, "/api/improvements?project=test", token)
    obj = json.loads(body)
    card = [a for a in obj["auto"] if a["signature"] == "raw:routed"][0]
    assert card["reroutes"] == 1 and card["last_reroute"]
    assert card["resolution"] == "watching"        # fresh route, not yet proven


def test_page_has_resolve_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("/api/rules/resolve", "runResolve", "resolve red"):
        assert needle in body, f"missing resolve hook: {needle}"


# --------------------------------------------------------------------------
# POST /api/rules/reflect_one: on-demand reflection of ONE failure signature —
# lets a single skill-call failure be learned from immediately (the UI's
# "reflect this failure" action), bypassing the recurrence-threshold gate.
# --------------------------------------------------------------------------

def test_reflect_one_reflects_a_single_named_failure(dashboard_server, monkeypatch):
    base, token = dashboard_server
    # one failure (below the recurrence threshold) -> the endpoint still reflects it
    ev_mod.append_event("tool_failed",
                        {"text": "boom", "signature": "skill:foo",
                         "action": "invoke skill foo"},
                        session_id="s1", project="test")
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect_one",
                        {"project": "test", "signature": "skill:foo"}, token)
    assert status == 200
    assert obj["ok"] is True and obj["reflected"] is True
    assert obj["signature"] == "skill:foo"


def test_reflect_one_unknown_signature_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect_one",
                        {"project": "test", "signature": "skill:never"}, token)
    assert obj["ok"] is False and "no such incident" in obj["error"]


def test_reflect_one_requires_signature(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect_one", {"project": "test"}, token)
    assert obj["ok"] is False and "signature" in obj["error"]


def test_reflect_one_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/reflect_one",
                        {"project": "*", "signature": "skill:x"}, token)
    assert status == 200
    assert obj["ok"] is False and "read-only" in obj["error"]


def test_reflect_one_requires_token(dashboard_server):
    base, token = dashboard_server
    req = urllib.request.Request(f"{base}/api/rules/reflect_one", data=b"{}",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_reflect_failure_hook(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    for needle in ("/api/rules/reflect_one", "reflectFailure"):
        assert needle in body, f"missing reflect-failure hook: {needle}"


def test_reflect_unexpected_error_returns_500(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:boom")

    def boom(sig, project=None):
        raise RuntimeError("reflector exploded")

    monkeypatch.setattr(refl_mod, "reflect_incident", boom)
    url = f"{base}/api/rules/reflect?token={token}"
    data = json.dumps({"project": "test"}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "expected 500"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "exploded" in obj["error"]


# --------------------------------------------------------------------------
# /api/improvements efficiency stats + per-lesson enrichment
# --------------------------------------------------------------------------
import time as time_mod


def _iso_ago(seconds):
    return time_mod.strftime("%Y-%m-%dT%H:%M:%S",
                             time_mod.gmtime(time_mod.time() - seconds)) + "Z"


def _seed_actions(project, session, ts, n, failures=0, sig="sig:x"):
    for i in range(n - failures):
        ev_mod.append_event("command_run", {"text": f"cmd{i}"},
                            session_id=session, ts=ts, project=project)
    for i in range(failures):
        ev_mod.append_event("tool_failed",
                            {"text": "boom", "signature": sig, "action": "x"},
                            session_id=session, ts=ts, project=project)


def _stats(base, token, project):
    status, body = _get(base, f"/api/improvements?project={project}", token)
    assert status == 200
    return json.loads(body)["stats"]


def test_efficiency_week_improvement_is_ok_with_negative_delta(dashboard_server):
    base, token = dashboard_server
    _seed_actions("eff", "s1", _iso_ago(10 * 86400), 22, failures=2)  # prev week
    _seed_actions("eff", "s2", _iso_ago(2 * 86400), 21, failures=1)   # this week
    week = _stats(base, token, "eff")["week"]
    assert week["disposition"] == "ok"
    assert week["cur_actions"] == 21 and week["prev_actions"] == 22
    assert week["delta_pct"] < 0                     # fewer failures = improvement


def test_efficiency_ignores_conversation_events(dashboard_server):
    base, token = dashboard_server
    _seed_actions("eff", "s1", _iso_ago(10 * 86400), 22, failures=2)
    _seed_actions("eff", "s2", _iso_ago(2 * 86400), 21, failures=1)
    before = _stats(base, token, "eff")["week"]
    for i in range(50):
        ev_mod.append_event("user_prompt", {"text": f"p{i}"}, session_id="s2",
                            ts=_iso_ago(3600), project="eff")
    after = _stats(base, token, "eff")["week"]
    assert after == before


def test_efficiency_sparse_dispositions(dashboard_server):
    base, token = dashboard_server
    # current data, no previous week -> no_baseline; empty day -> no_current_data
    _seed_actions("ebase", "s1", _iso_ago(2 * 86400), 21, failures=1)
    st = _stats(base, token, "ebase")
    assert st["week"]["disposition"] == "no_baseline"
    assert st["day"]["disposition"] == "no_current_data"
    # thin samples in both windows -> low_sample, never a colored percent
    _seed_actions("elow", "s1", _iso_ago(10 * 86400), 3, failures=1)
    _seed_actions("elow", "s2", _iso_ago(2 * 86400), 3)
    assert _stats(base, token, "elow")["week"]["disposition"] == "low_sample"
    # clean in both windows -> no_change at 0.0
    _seed_actions("ezero", "s1", _iso_ago(10 * 86400), 20)
    _seed_actions("ezero", "s2", _iso_ago(2 * 86400), 20)
    zero = _stats(base, token, "ezero")["week"]
    assert zero["disposition"] == "no_change" and zero["delta_pct"] == 0.0
    # failures where the baseline was zero -> regression marker, no percent
    _seed_actions("ereg", "s1", _iso_ago(10 * 86400), 20)
    _seed_actions("ereg", "s2", _iso_ago(2 * 86400), 21, failures=1)
    reg = _stats(base, token, "ereg")["week"]
    assert reg["disposition"] == "regression_from_zero" and reg["delta_pct"] is None


def test_efficiency_hivemind_aggregates_tallies(dashboard_server):
    base, token = dashboard_server
    # 12 + 12 actions per window: below the floor alone, above it combined
    _seed_actions("p1", "s1", _iso_ago(10 * 86400), 12, failures=2)
    _seed_actions("p1", "s2", _iso_ago(2 * 86400), 12, failures=1)
    _seed_actions("p2", "s1", _iso_ago(10 * 86400), 12)
    _seed_actions("p2", "s2", _iso_ago(2 * 86400), 12)
    week = _stats(base, token, "*")["week"]
    assert week["cur_actions"] == 24 and week["prev_actions"] == 24
    assert week["disposition"] == "ok"


def test_improvements_items_carry_resolution_and_timestamps(dashboard_server):
    base, token = dashboard_server
    # reflected rule, then a clean newer session -> resolved (green)
    _seed_actions("test", "g1", _iso_ago(9000), 1, failures=1, sig="sig:green")
    _seed_actions("test", "g1", _iso_ago(8000), 1, failures=1, sig="sig:green")
    # reflected rule that recurred cross-session -> unresolved (red)
    _seed_actions("test", "r1", _iso_ago(9000), 1, failures=1, sig="sig:red")
    _seed_actions("test", "r1", _iso_ago(8000), 1, failures=1, sig="sig:red")
    inc_mod.audit(project="test")
    inc_mod.project(project="test")
    _write_verdict("test", "sig:green")
    _write_verdict("test", "sig:red")
    inc_mod.project(project="test")
    ev_mod.append_event("command_run", {"text": "fine"}, session_id="g2",
                        ts=_iso_ago(1000), project="test")          # exposure
    _seed_actions("test", "r2", _iso_ago(500), 1, failures=1, sig="sig:red")
    status, body = _get(base, "/api/improvements?project=test", token)
    items = {a["signature"]: a for a in json.loads(body)["auto"]}
    green, red = items["sig:green"], items["sig:red"]
    assert green["resolution"] == "resolved" and green["leaks"] == 0
    assert green["first_seen"] and green["last_seen"] > green["first_seen"]
    assert red["resolution"] == "unresolved" and red["leaks"] == 1
    assert red["last_seen"] > red["first_seen"]


def test_page_has_sort_glyph_and_strip_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    for needle in ("impsort", "impdir", "toggleImpDir", "impCompare", "effStrip",
                   "effTile", "glyphSpan", "impLegend", "dismissLesson",
                   "last seen", "first seen"):
        assert needle in body, f"missing view hook: {needle}"


# --------------------------------------------------------------------------
# POST /api/rules/dismiss: explicit demotion from a lesson card
# --------------------------------------------------------------------------


def test_dismiss_happy_path_turns_card_gray(dashboard_server):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:bye")
    status, obj = _post(base, "/api/rules/dismiss",
                        {"project": "test", "signature": "sig:bye"}, token)
    assert status == 200
    assert obj == {"ok": True, "signature": "sig:bye"}
    demos = [e for e in ev_mod.read_all("test") if e.type == "rule_demoted"]
    assert demos and demos[0].payload["signature"] == "sig:bye"
    assert inc_mod.load("sig:bye", project="test")["status"] == "demoted"
    status, body = _get(base, "/api/improvements?project=test", token)
    item = [a for a in json.loads(body)["auto"] if a["signature"] == "sig:bye"][0]
    assert item["resolution"] == "dismissed"


def test_dismiss_unknown_or_missing_signature_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/dismiss",
                        {"project": "test", "signature": "nope:x"}, token)
    assert status == 200 and obj["ok"] is False
    assert "unknown signature" in obj["error"]
    status, obj = _post(base, "/api/rules/dismiss", {"project": "test"}, token)
    assert obj["ok"] is False
    assert not [e for e in ev_mod.read_all("test") if e.type == "rule_demoted"]


def test_post_oversized_body_is_rejected_without_unbounded_read(dashboard_server):
    # A caller advertising a huge Content-Length must be rejected before the
    # body is read, so an (even unauthenticated) POST can't force a large
    # allocation. We send a modest real body but lie about its length.
    base, token = dashboard_server
    url = f"{base}/api/append?token={token}"
    big = (1 << 20) + 1
    req = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(big)})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected rejection"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "too large" in obj["error"]
    except (urllib.error.URLError, ConnectionError):
        pass          # server closed the connection without reading the body — also acceptable


def test_dismiss_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/rules/dismiss",
                        {"project": "*", "signature": "x"}, token)
    assert status == 200
    assert obj["ok"] is False and "read-only" in obj["error"]


def test_dismiss_requires_token(dashboard_server):
    base, token = dashboard_server
    req = urllib.request.Request(f"{base}/api/rules/dismiss", data=b"{}",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_dismiss_unexpected_error_returns_500(dashboard_server, monkeypatch):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:boomd")

    def boom(sig, project=None):
        raise RuntimeError("demoter exploded")

    monkeypatch.setattr(inc_mod, "demote", boom)
    url = f"{base}/api/rules/dismiss?token={token}"
    data = json.dumps({"project": "test", "signature": "sig:boomd"}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "expected 500"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        obj = json.loads(e.read().decode("utf-8"))
        assert obj["ok"] is False and "exploded" in obj["error"]


def test_improvements_stats_miss_is_watching_not_resolved(dashboard_server):
    base, token = dashboard_server
    # incident file with no backing events (projection outlived its events)
    d = hp.project_dir("ghosty") / "rules" / "incidents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ghost-sig.md").write_text(
        "---\nsignature: ghost:sig\nstatus: active\noccurrences: 2\n"
        "rule_source: auto\nflag: \nsessions: 1\n---\n\n# Incident: ghost:sig\n\n"
        "## Raw Signal\n- Error excerpt: `boom`\n\n"
        "## Auto Evaluation\n- Prevention rule: do X\n\n"
        "## Human Evaluation\n<!-- -->\n\n"
        "## Active Prevention Rule\n> do X\n", encoding="utf-8")
    status, body = _get(base, "/api/improvements?project=ghosty", token)
    assert status == 200
    a = json.loads(body)["auto"][0]
    assert a["resolution"] == "watching"            # never green without events
    assert a["first_seen"] is None and a["last_seen"] is None
    assert a["leaks"] == 0


# --- review fixes: limit clamp, non-numeric guard, pending accounting ---

def test_reflect_clamps_oversized_limit(dashboard_server, monkeypatch):
    base, token = dashboard_server
    for i in range(5):
        _seed_incident("test", sig=f"sig:clamp{i}")
    calls = []
    monkeypatch.setattr(refl_mod, "reflect_incident",
                        lambda sig, project=None: calls.append(sig) or {"reflected": True})
    status, obj = _post(base, "/api/rules/reflect",
                        {"project": "test", "limit": 100}, token)
    assert len(calls) == 3                       # clamped to REFLECT_BATCH_LIMIT
    assert obj["reflected"] == 3 and obj["pending"] == 2


def test_reflect_rejects_non_numeric_limit(dashboard_server):
    base, token = dashboard_server
    _seed_incident("test", sig="sig:badlimit")
    status, obj = _post(base, "/api/rules/reflect",
                        {"project": "test", "limit": "abc"}, token)
    assert status == 200
    assert obj["ok"] is False and "integer" in obj["error"]


def test_reflect_pending_excludes_failed(dashboard_server, monkeypatch):
    base, token = dashboard_server
    for i in range(3):
        _seed_incident("test", sig=f"sig:mix{i}")
    seen = []

    def one_ok_rest_fail(sig, project=None):
        seen.append(sig)
        return {"reflected": len(seen) == 1}     # first ok, rest fail

    monkeypatch.setattr(refl_mod, "reflect_incident", one_ok_rest_fail)
    status, obj = _post(base, "/api/rules/reflect", {"project": "test"}, token)
    assert obj["reflected"] == 1 and obj["failed"] == 2
    assert obj["pending"] == 0                    # attempted-but-failed is not pending


# --------------------------------------------------------------------------
# Sessions notification center: /api/sessions activity+status, /api/task,
# /api/task_detail
# --------------------------------------------------------------------------


def _seed_session(project="dashproj", sid="sess-t1"):
    from mindhive import events
    events.append_event("file_touched",
                        {"text": "src/a.py", "plus": 5, "minus": 2, "tool": "Edit"},
                        session_id=sid, project=project)
    events.append_event("command_run",
                        {"text": "pytest -q", "desc": "Run tests"},
                        session_id=sid, project=project)
    events.append_event("caveat", {"text": "plan FIRST"},
                        session_id=sid, project=project)


def test_sessions_carry_activity_and_status(dashboard_server):
    base, token = dashboard_server
    _seed_session()
    status, body = _get(base, "/api/sessions?project=dashproj", token)
    assert status == 200
    (s,) = json.loads(body)
    assert s["status"] in ("ongoing", "completed")
    assert s["activity"]["files"] == 1
    assert s["activity"]["plus"] == 5 and s["activity"]["minus"] == 2
    assert s["activity"]["commands"] == 1 and s["activity"]["notes"] == 1


def test_task_endpoint_returns_capped_rows(dashboard_server):
    base, token = dashboard_server
    _seed_session()
    status, body = _get(base, "/api/task?project=dashproj&session=sess-t1", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["total"] == 3 and obj["truncated"] == 0
    assert [r["kind"] for r in obj["items"]] == ["touched", "ran", "caveat"]


def test_task_endpoint_hivemind_requires_session_project(dashboard_server):
    base, token = dashboard_server
    _seed_session()
    try:
        _get(base, "/api/task?project=*&session=sess-t1", token)
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400
    status, body = _get(
        base, "/api/task?project=*&session=sess-t1&session_project=dashproj",
        token)
    assert status == 200
    assert json.loads(body)["total"] == 3


def test_task_detail_degrades_without_transcript(dashboard_server):
    base, token = dashboard_server
    status, body = _get(
        base, "/api/task_detail?project=dashproj&session=no-such-sess", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["error"] == "transcript_unavailable" and obj["items"] == []


def test_root_page_includes_task_cards_fragments(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert "renderTaskCards" in body        # injected JS arrived
    assert ".tcard" in body                 # injected CSS arrived
    assert "__TASKS_JS__" not in body       # placeholders were replaced
    assert "__TASKS_CSS__" not in body


# --- Oversee: work sessions view (U3/U4) ---------------------------------

def _seed_loop_run(run_id, hour, reason=None, title="Fix the parser"):
    ev_mod.append_event("loop_run_start",
                        {"run_id": run_id, "title": title, "goal_dir": "d"},
                        ts=f"2026-07-07T{hour:02d}:00:00Z", project="test")
    ev_mod.append_event("loop_iteration",
                        {"run_id": run_id, "iteration": 1, "summary": "working",
                         "cost_usd": 0.1, "total_tokens": 100},
                        ts=f"2026-07-07T{hour:02d}:01:00Z", project="test")
    if reason:
        ev_mod.append_event("loop_run_end", {"run_id": run_id, "reason": reason},
                            ts=f"2026-07-07T{hour:02d}:02:00Z", project="test")


def test_oversee_lists_runs_and_online_flag(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10)                     # running (no end event)
    status, body = _get(base, "/api/oversee?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert [r["run_id"] for r in obj["runs"]] == ["run1"]
    r = obj["runs"][0]
    assert r["status"] == "running"
    assert r["title"] == "working"                 # latest summary supersedes
    # events are seeded in the past, so the heartbeat window has lapsed
    assert r["online"] is False and obj["any_online"] is False


def test_oversee_dismiss_removes_run(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10, reason="max_iterations")
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "run1"}, token)
    assert status == 200 and obj == {"ok": True, "run_id": "run1"}
    tombs = [e for e in ev_mod.read_all("test") if e.type == "loop_run_dismissed"]
    assert tombs and tombs[0].payload["cause"] == "manual"
    status, body = _get(base, "/api/oversee?project=test", token)
    obj = json.loads(body)
    assert obj["runs"] == [] and obj["dismissed"] == 1


def test_oversee_dismiss_unknown_or_missing_run_rejected(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "nope"}, token)
    assert status == 200 and obj["ok"] is False
    status, obj = _post(base, "/api/oversee/dismiss", {"project": "test"}, token)
    assert status == 200 and obj["ok"] is False
    assert not [e for e in ev_mod.read_all("test")
                if e.type == "loop_run_dismissed"]


def test_oversee_dismiss_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10)
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "*", "run_id": "run1"}, token)
    assert status == 200 and obj["ok"] is False
    assert "read-only" in obj["error"]


def test_oversee_requires_token(dashboard_server):
    base, token = dashboard_server
    try:
        _get(base, "/api/oversee")
        assert False, "expected 403"
    except urllib.error.HTTPError as e:
        assert e.code == 403


def test_page_has_oversee_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert "showOversee" in body and "Oversee" in body
    assert "/api/oversee" in body and "deleteRun" in body


def test_oversee_double_dismiss_is_idempotent(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10, reason="max_iterations")
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "run1"}, token)
    assert obj == {"ok": True, "run_id": "run1"}
    status, obj = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "run1"}, token)
    assert status == 200
    assert obj["ok"] is True and obj.get("already_dismissed") is True
    tombs = [e for e in ev_mod.read_all("test") if e.type == "loop_run_dismissed"]
    assert len(tombs) == 1                             # no duplicate tombstone


def test_oversee_includes_interactive_sessions(dashboard_server):
    base, token = dashboard_server
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ev_mod.append_event("session_start", {}, session_id="chat-1", ts=ts, project="test")
    ev_mod.append_event("user_prompt", {"text": "live chat work"},
                        session_id="chat-1", ts=ts, project="test")
    status, body = _get(base, "/api/oversee?project=test", token)
    obj = json.loads(body)
    kinds = {r["run_id"]: r["kind"] for r in obj["runs"]}
    assert kinds.get("chat-1") == "chat"
    chat = [r for r in obj["runs"] if r["run_id"] == "chat-1"][0]
    assert chat["title"] == "live chat work"
    assert chat["online"] is True and obj["any_online"] is True
    # manual delete works for chat sessions through the same endpoint
    status, res = _post(base, "/api/oversee/dismiss",
                        {"project": "test", "run_id": "chat-1"}, token)
    assert res["ok"] is True


# --- Oversee roadmap: plan attach, card-less plans, page hooks --------------

def _seed_roadmap(plan_id, hour, interrupted=False):
    ev_mod.append_event("roadmap",
                        {"action": "set", "plan_id": plan_id,
                         "prompt": "Build the parser fix",
                         "steps": [{"text": "a", "status": "done"},
                                   {"text": "b", "status": "active"}]},
                        ts=f"2026-07-07T{hour:02d}:00:30Z", project="test")
    if interrupted:
        ev_mod.append_event("roadmap",
                            {"action": "interrupt", "plan_id": plan_id,
                             "cause": "usage_limit"},
                            ts=f"2026-07-07T{hour:02d}:03:00Z", project="test")


def test_oversee_attaches_roadmap_to_matching_run(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10)
    _seed_roadmap("run1", 10)
    status, body = _get(base, "/api/oversee?project=test", token)
    assert status == 200
    obj = json.loads(body)
    r = obj["runs"][0]
    assert r["roadmap"]["plan_id"] == "run1"
    assert r["roadmap"]["prompt"] == "Build the parser fix"
    assert r["roadmap"]["progress"] == {"done": 1, "total": 2}
    assert obj["plans"] == []      # bound to a live card -> never duplicated


def test_oversee_runs_without_a_plan_carry_no_roadmap(dashboard_server):
    base, token = dashboard_server
    _seed_loop_run("run1", 10)
    status, body = _get(base, "/api/oversee?project=test", token)
    obj = json.loads(body)
    assert "roadmap" not in obj["runs"][0]
    assert obj["plans"] == []


def test_oversee_surfaces_cardless_interrupted_plans(dashboard_server):
    # The out-of-usage marker outlives its run: a plan whose run/session has
    # left the board still arrives via the top-level plans list.
    base, token = dashboard_server
    _seed_roadmap("ghost-plan", 10, interrupted=True)
    status, body = _get(base, "/api/oversee?project=test", token)
    obj = json.loads(body)
    assert [p["plan_id"] for p in obj["plans"]] == ["ghost-plan"]
    p = obj["plans"][0]
    assert p["status"] == "interrupted" and p["cause"] == "usage_limit"
    assert p["project"] == "test"
    assert obj["runs"] == []


def test_page_has_roadmap_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert "rmPanel" in body and "rmFromCard" in body and "rmPlanCard" in body
    assert "RM_OPEN" in body and "RM_NODE_OPEN" in body
    assert ".rmstep" in body and ".rmbar" in body and ".rmnode" in body
    assert ">roadmap</button>" in body


def test_memory_view_renders_roadmap_body(dashboard_server):
    # roadmap payloads have no "text" — the Memory view must render the
    # self-describing LEDGER body, never a blank plan row.
    base, token = dashboard_server
    _seed_roadmap("ghost-plan", 10)
    status, body = _get(base, "/api/memory?project=test", token)
    rows = [r for r in json.loads(body) if r["type"] == "roadmap"]
    assert rows and "Build the parser fix" in rows[0]["text"]


# --- gzip: responses compress when the client accepts it --------------------

def _get_gzip(base, path, token=None):
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("X-Hive-Token", token)
    req.add_header("Accept-Encoding", "gzip")
    with urllib.request.urlopen(req) as r:
        return r.status, r.headers.get("Content-Encoding"), r.read()


def test_page_gzips_when_accepted(dashboard_server):
    import gzip as _gz
    base, token = dashboard_server
    status, enc, body = _get_gzip(base, "/")
    assert status == 200 and enc == "gzip"
    assert b"MIND HIVE" in _gz.decompress(body) or b"showOversee" in _gz.decompress(body)
    # a client that doesn't accept gzip still gets identity (existing _get path)
    status, plain = _get(base, "/")
    assert status == 200 and "showOversee" in plain


def test_small_json_stays_identity(dashboard_server):
    base, token = dashboard_server
    status, enc, body = _get_gzip(base, "/api/state?project=test", token)
    assert status == 200
    # STATE payload for an empty test store is tiny -> below the gzip floor
    assert enc is None or len(body) >= 1400


# --- Improvements triage marks (who acts: claude vs dismiss) -----------------

def test_improvements_carry_triage_marks(dashboard_server):
    from mindhive import events as ev, incidents as inc
    base, token = dashboard_server
    for s in ("s1", "s2"):
        ev.append_event("tool_failed",
                        {"text": "boom", "signature": "raw:tri1", "action": "x"},
                        session_id=s, project="test")
    inc.project(project="test", threshold=2)
    status, body = _get(base, "/api/improvements?project=test", token)
    assert status == 200
    (a,) = [x for x in json.loads(body)["auto"] if x["signature"] == "raw:tri1"]
    # active with no rule -> unresolved -> pending agent work
    assert a["resolution"] == "unresolved"
    assert a["triage"] == "claude"
    assert a["triage_hint"]


# --- Self-Reliant provenance labels across the four sections ------------------

def test_memory_items_carry_self_reliant_origin(dashboard_server):
    from mindhive import events as ev
    base, token = dashboard_server
    ev.append_event("learning", {"text": "Self-Reliant learning 1: PRD"},
                    session_id="research-ab12cd34",
                    tags=["self-reliant", "learning"], project="test")
    ev.append_event("decision", {"text": "plain decision"},
                    session_id="sess-1", project="test")
    status, body = _get(base, "/api/memory?project=test", token)
    items = {m["text"]: m for m in json.loads(body)}
    assert items["Self-Reliant learning 1: PRD"]["origin"] == "self-reliant"
    assert items["plain decision"]["origin"] is None


def test_sessions_carry_self_reliant_origin(dashboard_server):
    from mindhive import events as ev
    base, token = dashboard_server
    ev.append_event("experiment_result", {"text": "Self-Reliant experiment 1"},
                    session_id="research-ab12cd34", tags=["self-reliant"],
                    project="test")
    ev.append_event("decision", {"text": "one"}, session_id="sess-1",
                    project="test")
    status, body = _get(base, "/api/sessions?project=test", token)
    sessions = {s["session_id"]: s for s in json.loads(body)}
    assert sessions["research-ab12cd34"]["origin"] == "self-reliant"
    assert sessions["sess-1"]["origin"] is None


def test_improvements_manual_notes_carry_self_reliant_origin(dashboard_server):
    from mindhive import events as ev
    base, token = dashboard_server
    ev.append_event("caveat", {"text": "self-reliant caveat"},
                    session_id="research-ab12cd34", tags=["self-reliant"],
                    project="test")
    ev.append_event("caveat", {"text": "ordinary caveat"},
                    session_id="sess-1", project="test")
    status, body = _get(base, "/api/improvements?project=test", token)
    notes = {m["text"]: m for m in json.loads(body)["manual"]}
    assert notes["self-reliant caveat"]["origin"] == "self-reliant"
    assert notes["ordinary caveat"]["origin"] is None


def test_oversee_labels_self_reliant_runs(dashboard_server):
    base, token = dashboard_server
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ev_mod.append_event("experiment_result", {"text": "Self-Reliant experiment"},
                        session_id="research-ab12cd34", tags=["self-reliant"],
                        ts=ts, project="test")
    status, body = _get(base, "/api/oversee?project=test", token)
    runs = {r["run_id"]: r for r in json.loads(body)["runs"]}
    sr = runs["research-ab12cd34"]
    assert sr["origin"] == "self-reliant"
    assert sr["title"] == "Self-Reliant run ab12cd34"


def test_page_renders_self_reliant_badge_everywhere(dashboard_server):
    # one shared badge helper, wired into Memory, Improvements, Oversee and
    # the Sessions cards fragment (taskview CARDS_JS is injected into the page)
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert status == 200
    assert ".pill.sr" in body                     # badge style
    assert body.count("srPill(") >= 5             # helper + 4 call sites
    assert "srPill(s.origin)" in body             # sessions fragment wired


def test_research_sources_route_reports_allowlist(dashboard_server, monkeypatch):
    monkeypatch.delenv("MINDHIVE_RESEARCH_WEB", raising=False)
    from mindhive import research
    base, token = dashboard_server
    status, body = _get(base, "/api/research/sources", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["ok"] and obj["enabled"]
    assert obj["domains"] == research.TRUSTED_SOURCE_DOMAINS


def test_workspace_auth_verify_route_registered(dashboard_server, monkeypatch):
    from mindhive import workspace
    base, token = dashboard_server
    monkeypatch.setattr(workspace, "verify_auth",
                        lambda name="default", force=False:
                        {"profile": name, "state": "ok", "detail": "connected"})
    status, res = _post(base, "/api/workspace/auth_verify",
                        {"name": "default"}, token)
    assert status == 200 and res["ok"] and res["state"] == "ok"
    # machine-level check: exempt from the HIVE MIND read-only guard
    status, res = _post(base, "/api/workspace/auth_verify",
                        {"name": "default", "project": "*"}, token)
    assert status == 200 and res["ok"] and res["state"] == "ok"


# --- prompt enrichment (Oversee panel API) ------------------------------------

def _enrich_inventory():
    return {"changed": True, "signature": "x", "inventory": {
        "plugins": [{"name": "p", "skills": [
            {"name": "planner",
             "trigger": "Create structured plans for multi-step tasks and "
                        "break down goal requirements"}]}],
        "local_skills": []}}


def test_enrich_settings_default_on(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/api/enrich/settings?project=test", token)
    assert status == 200
    obj = json.loads(body)
    assert obj["enabled"] is True and obj["source"] == "default"


def test_enrich_toggle_round_trip_and_audits(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/enrich/toggle",
                        {"project": "test", "enabled": False}, token)
    assert status == 200 and obj["ok"] is True
    assert obj["settings"]["enabled"] is False
    status, body = _get(base, "/api/enrich/settings?project=test", token)
    assert json.loads(body)["enabled"] is False
    toggles = [e for e in ev_mod.read_all("test")
               if e.type == "enrich_toggled"]
    assert len(toggles) == 1 and toggles[0].payload["enabled"] is False


def test_enrich_toggle_requires_boolean(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/enrich/toggle",
                        {"project": "test", "enabled": "yes"}, token)
    assert status == 200 and obj["ok"] is False


def test_enrich_toggle_rejected_for_hivemind(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/enrich/toggle",
                        {"project": "*", "enabled": False}, token)
    assert status == 200 and obj["ok"] is False
    assert "read-only" in obj["error"]


def test_enrich_preview_returns_package(dashboard_server, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory",
                        lambda claude_dir=None: _enrich_inventory())
    ev_mod.append_event("file_touched", {"path": "src/mindhive/dashboard.py"},
                        project="test")
    base, token = dashboard_server
    status, obj = _post(base, "/api/enrich/preview",
                        {"project": "test",
                         "prompt": "fix the dashboard poll loop"}, token)
    assert status == 200 and obj["ok"] is True
    res = obj["result"]
    assert res["enabled"] is True
    assert [s["path"] for s in res["sources"]] == ["src/mindhive/dashboard.py"]
    assert res["sop"] and res["questions"]
    assert "## Task" in res["enriched_prompt"]
    # a plain preview never spams the ledger
    assert not [e for e in ev_mod.read_all("test")
                if e.type == "prompt_enriched"]


def test_enrich_preview_record_appends_event(dashboard_server, monkeypatch):
    from mindhive import plugins
    monkeypatch.setattr(plugins, "live_inventory",
                        lambda claude_dir=None: _enrich_inventory())
    base, token = dashboard_server
    status, obj = _post(base, "/api/enrich/preview",
                        {"project": "test", "record": True,
                         "prompt": "fix the dashboard poll loop"}, token)
    assert obj["ok"] is True
    recs = [e for e in ev_mod.read_all("test") if e.type == "prompt_enriched"]
    assert len(recs) == 1 and recs[0].payload["sop_total"] == 6


def test_enrich_preview_requires_prompt(dashboard_server):
    base, token = dashboard_server
    status, obj = _post(base, "/api/enrich/preview", {"project": "test"},
                        token)
    assert status == 200 and obj["ok"] is False


def test_page_has_enrich_hooks(dashboard_server):
    base, token = dashboard_server
    status, body = _get(base, "/")
    assert "enPanel" in body and "Enrichment" in body
    assert "/api/enrich/settings" in body and "/api/enrich/preview" in body
    assert "/api/enrich/toggle" in body
    # HIVE MIND stub: the panel must never silently vanish under "*"
    assert "pick a concrete project in the header" in body
