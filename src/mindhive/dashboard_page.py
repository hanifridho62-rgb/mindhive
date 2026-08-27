# src/mindhive/dashboard_page.py
"""Presentation layer for the MIND HIVE dashboard: the page's shell.

The single-page HTML/CSS/JS control panel, isolated from ``dashboard.py`` so the
HTTP handler and data logic aren't buried under the embedded markup. Pure
presentation: takes a session token, returns a self-contained HTML document
string.

## What this file is, after the split

The SHELL only — the ``<head>``, the header, the nav, the panel, and the splice
chain that assembles everything else into them. The stylesheet and the ~2050
lines of page JS live in sibling modules, one per concern, because this file was
2396 lines: more than twice the project's 1000-line cap, which this docstring
used to record as a debt both this file and ``dashboard.py`` carried. Both are
now under it, and ``tests/test_file_size_cap.py`` fails the suite if either grows
back.

Spliced in, in the order the browser needs them:

* ``dashboard_css.CSS`` — this page's own stylesheet, including the
  reduced-motion guard for every animation it declares.
* ``dashboard_js_core.script()`` — element handles, the real-time engine, the
  render/paint choke points, project scoping, Overview. Everything else calls
  into it.
* ``dashboard_js_improve.JS`` — Memory and Improvements.
* ``dashboard_js_oversee.JS`` — Sessions and Oversee, plus prompt enrichment.
* ``dashboard_js_views.JS`` — the thin sections: Log, SOP, State, Briefing,
  Recall, Graph, Workspace, Append.
* ``dashboard_js_skills.JS`` — the Skill & Plugin panel.
* ``dashboard_js_actions.JS`` — the six long operations as polled jobs, the cat
  board's poll, and the boot sequence. LAST, and it has to be: those are the only
  statements on the page that execute at parse time instead of declaring a
  function.

The fragments are spliced FIRST in the chain, before ``__TOKEN__`` and the
``theme``/``cat_board``/``taskview`` placeholders, because those placeholders
live inside the fragments' text. Any other order would leave them unreplaced.

**The page is ONE ``<script>``.** The JS modules are a division for readers and
for the cap; they buy no isolation. Every fragment shares one scope, function
declarations hoist across the seams, and a function in one is called from
another — ``onProjChange`` (core) calls ``showMemory`` (improve), ``openSession``
(oversee) calls ``showMemory``. Do not mistake the file boundaries for module
boundaries.

## Two sibling dependencies, deliberately

``cat_board`` supplies the MindNetwork faculty board that this page and the swarm
page both render; the alternative was a second copy of the sprites and labels in
each page — two homes for one legend, drifting invisibly. ``theme`` supplies the
colour palette this document's CSS is written against: the same argument, for the
152 hex literals that used to be repeats of a value chosen somewhere else. Both
are themselves pure presentation, so this stays a presentation-only module."""
import json

from . import cat_board, theme
from . import dashboard_css
from . import dashboard_js_actions
from . import dashboard_js_core
from . import dashboard_js_improve
from . import dashboard_js_oversee
from . import dashboard_js_skills
from . import dashboard_js_views

