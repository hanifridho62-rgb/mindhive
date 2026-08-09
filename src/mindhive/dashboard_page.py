# src/mindhive/dashboard_page.py
"""Presentation layer for the MIND HIVE dashboard.

The single-page HTML/CSS/JS control panel, isolated from ``dashboard.py`` so
the HTTP handler and data logic aren't buried under ~380 lines of embedded
markup (and ``dashboard.py`` stays under the project's 1000-line file cap).
Pure presentation: takes a session token, returns a self-contained HTML
document string.

One sibling dependency, deliberately: ``cat_board`` supplies the MindNetwork
faculty board that this page and the swarm page both render. The alternative was a
second copy of the palettes, sprites and labels in each page — two homes for one
legend, drifting invisibly. ``cat_board`` is itself pure presentation, so this stays
a presentation-only module."""
import json

from . import cat_board


def render_page(token: str, tasks_css: str = "", tasks_js: str = "") -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MIND HIVE</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; background:#11131a; color:#e6e8ef; }
  header { padding: 12px 20px; background:#171a23; border-bottom:1px solid #2a2f3d;
    display:flex; align-items:center; gap:14px; position:sticky; top:0; z-index:5; }
  header h1 { margin:0; font-size:17px; letter-spacing:.5px; }
  header .dot { width:9px; height:9px; border-radius:50%; background:#3a4054; }
  header .dot.busy { background:#e0b341; animation: pulse 1s infinite; }
  header .dot.ok { background:#46c46a; }
  header #status { font-size:12px; color:#9aa3b8; }
  header #live { font-size:12px; cursor:pointer; user-select:none; margin-left:auto;
    color:#46c46a; }
  header #live.paused { color:#6b7488; }
  header #live .blip { animation: pulse 1.6s infinite; }
  header #synced { font-size:11px; color:#6b7488; min-width:118px; text-align:right; }
  header #projwrap { font-size:12px; color:#9aa3b8; display:flex; align-items:center; gap:6px; }
  header #projwrap select { padding:3px 6px; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  main { display:grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 50px); }
  nav { background:#141722; border-right:1px solid #2a2f3d; padding:12px; }
  nav .group { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:#6b7488;
    margin:14px 6px 4px; }
  nav button { display:block; width:100%; text-align:left; margin:4px 0; padding:8px 10px;
    background:#1d2230; color:#e6e8ef; border:1px solid #2a2f3d; border-radius:6px; cursor:pointer; }
  nav button:hover:not(:disabled) { background:#262d3d; }
  nav button:disabled { opacity:.45; cursor:wait; }
  section.panel { padding:18px 22px; }
  pre#out { white-space:pre-wrap; background:#0c0e14; border:1px solid #2a2f3d; border-radius:8px;
    padding:14px; min-height:120px; max-height:62vh; overflow:auto; }
  iframe#graph { width:100%; height:72vh; border:1px solid #2a2f3d; border-radius:8px; background:#fff; }
  input, select, textarea { background:#0c0e14; color:#e6e8ef; border:1px solid #2a2f3d;
    border-radius:6px; padding:6px 8px; font:inherit; }
  .row { margin:8px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .hidden { display:none; }
  .err { color:#ff8a8a; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .card { background:#0c0e14; border:1px solid #2a2f3d; border-radius:10px; padding:14px 16px; }
  .card .big { font-size:26px; font-weight:600; }
  .card .lbl { font-size:12px; color:#9aa3b8; margin-top:2px; }
  .card .big.ok { color:#46c46a; } .card .big.bad { color:#ff8a8a; }
  .types { margin-top:14px; display:flex; flex-wrap:wrap; gap:8px; }
  .pill { background:#1d2230; border:1px solid #2a2f3d; border-radius:999px; padding:3px 10px; font-size:12px; }
  .pill b { color:#fff; }
  .pill.est { color:#e0b341; border-color:#5a4a1f; }
  .pill.triclaude { color:#7fb4ff; border-color:#2c4a75; font-weight:700; }
  .pill.tridism { color:#8a93a6; }
  .pill.sr { color:#c9a6ff; border-color:#5b3f8a; background:#191227; }
  .hits { list-style:none; padding:0; margin:0; }
  .hits li { background:#0c0e14; border:1px solid #2a2f3d; border-radius:8px; padding:10px 12px; margin:8px 0; }
  .hits .file { font-size:12px; color:#7fb2ff; word-break:break-all; }
  .hits .snip { white-space:pre-wrap; margin-top:4px; }
  .hits .dup { color:#8a93a6; }
  .snip.one { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .skchip { display:inline-block; cursor:pointer; color:#9ecbff; background:#12182a;
    border:1px solid #2a3550; border-radius:6px; padding:1px 7px; margin:2px 3px 0 0; }
  .skchip:hover { background:#1b2540; border-color:#3d6; color:#dfeaff; }
  .statusDot { font-size:11px; margin-right:5px; vertical-align:middle; }
  .statusDot.sok { color:#3fb950; }
  .statusDot.sbad { color:#f85149; }
  .statusDot.snone { color:#3a4150; }
  .legitem { cursor:pointer; padding:1px 5px; border-radius:6px; }
  .legitem:hover { background:#1b2540; }
  .legitem.on { background:#233; outline:1px solid #3d6; }
  .callcount { font-size:11px; color:#9aa3b8; margin-left:3px; }
  .callbtn { margin-left:8px; cursor:pointer; background:#12281a; border:1px solid #2a5;
    color:#9f9; border-radius:6px; font-size:11px; padding:0 7px; }
  .callbtn:hover { background:#1b3a26; color:#dfffe6; }
  .skmeta { margin:10px 0; }
  .skmeta b { color:#7fb2ff; }
  .skbody { white-space:pre-wrap; background:#0c0e14; border:1px solid #2a2f3d;
    border-radius:8px; padding:10px 12px; margin-top:4px; max-height:52vh; overflow:auto; }
  h2.h { margin:2px 0 12px; font-size:16px; }
  .glyph { font-weight:700; margin-right:6px; }
  .glyph.res { color:#46c46a; } .glyph.unres { color:#ff8a8a; }
  .glyph.watch { color:#e0b341; } .glyph.dis { color:#8a93a6; }
  .legend { font-size:12px; color:#9aa3b8; margin:2px 0 10px; }
  .ovdone { color:#46c46a; font-weight:600; }
  .ovpend { color:#8a93a6; }
  .ovspin { color:#e0b341; display:inline-block; animation: pulse 1.6s infinite; }
  /* Interactive roadmap panel (Oversee): ask + progress bar + stepper */
  .rmwrap { margin:8px 0 4px; padding:8px 10px; background:#141824;
    border:1px solid #2a2f3d; border-radius:8px; }
  .rmask { margin-bottom:6px; }
  .rmint { color:#e0b341; font-size:12px; margin:2px 0 6px; }
  .rmbar { height:6px; background:#1d2230; border-radius:999px; overflow:hidden; }
  .rmfill { height:100%; background:#46c46a; border-radius:999px;
    transition:width .4s; }
  .rmstep { position:relative; padding:3px 0 3px 22px; cursor:pointer; }
  .rmstep::before { content:''; position:absolute; left:7px; top:0; bottom:0;
    width:2px; background:#2a2f3d; }
  .rmstep.rmfirst::before { top:50%; }
  .rmstep.rmlast::before { bottom:50%; }
  .rmnode { position:absolute; left:0; width:16px; text-align:center;
    background:#141824; }
  .rmnode.rmdone { color:#46c46a; }
  .rmnode.rmactive { color:#e0b341; animation: pulse 1.6s infinite; }
  .rmnode.rmpend { color:#8a93a6; }
  .rmnode.rmskip { color:#555c6e; }
  .rmdetail { color:#9aa3b8; font-size:11px; margin:2px 0; }
  /* Prompt enrichment panel (Oversee): toggle + questions + composed prompt */
  .enwrap { margin:0 0 12px; padding:10px 12px; background:#0c0e14;
    border:1px solid #2a2f3d; border-radius:8px; }
  .enwrap textarea { width:100%; box-sizing:border-box; background:#141824;
    color:#dfe6f3; border:1px solid #2a2f3d; border-radius:6px;
    padding:6px 8px; font:inherit; margin-top:6px; }
  .ensec { margin:8px 0 2px; }
  .enq { margin:6px 0; }
  .enq input { width:70%; background:#141824; color:#dfe6f3;
    border:1px solid #2a2f3d; border-radius:6px; padding:2px 8px;
    font:inherit; margin-top:2px; }
  .enok { color:#46c46a; font-weight:700; }
  .enbad { color:#ff8a8a; font-weight:700; }
  .enout { background:#141824; border:1px solid #2a2f3d; border-radius:8px;
    padding:8px 10px; white-space:pre-wrap; max-height:40vh; overflow:auto; }
  button.mini { font-size:11px; padding:1px 8px; margin-left:8px; background:#1d2230;
    color:#9aa3b8; border:1px solid #2a2f3d; border-radius:999px; cursor:pointer; }
  button.mini:hover { color:#ff8a8a; }
  /* MN-13: the faculty cat board, inline in the header. Every cat and panel rule
     comes from `cat_board.CSS`; this is only the container's own box, which differs
     from the swarm page's full-width strip. `margin-left:auto` pushes it to the
     header's trailing edge so it never shoves the project picker around as cats
     appear and vanish. */
  #mn-cats { display:flex; align-items:flex-end; gap:5px; margin-left:auto;
    flex:0 1 auto; overflow-x:auto; max-width:52%; }
  /* The session strip sits beside the faculty one rather than under it: this
     header is a single row, and a second band would push the whole page down
     every time a Claude starts working. Narrower cap so the faculty cats — the
     fixed seven — never get squeezed out by an unbounded session list. */
  #mn-sessions { display:flex; align-items:center; gap:5px; flex:0 1 auto;
    overflow-x:auto; max-width:34%; margin-left:8px; }
/*__CAT_CSS__*/
  /*__TASKS_CSS__*/
</style></head>
<body>
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
  <nav>
    <button onclick="showOverview()">Overview</button>
    <div class="group">Work</div>
    <button onclick="showOversee()">Oversee</button>
    <div class="group">Memory</div>
    <button onclick="showMemory()">Memory</button>
    <button onclick="showSessions()">Sessions</button>
    <button onclick="showImprovements(true)">Improvements</button>
    <button onclick="showLog()">Log</button>
    <button onclick="showSops()">SOP</button>
    <div class="group">View</div>
    <button onclick="showState()">State</button>
    <button onclick="showBriefing()">Briefing</button>
    <button onclick="showRecall()">Recall</button>
    <button onclick="showGraph()">Graph</button>
    <button onclick="showSkillPanel()">Skill &amp; Plugin</button>
    <div class="group">Workspace</div>
    <button onclick="showWorkspace('parallel')">MindParallel</button>
    <button onclick="showWorkspace('swarm')">MindSwarm</button>
    <button onclick="showWorkspace('research')">Self-Reliant</button>
    <div class="group">Actions</div>
    <button onclick="showAppend()">Append memory</button>
    <button onclick="doExport()">Export handoff.md</button>
    <button onclick="doAction('/api/project','Project (rebuild)')">Project</button>
    <button onclick="doAction('/api/sync','Sync')">Sync</button>
    <button onclick="doAction('/api/harvest','Catch up (harvest missed sessions)')">Catch up</button>
    <button onclick="doAction('/api/verify','Verify chain', 'GET')">Verify</button>
    <div class="group">Heavy</div>
    <button onclick="confirmAction('/api/embed','Embed')">Embed</button>
    <button onclick="confirmAction('/api/plugins/scan','Plugins scan')">Plugins scan</button>
    <button onclick="runReflect()">Reflect (learn from failures)</button>
    <button onclick="runResolve()">Resolve red (reroute failed rules)</button>
    <button onclick="confirmAction('/api/selftest','Self-test')">Self-test</button>
  </nav>
  <section class="panel">
    <div id="controls"></div>
    <div id="view"></div>
    <pre id="out" class="hidden"></pre>
    <iframe id="graph" class="hidden"></iframe>
  </section>
</main>
<script>
  const TOKEN = __TOKEN__;
  const out = document.getElementById('out');
  const view = document.getElementById('view');
  const graph = document.getElementById('graph');
  const controls = document.getElementById('controls');
  const busyDot = document.getElementById('busy');
  const statusEl = document.getElementById('status');
  const navButtons = () => Array.from(document.querySelectorAll('nav button'));
  const liveEl = document.getElementById('live');
  const syncedEl = document.getElementById('synced');

  // --- real-time engine ---
  // liveFn is the silent re-renderer for the current view (null = view isn't
  // auto-refreshable). A timer re-runs it on an interval, and actions that
  // mutate memory call refreshLive() so changes appear without a manual click.
  let liveFn = null;
  let liveOn = true;
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
    if (liveOn) refreshLive();
  }
  // silent refresh: no busy spinner, no nav-disable, errors swallowed so a
  // transient blip never disrupts what the user is reading.
  async function refreshLive(){
    if (!liveOn || !liveFn) return;
    if (document.hidden) return;
    if (busyDot.classList.contains('busy')) return;  // don't fight a manual op
    try { await liveFn(); markSynced(); } catch(_) { /* keep last good view */ }
  }
  setInterval(refreshLive, LIVE_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshLive(); });

  function esc(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function u(path){ return path + (path.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(TOKEN); }
  function setBusy(on, label){
    busyDot.className = 'dot' + (on ? ' busy' : '');
    statusEl.textContent = on ? (label || 'working…') : 'ready';
    navButtons().forEach(b => b.disabled = on);
  }
  // panel switching
  function only(el){ for (const e of [out, view, graph]) e.classList.toggle('hidden', e !== el); }
  function showText(v){ only(out); out.textContent = (typeof v === 'string') ? v : JSON.stringify(v, null, 2); }
  function showHTML(html){ only(view); view.innerHTML = html; }
  function fail(e){
    only(out);
    const msg = (e && e.message) || String(e);
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
  async function apiGet(path){
    const r = await fetch(u(withProj(path)), { headers: { 'X-Hive-Token': TOKEN } });
    const ct = r.headers.get('content-type') || '';
    return ct.includes('json') ? r.json() : r.text();
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
  // wrap any async unit of work with busy state + error handling
  async function run(label, fn){
    setBusy(true, label);
    try { await fn(); } catch(e) { fail(e); } finally { setBusy(false); }
  }

  async function renderOverview(){
    const o = await apiGet('/api/overview');
    const chip = o.chain_ok
      ? '<span class="big ok">OK</span>' : '<span class="big bad">CHECK</span>';
    const types = Object.entries(o.by_type || {}).sort((a,b)=>b[1]-a[1])
      .map(([t,n]) => '<span class="pill">'+esc(t)+' <b>'+n+'</b></span>').join('') || '<span class="lbl">no events yet</span>';
    showHTML(
      '<h2 class="h">'+esc(o.project)+'</h2>'
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

  // --- Memory: the semantic layer (decisions/caveats/…) — intent & context ---
  // MEM_ITEMS caches the last fetch so search/sort repaint instantly without a
  // round-trip; MEM_QUERY / MEM_SORT live outside the view so the 2.5s live
  // refresh (which only rewrites #view) preserves what you typed and picked.
  let MEM_ITEMS = [];
  let MEM_QUERY = '';
  let MEM_SORT = 'new';            // 'new' = newest first, 'old' = oldest first
  async function renderMemory(){
    const path = '/api/memory' + (SESSION ? '?session='+encodeURIComponent(SESSION) : '');
    MEM_ITEMS = await apiGet(path);
    paintMemory();
  }
  function paintMemory(){
    if (!MEM_ITEMS.length) { showText(SESSION ? 'No memory in this session.'
      : 'No memory yet — append a decision / caveat / experiment_result / …'); return; }
    const q = MEM_QUERY.trim().toLowerCase();
    let items = MEM_ITEMS;
    if (q) items = items.filter(m => (
        (m.text||'')+' '+(m.type||'')+' '+(m.session||'')+' '+(m.project||'')
        +' '+((m.tags||[]).join(' '))).toLowerCase().includes(q));
    // ts is an ISO timestamp, so a lexical compare orders it chronologically.
    items = items.slice().sort((a,b) => MEM_SORT==='new'
      ? String(b.ts||'').localeCompare(String(a.ts||''))
      : String(a.ts||'').localeCompare(String(b.ts||'')));
    if (!items.length) { showHTML('<h2 class="h">No memory matches “'+esc(MEM_QUERY)+'”.</h2>'); return; }
    const showProj = (PROJECT === '*');
    const total = MEM_ITEMS.length;
    const head = q ? items.length+' of '+total+' memory item'+(total>1?'s':'')+' match “'+esc(MEM_QUERY)+'”'
                   : items.length+' memory item'+(items.length>1?'s':'');
    showHTML('<h2 class="h">'+head
      + (SESSION ? ' in '+esc(SESSION) : '')+(showProj?' across all projects':'')+'</h2><ul class="hits">'
      + items.map(m => '<li><div class="file">'
          + (showProj && m.project ? esc(m.project)+' · ' : '')
          + esc(m.type)+' · '+esc(m.ts)+' · '+esc(m.session)
          + (m.tags && m.tags.length ? ' · ['+esc(m.tags.join(', '))+']' : '')
          + (m.similar_count ? ' · <span class="dup">+'+m.similar_count+' similar</span>' : '')
          + srPill(m.origin)
          + '</div><div class="snip">'+esc(m.text)+'</div></li>').join('')
      + '</ul>');
  }
  function onMemSearch(v){ MEM_QUERY = v; paintMemory(); }
  function toggleMemSort(){
    MEM_SORT = MEM_SORT==='new' ? 'old' : 'new';
    document.getElementById('memsort').textContent = MEM_SORT==='new' ? 'newest first' : 'oldest first';
    paintMemory();
  }
  function showMemory(){
    controls.innerHTML =
      (SESSION ? '<div class="row">filtered to session <b>'+esc(SESSION)+'</b> '
        + '<button onclick="clearSession()">show all</button></div>' : '')
      + '<div class="row"><input id="memq" placeholder="search memory…" style="min-width:260px" '
        + 'value="'+escAttr(MEM_QUERY)+'" oninput="onMemSearch(this.value)">'
      + '<button id="memsort" onclick="toggleMemSort()">'
        + (MEM_SORT==='new' ? 'newest first' : 'oldest first')+'</button></div>';
    liveFn = renderMemory; run('loading memory', renderMemory);
  }
  function clearSession(){ SESSION = null; showMemory(); }

  // --- Improvements: lessons learned from past failures + manual notes ---
  // IMP_ITEMS mirrors the /api/improvements shape ({auto, manual}); query and
  // the two filter selects live outside the view so the 2.5s live refresh
  // preserves what you typed and picked. IMP_BANNER carries the result line of
  // the last Reflect run until the view is re-entered from the nav.
  let IMP_ITEMS = {auto: [], manual: []};
  let IMP_QUERY = '';
  let IMP_STATUS = 'all';
  let IMP_CAT = 'all';
  let IMP_RES = 'all';             // resolution filter set by the legend glyphs
  let IMP_TRIAGE = 'all';          // who-acts filter: all | claude | dismiss
  let IMP_BANNER = '';
  let IMP_SORT = 'critical';       // 'new' | 'critical' | 'freq'
  let IMP_DIR = 'desc';            // toggle reverses the active order
  // Manual-note expansion survives the 2.5s repaint: DOM class state is wiped
  // on every rebuild, so the expanded set is the source of truth, keyed stably.
  const IMP_EXPANDED = new Set();
  function noteKey(m){ return (m.project||'')+'|'+(m.ts||'')+'|'+(m.session||''); }
  // Resolution glyph = the OUTCOME axis (did the lesson take?); the status /
  // category pills on the same card stay as the lifecycle axis — complementary.
  const IMP_GLYPHS = {
    resolved:   ['\\u2713', 'res',   'resolved \\u2014 rule active and holding'],
    unresolved: ['\\u2717', 'unres', 'unresolved \\u2014 no rule yet, or it recurred'],
    watching:   ['\\u25cf', 'watch', 'watching \\u2014 below threshold or rule not yet tested'],
    dismissed:  ['\\u25cb', 'dis',   'dismissed \\u2014 no resolution needed']};
  function glyphSpan(res){
    const g = IMP_GLYPHS[res] || IMP_GLYPHS.watching;
    return '<span class="glyph '+g[1]+'" title="'+escAttr(g[2])+'">'+g[0]+'</span>';
  }
  // legend glyphs double as one-click resolution filters (click again to
  // clear) — the same mechanism as the Skills & plugins status legend. The
  // second row is the WHO-ACTS axis: every card is marked either "for
  // Claude" (pending agent work) or "dismissible" (clear it manually).
  const TRIAGE_LEGEND = {
    claude:  ['for Claude', 'pending agent work \\u2014 hand these to Claude to root-cause and fix'],
    dismiss: ['dismissible', 'no agent work needed \\u2014 clear these manually with dismiss']};
  function triagePill(t, hint){
    if (t === 'claude')
      return '<span class="pill triclaude" title="'+escAttr(hint||TRIAGE_LEGEND.claude[1])
        + '">\\u26a1 for Claude</span>';
    if (t === 'dismiss')
      return '<span class="pill tridism" title="'+escAttr(hint||TRIAGE_LEGEND.dismiss[1])
        + '">dismissible</span>';
    return '';
  }
  function impLegend(){
    return '<div class="legend">'+['resolved','unresolved','watching','dismissed']
      .map(r => '<span class="legitem'+(IMP_RES===r?' on':'')+'" title="click to filter" '
        + 'onclick="setImpRes('+escAttr(JSON.stringify(r))+')">'
        + glyphSpan(r)+IMP_GLYPHS[r][2]+'</span>').join(' \\u00b7 ')+'</div>'
      + '<div class="legend">'+['claude','dismiss']
      .map(t => '<span class="legitem'+(IMP_TRIAGE===t?' on':'')+'" title="click to filter \\u2014 '
        + escAttr(TRIAGE_LEGEND[t][1])+'" onclick="setImpTriage('+escAttr(JSON.stringify(t))+')">'
        + triagePill(t)+' '+TRIAGE_LEGEND[t][1]+'</span>').join(' \\u00b7 ')+'</div>';
  }
  function setImpRes(r){
    IMP_RES = (IMP_RES === r) ? 'all' : r;   // toggle off when re-clicked
    paintImprovements();
  }
  function setImpTriage(t){
    IMP_TRIAGE = (IMP_TRIAGE === t) ? 'all' : t;
    paintImprovements();
  }
  // unknown resolutions render as the watching glyph, so filter them the same way
  function resKey(a){ return IMP_GLYPHS[a.resolution] ? a.resolution : 'watching'; }
  // --- efficiency strip: failure rate (tool_failed per 100 tool actions) vs
  // the previous day / week / month / 3 months. Tiles render placeholders
  // until stats arrive, and dispositions instead of fake percentages.
  const EFF_LABELS = [['day','24h'],['week','7 days'],['month','30 days'],['quarter','3 months']];
  function effTile(lbl, s){
    let v = '\\u2014', cls = '', sub = '';
    if (s && typeof s === 'object') {
      const c = s.cur_rate == null ? '\\u2014' : s.cur_rate;
      const p = s.prev_rate == null ? '\\u2014' : s.prev_rate;
      sub = ' \\u00b7 '+c+' vs '+p+' per 100 actions';
      if (s.disposition === 'ok') {
        if (s.delta_pct < 0) { v = '\\u25bc '+Math.abs(s.delta_pct)+'% fewer failures'; cls = ' ok'; }
        else if (s.delta_pct > 0) { v = '\\u25b2 '+s.delta_pct+'% more failures'; cls = ' bad'; }
        else v = 'no change';
      }
      else if (s.disposition === 'no_change') v = 'no change';
      else if (s.disposition === 'no_baseline') v = 'no baseline';
      else if (s.disposition === 'no_current_data') v = 'no recent data';
      else if (s.disposition === 'low_sample') v = 'low sample ('+s.cur_actions+'/'+s.prev_actions+' actions)';
      else if (s.disposition === 'regression_from_zero') { v = '\\u25b2 new failures'; cls = ' bad'; }
    }
    return '<div class="card"><div class="big'+cls+'" style="font-size:15px">'+v+'</div>'
      + '<div class="lbl">vs previous '+lbl+sub+'</div></div>';
  }
  function effStrip(){
    const st = (IMP_ITEMS && IMP_ITEMS.stats && typeof IMP_ITEMS.stats === 'object')
      ? IMP_ITEMS.stats : {};
    return '<div class="cards" style="margin-bottom:12px">'
      + EFF_LABELS.map(kl => effTile(kl[1], st[kl[0]])).join('')+'</div>';
  }
  // --- sorting: three keys, one direction toggle whose label always names
  // the full (key, direction) state so reversing 'critical' reads sensibly.
  const IMP_SORT_LABELS = {
    new: ['newest first', 'oldest first'],
    critical: ['most critical first', 'least critical first'],
    freq: ['most frequent first', 'least frequent first']};
  const IMP_RANK = {unresolved:0, watching:1, resolved:2, dismissed:3};
  function impDirLabel(){ return IMP_SORT_LABELS[IMP_SORT][IMP_DIR==='desc' ? 0 : 1]; }
  function syncImpDirBtn(){ const b = document.getElementById('impdir'); if (b) b.textContent = impDirLabel(); }
  function onImpSort(v){ IMP_SORT = v; syncImpDirBtn(); paintImprovements(); }
  function toggleImpDir(){ IMP_DIR = IMP_DIR==='desc' ? 'asc' : 'desc'; syncImpDirBtn(); paintImprovements(); }
  function impCompare(a, b){
    const ts = x => String(x.last_seen || '');   // missing timestamps sort oldest
    let c;
    if (IMP_SORT === 'new') c = ts(b).localeCompare(ts(a));
    else if (IMP_SORT === 'freq')
      c = (b.occurrences||0)-(a.occurrences||0) || ts(b).localeCompare(ts(a));
    else c = (IMP_RANK[a.resolution] ?? 1)-(IMP_RANK[b.resolution] ?? 1)
      || (b.leaks||0)-(a.leaks||0)
      || (b.occurrences||0)-(a.occurrences||0)
      || ts(b).localeCompare(ts(a));
    return IMP_DIR === 'desc' ? c : -c;
  }
  async function dismissLesson(sig){
    if (!confirm('Dismiss \\u201c'+sig+'\\u201d? It stops being enforced and shows the '
        + 'gray \\u25cb (no resolution needed).')) return;
    await run('dismissing\\u2026', async () => {
      const res = await apiPost('/api/rules/dismiss', {signature: sig});
      IMP_BANNER = res.ok ? ('Dismissed '+sig) : (res.error || 'dismiss failed');
    });
    showImprovements();
  }
  async function renderImprovements(){
    const res = await apiGet('/api/improvements');
    // Surface a server error instead of rendering it as an empty "no lessons"
    // state; run() shows it, and the live tick keeps the last good view.
    if (!res || res.ok === false) throw new Error((res && res.error) || 'improvements fetch failed');
    IMP_ITEMS = res;
    paintImprovements();
  }
  function fillSelect(id, values, cur){
    const el = document.getElementById(id);
    if (!el) return;
    const opts = ['all'].concat(values);
    // a selection that vanished from fresh data stays selected (may match nothing)
    if (cur !== 'all' && !opts.includes(cur)) opts.push(cur);
    const html = opts.map(v => '<option value="'+escAttr(v)+'"'
      + (v===cur?' selected':'')+'>'+esc(v)+'</option>').join('');
    // Skip the rewrite when nothing changed so a live tick can't close an open
    // dropdown or reset an in-progress selection under the user.
    if (el.innerHTML !== html) el.innerHTML = html;
  }
  function lessonPills(a){
    let p = triagePill(a.triage, a.triage_hint);
    p += (p ? ' ' : '') + '<span class="pill">'+esc(a.status||'?')+'</span>';
    if (a.category) p += ' <span class="pill">'+esc(a.category)+'</span>';
    if (a.rule_source && a.rule_source !== 'none')
      p += ' <span class="pill">'+esc(a.rule_source)+'</span>';
    if (a.reroutes) p += ' <span class="pill" title="a previous rule failed; an '
      + 'alternative route was deployed">rerouted \\u00d7'+a.reroutes+'</span>';
    if (a.flag === 'reroute_exhausted')
      p += ' <span class="pill" title="all automatic reroute attempts spent \\u2014 '
        + 'needs a human rule">reroute exhausted</span>';
    if (!a.active_rule) p += (a.status === 'candidate'
      ? ' <span class="pill">seen '+(a.occurrences||1)+'\\u00d7 \\u2014 reflects if it recurs</span>'
      : ' <span class="pill">not yet reflected</span>');
    return p;
  }
  // A ready-to-paste prompt for a for-Claude card, so "hand it to Claude"
  // is one click instead of hand-assembling context from the card.
  function claudePrompt(a){
    return 'MindHive incident `'+(a.signature||'')+'` ('+(a.occurrences||0)
      + 'x, '+(a.resolution||'')+'). '
      + (a.excerpt ? 'Error: '+a.excerpt+' ' : '')
      + (a.lesson ? 'Lesson so far: '+a.lesson+' ' : '')
      + (a.active_rule ? 'Current rule (leaking): '+a.active_rule+' ' : 'No rule yet. ')
      + 'Please root-cause this and fix it (or write/repair the prevention rule).';
  }
  async function copyPrompt(btn){
    const a = (IMP_ITEMS.auto||[]).find(x => x.signature === btn.dataset.sig);
    if (!a) return;
    try { await navigator.clipboard.writeText(claudePrompt(a)); }
    catch (e) {
      const ta = document.createElement('textarea');
      ta.value = claudePrompt(a); document.body.appendChild(ta);
      ta.select(); document.execCommand('copy'); ta.remove();
    }
    const old = btn.textContent; btn.textContent = 'copied \\u2713';
    setTimeout(() => { btn.textContent = old; }, 1200);
  }
  function lessonCard(a, showProj){
    const head = glyphSpan(a.resolution)
      + (showProj && a.project ? esc(a.project)+' \\u00b7 ' : '')
      + esc(a.signature)+' \\u00b7 '+(a.occurrences||0)+'\\u00d7 \\u00b7 '
      + (a.sessions||0)+' session'+((a.sessions||0)===1?'':'s')
      + (a.last_seen ? ' \\u00b7 last seen '+esc(a.last_seen) : '')
      + (a.triage === 'claude'
         ? '<button class="mini copybtn" data-sig="'+escAttr(a.signature)
           + '" title="copy a ready-to-paste prompt for Claude">copy prompt</button>'
         : '')
      + (a.resolution !== 'dismissed'
         ? '<button class="mini dismissbtn" data-sig="'+escAttr(a.signature)+'">dismiss</button>'
         : '');
    let body = '';
    if (a.first_seen) body += '<div class="snip dup">first seen '+esc(a.first_seen)
      + ((a.leaks||0) ? ' \\u00b7 recurred '+a.leaks+'\\u00d7 since the '
         + (a.reroutes ? 'route deployed' : 'rule activated') : '')
      + (a.last_reroute ? ' \\u00b7 route deployed '+esc(a.last_reroute) : '')
      + '</div>';
    if (a.lesson) body += '<div class="snip"><b>Lesson:</b> '+esc(a.lesson)+'</div>';
    if (a.active_rule) body += '<div class="snip"><b>Rule:</b> '+esc(a.active_rule)+'</div>';
    if (a.root_cause) body += '<div class="snip dup">Root cause: '+esc(a.root_cause)+'</div>';
    if (a.excerpt) body += '<div class="snip dup">'+esc(a.excerpt)+'</div>';
    return '<li><div class="file">'+head+'</div><div style="margin:4px 0">'
      + lessonPills(a)+'</div>'+body+'</li>';
  }
  function noteRow(m, showProj){
    const open = IMP_EXPANDED.has(noteKey(m));
    return '<li class="noterow" data-key="'+escAttr(noteKey(m))+'" style="cursor:pointer"><div class="file">'
      + (showProj && m.project ? esc(m.project)+' \\u00b7 ' : '')
      + esc(m.ts||'')+' \\u00b7 <span class="pill">manual</span>'
      + srPill(m.origin)
      + (m.similar_count ? ' <span class="dup">+'+m.similar_count+' similar</span>' : '')
      + '</div><div class="snip'+(open?'':' one')+'">'+esc(m.text||'')+'</div></li>';
  }
  function paintImprovements(){
    const auto = IMP_ITEMS.auto || [], manual = IMP_ITEMS.manual || [];
    fillSelect('impstatus', [...new Set(auto.map(a=>a.status).filter(Boolean))], IMP_STATUS);
    fillSelect('impcat', [...new Set(auto.map(a=>a.category).filter(Boolean))], IMP_CAT);
    const banner = IMP_BANNER
      ? '<div class="row"><span class="pill">'+esc(IMP_BANNER)+'</span></div>' : '';
    // strip + legend render on every branch, including the empty states — an
    // idle store still shows its four (honest) tiles.
    const strip = effStrip() + impLegend();
    if (!auto.length && !manual.length) {
      showHTML(banner + strip + '<h2 class="h">No lessons yet \\u2014 failures are '
        + 'captured automatically; recurring ones can be reflected into lessons '
        + '(Heavy \\u2192 Reflect).</h2>');
      return;
    }
    const q = IMP_QUERY.trim().toLowerCase();
    const lessons = auto.filter(a =>
      (IMP_RES==='all' || resKey(a)===IMP_RES)
      && (IMP_TRIAGE==='all' || a.triage===IMP_TRIAGE)
      && (IMP_STATUS==='all' || a.status===IMP_STATUS)
      && (IMP_CAT==='all' || a.category===IMP_CAT)
      && (!q || ((a.signature||'')+' '+(a.lesson||'')+' '+(a.prevention_rule||'')+' '
          +(a.root_cause||'')+' '+(a.excerpt||'')+' '+(a.project||'')).toLowerCase().includes(q)))
      .sort(impCompare);
    // manual notes carry no resolution/triage, so any active filter shows lessons only
    const notes = ((IMP_RES==='all' && IMP_TRIAGE==='all') ? manual : []).filter(m => !q
      || ((m.text||'')+' '+(m.project||'')+' '+((m.tags||[]).join(' '))).toLowerCase().includes(q))
      .sort((a,b) => { const c = String(b.ts||'').localeCompare(String(a.ts||''));
                       return IMP_DIR==='desc' ? c : -c; });
    if (!lessons.length && !notes.length) {
      showHTML(banner+strip+'<h2 class="h">No lessons match your filters.</h2>');
      return;
    }
    const showProj = (PROJECT === '*');
    let html = banner + strip;
    html += '<h2 class="h">'+lessons.length+' lesson'+(lessons.length===1?'':'s')
      + (showProj?' across all projects':'')+'</h2>';
    html += lessons.length
      ? '<ul class="hits">'+lessons.map(a=>lessonCard(a, showProj)).join('')+'</ul>'
      : '<div class="dup">none match</div>';
    if (notes.length) {
      html += '<h2 class="h" style="margin-top:16px">'+notes.length+' manual note'
        + (notes.length===1?'':'s')+'</h2><ul class="hits">'
        + notes.map(m=>noteRow(m, showProj)).join('')+'</ul>';
    }
    showHTML(html);
    view.querySelectorAll('.noterow').forEach(li => li.onclick = () => {
      const k = li.dataset.key, snip = li.querySelector('.snip');
      if (IMP_EXPANDED.has(k)) IMP_EXPANDED.delete(k); else IMP_EXPANDED.add(k);
      if (snip) snip.classList.toggle('one');
    });
    view.querySelectorAll('.dismissbtn').forEach(el =>
      el.onclick = (ev) => { ev.stopPropagation(); dismissLesson(el.dataset.sig); });
    view.querySelectorAll('.copybtn').forEach(el =>
      el.onclick = (ev) => { ev.stopPropagation(); copyPrompt(el); });
  }
  // Reflect keeps the Heavy confirm (token spend) but lands back on the
  // Improvements view with an inline result banner instead of confirmAction's
  // full-panel JSON dump.
  async function runReflect(){
    if(!confirm('Reflect (learn from failures) \\u2014 runs the claude CLI once '
      + 'per recurring incident (a few per click). Continue?')) return;
    // POST and repaint inside ONE run() so the live timer can't fire in a
    // busy=false gap between the reflect call and the follow-up render.
    liveFn = renderImprovements;
    await run('reflecting\\u2026', async () => {
      const res = await apiPost('/api/rules/reflect', {});
      IMP_BANNER = res.ok
        ? ('Reflect: '+res.reflected+' reflected \\u00b7 '+res.failed
           +' failed \\u00b7 '+res.pending+' pending')
        : (res.error || 'reflection failed');
      await renderImprovements();
    });
  }
  // Active resolution: reroute red rules whose nudge failed (and reflect the
  // rule-less ones) so they can turn green. Same Heavy confirm + inline
  // banner mechanism as Reflect.
  async function runResolve(){
    if(!confirm('Resolve red (find alternative routes for failed rules, reflect '
      + 'missing ones) \\u2014 runs the claude CLI once per red lesson '
      + '(a few per click). Continue?')) return;
    liveFn = renderImprovements;
    await run('resolving\\u2026', async () => {
      const res = await apiPost('/api/rules/resolve', {});
      IMP_BANNER = res.ok
        ? ('Resolve: '+(res.rerouted||0)+' rerouted \\u00b7 '+(res.reflected||0)
           +' reflected \\u00b7 '+(res.failed||0)+' failed \\u00b7 '
           +(res.pending||0)+' still red')
        : (res.error || 'resolution failed');
      await renderImprovements();
    });
  }
  function onImpSearch(v){ IMP_QUERY = v; paintImprovements(); }
  function onImpStatus(v){ IMP_STATUS = v; paintImprovements(); }
  function onImpCat(v){ IMP_CAT = v; paintImprovements(); }
  function showImprovements(clearBanner){
    if (clearBanner) IMP_BANNER = '';
    const sortOpts = [['critical','most critical'],['new','newest'],['freq','most frequent']]
      .map(kv => '<option value="'+kv[0]+'"'+(IMP_SORT===kv[0]?' selected':'')+'>'
                 +kv[1]+'</option>').join('');
    controls.innerHTML = '<div class="row">'
      + '<input id="impq" placeholder="search lessons\\u2026" style="min-width:260px" '
      + 'value="'+escAttr(IMP_QUERY)+'" oninput="onImpSearch(this.value)">'
      + '<label>status <select id="impstatus" onchange="onImpStatus(this.value)"></select></label>'
      + '<label>category <select id="impcat" onchange="onImpCat(this.value)"></select></label>'
      + '<label>sort <select id="impsort" onchange="onImpSort(this.value)">'+sortOpts+'</select></label>'
      + '<button id="impdir" onclick="toggleImpDir()">'+impDirLabel()+'</button>'
      + '<button onclick="runResolve()" title="reroute red rules whose nudge '
      + 'failed; reflect the rule-less ones">resolve red</button>'
      + '</div>';
    liveFn = renderImprovements; run('loading improvements', renderImprovements);
  }

  // --- Sessions: rendered by the notification-center fragment (taskview.py).
  async function renderSessions(){ return renderTaskCards(); }
  function showSessions(){ controls.innerHTML=''; liveFn = renderSessions; run('loading sessions', renderSessions); }
  function openSession(sid){ SESSION = sid; showMemory(); }

  // --- Oversee: live Claude work — loop runs AND interactive sessions ---
  // Cards mirror the Improvements view (glyph + pills + expandable body);
  // expansion state survives the live refresh via OV_EXPANDED. The expanded
  // detail is a Claude-style task checklist (\\u2713 done, pulsing \\u25d0
  // doing, \\u25cb pending) in the hive palette.
  let OV_DATA = null;
  const OV_EXPANDED = new Set();
  // Interactive roadmap panel state: which cards have the roadmap open, and
  // which step nodes are expanded — both Sets so the 2.5 s live refresh
  // never collapses what the user is reading (same idiom as OV_EXPANDED).
  const RM_OPEN = new Set();
  const RM_NODE_OPEN = new Set();
  function ovStatus(r){
    if (r.kind === 'chat') return ['\\u25c9', r.online ? 'sok' : 'snone', 'chat session'];
    if (r.status === 'running') return ['\\u25b6', r.online ? 'sok' : 'snone', 'running'];
    if (r.status === 'completed') return ['\\u2714', 'sok', 'completed'];
    if (r.status === 'breaker_open') return ['\\u26a1', 'sbad', 'tripped'];
    return ['\\u25a0', 'snone', r.status || 'ended'];
  }
  // An entry is either a bare string (chat cards) or {text, is_new} (loop
  // cards, and injected chat steps). The badge is the ONLY markup emitted; the
  // text is escaped either way, so a checklist item containing HTML cannot
  // reach innerHTML unescaped.
  function ovRow(glyph, cls, entry, dim){
    const obj = (entry && typeof entry === 'object') ? entry : {text: entry};
    const pill = obj.is_new
      ? '<span class="pill rmnew" title="not in the original plan \\u2014 added '
        + 'because the record proved it outstanding">new</span> ' : '';
    return '<div class="snip'+(dim?' dup':'')+'"><span class="'+cls+'">'+glyph
      + '</span> '+pill+esc(obj.text == null ? '' : obj.text)+'</div>';
  }
  function ovGroup(items, glyph, cls, dim){
    return (items || []).map(x => ovRow(glyph, cls, x, dim)).join('');
  }
  // Skill trail on chat cards: which skills fired, when (HH:MM + relative), the
  // outcome, and the recorded "why" — the args the skill was invoked with, else
  // a prompt-gate direction, else the skill's intended use, plus the prompt it
  // was serving ("during"). Ledger facts only, never synthesized. The skill
  // name links to its Skill & Plugin detail.
  function skillAgo(ts){
    const t = Date.parse(ts || '');
    if (!t) return '';
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return s + 's ago';
    const m = Math.floor(s / 60); if (m < 60) return m + 'm ago';
    const h = Math.floor(m / 60); if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }
  function ovOpenSkill(name, plugin){
    if (!name) return;
    showSkillPanel();
    showSkillDetail(name, plugin || null);
  }
  function ovSkillRow(k){
    const name = (k.plugin ? k.plugin + ':' : '') + (k.name || '');
    const why = [k.why, k.during ? 'during: ' + k.during : '']
      .filter(Boolean).join(' \\u00b7 ');
    const ago = skillAgo(k.ts);
    return '<div class="snip ovskill"><span class="'+(k.ok === false ? 'sbad' : 'sok')
      + '">\\u2726</span> <span class="dup">'+esc((k.ts || '').slice(11, 16))
      + (ago ? ' \\u00b7 '+esc(ago) : '')+'</span> '
      + '<span class="ovsklink" style="cursor:pointer;text-decoration:underline" '
      + 'data-skill="'+escAttr(k.name || '')+'" data-plugin="'+escAttr(k.plugin || '')
      + '">'+esc(name)+'</span>'
      + (k.ok === false ? ' <span class="sbad">failed</span>' : '')
      + (why ? '<div class="dup" style="margin-left:18px">'+esc(why)+'</div>' : '')
      + '</div>';
  }
  function ovSkills(r){
    const ks = r.skills || [];
    if (!ks.length) return '';
    const hidden = (r.skills_total || ks.length) - ks.length;
    // Compact distinct-skill summary with xN counts (first-appearance order).
    const counts = {}, order = [];
    ks.forEach(k => {
      const n = (k.plugin ? k.plugin + ':' : '') + (k.name || '');
      if (!(n in counts)){ counts[n] = 0; order.push(n); }
      counts[n]++;
    });
    const summary = order.map(n => esc(n) + (counts[n] > 1 ? ' \\u00d7'+counts[n] : ''))
      .join(' \\u00b7 ');
    return '<div class="snip dup">skills used: '+summary+'</div>'
      + ks.map(ovSkillRow).join('')
      + (hidden > 0 ? '<div class="snip dup">\\u2026 '+hidden+' earlier</div>' : '');
  }
  // The roadmap panel's data source (KTD5): an explicit roadmap plan when
  // the ledger has one for this run/session id — it carries the true
  // original ask and step statuses — else a client-side synthesis from the
  // card's own progress groups, so the roadmap button is never dead.
  function rmFromCard(r){
    const steps = [];
    (r.done || []).forEach(t => steps.push({text:t, status:'done', note:''}));
    if (r.doing) steps.push({text:r.doing, status:'active', note:''});
    (r.planned || []).forEach(t => steps.push({text:t, status:'pending', note:''}));
    (r.not_yet || []).forEach(t => steps.push({text:t, status:'skipped', note:''}));
    const done = steps.filter(s => s.status === 'done').length;
    return {plan_id:r.run_id, prompt:r.title || r.run_id, title:'',
            status:'active', cause:'', synthesized:true, steps:steps,
            progress:{done:done, total:steps.length}};
  }
  function rmGlyph(st){
    if (st === 'done') return ['\\u2714', 'rmdone'];
    if (st === 'active') return ['\\u25d0', 'rmactive'];
    if (st === 'skipped') return ['\\u25cb', 'rmskip'];
    return ['\\u25cb', 'rmpend'];
  }
  function rmPanel(rm, rid){
    const steps = rm.steps || [];
    const p = rm.progress || {done:0, total:steps.length};
    const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
    let html = '<div class="rmwrap">'
      + '<div class="rmask"><span class="dup">Asked:</span> '
      + esc(rm.prompt || '')+'</div>';
    if (rm.status === 'interrupted'){
      html += '<div class="rmint">\\u26a0 interrupted'
        + (rm.cause ? ' \\u2014 '+esc(rm.cause) : '')
        + ' \\u00b7 resume: <code>hive roadmap resume '+esc(rm.plan_id)
        + '</code></div>';
    }
    html += '<div class="rmbar"><div class="rmfill" style="width:'+pct+'%"></div></div>'
      + '<div class="dup" style="font-size:11px;margin:2px 0 6px">'
      + p.done+'/'+p.total+' steps done \\u00b7 '+pct+'%'
      + (rm.synthesized ? ' \\u00b7 live view \\u2014 no declared roadmap' : '')
      + '</div>';
    html += steps.map((s, i) => {
      const g = rmGlyph(s.status);
      const key = rid+':'+i;
      const edge = (i === 0 ? ' rmfirst' : '') + (i === steps.length-1 ? ' rmlast' : '');
      return '<div class="rmstep'+edge+'" data-key="'+escAttr(key)
        + '" title="click for step detail">'
        + '<span class="rmnode '+g[1]+'">'+g[0]+'</span>'
        // Provenance, not recency: this step was not in the plan the work
        // started from — the replenishment injector added it because the record
        // proved it outstanding. It stays marked after the step is done.
        + (s.new ? '<span class="pill rmnew" title="not in the original plan '
                   + '\\u2014 added because the record proved it outstanding">'
                   + 'new</span> ' : '')
        + '<span'+(s.status === 'skipped' ? ' class="dup"' : '')+'>'+esc(s.text)+'</span>'
        + (RM_NODE_OPEN.has(key)
            ? '<div class="rmdetail">status: '+esc(s.status)
              + (s.note ? ' \\u00b7 '+esc(s.note) : '')+'</div>' : '')
        + '</div>';
    }).join('');
    return html + '</div>';
  }
  // Interrupted (or card-less) plans keep their roadmap on the board even
  // after the run/session that owned them left it — the out-of-usage marker.
  function rmPlanCard(p, showProj){
    const bad = p.status === 'interrupted';
    return '<li><div class="file"><span class="statusDot '+(bad?'sbad':'snone')+'">'
      + (bad ? '\\u26a0' : '\\u25c9')+'</span> '
      + (showProj && p.project ? esc(p.project)+' \\u00b7 ' : '')
      + esc(p.title || p.plan_id)
      + ' <span class="pill">'+esc(p.status)+'</span></div>'
      + rmPanel(p, 'plan-'+p.plan_id)+'</li>';
  }
  function ovCard(r, showProj){
    const st = ovStatus(r);
    const open = OV_EXPANDED.has(r.run_id);
    const head = '<span class="statusDot '+st[1]+'">'+st[0]+'</span>'
      + (showProj && r.project ? esc(r.project)+' \\u00b7 ' : '')
      + '<span class="ovtitle" style="cursor:pointer" title="click for progress detail">'
      + esc(r.title || r.run_id)+'</span>'
      + srPill(r.origin)
      + '<button class="mini ovrm" data-rid="'+escAttr(r.run_id)
      + '" title="the ask, the plan, and how far along it is">roadmap</button>'
      + '<button class="mini ovdel" data-rid="'+escAttr(r.run_id)
      + '" data-proj="'+escAttr(r.project||'')+'">delete</button>';
    const chat = r.kind === 'chat';
    const pills = '<span class="pill">'+esc(st[2])+'</span> '
      + '<span class="pill">'+(r.iterations||0)+(chat
          ? ' command'+((r.iterations||0)===1?'':'s')
          : ' iteration'+((r.iterations||0)===1?'':'s'))+'</span>'
      + (chat && r.skills_total ? ' <span class="pill">'+r.skills_total
          + ' skill'+(r.skills_total===1?'':'s')+'</span>' : '')
      + (chat ? '' : ' <span class="pill">$'+(r.total_cost_usd||0).toFixed(2)+'</span>')
      + (r.online ? ' <span class="pill" style="color:#46c46a;border-color:#2a5">'
                    + (chat ? 'online \\u2014 working' : 'online')+'</span>' : '');
    let body = '';
    if (open){
      body += ovGroup(r.done, '\\u2713', 'ovdone');
      if (r.doing) body += ovRow('\\u25d0', 'ovspin', r.doing);
      body += ovGroup(r.planned, '\\u25cb', 'ovpend')
            + ovGroup(r.not_yet, '\\u25cb', 'ovpend', true);
      body += ovSkills(r);
      body += '<div class="snip dup">started '+esc(r.started||'')
            + ' \\u00b7 last event '+esc(r.last_event_ts||'')+'</div>';
    }
    if (RM_OPEN.has(r.run_id)){
      body += rmPanel(r.roadmap || rmFromCard(r), r.run_id);
    }
    return '<li data-rid="'+escAttr(r.run_id)+'"><div class="file">'+head+'</div>'
      + '<div style="margin:4px 0">'+pills+'</div>'+body+'</li>';
  }
  // --- Prompt enrichment (Oversee): improve a draft prompt before spending a
  // session on it — asks the user (clarifying questions), checks the source
  // code (candidate files), injects skills, and composes an Anthropic-SOP
  // shaped prompt. Default-ON server-side; the draft, answers, and last
  // result survive the 2.5 s live refresh via module state (OV_EXPANDED
  // idiom), and the refresh skips repaints while the user is typing here.
  let EN_SET = null;       // /api/enrich/settings result
  let EN_RESULT = null;    // last /api/enrich/preview result
  let EN_PROMPT = '';      // draft prompt text
  const EN_ANSWERS = {};   // question id -> typed answer
  function enOn(){ return !EN_SET || EN_SET.enabled !== false; }
  function enChip(met){
    return met ? '<span class="enok">\\u2713</span>'
               : '<span class="enbad">\\u2717</span>';
  }
  function enResult(r){
    let html = '<div class="ensec">'
      + (r.sop||[]).map(c => '<span class="pill" title="'+escAttr(c.hint||'')+'">'
          + enChip(c.met)+' '+esc(c.label)+'</span>').join(' ')+'</div>';
    if ((r.questions||[]).length){
      html += '<div class="ensec"><b>Asking you</b> \\u2014 answers fold into '
        + 'the composed prompt (re-enrich after answering):</div>'
        + r.questions.map(q => '<div class="enq">'+esc(q.text)
          + '<br><input class="enans" data-qid="'+escAttr(q.id)
          + '" placeholder="answer\\u2026" value="'+escAttr(EN_ANSWERS[q.id]||'')
          + '"></div>').join('');
    }
    if ((r.sources||[]).length){
      html += '<div class="ensec"><b>From the source code</b>: '
        + r.sources.map(s => '<span class="skchip" title="'+escAttr(s.why||'')
            + '">'+esc(s.path)+'</span>').join(' ')+'</div>';
    }
    const sk = ((r.skills||{}).directives||[]).concat((r.skills||{}).suggestions||[]);
    if (sk.length){
      html += '<div class="ensec"><b>Skills to inject</b>: '
        + sk.map(s => '<span class="pill'+(s.provenance==='measured'?'':' est')+'">'
            + esc(s.skill)+(s.plugin?' \\u00b7 '+esc(s.plugin):'')
            + ' \\u2014 '+esc(s.provenance)+'</span>').join(' ')+'</div>';
    }
    return html
      + '<div class="ensec"><b>Enriched prompt</b> (Anthropic-SOP shaped)'
      + ' <button id="encopy" class="mini">copy</button></div>'
      + '<pre class="enout">'+esc(r.enriched_prompt||'')+'</pre>';
  }
  function enPanel(){
    if (PROJECT === '*'){
      // Never vanish silently: under HIVE MIND the panel stays visible as a
      // stub saying where the feature lives (enrichment reads ONE project
      // namespace — ledger, settings sidecar, trained manifest).
      return '<div id="enpanel" class="enwrap"><div class="file">Enrichment'
        + ' <span class="dup">pick a concrete project in the header to use '
        + 'it \\u2014 enrichment works per project</span></div></div>';
    }
    const on = enOn();
    let html = '<div id="enpanel" class="enwrap"><div class="file">'
      + 'Enrichment'
      + '<button class="mini" id="entoggle" title="on by default; the choice '
      + 'persists per project">'+(on ? 'on \\u2014 turn off' : 'off \\u2014 turn on')
      + '</button>'
      + (on ? '' : ' <span class="dup">disabled \\u2014 the hook adds nothing '
                   + 'and this panel is idle</span>')
      + '</div>';
    if (on){
      html += '<textarea id="enprompt" rows="3" placeholder="Paste a draft '
        + 'prompt \\u2014 enrichment asks you, checks the source code, injects '
        + 'skills, and composes an Anthropic-SOP prompt">'+esc(EN_PROMPT)
        + '</textarea>'
        + '<div style="margin:6px 0"><button id="enrun">Enrich</button></div>'
        + (EN_RESULT ? enResult(EN_RESULT) : '');
    }
    return html + '</div>';
  }
  async function enrichNow(){
    const ta = view.querySelector('#enprompt');
    if (ta) EN_PROMPT = ta.value;
    if (!EN_PROMPT.trim()) return;
    await run('enriching prompt\\u2026', async () => {
      const res = await apiPost('/api/enrich/preview',
                                {prompt: EN_PROMPT, answers: EN_ANSWERS});
      if (!res.ok) { showText(res); return; }
      EN_RESULT = res.result;
      paintOversee();
    });
  }
  function wireEnrich(){
    const t = view.querySelector('#entoggle');
    if (t) t.onclick = async () => {
      const res = await apiPost('/api/enrich/toggle', {enabled: !enOn()});
      if (!res.ok) { showText(res); return; }
      EN_SET = res.settings;
      if (!EN_SET.enabled) EN_RESULT = null;
      paintOversee();
    };
    const go = view.querySelector('#enrun');
    if (go) go.onclick = () => enrichNow();
    const cp = view.querySelector('#encopy');
    if (cp) cp.onclick = async () => {
      if (!EN_RESULT) return;
      try { await navigator.clipboard.writeText(EN_RESULT.enriched_prompt); }
      catch(_){ /* clipboard may be unavailable; the text stays visible */ }
      // record the act, fire-and-forget: copying is real use, previews are not
      apiPost('/api/enrich/preview',
              {prompt: EN_PROMPT, answers: EN_ANSWERS, record: true});
      cp.textContent = 'copied \\u2713';
    };
    view.querySelectorAll('.enans').forEach(el => {
      el.oninput = () => { EN_ANSWERS[el.dataset.qid] = el.value; };
    });
    const ta = view.querySelector('#enprompt');
    if (ta) ta.oninput = () => { EN_PROMPT = ta.value; };
  }
  function paintOversee(){
    if (!OV_DATA) return;
    // Online/offline strip: shown ONLY while Claude is working on a session.
    const online = OV_DATA.any_online
      ? '<div class="row"><span class="pill" style="color:#46c46a;border-color:#2a5">'
        + '<span class="blip">\\u25cf</span> online \\u2014 Claude is working</span></div>'
      : '';
    const showProj = (PROJECT === '*');
    // Card-less roadmaps (interrupted markers, plans whose run left the
    // board) render above the session list so resumable work stays visible.
    const plans = OV_DATA.plans || [];
    const strip = plans.length
      ? '<h2 class="h">'+plans.length+' roadmap'
        + (plans.length===1?'':'s')+' awaiting resume</h2>'
        + '<ul class="hits">'+plans.map(p => rmPlanCard(p, showProj)).join('')+'</ul>'
      : '';
    if (!OV_DATA.runs.length && !plans.length){
      showHTML(enPanel() + online + '<h2 class="h">No work sessions \\u2014 interactive '
        + 'Claude sessions appear here as their telemetry flows, and '
        + '<code>hive loop</code> runs join them. Finished sessions are '
        + 'removed automatically.</h2>');
      wireEnrich();
      return;
    }
    showHTML(enPanel() + online + strip
      + (OV_DATA.runs.length
        ? '<h2 class="h">'+OV_DATA.runs.length+' work session'
          + (OV_DATA.runs.length===1?'':'s')
          + (OV_DATA.dismissed ? ' <span class="dup">\\u00b7 '+OV_DATA.dismissed
             +' finished &amp; removed</span>' : '')+'</h2>'
          + '<ul class="hits">'+OV_DATA.runs.map(r => ovCard(r, showProj)).join('')+'</ul>'
        : ''));
    wireEnrich();
    view.querySelectorAll('.ovtitle').forEach(el => el.onclick = () => {
      const rid = el.closest('li').dataset.rid;
      if (OV_EXPANDED.has(rid)) OV_EXPANDED.delete(rid); else OV_EXPANDED.add(rid);
      paintOversee();
    });
    view.querySelectorAll('.ovdel').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation(); deleteRun(el.dataset.rid, el.dataset.proj);
    });
    view.querySelectorAll('.ovrm').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation();
      const rid = el.dataset.rid;
      if (RM_OPEN.has(rid)) RM_OPEN.delete(rid); else RM_OPEN.add(rid);
      paintOversee();
    });
    view.querySelectorAll('.rmstep').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation();
      const key = el.dataset.key;
      if (RM_NODE_OPEN.has(key)) RM_NODE_OPEN.delete(key); else RM_NODE_OPEN.add(key);
      paintOversee();
    });
    view.querySelectorAll('.ovsklink').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation();
      ovOpenSkill(el.dataset.skill, el.dataset.plugin);
    });
  }
  async function renderOversee(){
    // Skip this refresh tick while the user is typing in the enrichment
    // panel — a repaint would eat the caret and the click-in-progress.
    const ae = document.activeElement;
    if (ae && ae.closest && ae.closest('#enpanel')) return;
    const [res, es] = await Promise.all([apiGet('/api/oversee'),
                                         apiGet('/api/enrich/settings')]);
    if (!res || res.ok === false) throw new Error((res && res.error) || 'oversee fetch failed');
    OV_DATA = res;
    EN_SET = (es && typeof es.enabled === 'boolean') ? es : {enabled: true};
    paintOversee();
  }
  async function deleteRun(rid, proj){
    if (!confirm('Delete this session from Oversee? Its events stay in the '
        + 'ledger (append-only), it just stops being shown.')) return;
    await run('deleting session\\u2026', async () => {
      const body = {run_id: rid};
      if (PROJECT === '*' && proj) body.project = proj;
      const res = await apiPost('/api/oversee/dismiss', body);
      if (!res.ok) { showText(res); return; }
      await renderOversee();
    });
  }
  function showOversee(){ controls.innerHTML=''; liveFn = renderOversee; run('loading oversee', renderOversee); }
  function showOverview(){ controls.innerHTML=''; liveFn = renderOverview; run('loading overview', renderOverview); }

  // --- mandatory code change-log (Log view) ---
  function logBadge(cat){
    const colors = {update:'#4a90d9', minor_change:'#6aa84f', deletion:'#d94a4a',
                    reduction:'#d9a44a', major_change:'#b04ad9', replacement:'#4ad9c8'};
    const c = colors[cat] || '#888';
    return '<span class="pill" style="color:'+c+';border-color:'+c+'">'+esc(cat||'?')+'</span>';
  }
  async function renderLog(){
    const res = await apiGet('/api/changelog');
    if (!res || !Array.isArray(res.entries)) throw new Error((res && res.error) || 'changelog fetch failed');
    const pend = res.pending && res.pending.files && res.pending.files.length
      ? '<div class="row"><span class="pill" style="color:#e0a030;border-color:#a67a20">'
        + '\\u26a0 ' + res.pending.files.length + ' unlogged file(s) \\u2014 record the mandatory entry: '
        + '<code>hive log &lt;category&gt; "brief explanation"</code></span></div>'
      : '';
    if (!res.entries.length){
      showHTML(pend + '<h2 class="h">No change-log entries yet \\u2014 every change to '
        + 'mindhive code must be recorded with '
        + '<code>hive log &lt;category&gt; "brief explanation"</code> ('
        + esc((res.categories || []).join(', ')) + ').</h2>');
      return;
    }
    const showProj = (PROJECT === '*');
    showHTML(pend + '<h2 class="h">'+res.entries.length+' change-log entr'
      + (res.entries.length===1?'y':'ies')+'</h2>'
      + '<ul class="hits">' + res.entries.map(e =>
        '<li><div class="file">'+logBadge(e.category)+' '+esc(e.text)+'</div>'
        + ((e.files||[]).length ? '<div class="snip">'+(e.files||[]).map(esc).join(' \\u00b7 ')+'</div>' : '')
        + '<div class="snip dup">'+esc(e.ts||'')+' \\u00b7 session '+esc(e.session||'')
        + (showProj && e.project ? ' \\u00b7 '+esc(e.project) : '') +'</div></li>').join('')
      + '</ul>');
  }
  function showLog(){ controls.innerHTML=''; liveFn = renderLog; run('loading change log', renderLog); }

  function sopBadge(action){
    const colors = {add:'#6aa84f', update:'#4a90d9', 'delete':'#d94a4a'};
    const c = colors[action] || '#888';
    return '<span class="pill" style="color:'+c+';border-color:'+c+'">'+esc(action||'?')+'</span>';
  }
  async function renderSops(){
    const res = await apiGet('/api/sops');
    if (!res || !Array.isArray(res.active)) throw new Error((res && res.error) || 'sop fetch failed');
    const showProj = (PROJECT === '*');
    if (!res.active.length && !res.history.length){
      showHTML('<h2 class="h">No SOPs yet \\u2014 record standing procedures with '
        + '<code>hive sop add &lt;id&gt; --title "\\u2026" --file &lt;body.md&gt;</code>. '
        + 'Active SOPs auto-load into every session via STATE.md / MEMORY.md.</h2>');
      return;
    }
    const act = res.active.map((r, i) =>
      '<li><div class="file">SOP '+(i+1)+': '+esc(r.title)
      + ' <span class="pill">'+esc(r.sop_id)+' v'+r.version+'</span>'
      + (showProj && r.project ? ' <span class="pill">'+esc(r.project)+'</span>' : '')
      + '</div><div class="snip" style="white-space:pre-wrap">'+esc(r.text)+'</div>'
      + '<div class="snip dup">created '+esc(r.created||'')+' \\u00b7 updated '+esc(r.updated||'')+'</div></li>').join('');
    const hist = res.history.map(e =>
      '<li><div class="file">'+sopBadge(e.action)+' '+esc(e.sop_id)
      + (e.title ? ' \\u2014 '+esc(e.title) : '') + '</div>'
      + '<div class="snip dup">'+esc(e.ts||'')+' \\u00b7 session '+esc(e.session||'')
      + (showProj && e.project ? ' \\u00b7 '+esc(e.project) : '') + '</div></li>').join('');
    showHTML('<h2 class="h">'+res.active.length+' active SOP'
      + (res.active.length===1?'':'s')+' (auto-loaded each session)</h2>'
      + '<ul class="hits">'+act+'</ul>'
      + '<h2 class="h">History ('+res.history.length+' lifecycle event'
      + (res.history.length===1?'':'s')+')</h2>'
      + '<ul class="hits">'+hist+'</ul>');
  }
  function showSops(){ controls.innerHTML=''; liveFn = renderSops; run('loading SOPs', renderSops); }

  async function renderState(){ showText(await apiGet('/api/state')); }
  function showState(){ controls.innerHTML=''; liveFn = renderState; run('loading state', renderState); }
  function showBriefing(){ controls.innerHTML=''; liveFn = null; run('loading briefing', async () => showText(await apiGet('/api/briefing'))); }

  function showRecall(){
    liveFn = null;
    controls.innerHTML = '<div class="row"><input id="q" placeholder="search query" style="min-width:260px">'
      + '<label><input type="checkbox" id="sem"> semantic</label>'
      + '<button onclick="runRecall()">Search</button></div>';
    showText('Enter a query and search.');
    const q = document.getElementById('q');
    q.focus();
    q.addEventListener('keydown', e => { if (e.key === 'Enter') runRecall(); });
  }
  function runRecall(){
    const q = document.getElementById('q').value;
    const sem = document.getElementById('sem').checked ? '1' : '0';
    run('searching', async () => {
      const hits = await apiGet('/api/recall?q='+encodeURIComponent(q)+'&semantic='+sem);
      if (!hits.length) { showText('No matches for "'+q+'".'); return; }
      showHTML('<h2 class="h">'+hits.length+' result'+(hits.length>1?'s':'')+'</h2><ul class="hits">'
        + hits.map(h => '<li><div class="file">'+esc(h.file||'')+'</div><div class="snip">'+esc(h.snippet||'')+'</div></li>').join('')
        + '</ul>');
    });
  }

  // Graph is a one-shot heavy render (not live-refreshed): fetch once and hand
  // the self-contained page to the iframe. Semantic links are the expensive,
  // opt-in build (an embedding search per event) so they stay off by default;
  // ticking the box reloads with ?semantic=1.
  function showGraph(){
    liveFn = null;
    let semantic = false;
    controls.innerHTML = '<label class="ctl" style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">'
      + '<input type="checkbox" id="gsem"> semantic links</label>';
    const load = () => run('building graph', async () => {
      const html = await apiGet('/api/graph' + (semantic ? '?semantic=1' : ''));
      only(graph); graph.srcdoc = html;
    });
    const cb = document.getElementById('gsem');
    if (cb) cb.addEventListener('change', () => { semantic = cb.checked; load(); });
    load();
  }

  // Workspace views (MindParallel / MindSwarm / Self-Reliant) are full pages
  // served by the server and loaded by URL — srcdoc must be cleared first or
  // it wins over src. 'research' lives under its own /api/research family.
  function showWorkspace(view){
    controls.innerHTML=''; liveFn = null;
    only(graph);
    graph.removeAttribute('srcdoc');
    graph.src = u(withProj(view === 'research' ? '/api/research/page'
                                               : '/api/workspace/'+view));
  }

  // Self-Reliant (an iframe) can ask the parent to open the Memory view filtered
  // to a PRD's backing events — the section's link into the memory it wrote. Only
  // trust messages from our own iframe on the same origin (never arbitrary posts).
  window.addEventListener('message', function(ev){
    if (ev.origin !== location.origin) return;
    if (!graph || ev.source !== graph.contentWindow) return;
    const d = ev.data;
    if (!d || d.type !== 'mh-self-reliant' || d.action !== 'show-memory') return;
    MEM_QUERY = String(d.query == null ? '' : d.query).slice(0, 200);
    showMemory();
  });

  function showAppend(){
    liveFn = null;
    controls.innerHTML = '<div class="row"><select id="etype">'
      + ['decision','caveat','experiment_result','next_step','open_question','state_set']
        .map(t=>'<option>'+t+'</option>').join('')
      + '</select></div>'
      + '<div class="row"><textarea id="etext" rows="3" style="width:min(100%,520px)" placeholder="one line"></textarea></div>'
      + '<div class="row"><input id="etags" placeholder="tags,comma,separated">'
      + '<button onclick="runAppend()">Append</button></div>';
    showText('Fill the form and append a memory event.');
  }
  function runAppend(){
    const body = { type: document.getElementById('etype').value,
      payload: document.getElementById('etext').value, tags: document.getElementById('etags').value };
    if (!body.payload.trim()) { showText('Nothing to append — type a line first.'); return; }
    run('appending', async () => {
      const res = await apiPost('/api/append', body);
      if (res.ok) { showOverview(); } else { showText(res); }
    });
  }

  // --- Skill & Plugin: the viewing panel over the skill database (U7) ---
  // Same mechanism as Improvements: renderPlugins fetches everything one poll
  // cycle needs in a single parallel burst, paintPlugins filters/renders
  // instantly from the in-memory caches. Query + filters live OUTSIDE the view
  // (in the controls bar) so the 2.5s live refresh — which only rewrites
  // #view — preserves what you typed and picked. Cards render COLLAPSED by
  // default (300+ skills must not paint as a wall of open cards); the expanded
  // set is keyed by skill name and survives every repaint, exactly like
  // IMP_EXPANDED.
  let PLUG_INV = {plugins: [], local_skills: [], counts: {}};
  let PLUG_STATUS = {};             // /api/skills/status: {name: 'ok'|'fail'}
  let PLUG_COUNTS = {};             // /api/skills/status: {name: total invoke count}
  let PLUG_STATS = {};              // /api/skills/stats: per-skill telemetry
  let PLUG_REC = {coverage: {measured: 0, total: 0}, recommendations: []};
  let PLUG_RULES = {};              // 'skill:<name>' -> improvements auto lesson
  let PLUG_QUERY = '';
  let PLUG_STATUSF = 'all';         // all | ok | fail | uncalled
  let PLUG_SRC = 'all';             // source plugin filter ('local' = standalone)
  let PLUG_MEASURED = false;        // measured-only toggle
  let PLUG_SORT = 'status';         // status | name | source
  let PLUG_DIR = 'asc';             // toggle reverses the active order
  const PLUG_EXPANDED = new Set();  // expanded cards, keyed by skill name
  const PLUG_DETAILS = {};          // name -> /api/skills/detail payload (lazy)
  const PLUG_HIST = {};             // name -> /api/skills/history list
  const PLUG_LASTCALL = {};         // name -> last invoke result from this tab
  const PLUG_REFLECT = {};          // name -> last reflect_one result line
  const PLUG_CALLING = new Set();   // in-flight invokes (button spinner state)
  const PLUG_REFLECTING = new Set();  // in-flight reflect_one calls
  let PLUG_GEN = 0;                 // poll generation: stale fetches discard themselves
  function statusDot(status){
    const cls = status === 'ok' ? 'sok' : (status === 'fail' ? 'sbad' : 'snone');
    const title = status === 'ok' ? 'last call succeeded'
      : (status === 'fail' ? 'last call FAILED \\u2014 see Needed changes' : 'never called');
    return '<span class="statusDot '+cls+'" title="'+title+'">\\u25cf</span>';
  }
  const PLUG_STATUS_LEGEND = {
    ok:       ['sok',  'green \\u2014 last call succeeded'],
    fail:     ['sbad', 'red \\u2014 last call failed'],
    uncalled: ['snone','grey \\u2014 never called']};
  // legend items double as one-click status filters (click again to clear);
  // the trailing badge explains `estimated` provenance, which isn't a filter.
  function plugLegend(){
    return '<div class="legend">'+['ok','fail','uncalled'].map(k =>
      '<span class="legitem'+(PLUG_STATUSF===k?' on':'')+'" title="click to filter" '
      + 'onclick="setPlugStatus('+escAttr(JSON.stringify(k))+')">'
      + '<span class="statusDot '+PLUG_STATUS_LEGEND[k][0]+'">\\u25cf</span>'
      + PLUG_STATUS_LEGEND[k][1]+'</span>').join(' \\u00b7 ')
      + ' \\u00b7 <span class="pill est">estimated</span> ranked by description breadth, not measured calls'
      + '</div>';
  }
  function setPlugStatus(k){
    PLUG_STATUSF = (PLUG_STATUSF === k) ? 'all' : k;   // toggle off when re-clicked
    const sel = document.getElementById('plugstatus'); if (sel) sel.value = PLUG_STATUSF;
    paintPlugins();
  }
  // stat strip: plugin/skill counts, measured coverage from the recommend
  // payload, and the overall completion rate — one cheap pass over the stats
  // map, shown honestly ('—' until anything has been called at all).
  function plugStrip(){
    const c = PLUG_INV.counts || {};
    const cov = PLUG_REC.coverage || {};
    let calls = 0, ok = 0;
    Object.values(PLUG_STATS).forEach(s => { calls += s.calls||0; ok += s.ok||0; });
    const rate = calls ? Math.round(ok/calls*100)+'%' : '\\u2014';
    const measured = cov.measured ?? 0;
    const total = cov.total ?? (c.skills_total ?? 0);
    // Cold-start honesty: before any skill has been called, show WHY the tiles
    // read zero rather than a bare '0/0' that looks broken.
    const rateLbl = calls ? ('completion rate \\u00b7 '+ok+'/'+calls+' calls')
                          : 'completion rate \\u00b7 no calls recorded yet';
    const measuredLbl = measured ? 'measured (\\u22653 calls)'
                                 : 'measured \\u00b7 call skills to start measuring';
    return '<div class="cards" style="margin-bottom:12px">'
      + '<div class="card"><div class="big">'+(c.plugins ?? 0)+'</div><div class="lbl">plugins installed</div></div>'
      + '<div class="card"><div class="big ok">'+(c.skills_total ?? 0)+'</div><div class="lbl">skills total</div></div>'
      + '<div class="card"><div class="big">'+measured+' of '+total+'</div><div class="lbl">'+measuredLbl+'</div></div>'
      + '<div class="card"><div class="big">'+rate+'</div><div class="lbl">'+rateLbl+'</div></div>'
      + '</div>';
  }
  const PLUG_SORT_LABELS = {
    status: ['needs-attention first', 'healthy first'],
    name:   ['name A\\u2192Z', 'name Z\\u2192A'],
    source: ['by plugin A\\u2192Z', 'by plugin Z\\u2192A']};
  const PLUG_STATUS_RANK = {fail: 0, uncalled: 1, ok: 2};
  function plugDirLabel(){ return PLUG_SORT_LABELS[PLUG_SORT][PLUG_DIR==='asc' ? 0 : 1]; }
  function syncPlugDirBtn(){ const b = document.getElementById('plugdir'); if (b) b.textContent = plugDirLabel(); }
  function onPlugSort(v){ PLUG_SORT = v; syncPlugDirBtn(); paintPlugins(); }
  function togglePlugDir(){ PLUG_DIR = PLUG_DIR==='asc' ? 'desc' : 'asc'; syncPlugDirBtn(); paintPlugins(); }
  function plugCompare(a, b){
    let c;
    if (PLUG_SORT === 'name') c = (a.name||'').localeCompare(b.name||'');
    else if (PLUG_SORT === 'source')
      c = (a.plugin||'local').localeCompare(b.plugin||'local') || (a.name||'').localeCompare(b.name||'');
    else c = (PLUG_STATUS_RANK[a.status] ?? 1)-(PLUG_STATUS_RANK[b.status] ?? 1)
      || (a.name||'').localeCompare(b.name||'');
    return PLUG_DIR === 'asc' ? c : -c;
  }
  // one flat entry per installed skill, joined with its telemetry and its
  // recommend-engine entry (primary or alternative) — the SINGLE in-memory
  // object list both the Recommended-now section and the full card list render
  // from, so a Call from either place updates both on the same repaint.
  function plugRecIndex(){
    const map = {};
    (PLUG_REC.recommendations || []).forEach(r => {
      if (!(r.name in map)) map[r.name] = r;
      (r.alternatives || []).forEach(a => { if (!(a.name in map)) map[a.name] = a; });
    });
    return map;
  }
  function plugSkillEntries(){
    const missing = new Set((PLUG_INV.plugins||[]).filter(p => p.present === false).map(p => p.name));
    const recMap = plugRecIndex();
    const out = [];
    const push = (name, plugin, trigger) => {
      const st = PLUG_STATS[name] || null;
      const rec = recMap[name] || null;
      out.push({name: name, plugin: plugin, trigger: trigger || '',
        status: (st && st.last_outcome) || PLUG_STATUS[name] || 'uncalled',
        stats: st, rec: rec,
        estimated: !!(rec && rec.provenance === 'estimated'),
        measured: rec ? rec.provenance === 'measured'
                      : !!(st && st.calls >= 3 && st.completion_rate != null),
        missing: plugin ? missing.has(plugin) : false});
    };
    (PLUG_INV.local_skills || []).forEach(s => push(s.name, null, s.trigger));
    (PLUG_INV.plugins || []).forEach(p => (p.skills || []).forEach(s => push(s.name, p.name, s.trigger)));
    return out;
  }
  function indexSkillRules(auto){
    PLUG_RULES = {};
    (auto || []).forEach(a => {
      const sig = a.signature || '';
      if (sig.indexOf('skill:') === 0 && !(sig in PLUG_RULES)) PLUG_RULES[sig] = a;
    });
  }
  // invoke-count badge right after the skill name — how many times this skill
  // has been called through MindHive (ok + failed), so callability reads off
  // the card list without expanding. Counts come from /api/skills/status;
  // per-skill telemetry (stats.calls) is the fallback when counts lag a poll.
  function callCount(name){
    const n = PLUG_COUNTS[name] || (PLUG_STATS[name] && PLUG_STATS[name].calls) || 0;
    return n ? '<span class="callcount" title="invoked '+n+' time'+(n===1?'':'s')
      + ' via MindHive">'+n+'\\u00d7</span>' : '';
  }
  // one poll cycle = one parallel fetch burst: inventory + status + stats +
  // recommend + improvements (for the skill:* prevention-rule index) + the
  // call history of every expanded card. Auxiliary fetches degrade to their
  // last good cache so a single flaky endpoint can't blank the view.
  // Each cycle stamps a generation before fetching; a cycle that resolves
  // after a newer one started (or after invokeSkill wrote fresher data)
  // discards its results — no cache writes, no paint.
  async function renderPlugins(){
    const gen = ++PLUG_GEN;
    const expanded = Array.from(PLUG_EXPANDED);
    const res = await Promise.all([
      apiGet('/api/plugins/inventory'),
      apiGet('/api/skills/status').catch(() => null),
      apiGet('/api/skills/stats').catch(() => null),
      apiGet('/api/skills/recommend').catch(() => null),
      apiGet('/api/improvements').catch(() => null),
    ].concat(expanded.map(n =>
      apiGet('/api/skills/history?name='+encodeURIComponent(n)+'&limit=25').catch(() => null))));
    if (gen !== PLUG_GEN) return;   // a newer cycle owns the caches now
    PLUG_INV = (res[0] && res[0].inventory) || {plugins: [], local_skills: [], counts: {}};
    if (res[1]) { PLUG_STATUS = res[1].status || {}; PLUG_COUNTS = res[1].counts || {}; }
    if (res[2] && res[2].stats) PLUG_STATS = res[2].stats;
    if (res[3] && res[3].recommendations) PLUG_REC = res[3];
    if (res[4] && res[4].auto) indexSkillRules(res[4].auto);
    expanded.forEach((n, i) => { if (res[5+i]) PLUG_HIST[n] = res[5+i]; });
    if (liveFn === renderPlugins) paintPlugins();
  }
  function fmtRate(r){ return r == null ? '\\u2014' : Math.round(r*100)+'%'; }
  function skMetrics(st){
    if (!st || !st.calls)
      return '<div class="skmeta"><b>Metrics</b> <span class="lbl">\\u2014 (uncalled)</span></div>';
    return '<div class="skmeta"><b>Metrics</b> <span class="lbl">'
      + st.calls+' call'+(st.calls===1?'':'s')+' \\u00b7 '+(st.ok||0)+' ok \\u00b7 '
      + (st.fail||0)+' fail \\u00b7 completion '+fmtRate(st.completion_rate)
      + ' \\u00b7 last outcome '+esc(st.last_outcome||'\\u2014')
      + ' \\u00b7 last used '+esc(st.last_used||'\\u2014')+'</span></div>';
  }
  // "Needed changes" = the active prevention rule reflected from this skill's
  // skill:<name> incident (R6); an unreflected incident is named as such.
  function skNeeded(name){
    const r = PLUG_RULES['skill:'+name];
    if (!r) return '';
    if (r.active_rule)
      return '<div class="skmeta"><b>Needed changes</b><div class="snip">'+esc(r.active_rule)+'</div>'
        + (r.lesson ? '<div class="snip dup">Lesson: '+esc(r.lesson)+'</div>' : '')+'</div>';
    return '<div class="skmeta"><b>Needed changes</b> <span class="lbl">failure recorded '
      + (r.occurrences||1)+'\\u00d7 \\u2014 not yet reflected into a rule</span></div>';
  }
  function skDescription(name){
    const d = PLUG_DETAILS[name];
    if (!d) return '<div class="skmeta"><span class="lbl">loading description\\u2026</span></div>';
    if (d.missing) return '<div class="skmeta"><span class="lbl">no SKILL.md found for this skill</span></div>';
    return '<div class="skmeta"><b>Intended use</b><div class="snip">'+esc(d.intended_use||'\\u2014')+'</div></div>'
      + '<div class="skmeta"><b>Added</b> <span class="lbl">'+esc(d.added_at||'unknown')
      + (d.source ? ' \\u00b7 '+esc(d.source) : '')+'</span></div>'
      + '<div class="skmeta"><b>Full description</b><pre class="skbody">'+esc(d.body||'')+'</pre></div>';
  }
  // Call history: every past invoke of this skill (green ✔ / red ✖), newest
  // first, from the same ledger events that drive the status dot. null/undefined
  // means "not fetched yet" — distinct from an empty (never called) history.
  function callHistoryHTML(hist){
    if (hist == null)
      return '<div class="skmeta"><b>Call history</b> <span class="lbl">loading\\u2026</span></div>';
    if (!hist.length)
      return '<div class="skmeta"><b>Call history</b> <span class="lbl">never called</span></div>';
    return '<div class="skmeta"><b>Call history</b> <span class="lbl">'+hist.length
      + ' call'+(hist.length===1?'':'s')+'</span><ul class="hits">'
      + hist.map(h => '<li><div class="file">'
          + (h.ok ? '<span class="ok">\\u2714 ok</span>' : '<span class="err">\\u2716 failed</span>')
          + ' \\u00b7 '+esc(h.ts||'')+(h.session ? ' \\u00b7 '+esc(h.session) : '')+'</div>'
          + (h.text ? '<div class="snip one">'+esc(h.text)+'</div>' : '')+'</li>').join('')
      + '</ul></div>';
  }
  function lastCallHTML(name){
    const lc = PLUG_LASTCALL[name];
    if (!lc) return '';
    return '<div class="skmeta"><b>Last call (this tab)</b> '
      + (lc.ok ? '<span class="ok">\\u2714 ok</span>'
               : '<span class="err">\\u2716 failed'+(lc.exit != null ? ' (exit '+esc(lc.exit)+')' : '')+'</span>')
      + '<pre class="skbody">'+esc(lc.excerpt||'(no output)')+'</pre></div>';
  }
  // Call button states: suppressed for skills of a plugin that's missing on
  // disk, spinner while ITS invoke is in flight (state lives in PLUG_CALLING
  // so the 2.5s repaint can't wipe it), normal otherwise.
  function callBtn(e){
    if (e.missing) return '';
    if (PLUG_CALLING.has(e.name))
      return '<button class="mini callbtn" disabled>\\u27f3 calling\\u2026</button>';
    return '<button class="mini callbtn skcall" data-name="'+escAttr(e.name)
      + '" data-plugin="'+escAttr(e.plugin||'')+'" '
      + 'title="run this skill via a headless Claude">\\u25b6 call</button>';
  }
  function modePill(mode){
    if (mode === 'auto')
      return ' <span class="pill" style="color:#46c46a;border-color:#2a5" '
        + 'title="proven track record \\u2014 may be applied automatically">auto</span>';
    return ' <span class="pill est" title="unproven \\u2014 suggested only, '
      + 'needs your approval before it runs">suggest</span>';
  }
  function skBadges(e){
    let b = ' \\u00b7 <span class="pill">'+esc(e.plugin||'local')+'</span>';
    if (e.estimated) b += ' <span class="pill est" title="score used the description-breadth prior \\u2014 no measured completion rate yet">estimated</span>';
    if (e.rec && e.rec.execution_mode) b += modePill(e.rec.execution_mode);
    if (e.rec && e.rec.outcome_category)
      b += ' <span class="pill">'+esc(e.rec.outcome_category)+'</span>';
    if (e.rec && e.rec.try_to_measure) b += ' <span class="pill est">try to measure</span>';
    if (e.missing) b += ' <span class="err">plugin missing on disk</span>';
    return b;
  }
  function skillCard(e){
    const open = PLUG_EXPANDED.has(e.name);
    let html = '<li class="skrow" data-name="'+escAttr(e.name)+'">'
      + '<div class="file skhead" data-name="'+escAttr(e.name)+'" data-plugin="'
      + escAttr(e.plugin||'')+'" style="cursor:pointer" title="click to '
      + (open?'collapse':'expand')+'">'
      + statusDot(e.status)+esc(e.name)+callCount(e.name)+skBadges(e)+callBtn(e)+'</div>';
    if (!open)
      return html + (e.trigger ? '<div class="snip one">'+esc(e.trigger)+'</div>' : '') + '</li>';
    html += skDescription(e.name) + skMetrics(e.stats) + skNeeded(e.name);
    if (e.status === 'fail' && !e.missing)
      html += '<div class="skmeta">'+(PLUG_REFLECTING.has(e.name)
        ? '<button class="mini" disabled>\\u27f3 reflecting\\u2026</button>'
        : '<button class="mini skreflect" data-name="'+escAttr(e.name)+'">\\u21bb reflect this failure into a rule</button>')
        + (PLUG_REFLECT[e.name] ? ' <span class="pill">'+esc(PLUG_REFLECT[e.name])+'</span>' : '')+'</div>';
    return html + lastCallHTML(e.name) + callHistoryHTML(PLUG_HIST[e.name]) + '</li>';
  }
  // Recommended now: the deterministic engine's ranked output (U6), rendered
  // from the same entry objects as the full list below. Primaries carry their
  // alternatives (overlap-group losers) and try-to-measure exploration flags,
  // every one labeled with measured/estimated provenance.
  const PLUG_REC_TOP = 5;
  function recChip(name){
    return '<span class="skchip" data-jump="'+escAttr(name)
      + '" title="jump to this skill\\u2019s card">'+esc(name)+'</span>';
  }
  function recSection(byName){
    const recs = (PLUG_REC.recommendations || []).slice(0, PLUG_REC_TOP);
    let html = '';
    if (recs.length){
      html += '<h2 class="h">Recommended now</h2><ul class="hits">'
        + recs.map(r => {
            const e = byName[r.name];
            return '<li><div class="file">'+statusDot(e ? e.status : 'uncalled')
              + recChip(r.name)
              + ' \\u00b7 <span class="pill'+(r.provenance==='estimated'?' est':'')+'">'+esc(r.provenance)+'</span>'
              + modePill(r.execution_mode)
              + ' \\u00b7 score '+esc(r.score)
              + (r.try_to_measure ? ' <span class="pill est">try to measure</span>' : '')
              + (e ? callBtn(e) : '')+'</div>'
              + (r.reason ? '<div class="snip dup">'+esc(r.reason)+'</div>' : '')
              + (r.description ? '<div class="snip">intended use: '+esc(r.description)+'</div>' : '')
              + ((r.alternatives||[]).length ? '<div class="snip">alternatives: '
                  + r.alternatives.map(a => recChip(a.name)+' <span class="dup">('+esc(a.provenance)
                      + (a.try_to_measure ? ' \\u00b7 try to measure' : '')+')</span>').join(' \\u00b7 ')+'</div>' : '')
              + '</li>';
          }).join('')+'</ul>';
    }
    // Discontinued: skills stopped by the improvements linkage (active
    // prevention rule + failure streak) — shown with WHY so recovery is clear.
    const disc = PLUG_REC.discontinued || [];
    if (disc.length){
      html += '<h2 class="h">Discontinued <span class="lbl">excluded until the '
        + 'incident resolves or a successful call breaks the streak</span></h2>'
        + '<ul class="hits">'+disc.map(d =>
            '<li><div class="file"><span class="statusDot sbad">\\u2716</span>'
            + recChip(d.name)+' \\u00b7 <span class="pill">'+d.fail_streak
            + ' consecutive failures</span></div>'
            + '<div class="snip"><b>Rule:</b> '+esc(d.rule)+'</div></li>').join('')
        + '</ul>';
    }
    return html;
  }
  // Plugins section = a COMPACT INDEX, not a second card render: name, version,
  // aggregate dot (any fail → red, else any ok → green), and skill-name chips
  // that jump to the skill's card above. Missing-on-disk plugins keep their err
  // badge (their skills have no cards — the registry lists them but the disk
  // scan found nothing to call).
  function aggDot(names, statusByName){
    const sts = names.map(n => statusByName[n] || 'uncalled');
    return statusDot(sts.includes('fail') ? 'fail' : (sts.includes('ok') ? 'ok' : 'uncalled'));
  }
  function pluginIndex(entries){
    const statusByName = {};
    entries.forEach(e => {
      if (!(e.name in statusByName) || e.status === 'fail') statusByName[e.name] = e.status;
    });
    const rows = (PLUG_INV.plugins || []).map(p => {
      const names = (p.skills || []).map(s => s.name);
      return '<li><div class="file">'+aggDot(names, statusByName)+esc(p.name)+' '+esc(p.version)
        + ' \\u00b7 '+esc(p.marketplace)
        + (p.present === false ? ' \\u00b7 <span class="err">missing on disk</span>' : '')
        + ' \\u00b7 '+names.length+' skill'+(names.length===1?'':'s')+'</div>'
        + (names.length ? '<div class="snip">'+names.map(recChip).join(' ')+'</div>' : '')
        + '</li>';
    });
    const loc = (PLUG_INV.local_skills || []).map(s => s.name);
    if (loc.length)
      rows.push('<li><div class="file">'+aggDot(loc, statusByName)
        + 'local skills \\u00b7 '+loc.length+' skill'+(loc.length===1?'':'s')+'</div>'
        + '<div class="snip">'+loc.map(recChip).join(' ')+'</div></li>');
    if (!rows.length) return '';
    return '<div class="lbl" style="margin-top:16px;margin-bottom:6px">plugins '
      + '\\u2014 click a skill name to jump to its card</div><ul class="hits">'+rows.join('')+'</ul>';
  }
  function paintPlugins(){
    const entries = plugSkillEntries();
    const byName = {};
    entries.forEach(e => { if (!(e.name in byName)) byName[e.name] = e; });
    fillSelect('plugmarket', [...new Set(entries.map(e => e.plugin || 'local'))], PLUG_SRC);
    const strip = plugStrip() + plugLegend();
    if (!entries.length){
      showHTML(strip + '<h2 class="h">No skills or plugins installed.</h2>');
      return;
    }
    const q = PLUG_QUERY.trim().toLowerCase();
    // search matches name + short description + plugin name (Improvements'
    // query pattern); default sort = needs attention: fail → uncalled → ok.
    const list = entries.filter(e =>
        (PLUG_STATUSF === 'all' || e.status === PLUG_STATUSF)
        && (PLUG_SRC === 'all' || (e.plugin || 'local') === PLUG_SRC)
        && (!PLUG_MEASURED || e.measured)
        && (!q || ((e.name||'')+' '+(e.trigger||'')+' '+(e.plugin||'local')).toLowerCase().includes(q)))
      .sort(plugCompare);
    let html = strip + recSection(byName);
    html += '<h2 class="h">All skills <span class="lbl">'+list.length+' of '
      + entries.length+' shown</span></h2>';
    html += list.length
      ? '<ul class="hits">'+list.map(skillCard).join('')+'</ul>'
      : '<div class="dup">none match your filters</div>';
    html += pluginIndex(entries);
    showHTML(html);
    view.querySelectorAll('.skhead').forEach(el =>
      el.onclick = () => toggleSkill(el.dataset.name, el.dataset.plugin || null));
    view.querySelectorAll('.skcall').forEach(el =>
      el.onclick = ev => { ev.stopPropagation(); invokeSkill(el.dataset.name, el.dataset.plugin || null); });
    view.querySelectorAll('.skreflect').forEach(el =>
      el.onclick = ev => { ev.stopPropagation(); reflectFailure(el.dataset.name); });
    view.querySelectorAll('[data-jump]').forEach(el =>
      el.onclick = ev => { ev.stopPropagation(); jumpToSkill(el.dataset.jump); });
  }
  // Expanding a card shows the skill detail IN the card: the SKILL.md body and
  // the call history load lazily on first expand and are cached; the history
  // cache is then refreshed on every poll while the card stays open.
  function showSkillDetail(name, plugin){
    PLUG_EXPANDED.add(name);
    if (!PLUG_DETAILS[name]){
      const q = '/api/skills/detail?name='+encodeURIComponent(name)
        + (plugin ? '&plugin='+encodeURIComponent(plugin) : '');
      apiGet(q).then(d => {
        PLUG_DETAILS[name] = (d && d.ok !== false) ? d : {missing: true};
        if (liveFn === renderPlugins) paintPlugins();
      }).catch(() => {});
    }
    if (!PLUG_HIST[name]){
      apiGet('/api/skills/history?name='+encodeURIComponent(name)+'&limit=25')
        .then(h => { PLUG_HIST[name] = h || []; if (liveFn === renderPlugins) paintPlugins(); })
        .catch(() => {});
    }
    paintPlugins();
  }
  function toggleSkill(name, plugin){
    if (PLUG_EXPANDED.has(name)){ PLUG_EXPANDED.delete(name); paintPlugins(); return; }
    showSkillDetail(name, plugin);
  }
  // jump target for recommendation + plugin-index chips: expand the card and
  // scroll to it; if the current filters hide it, reset them first.
  function jumpToSkill(name){
    const entry = plugSkillEntries().find(e => e.name === name);
    showSkillDetail(name, entry ? entry.plugin : null);
    let el = Array.from(view.querySelectorAll('.skrow')).find(li => li.dataset.name === name);
    if (!el){
      PLUG_QUERY = ''; PLUG_STATUSF = 'all'; PLUG_SRC = 'all'; PLUG_MEASURED = false;
      syncPlugControls();
      paintPlugins();
      el = Array.from(view.querySelectorAll('.skrow')).find(li => li.dataset.name === name);
    }
    if (el) el.scrollIntoView({behavior: 'smooth', block: 'center'});
  }
  function syncPlugControls(){
    const q = document.getElementById('plugq'); if (q) q.value = PLUG_QUERY;
    const st = document.getElementById('plugstatus'); if (st) st.value = PLUG_STATUSF;
    const pl = document.getElementById('plugmarket'); if (pl) pl.value = PLUG_SRC;
    const m = document.getElementById('plugmeasured'); if (m) m.checked = PLUG_MEASURED;
  }
  // Call a skill headlessly and update its card IN PLACE: the button carries a
  // spinner (state in PLUG_CALLING so live repaints keep it), then the card's
  // dot/metrics/history refresh — never a full-view result screen, which would
  // lose scroll and filter state. No global busy state either: each call
  // spawns a full `claude` and can take minutes, and the panel must keep
  // live-updating around it.
  async function invokeSkill(name, plugin){
    if (PLUG_CALLING.has(name)) return;
    if (!confirm('Call the "'+name+'" skill now? This runs a headless Claude and can take a while.')) return;
    PLUG_CALLING.add(name);
    if (liveFn === renderPlugins) paintPlugins();
    let res;
    try { res = await apiPost('/api/skills/invoke', {name: name, plugin: plugin || null}); }
    catch(e){ res = {ok: false, excerpt: String((e && e.message) || e)}; }
    PLUG_LASTCALL[name] = res;
    PLUG_STATUS[name] = res.ok ? 'ok' : 'fail';
    PLUG_COUNTS[name] = (PLUG_COUNTS[name]||0)+1;
    PLUG_CALLING.delete(name);
    PLUG_GEN++;   // invalidate any in-flight poll so it can't clobber the fresh result
    try {
      const r = await Promise.all([
        apiGet('/api/skills/stats').catch(() => null),
        apiGet('/api/skills/history?name='+encodeURIComponent(name)+'&limit=25').catch(() => null)]);
      if (r[0] && r[0].stats) PLUG_STATS = r[0].stats;
      if (r[1]) PLUG_HIST[name] = r[1];
    } catch(_){}
    if (liveFn === renderPlugins) paintPlugins();
  }
  // Learn from a failed call on demand: reflect the skill:<name> incident into
  // a prevention rule immediately (bypassing the recurrence gate). Spawns the
  // claude CLI once, so it's behind a confirm; the result lands in the card's
  // Needed-changes area in place.
  async function reflectFailure(name){
    if (PLUG_REFLECTING.has(name)) return;
    if (!confirm('Reflect the "'+name+'" failure into a prevention rule now? Runs the claude CLI once.')) return;
    PLUG_REFLECTING.add(name);
    if (liveFn === renderPlugins) paintPlugins();
    let res;
    try { res = await apiPost('/api/rules/reflect_one', {signature: 'skill:'+name}); }
    catch(e){ res = {ok: false, error: String((e && e.message) || e)}; }
    PLUG_REFLECT[name] = res.ok ? 'rule learned \\u2014 see Needed changes'
                                : (res.error || 'reflection failed');
    PLUG_REFLECTING.delete(name);
    try {
      const imp = await apiGet('/api/improvements');
      if (imp && imp.auto) indexSkillRules(imp.auto);
    } catch(_){}
    if (liveFn === renderPlugins) paintPlugins();
  }
  function onPlugSearch(v){ PLUG_QUERY = v; paintPlugins(); }
  function onPlugStatus(v){ PLUG_STATUSF = v; paintPlugins(); }
  function onPlugSrc(v){ PLUG_SRC = v; paintPlugins(); }
  function onPlugMeasured(v){ PLUG_MEASURED = v; paintPlugins(); }
  function showSkillPanel(){
    const statusOpts = [['all','all'],['fail','red (failed)'],['uncalled','grey (uncalled)'],['ok','green (ok)']]
      .map(kv => '<option value="'+kv[0]+'"'+(PLUG_STATUSF===kv[0]?' selected':'')+'>'+kv[1]+'</option>').join('');
    const sortOpts = [['status','needs attention'],['name','name'],['source','plugin']]
      .map(kv => '<option value="'+kv[0]+'"'+(PLUG_SORT===kv[0]?' selected':'')+'>'+kv[1]+'</option>').join('');
    controls.innerHTML = '<div class="row">'
      + '<input id="plugq" placeholder="search skills\\u2026" style="min-width:240px" '
      + 'value="'+escAttr(PLUG_QUERY)+'" oninput="onPlugSearch(this.value)">'
      + '<label>plugin <select id="plugmarket" onchange="onPlugSrc(this.value)"></select></label>'
      + '<label>status <select id="plugstatus" onchange="onPlugStatus(this.value)">'+statusOpts+'</select></label>'
      + '<label><input type="checkbox" id="plugmeasured"'+(PLUG_MEASURED?' checked':'')
      + ' onchange="onPlugMeasured(this.checked)"> measured only</label>'
      + '<label>sort <select id="plugsort" onchange="onPlugSort(this.value)">'+sortOpts+'</select></label>'
      + '<button id="plugdir" onclick="togglePlugDir()">'+plugDirLabel()+'</button>'
      + '</div>';
    liveFn = renderPlugins;
    run('loading skill & plugin', renderPlugins);
  }
  function doExport(){ window.location = u('/api/export'); }

  function doAction(path, label, method){
    controls.innerHTML=''; liveFn = null;
    run(label+'…', async () => showText(method === 'GET' ? await apiGet(path) : await apiPost(path, {})));
  }
  function confirmAction(path, label){
    if(!confirm(label + ' — this can take a while. Continue?')) return;
    let body = {};
    if(path === '/api/embed' && confirm('Force past LIVE_LOCK? (only if the box is idle)')) body = { force: true };
    controls.innerHTML=''; liveFn = null;
    run(label+' running…', async () => showText(await apiPost(path, body)));
  }

  //__TASKS_JS__

  // --- MN-13: the faculty cat board ---------------------------------------
  // Injected from `cat_board.script()` — the same implementation the swarm page
  // renders, not a copy of it.
/*__CAT_JS__*/
  // Its own interval rather than a hook into refreshLive(): refreshLive returns
  // early whenever the visible section is not live-refreshable, when a manual
  // operation is running, or before any section has registered a liveFn — and the
  // cat board's whole job is to be readable exactly then, when something is slow or
  // stuck. Sharing that timer would blank the board at the moment it matters.
  // Errors are swallowed for the same reason the swarm page keeps this call outside
  // its main try: a failing board must not take out the page, and a failing page
  // must not take out the board.
  async function pollCats(){
    if (!liveOn || document.hidden) return;
    // ONE fetch feeds both strips — see the swarm page's poll for why.
    try {
      const board = await apiGet('/api/mindnet/cats');
      paintCats(board.cats || []);
      paintSessions(board);
    } catch(_){}
  }
  setInterval(pollCats, LIVE_MS);

  // populate the project picker, then land on the overview
  (async () => { await loadProjects(); showOverview(); pollCats(); })();
</script>
</body></html>""" \
    .replace("/*__CAT_CSS__*/", cat_board.CSS) \
    .replace("<!--__CAT_HTML__-->", cat_board.HTML) \
    .replace("/*__CAT_JS__*/", cat_board.script()) \
    .replace("__TOKEN__", json.dumps(token)) \
    .replace("/*__TASKS_CSS__*/", tasks_css) \
    .replace("//__TASKS_JS__", tasks_js)
