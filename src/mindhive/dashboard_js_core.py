# SPDX-License-Identifier: Apache-2.0
# src/mindhive/dashboard_js_core.py
"""The page's runtime: element handles, the real-time engine, the render/paint
choke points, project scoping and the Overview section.

Everything in every other JS module calls into this one — `esc`, `u`,
`apiGet`/`apiPost`, `run`, `showHTML`, `liveFn` — and nothing here calls
back out except `showOverview`, which `onProjChange` needs. That asymmetry
is why this is the first fragment concatenated into the page and the one a
reader should open first.

Extracted verbatim from ``dashboard_page.py`` when that file reached 2396 lines,
more than twice the project's 1000-line cap. Pure text: this module holds one
string constant and imports nothing. ``dashboard_page.render_page`` splices it
back in at ``/*__JS_CORE__*/``, in source order, so the served page is
byte-identical to the one the single file produced.

Not independently loadable in a browser: the whole page is ONE ``<script>``, so
every fragment shares one scope and function declarations hoist across the seams.
The split is for readers and for the cap, and it buys no isolation — a function
here may be called from any other fragment, and is.
"""
# Every stamp in this ledger is UTC, and every reader of this dashboard is
# in Jakarta. Printing one without the other means doing the arithmetic in
# your head on every glance -- and getting it wrong at the two moments it
# matters most, because 20:00Z and 03:00 WIB are the same instant on
# DIFFERENT DAYS.
#
# Its own constant, not a few lines inside the page script, for the same
# reason `PAINT_GUARD_JS` is: a pure function of a string can be executed by
# a test, and `tests/test_dashboard_clock_node.py` runs exactly these bytes
# under node. A copy retyped in a fixture would keep passing after the
# page's own copy changed.
CLOCK_JS = """\
  // Asia/Jakarta is UTC+7 and has no daylight saving -- it has had none
  // since 1964 -- so the offset is arithmetic rather than a timezone
  // database lookup. `Intl` would work and would also make the page's
  // output depend on which ICU data the browser shipped with; a fixed
  // offset is the same answer everywhere, including under node.
  const WIB_OFFSET_MIN = 7 * 60;
  function fmtTs(iso){
    const raw = String(iso == null ? '' : iso);
    if (!raw) return '';
    const t = Date.parse(raw);
    // Unparseable is shown VERBATIM, never blanked and never thrown on. A
    // stamp this page cannot read is still evidence, and swallowing it
    // would hide a malformed row instead of surfacing it.
    if (!isFinite(t)) return raw;
    const utc = new Date(t), wib = new Date(t + WIB_OFFSET_MIN * 60000);
    const p = n => String(n).padStart(2, '0');
    const day = x => x.getUTCFullYear() + '-' + p(x.getUTCMonth() + 1)
                     + '-' + p(x.getUTCDate());
    const hm = x => p(x.getUTCHours()) + ':' + p(x.getUTCMinutes());
    // The WIB DATE is printed only when it differs. Suppressing it always
    // would render 20:00Z as a bare '03:00 WIB' and read as seven hours
    // ago rather than tomorrow morning; printing it always would double
    // the length of every stamp on the page for no information.
    const rolled = day(utc) !== day(wib);
    return day(utc) + ' ' + hm(utc) + 'Z \u00b7 '
      + (rolled ? day(wib) + ' ' : '') + hm(wib) + ' WIB';
  }
  function fmtHm(iso){
    // The clock-face form: the same two zones, no date. For stamps that
    // share a line with other text and whose date is already established by
    // the group they sit in -- the skill trail's marker is 5 characters wide
    // by design. Same totality rule as fmtTs: unparseable comes back
    // verbatim, so a malformed row is surfaced rather than blanked.
    const raw = String(iso == null ? '' : iso);
    if (!raw) return '';
    const t = Date.parse(raw);
    if (!isFinite(t)) return raw;
    const p = n => String(n).padStart(2, '0');
    const hm = x => p(x.getUTCHours()) + ':' + p(x.getUTCMinutes());
    return hm(new Date(t)) + 'Z \u00b7 '
      + hm(new Date(t + WIB_OFFSET_MIN * 60000)) + ' WIB';
  }
"""


