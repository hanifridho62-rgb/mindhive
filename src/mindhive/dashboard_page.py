# src/mindhive/dashboard_page.py
"""Presentation layer for the MIND HIVE dashboard: the page's shell.

The single-page HTML/CSS/JS control panel, isolated from ``dashboard.py`` so the
HTTP handler and data logic aren't buried under the embedded markup. Pure
presentation: takes a session token, returns a self-contained HTML document
string.

## What this file is, after the split

The SHELL only — the ``<head>``, the header, the rail (the sections ``<nav>``
and the ``#actions`` group under it), the panel, and the splice chain that
assembles everything else into them. The stylesheet and the ~2050
lines of page JS live in sibling modules, one per concern, because this file was
2396 lines: more than twice the project's 1000-line cap, which this docstring
used to record as a debt both this file and ``dashboard.py`` carried. Both are
now under it, and ``tests/test_file_size_cap.py`` fails the suite if either grows
back.

Spliced in, in the order the browser needs them:

* ``static/dashboard.js`` — FIRST in the script, and from a FILE rather than
  a module: ``window.MindHive``, the paint-signature helper and the latched
  poller (blueprint Pillar 4), read once at import through
  ``importlib.resources`` (``DASHBOARD_JS``) and served on its own at
  ``/static/dashboard.js`` by ``dashboard_get_v1``. It declares nothing
  outside its own closure and touches no DOM while loading, which is what
  lets it go ahead of everything.
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
  board's poll, and the boot sequence. LAST, and it has to be: apart from the
  static file's closure, which only hangs a namespace on ``window``, those are
  the only statements on the page that execute at parse time instead of
  declaring a function.

The fragments are spliced FIRST in the chain, before ``__TOKEN__`` and the
``theme``/``cat_board``/``taskview`` placeholders, because those placeholders
live inside the fragments' text. Any other order would leave them unreplaced.
``static/dashboard.js`` is spliced LAST, after every placeholder is gone, so
it arrives byte for byte: nothing in a file that is also served verbatim is
ever read as a placeholder.

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
from importlib import resources

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


# The MindSwarm nav entry, lifted out of the shell so `dashboard.mindswarm_
# enabled` can leave it out. Default-on, so every `render_page(` caller that
# predates the parameter renders exactly what it rendered before it existed.
#
# The `title=` is where the soft deprecation actually reaches a reader. An
# operator who works from the dashboard never sees a CLI notice, so a
# deprecation that lives only in `mindnet/orchestrator.py` is a deprecation
# they are never told about — and the tooltip says "kept", not "going away",
# because that is the decision that was made.
MINDSWARM_NAV_BUTTON = (
    """<button class="nav-link" data-sec="ws-swarm" onclick="showWorkspace('swarm')" """
    """title="Kept and still supported. New work goes through the mission """
    """pipeline; start the dashboard with MINDHIVE_MINDSWARM=0 to hide """
    """this row.">MindSwarm</button>""")


# What stands in the row's place when it is hidden. The tooltip naming the
# variable lives INSIDE the button, so hiding the row also hides the only
# in-product instruction for bringing it back -- an adversarial review of
# this change found that, and it is a real wart in a change whose whole
# premise is reversibility. An HTML comment is not discoverable by eye and
# does not pretend to be; it is what a reader who opens the page source
# finds, and docs/session-legend.md carries the durable answer.
SWARM_HIDDEN_NOTE = (
    "<!-- MindSwarm nav row hidden by MINDHIVE_MINDSWARM=0. Restart the "
    "dashboard with that variable unset, or set to 1, to restore this "
    "row -- this process read its environment at launch and cannot see "
    "a change made in a shell after it. Nothing was removed: "
    "/api/workspace/swarm still answers. -->")


# `static/dashboard.js` (blueprint Pillar 4), read ONCE, at import, and through
# `importlib.resources` rather than a path beside `__file__`: the frozen build
# has no source tree to walk -- the rule `pgmirror` set for its migrations, and
# the reason `build/build_app.py` collects the package's data. The page inlines
# it at the top of its one script block; `dashboard_get_v1` serves the same
# string at `/static/dashboard.js`.
DASHBOARD_JS = (resources.files("mindhive") / "static" / "dashboard.js").read_text(
    encoding="utf-8")