# The stale-paint discard decision, held as one named constant instead of being
# typed straight into the page string below. Two consumers need the SAME bytes:
# the browser, which gets it inlined at `/*__PAINT_GUARD__*/`, and the tier-1
# node unit test (`tests/node/paint_guard.test.mjs`, run by
# `tests/test_paint_guard_node.py` under `pytest -m node`), which imports it and
# executes it. A test that retyped the comparison would keep passing after the
# page's own copy changed — the exact vacuous fixture this repo's ledger warns
# about. `tests/test_dashboard.py` asserts the seam itself, in the DEFAULT suite,
# so "the page still ships this text" is never taken on trust either.
#
# Pure by necessity, not by taste: it reads no globals, touches no DOM and does
# no I/O, which is the only reason node can import it outside a browser at all.
# `currentLiveFn` is a parameter rather than a closed-over `liveFn` for the same
# reason.
PAINT_GUARD_JS = """\
  // shouldPaint answers one question, asked at paint time by every renderer
  // that registers itself as a liveFn: is the render about to write to the
  // panel still the render the user is waiting for?
  //
  // Identity, not equality, and deliberately so. `liveFn` IS the record of
  // which section the user picked, because showOverview()/showOversee()/… each
  // assign it SYNCHRONOUSLY before starting their fetch. A render that started
  // earlier therefore compares unequal from the instant another section is
  // clicked — no generation counter, no token threaded through showHTML(),
  // which takes a string and cannot know who sent it.
  //
  // What it does NOT do, said out loud so nothing is expected of it that it
  // does not give: two in-flight renders of the SAME section both compare
  // equal and both paint. Within-section ordering is a different problem with
  // its own fix (PLUG_GEN). This one is cross-section only.
  //
  // SCOPE is not this function's job either, and that was the reachable
  // instance of the gap above: `onProjChange` switches PROJECT and calls
  // showOverview(), so a user already on Overview has liveFn === renderOverview
  // on both sides of the switch and the old project's rows compared equal.
  // Closed elsewhere, at `apiGet` — one choke point every renderer and both
  // pollers already pass through — rather than by teaching this comparison
  // about globals it is deliberately pure of.
  function shouldPaint(currentLiveFn, renderFn){
    return currentLiveFn === renderFn;
  }"""


def render_page(token: str, tasks_css: str = "", tasks_js: str = "") -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MIND HIVE</title>
<style>
/*__DASH_CSS__*/
</style></head>
<body>
<!-- First in the DOM because that is the only place a skip link works: the rail
     is 26 buttons and it comes before the panel, so without this the keyboard
     pays 27 tabs to reach the first control inside the section it just chose.
     It is a plain anchor to `#panel`, which carries `tabindex="-1"` so the jump
     MOVES FOCUS rather than only scrolling -- an anchor to a non-focusable
     element leaves the keyboard exactly where it was, which is the standard way
     this feature ships broken. `#panel` and not `#view`: `showSkeleton` refuses
     to paint while the focus is inside `#view`, so focusing the pane itself
     would have quietly turned the loading placeholder off. -->
<a id="skip" href="#panel">skip to content</a>
<!-- Where the reader is inside a long section. Fixed under the header, scaled
     rather than resized, and outside <main> for the same reason #totop is: it
     belongs to the viewport, not to the section. -->
<div id="prog" aria-hidden="true"></div>
<header>
  <h1>MIND HIVE</h1>
  <span id="busy" class="dot"></span>
  <span id="status">ready</span>
  <label id="projwrap" title="Which project's memory to show">project
    <select id="proj" onchange="onProjChange()"></select></label>
  <span id="live" title="Real-time updates — click to pause/resume" onclick="toggleLive()"><span class="blip">&#9679;</span> live</span>
  <span id="synced"></span>
  <!--__CAT_HTML__-->
