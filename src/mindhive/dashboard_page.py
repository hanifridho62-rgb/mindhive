# src/mindhive/dashboard_page.py
"""Presentation layer for the MIND HIVE dashboard.

The single-page HTML/CSS/JS control panel, isolated from ``dashboard.py`` so
the HTTP handler and data logic aren't buried under ~380 lines of embedded
markup (and ``dashboard.py`` stays under the project's 1000-line file cap).
Pure presentation: takes a session token, returns a self-contained HTML
document string. No sibling-module dependencies."""
import json


def render_page(token: str) -> str:
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
  .hits { list-style:none; padding:0; margin:0; }
  .hits li { background:#0c0e14; border:1px solid #2a2f3d; border-radius:8px; padding:10px 12px; margin:8px 0; }
  .hits .file { font-size:12px; color:#7fb2ff; word-break:break-all; }
  .hits .snip { white-space:pre-wrap; margin-top:4px; }
  .hits .dup { color:#8a93a6; }
  h2.h { margin:2px 0 12px; font-size:16px; }
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
</header>
<main>
  <nav>
    <button onclick="showOverview()">Overview</button>
    <div class="group">Memory</div>
    <button onclick="showMemory()">Memory</button>
    <button onclick="showSessions()">Sessions</button>
    <div class="group">View</div>
    <button onclick="showState()">State</button>
    <button onclick="showBriefing()">Briefing</button>
    <button onclick="showRecall()">Recall</button>
    <button onclick="showGraph()">Graph</button>
    <button onclick="showPlugins()">Plugins</button>
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
    if (PROJECT) b.project = PROJECT;
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

  // --- Sessions: the episode layer; click one to scope Memory to it ---
  async function renderSessions(){
    const items = await apiGet('/api/sessions');
    if (!items.length) { showText('No sessions yet.'); return; }
    showHTML('<h2 class="h">'+items.length+' session'+(items.length>1?'s':'')+'</h2><ul class="hits">'
      + items.map(s => '<li class="sessrow" data-sid="'+escAttr(s.session_id)+'" style="cursor:pointer">'
          + '<div class="file">'+(PROJECT==='*' && s.project ? esc(s.project)+' · ' : '')+esc(s.session_id)+'</div>'
          + '<div class="snip">'+s.events+' events · '+s.memory+' memory · '
          + esc(s.first_ts||'')+' → '+esc(s.last_ts||'')+'</div></li>').join('')
      + '</ul>');
    view.querySelectorAll('.sessrow').forEach(el => el.onclick = () => openSession(el.dataset.sid));
  }
  function showSessions(){ controls.innerHTML=''; liveFn = renderSessions; run('loading sessions', renderSessions); }
  function openSession(sid){ SESSION = sid; showMemory(); }
  function showOverview(){ controls.innerHTML=''; liveFn = renderOverview; run('loading overview', renderOverview); }

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

  function showGraph(){ controls.innerHTML=''; liveFn = null; run('building graph', async () => {
    const html = await apiGet('/api/graph'); only(graph); graph.srcdoc = html;
  }); }

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

  // Plugins view auto-refreshes when stale: it asks the server to scan only if
  // the catalog is missing or >24h old, so just opening the view keeps it current
  // without forcing a heavy rescan. The Heavy "Plugins scan" button forces one.
  function showPlugins(){ controls.innerHTML=''; liveFn = null; run('refreshing plugins', async () => {
    const res = await apiPost('/api/plugins/autoscan', {});
    const note = res.scanned ? 'scanned just now' : ('catalog fresh ('+(Number(res.age_hours)||0).toFixed(1)+'h old)');
    showText(note + '\\n\\n' + JSON.stringify(res.result || res, null, 2));
  }); }
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

  // populate the project picker, then land on the overview
  (async () => { await loadProjects(); showOverview(); })();
</script>
</body></html>""".replace("__TOKEN__", json.dumps(token))
