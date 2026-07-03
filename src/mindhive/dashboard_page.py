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
  .snip.one { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .skchip { display:inline-block; cursor:pointer; color:#9ecbff; background:#12182a;
    border:1px solid #2a3550; border-radius:6px; padding:1px 7px; margin:2px 3px 0 0; }
  .skchip:hover { background:#1b2540; border-color:#3d6; color:#dfeaff; }
  .statusDot { font-size:11px; margin-right:5px; vertical-align:middle; }
  .statusDot.sok { color:#3fb950; }
  .statusDot.sbad { color:#f85149; }
  .statusDot.snone { color:#3a4150; }
  .skmeta { margin:10px 0; }
  .skmeta b { color:#7fb2ff; }
  .skbody { white-space:pre-wrap; background:#0c0e14; border:1px solid #2a2f3d;
    border-radius:8px; padding:10px 12px; margin-top:4px; max-height:52vh; overflow:auto; }
  h2.h { margin:2px 0 12px; font-size:16px; }
  .glyph { font-weight:700; margin-right:6px; }
  .glyph.res { color:#46c46a; } .glyph.unres { color:#ff8a8a; }
  .glyph.watch { color:#e0b341; } .glyph.dis { color:#8a93a6; }
  .legend { font-size:12px; color:#9aa3b8; margin:2px 0 10px; }
  button.mini { font-size:11px; padding:1px 8px; margin-left:8px; background:#1d2230;
    color:#9aa3b8; border:1px solid #2a2f3d; border-radius:999px; cursor:pointer; }
  button.mini:hover { color:#ff8a8a; }
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
    <button onclick="showImprovements(true)">Improvements</button>
    <div class="group">View</div>
    <button onclick="showState()">State</button>
    <button onclick="showBriefing()">Briefing</button>
    <button onclick="showRecall()">Recall</button>
    <button onclick="showGraph()">Graph</button>
    <button onclick="showPlugins()">Skills &amp; plugins</button>
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

  // --- Improvements: lessons learned from past failures + manual notes ---
  // IMP_ITEMS mirrors the /api/improvements shape ({auto, manual}); query and
  // the two filter selects live outside the view so the 2.5s live refresh
  // preserves what you typed and picked. IMP_BANNER carries the result line of
  // the last Reflect run until the view is re-entered from the nav.
  let IMP_ITEMS = {auto: [], manual: []};
  let IMP_QUERY = '';
  let IMP_STATUS = 'all';
  let IMP_CAT = 'all';
  let IMP_BANNER = '';
  let IMP_SORT = 'critical';       // 'new' | 'critical' | 'freq'
  let IMP_DIR = 'desc';            // toggle reverses the active order
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
  function impLegend(){
    return '<div class="legend">'+['resolved','unresolved','watching','dismissed']
      .map(r => glyphSpan(r)+IMP_GLYPHS[r][2]).join(' \\u00b7 ')+'</div>';
  }
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
    IMP_ITEMS = await apiGet('/api/improvements');
    paintImprovements();
  }
  function fillSelect(id, values, cur){
    const el = document.getElementById(id);
    if (!el) return;
    const opts = ['all'].concat(values);
    // a selection that vanished from fresh data stays selected (may match nothing)
    if (cur !== 'all' && !opts.includes(cur)) opts.push(cur);
    el.innerHTML = opts.map(v => '<option value="'+escAttr(v)+'"'
      + (v===cur?' selected':'')+'>'+esc(v)+'</option>').join('');
  }
  function lessonPills(a){
    let p = '<span class="pill">'+esc(a.status||'?')+'</span>';
    if (a.category) p += ' <span class="pill">'+esc(a.category)+'</span>';
    if (a.rule_source && a.rule_source !== 'none')
      p += ' <span class="pill">'+esc(a.rule_source)+'</span>';
    if (!a.active_rule) p += (a.status === 'candidate'
      ? ' <span class="pill">seen '+(a.occurrences||1)+'\\u00d7 \\u2014 reflects if it recurs</span>'
      : ' <span class="pill">not yet reflected</span>');
    return p;
  }
  function lessonCard(a, showProj){
    const head = glyphSpan(a.resolution)
      + (showProj && a.project ? esc(a.project)+' \\u00b7 ' : '')
      + esc(a.signature)+' \\u00b7 '+(a.occurrences||0)+'\\u00d7 \\u00b7 '
      + (a.sessions||0)+' session'+((a.sessions||0)===1?'':'s')
      + (a.last_seen ? ' \\u00b7 last seen '+esc(a.last_seen) : '')
      + (a.resolution !== 'dismissed'
         ? '<button class="mini dismissbtn" data-sig="'+escAttr(a.signature)+'">dismiss</button>'
         : '');
    let body = '';
    if (a.first_seen) body += '<div class="snip dup">first seen '+esc(a.first_seen)
      + ((a.leaks||0) ? ' \\u00b7 recurred '+a.leaks+'\\u00d7 since the rule activated' : '')
      + '</div>';
    if (a.lesson) body += '<div class="snip"><b>Lesson:</b> '+esc(a.lesson)+'</div>';
    if (a.active_rule) body += '<div class="snip"><b>Rule:</b> '+esc(a.active_rule)+'</div>';
    if (a.root_cause) body += '<div class="snip dup">Root cause: '+esc(a.root_cause)+'</div>';
    if (a.excerpt) body += '<div class="snip dup">'+esc(a.excerpt)+'</div>';
    return '<li><div class="file">'+head+'</div><div style="margin:4px 0">'
      + lessonPills(a)+'</div>'+body+'</li>';
  }
  function noteRow(m, showProj){
    return '<li class="noterow" style="cursor:pointer"><div class="file">'
      + (showProj && m.project ? esc(m.project)+' \\u00b7 ' : '')
      + esc(m.ts||'')+' \\u00b7 <span class="pill">manual</span>'
      + (m.similar_count ? ' <span class="dup">+'+m.similar_count+' similar</span>' : '')
      + '</div><div class="snip one">'+esc(m.text||'')+'</div></li>';
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
      (IMP_STATUS==='all' || a.status===IMP_STATUS)
      && (IMP_CAT==='all' || a.category===IMP_CAT)
      && (!q || ((a.signature||'')+' '+(a.lesson||'')+' '+(a.prevention_rule||'')+' '
          +(a.root_cause||'')+' '+(a.excerpt||'')+' '+(a.project||'')).toLowerCase().includes(q)))
      .sort(impCompare);
    const notes = manual.filter(m => !q
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
    view.querySelectorAll('.noterow .snip').forEach(el =>
      el.parentElement.onclick = () => el.classList.toggle('one'));
    view.querySelectorAll('.dismissbtn').forEach(el =>
      el.onclick = (ev) => { ev.stopPropagation(); dismissLesson(el.dataset.sig); });
  }
  // Reflect keeps the Heavy confirm (token spend) but lands back on the
  // Improvements view with an inline result banner instead of confirmAction's
  // full-panel JSON dump.
  async function runReflect(){
    if(!confirm('Reflect (learn from failures) \\u2014 runs the claude CLI once '
      + 'per recurring incident (a few per click). Continue?')) return;
    await run('reflecting\\u2026', async () => {
      const res = await apiPost('/api/rules/reflect', {});
      IMP_BANNER = res.ok
        ? ('Reflect: '+res.reflected+' reflected \\u00b7 '+res.failed
           +' failed \\u00b7 '+res.pending+' pending')
        : (res.error || 'reflection failed');
    });
    showImprovements();
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
      + '</div>';
    liveFn = renderImprovements; run('loading improvements', renderImprovements);
  }

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

  // Skills & plugins: the live list re-renders every LIVE_MS via liveFn, and
  // the server rebuilds only when its cheap disk signature changes — so an
  // install, uninstall, or local-panel skill delete shows up within seconds
  // at near-zero polling cost. The free/paid ranking stays below the list,
  // fed by the stale-gated evaluation sidecar (autoscan kicked on view open;
  // the Heavy "Plugins scan" button still forces a full re-evaluation).
  let PLUG_RANKING = null;
  let PLUG_STATUS = {};             // {skillName: 'ok'|'fail'} last call outcome
  // green when the last headless call succeeded, red when it failed, grey when
  // never called. A skill name calls invokeSkill(name, plugin) to run it.
  function statusDot(name){
    const s = PLUG_STATUS[name];
    const cls = s === 'ok' ? 'sok' : (s === 'fail' ? 'sbad' : 'snone');
    const title = s === 'ok' ? 'last call succeeded'
      : (s === 'fail' ? 'last call FAILED — see Improvements' : 'never called');
    return '<span class="statusDot '+cls+'" title="'+title+'">\\u25cf</span>';
  }
  async function renderPlugins(){
    const res = await apiGet('/api/plugins/inventory');
    const inv = res.inventory || {};
    const plugs = inv.plugins || [];
    const local = inv.local_skills || [];
    const c = inv.counts || {};
    try { PLUG_STATUS = await apiGet('/api/skills/status') || {}; } catch(_){}
    let html = '<h2 class="h">Skills &amp; plugins</h2>'
      + '<div class="cards">'
      + '<div class="card"><div class="big">'+(c.plugins ?? 0)+'</div><div class="lbl">plugins installed</div></div>'
      + '<div class="card"><div class="big ok">'+(c.skills_total ?? 0)+'</div><div class="lbl">skills total</div></div>'
      + '<div class="card"><div class="big">'+(c.local_skills ?? 0)+'</div><div class="lbl">local skills</div></div>'
      + '</div>';
    // rows are click-to-expand: a skill name calls showSkillDetail(name, plugin)
    // which fetches /api/skills/detail and renders intended-use FIRST, then the
    // date it was added, then the full SKILL.md body, into an inline panel.
    const skillChip = (name, plugin) => statusDot(name) + '<span class="skchip" onclick="showSkillDetail('
        + escAttr(JSON.stringify(name)) + ',' + escAttr(JSON.stringify(plugin||null)) + ')">'
        + esc(name)+'</span>';
    if (local.length){
      html += '<h3 class="h">Local skills</h3><ul class="hits">'
        + local.map(s => '<li><div class="file">'+skillChip(s.name, null)+'</div>'
            + (s.trigger ? '<div class="snip">'+esc(s.trigger)+'</div>' : '')+'</li>').join('')
        + '</ul>';
    }
    html += '<h3 class="h">Plugins</h3>' + (plugs.length ? '<ul class="hits">'
      + plugs.map(p => '<li><div class="file">'+esc(p.name)+' '+esc(p.version)
          + ' \\u00b7 '+esc(p.marketplace)
          + (p.present === false ? ' \\u00b7 <span class="err">missing on disk</span>' : '')
          + ' \\u00b7 '+p.skills.length+' skill'+(p.skills.length===1?'':'s')+'</div>'
          + (p.skills.length ? '<div class="snip">'+p.skills.map(s=>skillChip(s.name, p.name)).join(' ')+'</div>' : '')
          + '</li>').join('')
      + '</ul>' : '<div class="lbl">no plugins installed</div>');
    if (PLUG_RANKING && (PLUG_RANKING.recommendations || []).length){
      html += '<h3 class="h">Recommended (free, by breadth)</h3><ul class="hits">'
        + PLUG_RANKING.recommendations.map(r => '<li><div class="file">'+esc(r.name)
            + ' \\u00b7 score '+esc(r.breadth_score)+'</div>'
            + (r.note ? '<div class="snip">'+esc(r.note)+'</div>' : '')+'</li>').join('')
        + '</ul>';
    }
    showHTML(html);
  }
  // Click-through detail: intended use at the very top, then when it was added,
  // then the full SKILL.md body. Pauses the live refresh so the panel you're
  // reading isn't wiped by the 2.5s inventory poll.
  async function showSkillDetail(name, plugin){
    liveFn = null;
    const q = '/api/skills/detail?name='+encodeURIComponent(name)
      + (plugin ? '&plugin='+encodeURIComponent(plugin) : '');
    run('loading '+name, async () => {
      const d = await apiGet(q);
      if (!d || d.ok === false){ showText('No detail found for “'+name+'”.'); return; }
      const plug = d.source && d.source !== 'local' ? d.source : null;
      showHTML('<div class="row"><button onclick="showPlugins()">\\u2190 back to skills &amp; plugins</button>'
        + '<button onclick="invokeSkill('+escAttr(JSON.stringify(d.name))+','+escAttr(JSON.stringify(plug))+')">'
        + '\\u25b6 call this skill</button>'+statusDot(d.name)+'</div>'
        + '<h2 class="h">'+esc(d.name)+'</h2>'
        + '<div class="skmeta"><b>Intended use</b><div class="snip">'+esc(d.intended_use || '—')+'</div></div>'
        + '<div class="skmeta"><b>Added</b> <span class="lbl">'+esc(d.added_at || 'unknown')
        + (d.source ? ' \\u00b7 '+esc(d.source) : '')+'</span></div>'
        + '<div class="skmeta"><b>Full description</b><pre class="skbody">'+esc(d.body || '')+'</pre></div>');
    });
  }
  // Call a skill headlessly via MindHive. Each call spawns a full `claude`, so
  // it's gated behind an explicit confirm. The dot turns green on success, red
  // on failure; a failure is recorded as an incident and flows into the
  // Improvements loop so the system learns from bad calls.
  function invokeSkill(name, plugin){
    if(!confirm('Call the "'+name+'" skill now? This runs a headless Claude and can take a while.')) return;
    controls.innerHTML=''; liveFn = null;
    run('calling '+name+'…', async () => {
      const res = await apiPost('/api/skills/invoke', {name: name, plugin: plugin});
      PLUG_STATUS[name] = res.ok ? 'ok' : 'fail';
      const head = res.ok ? '<span class="ok">\\u2714 '+esc(name)+' succeeded</span>'
                          : '<span class="err">\\u2716 '+esc(name)+' failed (exit '+esc(res.exit)+')</span>';
      showHTML('<div class="row"><button onclick="showPlugins()">\\u2190 back</button> '
        + '<button onclick="showImprovements(true)">open Improvements</button></div>'
        + '<h2 class="h">'+head+'</h2>'
        + '<div class="skmeta"><b>Output</b><pre class="skbody">'+esc(res.excerpt || '(no output)')+'</pre></div>'
        + (res.ok ? '' : '<div class="lbl">This failure was recorded — the Improvements view will learn a prevention rule from it.</div>'));
    });
  }
  function showPlugins(){ controls.innerHTML=''; liveFn = renderPlugins; run('loading skills & plugins', async () => {
    await renderPlugins();                        // the live list paints first
    apiPost('/api/plugins/autoscan', {})          // ranking refreshes stale-gated
      .then(() => apiGet('/api/plugins/recommend'))
      .then(cat => { PLUG_RANKING = cat; if (liveFn === renderPlugins) return renderPlugins(); })
      .catch(()=>{});
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