def render_page(token: str, tasks_css: str = "", tasks_js: str = "",
                swarm: bool = True, extra_js: str = "") -> str:
    """The served page.

    *extra_js* is spliced after every built-in fragment, so `u()`, `TOKEN` and
    the rest of the page's module scope are already defined when it runs. It
    defaults to `""` and `hive dashboard` never passes it: the one caller is
    `hive ui`, whose heartbeat has no business in a panel meant to stay up."""
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
  <!-- The rail: two landmarks in one sticky column. `nav` holds the sixteen
       buttons that take you somewhere, each with a `data-sec`; `#actions` holds
       the ten that DO something (blueprint Bug 13). Assistive tech lists
       "sections" and "actions" as two things, because they are. -->
  <div id="rail">
  <nav aria-label="sections">
    <!-- ONE indicator that travels, rather than sixteen that light up. The
         active button carries the STATE (`.on` for the eye, `aria-current` for
         assistive tech); this bar is the part that MOVES, a single absolutely
         positioned element translated to the active button's offset. Switching
         section therefore animates one compositor property on one node instead
         of repainting two rows of buttons. -->
    <span id="navdot" aria-hidden="true"></span>
    <button class="nav-link" data-sec="overview" onclick="showOverview()">Overview</button>
    <div class="group">Work</div>
    <button class="nav-link" data-sec="oversee" onclick="showOversee()">Oversee</button>
    <div class="group">Memory</div>
    <button class="nav-link" data-sec="memory" onclick="showMemory()">Memory</button>
    <button class="nav-link" data-sec="sessions" onclick="showSessions()">Sessions</button>
    <button class="nav-link" data-sec="improvements" onclick="showImprovements(true)">Improvements</button>
    <button class="nav-link" data-sec="log" onclick="showLog()">Log</button>
    <button class="nav-link" data-sec="sops" onclick="showSops()">SOP</button>
    <!-- A section, not a command -- it opens a form -- so it lives with the
         Memory sections it writes to, not with the actions it used to sit
         beside. -->
    <button class="nav-link" data-sec="append" onclick="showAppend()">Append memory</button>
    <div class="group">View</div>
    <button class="nav-link" data-sec="state" onclick="showState()">State</button>
    <button class="nav-link" data-sec="briefing" onclick="showBriefing()">Briefing</button>
    <button class="nav-link" data-sec="recall" onclick="showRecall()">Recall</button>
    <button class="nav-link" data-sec="graph" onclick="showGraph()">Graph</button>
    <button class="nav-link" data-sec="skills" onclick="showSkillPanel()">Skill &amp; Plugin</button>
    <div class="group">Workspace</div>
    <button class="nav-link" data-sec="ws-parallel" onclick="showWorkspace('parallel')">MindParallel</button>
    <!--__SWARM_NAV__-->
    <button class="nav-link" data-sec="ws-research" onclick="showWorkspace('research')">Self-Reliant</button>
  </nav>
  <div id="actions" role="group" aria-label="actions">
    <div class="group">Actions</div>
    <button class="nav-action" onclick="doExport()">Export handoff.md</button>
    <button class="nav-action" onclick="doAction('/api/project','Project (rebuild)')">Project</button>
    <button class="nav-action" onclick="doAction('/api/sync','Sync')">Sync</button>
    <button class="nav-action" onclick="runJob('/api/harvest','Catching up')">Catch up</button>
    <button class="nav-action" onclick="doAction('/api/verify','Verify chain', 'GET')">Verify</button>
    <div class="group">Heavy</div>
    <button class="nav-action" onclick="runEmbed()">Embed</button>
    <button class="nav-action" onclick="confirmJob('/api/plugins/scan','Plugins scan')">Plugins scan</button>
    <button class="nav-action" onclick="runReflect()">Reflect (learn from failures)</button>
    <button class="nav-action" onclick="runResolve()">Resolve red (reroute failed rules)</button>
    <button class="nav-action" onclick="confirmJob('/api/selftest','Self-test')">Self-test</button>
  </div>
  </div>
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
/*__STATIC_JS__*/
/*__JS_CORE__*/
/*__JS_IMPROVE__*/
/*__JS_OVERSEE__*/
/*__JS_VIEWS__*/
/*__JS_SKILLS__*/
/*__JS_ACTIONS__*/
/*__EXTRA_JS__*/
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
    .replace("<!--__SWARM_NAV__-->",
             MINDSWARM_NAV_BUTTON if swarm else SWARM_HIDDEN_NOTE) \
    .replace("__TOKEN__", json.dumps(token)) \
    .replace("/*__TASKS_CSS__*/", tasks_css) \
    .replace("//__TASKS_JS__", tasks_js) \
    .replace("/*__EXTRA_JS__*/", extra_js) \
    .replace("/*__STATIC_JS__*/", DASHBOARD_JS)
