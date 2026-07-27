# src/mindhive/swarm_page.py
"""MindSwarm — the Workspace's mission view.

Give it a mission and the backend (workspace.py) spins up a coordinator plus
builders, scouts, and reviewers, all sharing the Mind Hive ledger as swarm
memory. This page renders the live mission tree — coordinator at the center,
role-colored branches to every agent — an inspector for any agent's live
stream, and a single command bar that directs one agent (@name) or all of them
(@all).

Pure presentation: token in, HTML string out; all state is server-side.
"""
import json

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MindSwarm</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.45 system-ui, sans-serif; color:#e6edf3; height:100vh;
    display:flex; flex-direction:column; overflow:hidden; background:#0e1116; }
  header { display:flex; align-items:center; gap:10px; padding:8px 14px;
    background:#161b22; border-bottom:1px solid #30363d; flex:0 0 auto; }
  header .logo { color:#e8b64c; font-weight:700; letter-spacing:1px; font-size:13px; }
  header input { flex:1; min-width:120px; }
  header .n { width:44px; }
  .badge { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid #3d6; }
  .badge.run { color:#7ee787; border-color:#2ea043; animation:pulse 1.4s infinite; }
  .badge.idle { color:#8b949e; border-color:#3a4054; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  button { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px;
    padding:4px 10px; cursor:pointer; font:inherit; }
  button:hover { background:#30363d; }
  button.stop { color:#ff8a8a; border-color:#5a2e2e; }
  input, select { background:#0e1116; color:#e6edf3; border:1px solid #30363d;
    border-radius:6px; padding:4px 8px; font:inherit; }
  input:focus, select:focus { outline:none; border-color:#58a6ff; }
  #wrap { flex:1 1 auto; display:flex; min-height:0; }
  #stage { flex:1; position:relative; overflow:hidden; cursor:grab;
    background-image: radial-gradient(#21262d 1px, transparent 1px);
    background-size: 26px 26px; }
  #stage.panning { cursor:grabbing; }
  #world { position:absolute; left:0; top:0; transform-origin:0 0; }
  svg#edges { position:absolute; left:0; top:0; overflow:visible; pointer-events:none; }
  .chip { position:absolute; transform:translate(-50%,-50%); white-space:nowrap;
    border:1px solid; border-radius:8px; padding:4px 10px; font-size:12px; cursor:pointer;
    background:#161b22cc; display:flex; flex-direction:column; align-items:center; gap:2px; }
  .chip .row { display:flex; align-items:center; gap:6px; }
  .chip:hover { filter:brightness(1.3); }
  .chip.sel { box-shadow:0 0 0 2px #fff5; }
  .chip .dot { width:7px; height:7px; border-radius:50%; background:#3a4054; }
  .chip .dot.running { background:#e0b341; animation:pulse 1s infinite; }
  .chip .dot.done { background:#46c46a; } .chip .dot.error { background:#ff6b6b; }
  #insp { width:340px; flex:0 0 auto; background:#161b22; border-left:1px solid #30363d;
    padding:12px; overflow-y:auto; display:none; }
  #insp h2 { margin:0 0 6px; font-size:14px; }
  #insp pre { white-space:pre-wrap; word-break:break-word; background:#0e1116;
    border:1px solid #30363d; border-radius:6px; padding:8px;
    font:11.5px/1.5 ui-monospace, Consolas, monospace; max-height:60vh; overflow-y:auto; }
  #hint { position:absolute; bottom:56px; left:50%; transform:translateX(-50%);
    color:#8b949e; font-size:11.5px; }
  #cmdbar { flex:0 0 auto; display:flex; gap:8px; padding:10px 14px; align-items:center;
    background:#161b22; border-top:1px solid #30363d; }
  #cmdbar select { color:#e8b64c; }
  #cmdbar input { flex:1; }
  .quick { font-size:11px; }
  #toast { position:fixed; top:52px; left:50%; transform:translateX(-50%);
    background:#21262d; border:1px solid #3d6; border-radius:8px; padding:5px 14px;
    display:none; z-index:9; }
  #empty { position:absolute; inset:0; display:flex; align-items:center;
    justify-content:center; color:#8b949e; text-align:center; }
  /* --- MN-5: the faculty cat board -------------------------------------
     A debugging surface, not decoration. A cat that has been dim for hours
     is the visible form of a silent hang, which is the whole reason the
     strip exists — so `dim` is muted but never hidden, and only the 6-hour
     rule removes a cat entirely (that filtering happens server-side in
     ui_cats.board; this stylesheet only renders what arrives). */
  #mn-cats { flex:0 0 auto; display:flex; align-items:center; gap:6px;
    padding:5px 14px; background:#12161d; border-bottom:1px solid #30363d;
    min-height:34px; overflow-x:auto; }
  #mn-cats .lbl { color:#8b949e; font-size:10.5px; letter-spacing:.6px;
    text-transform:uppercase; margin-right:2px; flex:0 0 auto; }
  #mn-cats .none { color:#6e7681; font-size:11.5px; }
  .mn-cat { position:relative; display:flex; align-items:center; gap:5px;
    border:1px solid #30363d; border-radius:999px; padding:2px 9px 2px 5px;
    background:#161b22; cursor:pointer; flex:0 0 auto; font-size:11.5px; }
  .mn-cat:hover { border-color:#58a6ff; }
  .mn-cat svg { width:19px; height:19px; display:block; }
  .mn-cat.active { color:#7ee787; border-color:#2ea043; }
  .mn-cat.active svg { animation:mn-bob 1.6s ease-in-out infinite; }
  .mn-cat.dim { color:#8b949e; opacity:.55; }
  .mn-cat.dim svg { animation:none; }
  @keyframes mn-bob { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-2px)} }
  #mn-cat-panel { position:fixed; z-index:20; display:none; min-width:210px;
    background:#161b22; border:1px solid #30363d; border-radius:8px;
    padding:8px 10px; font-size:11.5px; box-shadow:0 6px 22px #0009; }
  #mn-cat-panel h3 { margin:0 0 5px; font-size:12px; color:#e8b64c; }
  #mn-cat-panel .kv { display:flex; gap:6px; }
  #mn-cat-panel .kv span:first-child { color:#8b949e; min-width:58px; }
  #mn-cat-panel .warn { margin-top:5px; color:#ff9e64; }
</style></head>
<body>
<header>
  <span class="logo">&#10022; MISSION</span>
  <input id="mission" placeholder="give the swarm a mission&hellip;">
  <label class="quick">builders <input class="n" id="nb" type="number" value="3" min="0" max="12"></label>
  <label class="quick">scouts <input class="n" id="ns" type="number" value="2" min="0" max="8"></label>
  <label class="quick">reviewers <input class="n" id="nr" type="number" value="2" min="0" max="8"></label>
  <button onclick="startMission()">Launch</button>
  <span id="mstate" class="badge idle">IDLE</span>
  <button class="stop" onclick="stopMission()">&#9888; Stop</button>
</header>
<div id="mn-cats"><span class="lbl">faculties</span>
  <span class="none">no MindNetwork activity</span></div>
<div id="mn-cat-panel"></div>
<div id="wrap">
  <div id="stage">
    <div id="world">
      <svg id="edges" width="10" height="10"></svg>
      <div id="chips"></div>
    </div>
    <div id="empty">No mission running.<br>Type a mission above and Launch &mdash;
      a coordinator, builders, scouts and reviewers will appear here.</div>
    <div id="hint">Click a chip to inspect &middot; drag to pan &middot; scroll to zoom
      &middot; @ to direct an agent</div>
  </div>
  <div id="insp">
    <div style="display:flex;gap:6px;align-items:center">
      <h2 id="iname" style="flex:1">agent</h2>
      <button onclick="stopSel()">&#9632; stop</button>
      <button onclick="closeInsp()">&#10005;</button>
    </div>
    <div id="imeta" style="color:#8b949e;font-size:11.5px;margin-bottom:4px"></div>
    <div id="iskills" style="color:#9ecbff;font-size:11.5px;margin-bottom:8px"></div>
    <pre id="iout"></pre>
  </div>
</div>
<div id="cmdbar">
  <select id="target"><option value="all">@all</option></select>
  <input id="cmd" placeholder="Direct the Swarm&hellip; (@ to target an agent)">
  <button onclick="sendCmd()">&#10148;</button>
  <button class="quick" onclick="quick('Give a concise status report: what you did, what is blocking, what is next.')">Status report</button>
  <button class="quick" onclick="quick('Wrap up: finish or checkpoint your current step, commit learnings to Mind Hive, and summarize.')">Wrap up</button>
</div>
<div id="toast"></div>
<script>
  const TOKEN = __TOKEN__;
  const PROJ = new URLSearchParams(location.search).get('project') || '';
  const ROLE_STYLE = {
    coordinator: '#ff7b54', builder: '#7ee787', scout: '#e8b64c', reviewer: '#f0883e',
    agent: '#8ab4f8' };
  const ROLE_ICON = { coordinator:'&#9819;', builder:'&#128295;', scout:'&#128269;',
    reviewer:'&#128065;', agent:'&#9679;' };
  // 6 pixel cats (white, calico, orange, grey tabby, siamese, black) drawn as
  // inline SVG rect grids — no image files, so the page stays offline-self-
  // contained. A node's cat is picked deterministically from its agent id:
  // random per new node, stable across refreshes. Grid chars: b body, p patch,
  // e eye, n nose, . empty.
  const CAT_GRID = [
    '.b........b.',
    '.bb......bb.',
    '.bpb....bpb.',
    '.bbbbbbbbbb.',
    'bbbbbbbbbbbb',
    'bbeebbbbeebb',
    'bbbbbnnbbbbb',
    '.bbbbbbbbbb.',
    '.bpbbbbbbpb.',
    '.bbbbbbbbbb.',
    '.bbbbbbbbbb.',
    '..bb.bb.bb..'];
  const CAT_PALETTES = [
    {name:'white',   b:'#f2f2f0', p:'#e3e3df', e:'#4aa3ff'},
    {name:'calico',  b:'#f2f2f0', p:'#e8863a', e:'#3fb950'},
    {name:'orange',  b:'#e8863a', p:'#c9641f', e:'#3fb950'},
    {name:'tabby',   b:'#9aa2ad', p:'#6b7280', e:'#e8b64c'},
    {name:'siamese', b:'#e8dcc8', p:'#5b4636', e:'#58a6ff'},
    {name:'black',   b:'#262a33', p:'#16181d', e:'#e8b64c'}];
  function catFor(id){ return (parseInt(String(id).slice(0,4), 16) || 0) % 6; }
  function catSVG(idx, size){
    const c = CAT_PALETTES[idx % 6];
    const fill = {b:c.b, p:c.p, e:c.e, n:'#ff8fb3'};
    let rects = '';
    CAT_GRID.forEach((row, y) => { for (let x = 0; x < row.length; x++) {
      const col = fill[row[x]];
      if (col) rects += '<rect x="'+x+'" y="'+y+'" width="1" height="1" fill="'+col+'"/>';
    }});
    return '<svg viewBox="0 0 12 12" width="'+size+'" height="'+size
      + '" shape-rendering="crispEdges">'+rects+'</svg>';
  }
  function api(path){ return fetch(withP(path) + (path.includes('?')?'&':'?') + 'token='
      + encodeURIComponent(TOKEN), {headers:{'X-Hive-Token':TOKEN}}).then(r=>r.json()); }
  function apiPost(path, body){
    const b = Object.assign({}, body||{});
    if (PROJ && PROJ !== '*') b.project = PROJ;
    return fetch(withP(path)+'?token='+encodeURIComponent(TOKEN), {method:'POST',
      headers:{'Content-Type':'application/json','X-Hive-Token':TOKEN},
      body:JSON.stringify(b)}).then(r=>r.json());
  }
  function withP(path){ return (PROJ && PROJ !== '*')
    ? path + (path.includes('?')?'&':'?') + 'project=' + encodeURIComponent(PROJ) : path; }
  function esc(s){ return String(s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  let TID = null;
  function toast(msg, bad){
    const t = document.getElementById('toast');
    t.textContent = msg; t.style.borderColor = bad ? '#a55' : '#3d6';
    t.style.display = 'block';
    clearTimeout(TID); TID = setTimeout(()=>{ t.style.display='none'; }, 3200);
  }

  // --- mission lifecycle ---
  async function startMission(){
    const m = document.getElementById('mission').value.trim();
    if (!m) { toast('type a mission first', true); return; }
    const res = await apiPost('/api/workspace/mission', {mission: m,
      builders: +document.getElementById('nb').value,
      scouts: +document.getElementById('ns').value,
      reviewers: +document.getElementById('nr').value});
    if (!res.ok) { toast(res.error||'launch failed', true); return; }
    toast('mission launched — '+res.agents.length+' agents');
  }
  async function stopMission(){
    if (!confirm('Stop the whole swarm?')) return;
    await apiPost('/api/workspace/mission_stop', {});
  }
  function quick(text){ document.getElementById('cmd').value = text; sendCmd(); }
  // "@Name rest of command" targets that agent (longest case-insensitive name
  // match wins, so "Builder 10" beats "Builder 1"). "@all" is reserved and
  // always fans out, shadowing any agent literally named "all". Matching is
  // scoped to swarm agents — the same set paint() draws and the dropdown
  // lists — so a mention can never target a MindParallel pane the dropdown
  // doesn't show. Returns null when the text has no mention or nothing
  // matches — caller falls back to the dropdown target.
  function resolveMention(text){
    if (text[0] !== '@') return null;
    const rest = text.slice(1);
    if (/^all(\\s|$)/i.test(rest)) return {target:'all', text: rest.slice(3).trim()};
    let best = null;
    AGENTS.filter(a => a.swarm).forEach(a => {
      const nm = a.name || '';
      if (nm && rest.toLowerCase().startsWith(nm.toLowerCase())
          && (rest.length === nm.length || rest[nm.length] === ' ')
          && (!best || nm.length > best.nm.length)) best = {nm: nm, id: a.id};
    });
    return best ? {target: best.id, text: rest.slice(best.nm.length).trim()} : null;
  }
  async function sendCmd(){
    const inp = document.getElementById('cmd');
    const raw = inp.value.trim(); if (!raw) return;
    const m = resolveMention(raw);
    if (m && !m.text) { toast('add a command after the @mention', true); return; }
    const tsel = document.getElementById('target');
    const target = m ? m.target : tsel.value;
    const res = await apiPost('/api/workspace/command',
      {target: target, text: m ? m.text : raw});
    if (res.ok) { inp.value = '';
      // sync the dropdown only after the send succeeded, so a failed send
      // doesn't silently switch the persistent target
      if (m && [...tsel.options].some(o => o.value === m.target)) tsel.value = m.target;
      toast(res.sent != null ? 'sent to '+res.sent+' agent(s)'
        + (res.busy ? ' ('+res.busy+' busy)' : '') : 'sent'); }
    else toast(res.error||'failed', true);
  }
  document.getElementById('cmd').addEventListener('keydown',
    e => { if (e.key === 'Enter') sendCmd(); });

  // --- mission tree layout: coordinator center, role-grouped branches ---
  let AGENTS = [], SEL = null, CURSOR = 0;
  function positions(swarm){
    const W = document.getElementById('stage').clientWidth || 900;
    const H = document.getElementById('stage').clientHeight || 600;
    const cx = W/2, cy = H/2, pos = {};
    const coord = swarm.find(a => a.role === 'coordinator');
    if (coord) pos[coord.id] = {x:cx, y:cy};
    const groups = {builder:[], scout:[], reviewer:[], agent:[]};
    swarm.forEach(a => { if (a.role !== 'coordinator')
      (groups[a.role]||groups.agent).push(a); });
    // builders fan across the top; scouts lower-left; reviewers lower-right
    const fan = (list, a0, a1, r0) => list.forEach((a, i) => {
      const t = list.length === 1 ? 0.5 : i/(list.length-1);
      const ang = a0 + (a1-a0)*t, r = r0 + (i%2)*54;
      pos[a.id] = {x: cx + Math.cos(ang)*r, y: cy + Math.sin(ang)*r};
    });
    fan(groups.builder, -2.65, -0.5, 205);
    fan(groups.scout, 2.35, 3.55, 195);
    fan(groups.reviewer, -0.25, 1.1, 205);
    fan(groups.agent, 1.2, 2.0, 230);
    return pos;
  }
  function paint(){
    const swarm = AGENTS.filter(a => a.swarm);
    document.getElementById('empty').style.display = swarm.length ? 'none' : 'flex';
    const chips = document.getElementById('chips');
    const edges = document.getElementById('edges');
    const pos = positions(swarm);
    const coord = swarm.find(a => a.role === 'coordinator');
    edges.innerHTML = swarm.filter(a => coord && a.id !== coord.id).map(a => {
      const p = pos[a.id], c = pos[coord.id];
      if (!p || !c) return '';
      const midY = (c.y + p.y)/2;
      return '<path d="M'+c.x+' '+c.y+' L'+c.x+' '+midY+' L'+p.x+' '+midY
        +' L'+p.x+' '+p.y+'" fill="none" stroke="'+(ROLE_STYLE[a.role]||'#888')
        +'" stroke-width="1.6" stroke-opacity="0.75"/>';
    }).join('');
    chips.innerHTML = swarm.map(a => {
      const p = pos[a.id] || {x:0, y:0}, col = ROLE_STYLE[a.role] || '#888';
      return '<div class="chip'+(SEL===a.id?' sel':'')+'" data-id="'+a.id
        +'" style="left:'+p.x+'px;top:'+p.y+'px;border-color:'+col+';color:'+col+'">'
        + catSVG(catFor(a.id), 30)
        + '<span class="row">'+(ROLE_ICON[a.role]||'') + ' ' + esc(a.name)
        + ' <span class="dot '+a.status+'"></span></span></div>';
    }).join('');
    chips.querySelectorAll('.chip').forEach(el =>
      el.onclick = ev => { ev.stopPropagation(); inspect(el.dataset.id); });
    // mission badge + command targets
    const running = swarm.some(a => a.status === 'running');
    const ms = document.getElementById('mstate');
    ms.className = 'badge ' + (running ? 'run' : 'idle');
    ms.textContent = running ? '\\u25cf RUNNING' : (swarm.length ? 'SETTLED' : 'IDLE');
    const tsel = document.getElementById('target');
    const want = '<option value="all">@all</option>' + swarm.map(a =>
      '<option value="'+a.id+'"'+(tsel.value===a.id?' selected':'')+'>@'
      +esc(a.name)+'</option>').join('');
    if (tsel.innerHTML !== want) { const v = tsel.value; tsel.innerHTML = want;
      if ([...tsel.options].some(o => o.value === v)) tsel.value = v; }
  }
  // --- inspector: one agent's live stream ---
  function inspect(id){
    SEL = id; CURSOR = 0;
    document.getElementById('iout').textContent = '';
    document.getElementById('insp').style.display = 'block';
    paint();
  }
  function closeInsp(){ SEL = null; document.getElementById('insp').style.display = 'none'; paint(); }
  async function stopSel(){ if (SEL) await apiPost('/api/workspace/stop', {id: SEL}); }
  async function pollInspector(){
    if (!SEL) return;
    const a = AGENTS.find(x => x.id === SEL);
    if (!a) { closeInsp(); return; }
    document.getElementById('iname').textContent = a.name;
    document.getElementById('imeta').textContent =
      a.role + ' \\u00b7 ' + a.profile + ' \\u00b7 ' + a.status
      + (a.session_id ? ' \\u00b7 session ' + a.session_id.slice(0,8) : '')
      + (a.last_turns != null ? ' \\u00b7 ' + a.last_turns + ' turns' : '')
      + (a.last_cost != null ? ' \\u00b7 $' + (+a.last_cost).toFixed(4) : '');
    document.getElementById('iskills').textContent =
      'skills used: ' + ((a.skills_used || []).join(', ') || '\\u2014');
    const o = await api('/api/workspace/output?id='+SEL+'&since='+CURSOR);
    if (!o.ok) return;
    const pre = document.getElementById('iout');
    if (o.lines && o.lines.length) {
      const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      pre.textContent += (pre.textContent ? '\\n' : '') + o.lines.join('\\n');
      if (stick) pre.scrollTop = pre.scrollHeight;
    }
    CURSOR = o.next;
  }

  // --- pan / zoom ---
  const stage = document.getElementById('stage');
  const world = document.getElementById('world');
  let view = {x:0, y:0, k:1}, panning = null;
  function applyView(){ world.style.transform =
    'translate('+view.x+'px,'+view.y+'px) scale('+view.k+')'; }
  stage.addEventListener('mousedown', ev => {
    panning = {x: ev.clientX - view.x, y: ev.clientY - view.y};
    stage.classList.add('panning'); });
  window.addEventListener('mousemove', ev => { if (!panning) return;
    view.x = ev.clientX - panning.x; view.y = ev.clientY - panning.y; applyView(); });
  window.addEventListener('mouseup', () => { panning = null;
    stage.classList.remove('panning'); });
  stage.addEventListener('wheel', ev => {
    ev.preventDefault();
    const f = ev.deltaY < 0 ? 1.1 : 0.9;
    view.x = ev.clientX - (ev.clientX - view.x) * f;
    view.y = ev.clientY - (ev.clientY - view.y) * f;
    view.k *= f; applyView();
  }, {passive:false});

  // --- MN-5: the faculty cat board ---------------------------------------
  // Every threshold decision is made server-side in ui_cats.board: the 6-hour
  // rule removes a cat from the payload entirely, and the 180-second rule
  // arrives as display:"dim". Nothing here recomputes either — one home for
  // those numbers, and this loop cannot drift from the tests that pin them.
  const CAT_SVG = `<svg viewBox="0 0 16 16" shape-rendering="crispEdges"
    aria-hidden="true"><g fill="currentColor">
    <rect x="2" y="3" width="2" height="3"/><rect x="12" y="3" width="2" height="3"/>
    <rect x="3" y="5" width="10" height="7"/><rect x="4" y="12" width="8" height="1"/>
    <rect x="13" y="8" width="2" height="1"/><rect x="15" y="6" width="1" height="2"/>
    </g><g fill="#0e1116"><rect x="5" y="7" width="2" height="2"/>
    <rect x="9" y="7" width="2" height="2"/><rect x="7" y="10" width="2" height="1"/>
    </g></svg>`;
  const CAT_LABEL = { 'skill-plugin':'Skills', 'sop':'SOPs',
    'improvements':'Improvements', 'history':'History', 'task-graph':'Tasks',
    'compatibility':'Compatibility', 'pr-green':'PR green' };

  function catAge(s){
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s/60) + 'm';
    return (s/3600).toFixed(1) + 'h';
  }

  function catPanel(cat, el){
    const p = document.getElementById('mn-cat-panel');
    // textContent throughout: task_id and phase are ledger-supplied strings
    // and this is the one place they reach the DOM.
    p.innerHTML = '';
    const h = document.createElement('h3');
    h.textContent = CAT_LABEL[cat.faculty] || cat.faculty;
    p.appendChild(h);
    const rows = [['task', cat.task_id || '—'], ['phase', cat.phase || '—'],
                  ['skill', cat.skill || '—'], ['state', cat.status],
                  ['last seen', catAge(cat.age_secs) + ' ago']];
    for (const [k, v] of rows){
      const d = document.createElement('div'); d.className = 'kv';
      const a = document.createElement('span'); a.textContent = k;
      const b = document.createElement('span'); b.textContent = v;
      d.appendChild(a); d.appendChild(b); p.appendChild(d);
    }
    // The reason this board exists: awake but not moving is a stuck parallel
    // run, and the user asked to be able to see that at a glance.
    if (cat.status === 'active' && cat.age_secs >= 900){
      const w = document.createElement('div'); w.className = 'warn';
      w.textContent = 'active with no update for ' + catAge(cat.age_secs)
        + ' — possible silent hang';
      p.appendChild(w);
    }
    const r = el.getBoundingClientRect();
    p.style.left = Math.round(r.left) + 'px';
    p.style.top = Math.round(r.bottom + 6) + 'px';
    p.style.display = 'block';
  }

  function paintCats(cats){
    const bar = document.getElementById('mn-cats');
    const open = bar.querySelector('.mn-cat.sel');
    const openFaculty = open ? open.dataset.faculty : null;
    bar.innerHTML = '';
    const lbl = document.createElement('span');
    lbl.className = 'lbl'; lbl.textContent = 'faculties';
    bar.appendChild(lbl);
    if (!cats.length){
      const n = document.createElement('span');
      n.className = 'none'; n.textContent = 'no MindNetwork activity';
      bar.appendChild(n);
      document.getElementById('mn-cat-panel').style.display = 'none';
      return;
    }
    for (const cat of cats){
      const el = document.createElement('span');
      el.className = 'mn-cat ' + cat.display;
      el.dataset.faculty = cat.faculty;
      el.innerHTML = CAT_SVG;
      const t = document.createElement('span');
      t.textContent = CAT_LABEL[cat.faculty] || cat.faculty;
      el.appendChild(t);
      el.title = (CAT_LABEL[cat.faculty] || cat.faculty)
        + ' — ' + cat.status + ', last seen ' + catAge(cat.age_secs) + ' ago';
      el.onclick = (ev) => {
        ev.stopPropagation();
        const already = el.classList.contains('sel');
        bar.querySelectorAll('.mn-cat.sel').forEach(x => x.classList.remove('sel'));
        if (already){ document.getElementById('mn-cat-panel').style.display = 'none'; }
        else { el.classList.add('sel'); catPanel(cat, el); }
      };
      // Re-open the panel the poll just replaced, so a 1.2 s refresh does not
      // close a panel the user is reading.
      if (cat.faculty === openFaculty){ el.classList.add('sel'); }
      bar.appendChild(el);
      if (cat.faculty === openFaculty){ catPanel(cat, el); }
    }
  }
  document.addEventListener('click', () => {
    document.getElementById('mn-cat-panel').style.display = 'none';
    document.querySelectorAll('#mn-cats .mn-cat.sel')
      .forEach(x => x.classList.remove('sel'));
  });

  async function pollCats(){
    try { paintCats((await api('/api/mindnet/cats')).cats || []); } catch(_){}
  }

  // --- poll loop ---
  async function poll(){
    try {
      const res = await api('/api/workspace/agents');
      AGENTS = res.agents || [];
      if (res.mission) document.getElementById('mission').value = res.mission.mission;
      paint();
      await pollInspector();
    } catch(_){}
    // Outside the try above on purpose: a workspace fetch failure must not
    // also take out the cat board, which is the surface you look at when
    // something is wrong.
    await pollCats();
  }
  (async function loop(){ await poll(); setTimeout(loop, 1200); })();
</script>
</body></html>"""


def render_swarm(token: str) -> str:
    # Escape every "<" in the JSON-encoded token so neither "</script>" nor
    # "<!--" can break out of the inline <script> block.
    return _PAGE.replace("__TOKEN__", json.dumps(token).replace("<", "\\u003c"))