JS = """\
  const TOKEN = __TOKEN__;
  const out = document.getElementById('out');
  const view = document.getElementById('view');
  const graph = document.getElementById('graph');
  const controls = document.getElementById('controls');
  const busyDot = document.getElementById('busy');
  const statusEl = document.getElementById('status');
  const panelEl = document.getElementById('panel');
  const announcer = document.getElementById('ann');
  const liveEl = document.getElementById('live');
  const syncedEl = document.getElementById('synced');
  const headEl = document.querySelector('header');
  const progEl = document.getElementById('prog');
  // The header's height, published to the stylesheet.
  //
  // Four things now have to start exactly below the header -- the sticky rail,
  // the sticky section heading, the progress bar, and `main`'s minimum height --
  // and the header is not a fixed size: it is 52px on the first frame and 68px
  // once the faculty and session strips have arrived, which is a second or two
  // later on a real store. A constant in the sheet would therefore be wrong for
  // the first seconds of every session and wrong again for anyone whose project
  // name wraps, and being wrong here means either a 16px gap under the header or
  // a rail sliding under it.
  //
  // A CSS custom property rather than four inline styles: the sheet keeps saying
  // what it wants (`top: var(--headh)`), the page only supplies the number, and
  // a rule added later inherits the answer without being wired up.
  function measureHeader(){
    if (!headEl) return;
    document.documentElement.style.setProperty(
      '--headh', Math.round(headEl.getBoundingClientRect().height) + 'px');
  }
  measureHeader();
  // The strips arrive from a poll, not from a resize, so `resize` alone would
  // never fire for the change that actually matters. ResizeObserver watches the
  // element itself and answers both.
  if (headEl && window.ResizeObserver) new ResizeObserver(measureHeader).observe(headEl);
  // Every animation on this page is dropped in CSS under prefers-reduced-motion;
  // a scripted smooth scroll is the one piece of motion a stylesheet can't reach.
  //
  // A FUNCTION, not a const read at load. The CSS media query re-evaluates the
  // moment the OS setting changes, so a reader who turns reduce-motion on with
  // the board already open got a page where every animation had stopped and the
  // scripted scrolls still glided — the stylesheet had followed them and the
  // script had not. This board stays open for days; a preference it only reads
  // once is a preference it mostly ignores. (#totop's click handler below was
  // already written this way and says so; this is the older call site catching
  // up, so the page has one answer to the question instead of two.)
  function reduceMotion(){
    return !!(window.matchMedia
              && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // --- real-time engine ---
  // liveFn is the silent re-renderer for the current view (null = view isn't
  // auto-refreshable). A timer re-runs it on an interval, and actions that
  // mutate memory call refreshLive() so changes appear without a manual click.
  //
  // It is ALSO the answer to "which section does the user want right now", and
  // that second job is what stops a section nobody picked from painting itself.
  // Every renderer below re-reads liveFn after its await and returns — no cache
  // write, no paint, no throw — when it no longer matches itself. Reported as:
  // "I click Improvements, it moves there briefly, then goes back to Oversee."
  // The path is refreshLive(), which calls liveFn() DIRECTLY and never goes
  // through run(): a poll of Oversee started 2 s ago is still in flight when
  // Overview is clicked, /api/overview happens to answer first, and the poller's
  // showHTML() then lands last — painting Oversee under a "ready" status, with
  // no error anywhere. A generation counter stamped inside run() cannot see
  // those poller renders at all; function identity can, because assigning
  // liveFn IS how a section gets picked.
  let liveFn = null;
/*__PAINT_GUARD__*/
  let liveOn = true;
  // The boot sequence lands on the overview only if the user hasn't already
  // picked a view. loadProjects() takes seconds on a large ledger and the nav
  // stays clickable throughout (setBusy never fires for it), so a click in that
  // window used to be overwritten by boot's own showOverview() with no error.
  let viewPicked = false;
  const LIVE_MS = 2500;
  function pad2(n){ return (n<10?'0':'')+n; }
  function markSynced(){
    const d = new Date();
    syncedEl.textContent = 'updated ' + pad2(d.getHours())+':'+pad2(d.getMinutes())+':'+pad2(d.getSeconds());
  }
  function toggleLive(){
    liveOn = !liveOn;
    liveEl.classList.toggle('paused', !liveOn);
    liveEl.innerHTML = (liveOn ? '<span class="blip">&#9679;</span> live' : '&#9679; paused');
    // The tooltip follows the same precedence as the colour: paused is what the
    // user asked for, so it replaces a health message about a poll that is no
    // longer running. The strike count is deliberately NOT reset — a server
    // that was dead before the pause is probably dead after it, and resuming
    // runs a tick immediately, which is what re-establishes the truth.
    liveEl.title = liveTitle();
    if (liveOn) refreshLive();
  }
  // silent refresh: no busy spinner, no status text, errors swallowed so a
  // transient blip never disrupts what the user is reading — but counted, so a
  // sustained outage stops passing for health (see markLiveHealth).
  //
  // The in-flight guard is load-bearing, not defensive. Measured 2026-08-10 against
  // the 31k-event store: the section endpoints cost 1.2-1.9 s of pure-Python fold, so
  // a 2500 ms timer overlaps its own previous request the moment anything else
  // competes for the interpreter. Overlap does not divide that work, it queues it —
  // four concurrent requests measured 4.99 s against 5.3 s run serially, i.e. the GIL
  // serialises the folds — and a user's click lands at the back of the queue. Skipping
  // a tick costs one stale interval; not skipping cost 6-12 s section loads.
  let liveInFlight = false;
  // The one case `finally` cannot cover: a fetch that never settles. `fetch`
  // has no default timeout, so a wedged server thread — the exact failure the
  // server-side locks in this change are about — leaves the latch held forever,
  // every later tick returns at the line below, and the chip goes on saying
  // "live" while nothing has refreshed for minutes. So record WHEN the latch
  // was taken and let a tick that finds it held too long report the failure
  // through the same strike counter a rejected fetch uses.
  //
  // It deliberately does NOT clear the latch or abort the request. Against a
  // server that is merely slow, either would replace one stuck request with a
  // fresh one every 2.5 s — piling work onto the queue is what the latch exists
  // to prevent. Skipping stays the behaviour; only the lie stops.
  let liveSince = 0;
  const LIVE_STUCK_MS = LIVE_MS * 2;
  // Swallowing every failure was right for ONE blip and wrong for a dead server:
  // the chip went on claiming "live" while nothing had refreshed for minutes.
  // So count CONSECUTIVE failures — three in a row (7.5 s) is past any transient
  // — and let a single success reset the count, so a flaky endpoint that
  // recovers never shows the state at all. The view itself is still left alone:
  // the last good render stays on screen, only the chip stops lying about it.
  const LIVE_STALE_STRIKES = 3;
  let liveFails = 0;
  // One place decides what the chip claims, because two places disagreed: the
  // health message outlived the pause that made it untrue.
  function liveTitle(){
    if (!liveOn) return 'Paused \\u2014 click to resume';
    return liveFails >= LIVE_STALE_STRIKES
      ? 'Not refreshing \\u2014 '+liveFails+' updates failed in a row. Click to pause/resume.'
      : 'Real-time updates \\u2014 click to pause/resume';
  }
  function markLiveHealth(ok){
    liveFails = ok ? 0 : liveFails + 1;
    liveEl.classList.toggle('stale', liveFails >= LIVE_STALE_STRIKES);
    liveEl.title = liveTitle();
  }
  async function refreshLive(){
    if (!liveOn || !liveFn) return;
    if (document.hidden) return;
    if (busyDot.classList.contains('busy')) return;  // don't fight a manual op
    if (liveInFlight) {                              // skip this tick, never queue it
      if (Date.now() - liveSince > LIVE_STUCK_MS) markLiveHealth(false);
      return;
    }
    liveInFlight = true;
    liveSince = Date.now();
    // Capture the section this tick is FOR. A discarded render returns
    // `undefined` exactly like a successful one — shouldPaint just returns
    // early — so without this the poller stamped "updated HH:MM:SS" and reset
    // the failure count for a paint that never happened. That is the same lie
    // the stale chip exists to stop, arriving through the other door: the one
    // widget a user checks to decide whether the view in front of them is
    // current. Re-reading `liveFn` after the await IS the discard test, and it
    // is the same comparison every renderer makes at its own paint site.
    const fn = liveFn;
    try {
      await fn();
      if (liveFn === fn) { markSynced(); markLiveHealth(true); }
    }
    // A scope change is not an outage. The tick was discarded because the user
    // moved the picker, and counting it as a failed update would walk the chip
    // toward "not refreshing" for something the user did on purpose.
    catch(e) { if (!e || e.name !== 'StaleScope') markLiveHealth(false); }
    finally { liveInFlight = false; }
  }
  setInterval(refreshLive, LIVE_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshLive(); });

/*__CLOCK__*/
  function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function u(path){ return path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN); }
  // Busy state WITHOUT locking the nav. Disabling every nav button was the old
  // way to stop a second section landing mid-fetch — but it never covered the
  // poller (refreshLive bypasses setBusy entirely), it made the nav feel dead
  // for the 1-2 s a section costs, and it was the app's only busy signal for a
  // screen reader. The paint-time discard guards make the lock unnecessary;
  // aria-busy states the same fact without taking the controls away.
  function setBusy(on, label){
    busyDot.className = 'dot' + (on ? ' busy' : '');
    statusEl.textContent = on ? (label || 'working…') : 'ready';
    if (panelEl) panelEl.setAttribute('aria-busy', on ? 'true' : 'false');
  }
  // The polite live region: the two facts a sighted user reads off the panel and
  // the status chip — which section is coming up, and how the operation ended.
  function announce(msg){ if (announcer) announcer.textContent = msg; }

  // WHICH section is showing, said on the rail itself.
  //
  // Keyed on the button's own `data-sec`, NOT on run()'s label. The labels are
  // sentences -- 'loading change log', 'building graph' -- that do not match
  // their buttons and are free to be reworded; a table keyed by one of those is
  // exactly how the table and the sections drift apart, which is the same
  // reason run() carries a shape rather than a flag plus a lookup.
  const navEl = document.querySelector('nav');
  // The sticky grid item that holds the nav and the actions group: the
  // offsetParent both `#navdot` and the buttons resolve against, and the
  // rail's scrollport. `nav` itself is static and unscrolled since the split.
  const railEl = document.getElementById('rail');
  const navDot = document.getElementById('navdot');
  function markSection(id){
    let hit = null;
    for (const b of navEl.querySelectorAll('button[data-sec]')){
      const on = b.dataset.sec === id;
      b.classList.toggle('on', on);
      if (on){ hit = b; b.setAttribute('aria-current', 'page'); }
      else b.removeAttribute('aria-current');
    }
    // The panel's entrance, and the three lines are all load-bearing. Removing
    // the class and adding it back in the same task is a no-op -- the browser
    // never sees the intermediate state -- so a layout read goes between them to
    // flush it, and without that only the FIRST section of a session animates.
    // One forced layout per section entry, which is what this function marks;
    // the live poll repaints without ever coming through here.
    if (panelEl) {
      panelEl.classList.remove('swap');
      void panelEl.offsetWidth;
      panelEl.classList.add('swap');
    }
    if (!hit) return;
    // offsetTop is read against `#rail`, the offsetParent: the sheet makes it
    // sticky (a positioned value) and leaves `nav` static for precisely this,
    // so the bar, absolutely positioned in the same box, lands on the button.
    navDot.style.height = hit.offsetHeight + 'px';
    navDot.style.translate = '0 ' + hit.offsetTop + 'px';
    navDot.classList.add('lit');
    // The rail is its own scrollport now that it sticks (`max-height` +
    // `overflow-y` in the sheet), so on a short window the marked button can be
    // outside it -- reachable by Alt+arrow and invisible while it is reached.
    // Scrolled by hand rather than with `scrollIntoView`, which walks EVERY
    // scrollable ancestor: the document is one of those, and the reader's place
    // in a 42000px list is not this function's to move.
    const top = hit.offsetTop, bottom = top + hit.offsetHeight;
    if (top < railEl.scrollTop) railEl.scrollTop = top;
    else if (bottom > railEl.scrollTop + railEl.clientHeight)
      railEl.scrollTop = bottom - railEl.clientHeight;
  }
  // --- the rail, and the long list, from the keyboard ------------------------
  // A 26-button rail that only answers the mouse makes the most-repeated action
  // on the board the slowest one, and the board is read for hours. `[data-sec]`
  // is the same attribute the active marker and the pairing test key on, so
  // "which buttons own a section" has ONE definition here, not three.
  const TOTOP_AT = 240;                 // px of scroll before the way back earns its place
  function stepSection(delta){
    const bs = [...navEl.querySelectorAll('button[data-sec]')];
    if (!bs.length) return;
    const at = bs.findIndex(b => b.classList.contains('on'));
    // Wraps, deliberately: the rail is a ring of sixteen, and stopping dead at
    // either end would make the shortcut useless exactly where it is quickest
    // to reach the other end. `.click()` rather than calling the section
    // function, so the keyboard and the mouse go through the same door.
    bs[(at < 0 ? 0 : at + delta + bs.length) % bs.length].click();
  }
  function typingInto(el){
    return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                    || el.tagName === 'SELECT' || el.isContentEditable);
  }
  document.addEventListener('keydown', e => {
    // Escape first and unconditionally -- it is the one key that has to work
    // WHILE typing, because getting out of a field is what it is for.
    if (e.key === 'Escape'){
      if (typeof mnClosePanel === 'function') mnClosePanel(null);
      if (typingInto(document.activeElement)) document.activeElement.blur();
      return;
    }
    // Enter and Space on a card that goes somewhere. `role="button"` promises
    // exactly this and a <div> does not keep it on its own; delegated here so
    // seven cards repainted every 2.5 s do not carry seven listeners.
    const card = e.target.closest && e.target.closest('.card.go');
    if (card && (e.key === 'Enter' || e.key === ' ')){
      e.preventDefault(); card.click(); return;
    }
    // Everything below is a bare key, so it must not fire while the reader is
    // writing a query -- `/` is a character before it is a shortcut.
    if (typingInto(document.activeElement) || e.ctrlKey || e.metaKey) return;
    if (e.altKey && (e.key === 'ArrowDown' || e.key === 'ArrowUp')){
      e.preventDefault(); stepSection(e.key === 'ArrowDown' ? 1 : -1); return;
    }
    if (e.key === '/' && !e.altKey && !e.shiftKey){
      e.preventDefault();
      showRecall();
      const q = document.getElementById('q');
      if (q) q.focus();
    }
  });
  const toTopEl = document.getElementById('totop');
  // --- one scroll listener, three answers ------------------------------------
  // Round E added a listener for the way back to the top; Round F asks two more
  // questions of the same event -- is the header sitting on scrolled content,
  // and how far through this section is the reader -- and they share ONE
  // listener rather than taking three. A scroll event fires dozens of times per
  // flick, and every extra handler on it is paid at exactly the moment the page
  // is trying to stay smooth.
  //
  // Two of the three are threshold CROSSINGS and write nothing while the answer
  // holds. The third is continuous by nature, so it is deferred to a frame: many
  // scroll events land inside one frame and only the last one's position is ever
  // seen, so `progQueued` coalesces them into a single write.
  let toTopUp = false, headDeep = false, progQueued = false;
  // 4px, not 0: `scrollY` is fractional on a trackpad and a threshold of zero
  // would toggle the class back and forth across a single pixel of overscroll.
  const DEEP_AT = 4;
  function paintProgress(){
    progQueued = false;
    if (!progEl) return;
    const room = document.documentElement.scrollHeight - innerHeight;
    // Under half a screen of travel there is no position worth reporting, and a
    // bar pinned near 100% on a section that barely scrolls is a lie about how
    // much is left. Overview is that section; the Log is why this exists.
    const worth = room > innerHeight * 0.5;
    progEl.classList.toggle('on', worth);
    const at = worth ? Math.min(1, Math.max(0, scrollY / room)) : 0;
    // `scale`, not `width`: an individual transform property, so the browser
    // composites it instead of laying the band out again on every frame.
    progEl.style.scale = at + ' 1';
  }
  function onScroll(){
    const up = scrollY > TOTOP_AT;
    if (up !== toTopUp){ toTopUp = up; if (toTopEl) toTopEl.classList.toggle('up', up); }
    const deep = scrollY > DEEP_AT;
    if (deep !== headDeep){ headDeep = deep; if (headEl) headEl.classList.toggle('deep', deep); }
    if (progEl && !progQueued){ progQueued = true; requestAnimationFrame(paintProgress); }
  }
  // `passive`, so the handler is never allowed to block the scroll it reports on.
  addEventListener('scroll', onScroll, {passive: true});
  // The document's HEIGHT moves without anybody scrolling -- a section swap
  // takes it from 1439px to 42016px -- and the bar would go on reporting a
  // position computed against the old one until the next wheel event. Observed
  // rather than hooked into the render path: a ResizeObserver callback runs
  // after layout, so reading `scrollHeight` there costs nothing, while the same
  // read inside `paintInto` would force a synchronous layout on every paint.
  if (progEl && window.ResizeObserver)
    new ResizeObserver(() => onScroll()).observe(document.body);
  if (toTopEl){
    toTopEl.addEventListener('click', () => {
      // Read at click time, not at load: a reader who turns the OS setting on
      // mid-session gets the answer they asked for without reloading. A smooth
      // scroll IS motion, and a long one is the kind that reads as the page
      // moving on its own.
      scrollTo({top: 0, behavior: reduceMotion() ? 'auto' : 'smooth'});
      // Focus goes back to the rail, where the reader came from -- otherwise
      // the keyboard is left at the bottom of a list the eye has just left.
      const on = navEl.querySelector('button[data-sec].on');
      if (on) on.focus();
    });
  }

  // panel switching
  // Panel swaps that bypass run() (Recall, Append, the workspace iframes) still
  // count as the user picking a view — see viewPicked.
  function only(el){
    viewPicked = true;
    // The frame is deliberately NOT unloaded on the way out, and this is the
    // second answer to the same problem. Each workspace page runs its own poll
    // loop and hiding an iframe does not stop its timers, so leaving the section
    // used to leave it fetching /api/workspace/agents about once a second
    // forever - measured 5 then 6 requests per 6 s window after clicking away.
    // Blanking the frame to about:blank stopped that, and made losing the
    // document unconditional. It was not, on its own, what lost the reader's
    // text: an A/B of the same trial with the blanking in place and with it cut
    // back out lost a typed project name BOTH times, because showWorkspace
    // re-navigated the frame on every visit anyway. Both halves are gone now -
    // the pages skip their own tick while this class is set (see
    // mindhive/pane.py) and showWorkspace reuses a frame that already holds the
    // view, so the traffic stops and the half-typed sentence survives.
    for (const e of [out, view, graph]) e.classList.toggle('hidden', e !== el);
  }
  // --- the whole-panel repaint guard ---
  // Eight of the nine live sections end their render in showText()/showHTML()
  // with a COMPLETE payload, and the 2.5 s poll re-runs them whether or not
  // anything changed. Writing that payload back when it is byte-identical to
  // what is already on screen is neither free nor invisible: assigning
  // innerHTML re-parses the markup and assigning textContent replaces the text
  // node, so on every tick the reader's selection inside the panel collapses,
  // focus on a control inside it falls back to <body>, :hover resets, and any
  // entrance animation in the subtree replays from 0%. On the real ledger that
  // is 239 change-log rows, or the whole of STATE.md, re-parsed every 2.5 s to
  // produce the pixels that were already there.
  //
  // Overview already dodges this with a `data-shape` signature and in-place
  // value writes, because its structure holds while its numbers move; the cat
  // strips dodge it with `dataset.sig` for the same reason. The list sections
  // have no per-value structure to write into -- but between two ticks their
  // payload is usually IDENTICAL, which is a cheaper question to ask. So stamp
  // what was painted and skip the write when nothing changed.
  //
  // The stamp is a hash, not the payload: keeping a second copy of every panel
  // string is the memory half of the cost this guard exists to avoid.
  function paintSig(s){
    let h = 0x811c9dc5;                                  // FNV-1a, 32-bit
    for (let i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
    return (h >>> 0).toString(36) + ':' + s.length;      // length breaks collisions
  }
  // Any writer that changes a pane WITHOUT coming through paintInto must drop
  // the stamp, or a later payload that happens to match it would be skipped
  // against a DOM that has since moved on. There are exactly two such writers,
  // fail() and ovRefresh(), and two tests in tests/test_dashboard.py keep that
  // list honest: test_every_registered_panel_writer_drops_the_paint_stamp reads
  // each one out of the served page, and
  // test_no_unregistered_function_writes_a_pane_behind_the_paint_stamp
  // attributes every direct pane assignment to the function it sits in and
  // fails on any that is not registered.
  function paintInto(el, prop, value){
    // Read BEFORE only(), which is about to show this pane. A pane that was
    // hidden is a section the reader is ARRIVING at, and arriving always
    // repaints -- for two reasons. The blunt one: a hidden #view still HOLDS
    // the previous section's markup, because only() hides the panes rather
    // than emptying them, so "the stamp matches" says nothing about what the
    // reader can see, and treating it as if it did is the two-click blank
    // board coming back through a new door. The second is the entrance:
    // renderOverview stages its stagger on ARRIVAL, and an animation replays
    // only when the nodes are re-inserted, so a skipped write is a skipped
    // entrance. Neither costs anything to keep -- a section switch repaints
    // once either way, and the churn this guard exists to remove is the POLL
    // rewriting a panel that is already on screen and already correct.
    const wasHidden = el.classList.contains('hidden');
    only(el);
    const sig = paintSig(value);
    if (!wasHidden && el.dataset.paint === sig) return false;   // already up, unchanged
    // Cleared before the write and set after it -- paintCats' hard-won rule.
    // Stamping up front means a throw mid-write leaves a half-painted pane that
    // every identical poll afterwards refuses to repair.
    delete el.dataset.paint;
    el[prop] = value;
    el.dataset.paint = sig;
    return true;
  }
  function showText(v){ paintInto(out, 'textContent', (typeof v === 'string') ? v : JSON.stringify(v, null, 2)); }
  function showHTML(html){ paintInto(view, 'innerHTML', html); }
  function fail(e){
    only(out);
    // Direct writer: everything below assigns out.innerHTML itself. Drop the
    // stamp so the next successful render of this section repaints over the
    // error instead of matching a signature the error has already replaced --
    // which would leave a stale failure on screen under a "ready" status.
    delete out.dataset.paint;
    const msg = (e && e.message) || String(e);
    if (e && e.pane) {
      // A job that RAN and reported a bad outcome is not a transport error, and
      // for a failed suite its output is the most useful thing on the screen. The
      // reason goes on top; the output stays. Without this the one error path
      // would replace four minutes of pytest with a single line.
      out.innerHTML = '<span class="err">'+ esc(msg) +'</span>\\n\\n' + esc(e.pane);
      return;
    }
    if (/failed to fetch|networkerror|load failed/i.test(msg)) {
      out.innerHTML = '<span class="err">Can\\'t reach the MIND HIVE server.</span>\\n\\n'
        + 'The server has stopped, or this tab is from an earlier run. Re-open '
        + '<b>MindHive.exe</b> \\u2014 it opens a fresh tab. Each run uses its own one-time '
        + 'link, so older tabs stop working.';
    } else {
      out.innerHTML = '<span class="err">'+ esc(msg) +'</span>';
    }
  }

  // --- project / session scoping ---
  // PROJECT is the selected store namespace ('' = server's default, the
  // richest one). SESSION optionally narrows the Memory view to one session.
  let PROJECT = localStorage.getItem('mh_project') || '';
  let SESSION = null;
  function escAttr(s){ return esc(s).replace(/"/g,'&quot;'); }
  // Provenance badge: rendered wherever a row/card carries origin:'self-reliant'
  // (server-computed by events.origin_of, so Memory / Sessions / Improvements /
  // Oversee can never disagree about what a Self-Reliant run produced).
  function srPill(o){
    return o === 'self-reliant'
      ? ' <span class="pill sr" title="produced by a Self-Reliant run">\\u2726 self-reliant</span>'
      : '';
  }
  function withProj(path){
    return PROJECT ? path + (path.includes('?')?'&':'?') + 'project=' + encodeURIComponent(PROJECT) : path;
  }
  // SCOPE identity, the half `shouldPaint` cannot answer. It compares which
  // SECTION is live; a project switch changes neither the section nor the
  // renderer, so a response fetched for project A used to paint its rows under
  // project B's label with nothing wrong on screen to show for it — the numbers
  // were simply another namespace's until the next poll replaced them.
  //
  // Enforced HERE, at the one place every renderer and both pollers already go
  // through, rather than in nine renderers that would each have to remember.
  // `apiPost` is deliberately NOT guarded: a mutation really did run against
  // the project it named in its own body, so its result is worth showing.
  function StaleScope(scope){
    const e = new Error('a response for project ' + JSON.stringify(scope)
                        + ' arrived after the picker had already moved');
    e.name = 'StaleScope';
    return e;
  }
  async function apiGet(path){
    const scope = PROJECT;              // the namespace this question was asked of
    const r = await fetch(u(withProj(path)), { headers: { 'X-Hive-Token': TOKEN } });
    const ct = r.headers.get('content-type') || '';
    const data = ct.includes('json') ? await r.json() : await r.text();
    // An EMPTY scope is not a project, so acquiring one is not a switch.
    // `withProj` omits the parameter entirely while PROJECT is '', so the
    // server answered from default_project() — and loadProjects then sets
    // PROJECT to `data.current`, which IS that project. Discarding on that
    // transition threw away the answer to the reader's own first click:
    // measured at boot with Graph, whose build came back as
    //   building graph — failed
    //   a response for project "" arrived after the picker had already moved
    // while the identical click a few seconds later rendered 22.8 MB fine.
    //
    // Nothing else can move PROJECT inside that window, which is what makes
    // this safe rather than a hole in the guard: `<select id="proj">` is empty
    // markup until loadProjects fills it, so while the scope is '' the only
    // writer is loadProjects' own assignment. A USER switch — the case this
    // guard exists for — cannot happen before there is anything to switch to.
    if (scope !== '' && PROJECT !== scope) throw StaleScope(scope);
    return data;
  }
  async function apiPost(path, body){
    const b = Object.assign({}, body || {});
    // a caller-supplied project wins (e.g. HIVE MIND rows acting on their own
    // namespace); otherwise scope to the picker like every other call
    if (PROJECT && !b.project) b.project = PROJECT;
    const r = await fetch(u(withProj(path)), { method:'POST', headers:{ 'Content-Type':'application/json',
      'X-Hive-Token': TOKEN }, body: JSON.stringify(b) });
    return r.json();
  }
  async function loadProjects(){
    try {
      const data = await apiGet('/api/projects');
      const sel = document.getElementById('proj');
      // Only a project the picker can actually SHOW may be selected. The
      // remembered value used to win outright — `PROJECT || data.current || …`
      // — and a namespace that has since been deleted is still a string, so it
      // kept winning. A <select> whose value matches no option falls back to
      // its FIRST, which here is '*': measured against the real store, the
      // picker read HIVE MIND — the unified view, the widest scope there is —
      // while every request carried `a-project-that-was-deleted` and the board
      // stayed empty. Nothing on screen said so, and it survived every reload,
      // because nothing rewrote the remembered value either.
      //
      // `data.current` is not a safe fallback on its own: the server echoes the
      // project it was ASKED for, so a request carrying the ghost comes back
      // naming the ghost. Every candidate is checked against what was offered.
      const offered = new Set();
      if (data.hivemind) offered.add('*');
      for (const p of (data.projects || [])) offered.add(p.name);
      const cur = [PROJECT, data.current, data.default].find(n => n && offered.has(n)) || '';
      let html = '';
      if (data.hivemind) {                       // unified view, always first
        html += '<option value="*"'+(cur==='*'?' selected':'')+'>'
              + esc(data.hivemind.name)+' ('+data.hivemind.events+')</option>';
      }
      html += (data.projects || []).map(p => '<option value="'+escAttr(p.name)+'"'
          +(p.name===cur?' selected':'')+'>'+esc(p.name)+' ('+p.events+')</option>').join('');
      sel.innerHTML = html || '<option value="">(no projects)</option>';
      PROJECT = cur;
      // Don't leave a name the server no longer offers in storage, or the same
      // wrong scope is restored on the next reload and every one after it.
      if (cur && localStorage.getItem('mh_project') !== cur)
        localStorage.setItem('mh_project', cur);
    } catch(_) { /* keep server default */ }
  }
  function onProjChange(){
    PROJECT = document.getElementById('proj').value;
    localStorage.setItem('mh_project', PROJECT);
    SESSION = null;
    showOverview();
  }
  // Loading feedback, two staged thresholds rather than one. Below SKEL_MS a
  // placeholder reads as a flicker instead of as progress, so a fast section
  // paints nothing extra; past SLOW_MS a bare "working…" chip stops informing
  // and starts looking like a hang, so the status line says how long it's been.
  // Measured 2026-08-10: the section endpoints cost 1.2-1.9 s against the 31k
  // store, which is exactly the band where a skeleton earns its place. 150 ms
  // because that band is 8-12x further out — the threshold's whole job is to
  // suppress the flicker on a paint that was already effectively instant (a memo
  // hit, an empty namespace), and 300 ms was also swallowing the first sixth of
  // every real load, which is the part where the user is still deciding whether
  // the click registered.
  const SKEL_MS = 150, SLOW_MS = 10000;
  // One shape per thing a section paints, named by the section's own layout so a
  // call site reads as a claim about what is coming. Each was checked against the
  // real panel in a browser, which is how `sessions` ended up on `rows`: its cards
  // are called `.tcard` and read like cards in the source, but they render as
  // stacked full-width rows 42px tall, not as a grid.
  //   cards  Overview only: a heading, then the `.cards` grid. Six tiles because
  //          that grid is auto-fit and Overview paints six stat cards, so any
  //          other count produces a different column layout than the one arriving.
  //   rows   every `ul.hits` list, plus Sessions: a heading, then stacked rows.
  //   block  State and Briefing, which paint into `pre#out` — one filled box, and
  //          no heading line, because their title is text inside the block.
  // Graph and the workspace iframes take no shape at all: their panel is an
  // iframe, and a placeholder in our document would sit behind a frame that
  // arrives with its own background.
  const SKEL_SHAPES = {
    cards: '<div class="bar w40"></div><div class="cards">'
           + '<div class="tile"></div>'.repeat(6) + '</div>',
    rows: '<div class="bar w40"></div>' + '<div class="line"></div>'.repeat(4),
    block: '<div class="pane"></div>',
  };
  function showSkeleton(shape){
    // An unknown name paints nothing rather than throwing: the skeleton is
    // feedback about work, and feedback must never be the thing that breaks the
    // work. `test_dashboard_theme.py` checks every call site's name statically,
    // which is where a typo is supposed to be caught — here it would only ever
    // be caught by a user watching a section not paint.
    const markup = SKEL_SHAPES[shape];
    if (!markup) return;
    // Same rule renderOversee already applies to the enrichment panel: never
    // repaint the view out from under a caret. A skeleton that eats what
    // somebody is typing is worse than no skeleton.
    const ae = document.activeElement;
    if (ae && ae.closest && ae.closest('#view')) return;
    showHTML('<div class="skel" aria-hidden="true">' + markup + '</div>');
  }
  // wrap any async unit of work with busy state + error handling
  // Every run() gets a generation. `shouldPaint` covers renderers that register
  // a liveFn; it structurally CANNOT cover the run()-scoped paths that set
  // `liveFn = null` — Briefing, Recall, Graph, doAction, runJob — because
  // shouldPaint(null, anonymousFn) is false for all of them, so an identity
  // check there would discard every paint rather than only the stale ones.
  //
  // Corrections 6 in the plan rejected a generation guard for POLLER renders,
  // and that still holds: refreshLive calls liveFn() directly and never enters
  // run(), so a counter here would tag nothing for it. It says nothing about
  // run()-scoped work, which this counter is exactly the right shape for.
  //
  // It also covers three wrapper effects that shouldPaint never touched: a
  // stale run's completion ANNOUNCEMENT (the #ann live region is not the panel,
  // so a screen-reader user heard "loading oversee — ready" after they had
  // already moved to Overview), a stale run clearing busy while a newer one is
  // still loading, and stale skeleton/slow-status timers repainting over the
  // section that replaced them.
  // `shape` gates the skeleton, and only the skeleton. It shipped firing for
  // EVERY run() caller, which is wrong in a way that only shows up on the paths
  // nobody demos: dismissing an improvement, reflecting, resolving, enriching a
  // prompt, deleting a session, appending a memory, and both Action buttons all
  // go through run() from inside an already-painted view. A placeholder there
  // wipes the thing the user was reading to announce work that does not replace
  // it. Section loads are the only runs whose whole job is to replace the panel.
  //
  // It carries the section's SHAPE rather than a boolean because the two facts a
  // caller has to state — "this run replaces the panel" and "this is what will be
  // there" — are one decision, and splitting them into a flag plus a lookup table
  // keyed by label is how the table and the sections drift apart. Omitted means
  // no skeleton, which is the correct default for everything that is not a
  // section load.
  let RUN_GEN = 0;
  async function run(label, fn, shape){
    // Set before the first await: the section is chosen at click time, not when
    // its fetch returns, and boot may resolve in between.
    viewPicked = true;
    const gen = ++RUN_GEN;
    const current = () => gen === RUN_GEN;
    setBusy(true, label);
    announce(label);
    const skel = shape
      ? setTimeout(() => { if (current()) showSkeleton(shape); }, SKEL_MS)
      : null;
    const slow = setTimeout(() => {
      if (current())
        statusEl.textContent = (label || 'working') + ' \\u2014 still loading\\u2026';
    }, SLOW_MS);
    // `current` is passed to fn so the anonymous run()-scoped bodies can guard
    // their own paint. Renderers that take no argument ignore it, unchanged.
    let failed = false;
    try { await fn(current); if (current()) announce((label || 'operation') + ' \\u2014 ready'); }
    catch(e) {
      // A stale failure is swallowed on purpose, the same way refreshLive
      // swallows a transient blip: painting it would put an error over the
      // section the user is now reading, which is the clobber this guard exists
      // to stop. The live run still surfaces its own errors.
      failed = true;
      if (current()) { fail(e); announce((label || 'operation') + ' \\u2014 failed'); }
    }
    finally {
      clearTimeout(skel); clearTimeout(slow);
      if (current()) {
        setBusy(false);
        // "ready" is setBusy's RESTING state, not a verdict, and it was
        // overwriting the outcome: every failed operation ended with the status
        // chip reading "ready" while the live region said "failed" and the error
        // sat in the panel below. Two surfaces, two different stories, and the
        // one a sighted user actually looks at was the wrong one.
        if (failed) statusEl.textContent = (label || 'operation') + ' \\u2014 failed';
      }
    }
  }

  // The signature of everything this panel DRAWS. Overview repaints on a 2.5 s
  // poll, and until this guard it rebuilt all seven cards and every type chip
  // whether or not one number had moved. Measured in the browser with the
  // pointer held perfectly still on a card: after 8 s the node was gone,
  // replaced, and its hover lift was caught mid-transition at -0.94px instead
  // of the settled -2px -- the card restarted its lift four times while nobody
  // touched it. Same defect the three cat strips already carry a guard for, on
  // the panel that is on screen most.
  //
  // Every field the markup reads is in here and nothing else is. `last_ts` is
  // DATA -- it moves when an event actually lands, not with the clock -- so it
  // belongs. A value that ticked with the poll itself would make this guard
  // permanently false, which is the failure `age_secs` caused on the cat strips.
  // What the panel's SHAPE is -- which cards exist, which chips exist and in
  // what order, whether the injection strip is there. Deliberately NOT the
  // numbers: a number is text, and text can be written into a node that is
  // already on screen. Folding the counts in here would make the guard fire
  // almost never on a busy machine, which is the same as not having one.
  function ovShape(o){
    return JSON.stringify([o.project, o.projects != null, o.chain_ok,
                           ovTypes(o).map(([t]) => t), o.injection]);
  }
  function ovTypes(o){
    return Object.entries(o.by_type || {}).sort((a,b) => b[1]-a[1]);
  }
  // Write the changed numbers into the nodes that are already there, and mark
  // the ones that MOVED. This is the half that keeps a hovered card hovered:
  // the element is never replaced, so its lift, its focus and its selection all
  // survive a tick. The bump is not decoration -- on a board whose whole job is
  // reporting what just happened, "which of these seven numbers changed" is the
  // question, and a value that changes silently makes the reader diff it by eye.
  function ovRefresh(o){
    const val = {projects:o.projects, events:o.events,
                 memory:o.memory_events ?? '—',
                 auto:o.auto_events ?? '—',
                 sessions:o.sessions ?? '—',
                 last:fmtTs(o.last_ts) || '—'};
    const counts = Object.fromEntries(ovTypes(o));
    const moved = [];
    for (const el of view.querySelectorAll('.cards[data-shape] [data-k]')){
      const next = String(val[el.dataset.k]);
      if (el.textContent !== next){ el.textContent = next; moved.push(el); }
    }
    for (const el of view.querySelectorAll('.types .pill[data-t] b')){
      const next = String(counts[el.parentNode.dataset.t]);
      if (el.textContent !== next){ el.textContent = next; moved.push(el); }
    }
    if (!moved.length) return;
    // Direct writer: the numbers above were written into the live grid, so the
    // stamp showHTML left behind now describes markup that is no longer on
    // screen. Drop it, and the next full paint of this section is a real one.
    delete view.dataset.paint;
    // Restart the flash by dropping the class and re-adding it on the NEXT
    // frame. The usual trick -- reading `offsetWidth` to force a reflow -- would
    // flush layout synchronously on every tick that changed a number, which is
    // a real cost paid to restart a decoration. A frame is free.
    for (const el of moved) el.classList.remove('bump');
    requestAnimationFrame(() => { for (const el of moved) el.classList.add('bump'); });
  }
  async function renderOverview(){
    const o = await apiGet('/api/overview');
    if (!shouldPaint(liveFn, renderOverview)) return;  // newer section owns the panel
    // `[data-sig]` narrows this to OVERVIEW's own grid: Improvements and Skill
    // both paint a `.cards` too, and matching one of those would make arriving
    // at Overview look like staying on it.
    const prev = document.querySelector('#view .cards[data-shape]');
    // `contains` alone was not enough, and the gap was reachable in two clicks.
    // The panel shows ONE of `#view`, `#out` and the graph frame at a time, and
    // `only()` hides the other two rather than emptying them -- so after any
    // section that renders through `showText` (Memory with nothing in it, State,
    // Briefing, and every error path), `#view` still CONTAINS the last overview
    // grid while `#out` is what the reader is looking at. Arriving back at
    // Overview with the counts unchanged then took the refresh branch, wrote the
    // same numbers into a hidden grid and returned, and the board sat there with
    // the rail saying Overview, the status saying ready, and "No memory yet" on
    // screen. Being ON SCREEN is the thing the branch below actually means by
    // `live`, so it is the thing that gets asked.
    const live = prev && view.contains(prev) && !view.classList.contains('hidden');
    const shape = ovShape(o);
    // Structure held, values written in place -- the split the cat strips
    // already make between "the strip is the same" and "the clock still moves".
    if (live && prev.dataset.shape === shape){ ovRefresh(o); return; }
    // Entrance motion belongs to ARRIVING at the section, not to a number
    // changing. `live` is exactly that distinction: a grid already on screen
    // means the user is here and watching, and replaying a stagger under them
    // every time an event lands would be the 2.5 s flicker this guard exists to
    // remove, wearing a nicer coat.
    const enter = live ? '' : ' enter';
    const chip = o.chain_ok
      ? '<span class="big ok">OK</span>' : '<span class="big bad">CHECK</span>';
    const types = ovTypes(o)
      .map(([t,n]) => '<span class="pill" data-t="'+escAttr(t)+'">'+esc(t)
                      +' <b>'+n+'</b></span>').join('')
      || '<span class="lbl">no events yet</span>';
    // The SOP-injection verdict, and ONLY when it is degraded — the server sends
    // null for `verbatim`, so a healthy panel renders zero extra lines and this
    // strip stays worth reading. It is deliberately NOT a card: the cards are
    // counts for THIS namespace and this verdict is global, which the line says
    // in its own words rather than leaving to the layout.
    const inj = o.injection;
    const injHTML = !inj ? '' : (
      '<div class="injwarn"><b>SOP injection: '+esc(inj.verdict)+'</b> \\u2014 '
      + esc(inj.sentence || '')
      + ((inj.pointers || []).length
         ? '<span class="lbl">arrived as pointers, not bodies: '
           + inj.pointers.map(esc).join(', ')
           + ' \\u2014 read before acting (<code>hive sop show &lt;id&gt;</code>)</span>'
         : '')
      + '<span class="lbl">This verdict is global to this machine, not '
      + esc(o.project) + ' \\u2014 the per-prompt pass reads the global '
      + 'mandatory-SOP sidecar. Full report: <code>hive sop injection</code></span>'
      + '</div>');
    showHTML(
      '<h2 class="h">'+esc(o.project)+'</h2>'
      + injHTML
      + '<div class="cards'+enter+'" data-shape="'+escAttr(shape)+'">'
      + (o.projects != null ? '<div class="card"><div class="big" data-k="projects">'+o.projects+'</div><div class="lbl">projects</div></div>' : '')
      + '<div class="card"><div class="big" data-k="events">'+o.events+'</div><div class="lbl">events</div></div>'
      + '<div class="card go" role="button" tabindex="0" onclick="showMemory()"><div class="big ok" data-k="memory">'+(o.memory_events ?? '—')+'</div><div class="lbl">memory</div></div>'
      + '<div class="card"><div class="big" data-k="auto">'+(o.auto_events ?? '—')+'</div><div class="lbl">session / auto</div></div>'
      + '<div class="card go" role="button" tabindex="0" onclick="showSessions()"><div class="big" data-k="sessions">'+(o.sessions ?? '—')+'</div><div class="lbl">sessions</div></div>'
      + '<div class="card"><div>'+chip+'</div><div class="lbl">hash chain</div></div>'
      + '<div class="card"><div class="big" style="font-size:15px" data-k="last">'+esc(fmtTs(o.last_ts) || '—')+'</div><div class="lbl">last activity</div></div>'
      + '</div><div class="types">'+types+'</div>');
  }
"""


def script() -> str:
    """The core script with the clock helper spliced in.

    Spliced rather than concatenated so the helper lands ABOVE its callers in
    one shared scope -- `fmtTs` is used by the improve, oversee, skills and
    views modules, exactly as `esc` is."""
    return JS.replace("/*__CLOCK__*/", CLOCK_JS)
