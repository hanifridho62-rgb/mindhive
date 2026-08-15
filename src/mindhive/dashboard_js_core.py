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
  // Every animation on this page is dropped in CSS under prefers-reduced-motion;
  // a scripted smooth scroll is the one piece of motion a stylesheet can't reach.
  const REDUCE_MOTION = !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

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
  // panel switching
  // Panel swaps that bypass run() (Recall, Append, the workspace iframes) still
  // count as the user picking a view — see viewPicked.
  function only(el){ viewPicked = true; for (const e of [out, view, graph]) e.classList.toggle('hidden', e !== el); }
  function showText(v){ only(out); out.textContent = (typeof v === 'string') ? v : JSON.stringify(v, null, 2); }
  function showHTML(html){ only(view); view.innerHTML = html; }
  function fail(e){
    only(out);
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
    if (PROJECT !== scope) throw StaleScope(scope);
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
      const cur = PROJECT || data.current || data.default || '';
      let html = '';
      if (data.hivemind) {                       // unified view, always first
        html += '<option value="*"'+(cur==='*'?' selected':'')+'>'
              + esc(data.hivemind.name)+' ('+data.hivemind.events+')</option>';
      }
      html += (data.projects || []).map(p => '<option value="'+escAttr(p.name)+'"'
          +(p.name===cur?' selected':'')+'>'+esc(p.name)+' ('+p.events+')</option>').join('');
      sel.innerHTML = html || '<option value="">(no projects)</option>';
      PROJECT = cur;
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

  async function renderOverview(){
    const o = await apiGet('/api/overview');
    if (!shouldPaint(liveFn, renderOverview)) return;  // newer section owns the panel
    const chip = o.chain_ok
      ? '<span class="big ok">OK</span>' : '<span class="big bad">CHECK</span>';
    const types = Object.entries(o.by_type || {}).sort((a,b)=>b[1]-a[1])
      .map(([t,n]) => '<span class="pill">'+esc(t)+' <b>'+n+'</b></span>').join('') || '<span class="lbl">no events yet</span>';
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
      + '<div class="cards">'
      + (o.projects != null ? '<div class="card"><div class="big">'+o.projects+'</div><div class="lbl">projects</div></div>' : '')
      + '<div class="card"><div class="big">'+o.events+'</div><div class="lbl">events</div></div>'
      + '<div class="card"><div class="big ok">'+(o.memory_events ?? '—')+'</div><div class="lbl">memory</div></div>'
      + '<div class="card"><div class="big">'+(o.auto_events ?? '—')+'</div><div class="lbl">session / auto</div></div>'
      + '<div class="card"><div class="big">'+(o.sessions ?? '—')+'</div><div class="lbl">sessions</div></div>'
      + '<div class="card"><div>'+chip+'</div><div class="lbl">hash chain</div></div>'
      + '<div class="card"><div class="big" style="font-size:15px">'+esc(o.last_ts || '—')+'</div><div class="lbl">last activity</div></div>'
      + '</div><div class="types">'+types+'</div>');
  }
"""