</header>
<main>
  <nav aria-label="sections">
    <!-- ONE indicator that travels, rather than sixteen that light up. The
         active button carries the STATE (`.on` for the eye, `aria-current` for
         assistive tech); this bar is the part that MOVES, a single absolutely
         positioned element translated to the active button's offset. Switching
         section therefore animates one compositor property on one node instead
         of repainting two rows of buttons. -->
    <span id="navdot" aria-hidden="true"></span>
    <button data-sec="overview" onclick="showOverview()">Overview</button>
    <div class="group">Work</div>
    <button data-sec="oversee" onclick="showOversee()">Oversee</button>
    <div class="group">Memory</div>
    <button data-sec="memory" onclick="showMemory()">Memory</button>
    <button data-sec="sessions" onclick="showSessions()">Sessions</button>
    <button data-sec="improvements" onclick="showImprovements(true)">Improvements</button>
    <button data-sec="log" onclick="showLog()">Log</button>
    <button data-sec="sops" onclick="showSops()">SOP</button>
    <div class="group">View</div>
    <button data-sec="state" onclick="showState()">State</button>
    <button data-sec="briefing" onclick="showBriefing()">Briefing</button>
    <button data-sec="recall" onclick="showRecall()">Recall</button>
    <button data-sec="graph" onclick="showGraph()">Graph</button>
    <button data-sec="skills" onclick="showSkillPanel()">Skill &amp; Plugin</button>
    <div class="group">Workspace</div>
    <button data-sec="ws-parallel" onclick="showWorkspace('parallel')">MindParallel</button>
    <button data-sec="ws-swarm" onclick="showWorkspace('swarm')">MindSwarm</button>
    <button data-sec="ws-research" onclick="showWorkspace('research')">Self-Reliant</button>
    <div class="group">Actions</div>
    <button data-sec="append" onclick="showAppend()">Append memory</button>
    <button onclick="doExport()">Export handoff.md</button>
    <button onclick="doAction('/api/project','Project (rebuild)')">Project</button>
    <button onclick="doAction('/api/sync','Sync')">Sync</button>
    <button onclick="runJob('/api/harvest','Catching up')">Catch up</button>
    <button onclick="doAction('/api/verify','Verify chain', 'GET')">Verify</button>
    <div class="group">Heavy</div>
    <button onclick="runEmbed()">Embed</button>
    <button onclick="confirmJob('/api/plugins/scan','Plugins scan')">Plugins scan</button>
    <button onclick="runReflect()">Reflect (learn from failures)</button>
    <button onclick="runResolve()">Resolve red (reroute failed rules)</button>
    <button onclick="confirmJob('/api/selftest','Self-test')">Self-test</button>
  </nav>
  <!-- The screen-reader channel. Until the nav stopped being disabled during a
       fetch, `disabled` on every nav button WAS the only signal assistive tech
       got that the app was busy — a side effect of a lock, not a statement. It
       is now said properly: aria-busy on the panel while it loads, and this
       polite region for the two facts a sighted user reads off the screen —
       which section is coming up, and how the operation ended.

       OUTSIDE the panel, deliberately. It shipped inside it, and inside is
       where it is silent: `aria-busy="true"` tells assistive tech to defer
       updates within that subtree, and run() raises aria-busy before the
       opening announce() and lowers it only in its finally — after the "ready"
       announcement. Every announcement this channel makes was made while its
       own container was marked busy, so the whole channel could be coalesced
       away. A live region has to sit outside the thing whose busyness it
       reports. -->
  <div id="ann" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>
  <!-- `tabindex="-1"` is what makes the skip link above move the focus and not
       just the scroll position. -1 keeps it out of the tab ORDER: it is a
       target, never a stop. -->
  <section class="panel" id="panel" aria-busy="false" tabindex="-1">
    <div id="controls"></div>
    <div id="view"></div>
    <pre id="out" class="hidden"></pre>
    <iframe id="graph" class="hidden"></iframe>
  </section>
</main>
<!-- Outside <main> because it belongs to the viewport, not to the section: it is
     position:fixed, and nesting it inside the scrolling content would put a
     fixed element inside a grid item for no reason. Ships in the markup rather
     than being injected on first scroll so the sheet, not a script, decides
     whether it is visible. -->
<button id="totop" aria-label="back to top" title="back to top">&uarr; top</button>
<script>
/*__JS_CORE__*/
/*__JS_IMPROVE__*/
/*__JS_OVERSEE__*/
/*__JS_VIEWS__*/
/*__JS_SKILLS__*/
/*__JS_ACTIONS__*/
</script>
</body></html>""" \
    .replace("/*__DASH_CSS__*/", dashboard_css.CSS) \
    .replace("/*__JS_CORE__*/", dashboard_js_core.script()) \
    .replace("/*__JS_IMPROVE__*/", dashboard_js_improve.JS) \
    .replace("/*__JS_OVERSEE__*/", dashboard_js_oversee.JS) \
    .replace("/*__JS_VIEWS__*/", dashboard_js_views.JS) \
    .replace("/*__JS_SKILLS__*/", dashboard_js_skills.JS) \
    .replace("/*__JS_ACTIONS__*/", dashboard_js_actions.JS) \
    .replace("/*__PALETTE__*/", theme.PALETTE_CSS) \
    .replace("/*__PAINT_GUARD__*/", PAINT_GUARD_JS) \
    .replace("/*__CAT_CSS__*/", cat_board.CSS) \
    .replace("<!--__CAT_HTML__-->", cat_board.HTML) \
    .replace("/*__CAT_JS__*/", cat_board.script()) \
    .replace("__TOKEN__", json.dumps(token)) \
    .replace("/*__TASKS_CSS__*/", tasks_css) \
    .replace("//__TASKS_JS__", tasks_js)
