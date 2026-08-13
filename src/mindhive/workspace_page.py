# src/mindhive/workspace_page.py
"""MindParallel — the Workspace's parallel-terminal view.

A self-contained HTML page (served inside the dashboard's iframe, like the
graph view) showing up to 16 agent panes at once, in either of two layouts:
a binary split tree (any pane splits horizontally or vertically) or a tab
strip (one full-size agent per tab, each spawnable under its own account
profile). Each pane is one workspace agent: a headless Claude Code run whose
stream is polled from /api/workspace/output.

Customize-then-run: dragging a skill chip onto a pane STAGES it into that
pane's recipe (removable chips + a command input); nothing executes until the
pane's explicit ▶ Run, which composes the staged skills and command into one
prompt. Kanban cards keep dispatching immediately.

Orphan healing: agents live server-side while the split layout lives in
localStorage, so on every poll any server agent not shown anywhere is adopted
into a pane (tabs render from server truth by construction), and a Clear idle
button culls settled agents server-side.

Pure presentation: token in, HTML string out. All state lives server-side in
workspace.py except the layout, view mode, and staged recipes, which are the
browser's own (localStorage).
"""
import json

from . import theme

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MindParallel</title>
<style>
/*__STATUS__*/
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.45 system-ui, sans-serif; background:#0e1116; color:#e6edf3;
    height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  header { display:flex; align-items:center; gap:12px; padding:8px 14px;
    background:#161b22; border-bottom:1px solid #30363d; flex:0 0 auto; }
  header h1 { margin:0; font-size:14px; letter-spacing:1px; }
  header .lbl { color:#8b949e; font-size:12px; }
  button { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px;
    padding:4px 10px; cursor:pointer; font:inherit; }
  button:hover { background:#30363d; }
  button.warn:hover { color:var(--bad); }
  input, select, textarea { background:#0e1116; color:#e6edf3; border:1px solid #30363d;
    border-radius:6px; padding:4px 8px; font:inherit; }
  input:focus, select:focus, textarea:focus { outline:none; border-color:#58a6ff; }
  #main { flex:1 1 auto; display:flex; min-height:0; }
  #side { width:230px; flex:0 0 auto; background:#161b22; border-right:1px solid #30363d;
    padding:10px; overflow-y:auto; }
  #side h3 { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:#8b949e;
    margin:12px 2px 6px; }
  .skchip { display:block; cursor:grab; color:#9ecbff; background:#12182a;
    border:1px solid #2a3550; border-radius:6px; padding:3px 8px; margin:4px 0;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:12px; }
  .skchip:hover { background:#1b2540; }
  .prof { display:flex; align-items:center; gap:6px; margin:4px 0; font-size:12px; }
  .prof .name { flex:1; overflow:hidden; text-overflow:ellipsis; }
  .prof button { padding:0 5px; font-size:10px; }
  .adot { width:8px; height:8px; border-radius:50%; background:#3a4054; flex:0 0 auto; }
  .adot.ok { background:var(--ok); } .adot.expired { background:var(--warn); }
  .adot.none { background:#ff6b6b; } .adot.unknown { background:#8b949e; }
  .pin select { max-width:110px; font-size:11px; }
  #grid { flex:1 1 auto; display:flex; min-width:0; padding:6px; gap:0; }
  #grid.tabs { flex-direction:column; padding:0 6px 6px; }
  #tabbar { display:flex; gap:4px; padding:6px 0 0; overflow-x:auto; flex:0 0 auto; }
  .tab { display:flex; align-items:center; gap:6px; padding:4px 10px; cursor:pointer;
    border:1px solid #30363d; border-bottom:none; border-radius:8px 8px 0 0;
    background:#161b22; font-size:12px; white-space:nowrap; }
  .tab.on { background:#0e1116; border-color:#58a6ff; }
  .tab .tprof { color:#8b949e; font-size:11px; }
  .split { display:flex; flex:1; min-width:0; min-height:0; gap:6px; }
  .split.v { flex-direction:column; }
  .pane { flex:1; min-width:0; min-height:0; display:flex; flex-direction:column;
    background:#0e1116; border:1px solid #30363d; border-radius:8px; overflow:hidden; }
  .pane.drop { border-color:#3d6; box-shadow:0 0 0 1px #3d6 inset; }
  .phead { display:flex; align-items:center; gap:6px; padding:3px 8px;
    background:#161b22; border-bottom:1px solid #30363d; flex:0 0 auto; font-size:12px; }
  .phead .nm { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .phead .sp { flex:1; }
  .phead button { padding:0 6px; font-size:11px; border-radius:4px; }
  .dot { width:8px; height:8px; border-radius:50%; background:#3a4054; flex:0 0 auto; }
  .dot.running { background:var(--warn); animation:pulse 1s infinite; }
  .dot.done { background:var(--ok); } .dot.error { background:#ff6b6b; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  /* D8: the sheet that turns an animation ON owns switching it off, so a
     second consumer of this CSS inherits the guard instead of quietly
     losing it. Repeating motion is a trigger for some people, not a
     preference. This page is served into the dashboard's own iframe, so
     the host page's guard does not reach it. */
  @media (prefers-reduced-motion: reduce) {
    .dot.running { animation:none !important; }
  }
  .pout { flex:1; overflow-y:auto; padding:8px 10px; margin:0; white-space:pre-wrap;
    word-break:break-word; font:12px/1.5 ui-monospace, Consolas, monospace; color:#c9d1d9; }
  .recipe { display:flex; flex-wrap:wrap; gap:4px; padding:4px 6px;
    border-top:1px solid #30363d; flex:0 0 auto; }
  .recipe:empty { display:none; }
  .rchip { display:inline-flex; align-items:center; gap:5px; color:#9ecbff;
    background:#12182a; border:1px solid #2a3550; border-radius:999px;
    padding:1px 8px; font-size:11px; }
  .rchip b { cursor:pointer; color:var(--bad); font-weight:400; }
  .pin { display:flex; gap:6px; padding:6px; border-top:1px solid #30363d; flex:0 0 auto; }
  .pin input { flex:1; min-width:0; }
  .spawn { margin:auto; text-align:center; color:#8b949e; }
  .spawn input, .spawn select { display:block; margin:6px auto; width:180px; }
  .spawn .recipe { border-top:none; justify-content:center; margin-top:4px; }
  #board { flex:0 0 auto; border-top:1px solid #30363d; background:#161b22;
    padding:8px 12px; max-height:34vh; overflow-y:auto; }
  #board.hidden { display:none; }
  #cols { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
  .col { background:#0e1116; border:1px solid #30363d; border-radius:8px; padding:8px;
    min-height:70px; }
  .col h4 { margin:0 0 6px; font-size:11px; text-transform:uppercase; color:#8b949e; }
  .card { background:#21262d; border:1px solid #30363d; border-radius:6px; padding:6px 8px;
    margin:5px 0; cursor:grab; font-size:12px; }
  .card .who { color:#8b949e; font-size:11px; }
  .card button { font-size:11px; padding:0 6px; margin-left:6px; }
  .col.drop { border-color:#3d6; }
  #toast { position:fixed; bottom:14px; left:50%; transform:translateX(-50%);
    background:#21262d; border:1px solid #3d6; border-radius:8px; padding:6px 14px;
    display:none; z-index:9; }
  .err { color:var(--bad); }
</style></head>
<body>
<header>
  <h1>MINDPARALLEL</h1>
  <span class="lbl" id="count">0/16 agents</span>
  <button id="viewbtn" onclick="toggleView()">Tabs</button>
  <button id="panebtn" onclick="togglePane()">&#43; pane</button>
  <button onclick="toggleBoard()">Kanban</button>
  <button class="warn" onclick="clearIdle()">Clear idle</button>
  <span class="lbl" style="margin-left:auto">drop skills to stage a recipe &middot;
    &#9654; Run launches it &middot; cards dispatch instantly</span>
</header>
<div id="main">
  <div id="side">
    <h3>Skills (drag onto a pane)</h3>
    <input id="skq" placeholder="search skills&hellip;" style="width:100%"
      oninput="paintSkills()">
    <div id="skills" class="lbl">loading&hellip;</div>
    <h3>Accounts / profiles</h3>
    <div id="profs"></div>
    <input id="pname" placeholder="profile name" style="width:100%;margin-top:4px">
    <input id="pdir" placeholder="CLAUDE_CONFIG_DIR path" style="width:100%;margin-top:4px">
    <button style="margin-top:4px" onclick="addProfile()">Add profile</button>
    <div class="lbl" style="margin-top:6px">Each profile is a separate Claude Code
      login (its own CLAUDE_CONFIG_DIR), so accounts run in parallel &mdash; one per
      tab or pane, up to 16.</div>
  </div>
  <div id="grid"></div>
</div>
<div id="board" class="hidden">
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
    <b style="font-size:12px">KANBAN &mdash; cards dispatch agents</b>
    <input id="ctitle" placeholder="task title" style="min-width:180px">
    <input id="cprompt" placeholder="prompt for the agent (defaults to title)" style="flex:1">
    <button onclick="addCard()">Add card</button>
  </div>
  <div id="cols"></div>
</div>
<div id="toast"></div>
<script>
  const TOKEN = __TOKEN__;
  const PROJ = new URLSearchParams(location.search).get('project') || '';
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

  // --- view mode: 'splits' (binary tree) or 'tabs' (one full-size agent) ---
  let VIEW = localStorage.getItem('mh_ws_view') || 'splits';
  let ACTIVE = localStorage.getItem('mh_ws_tab') || null;
  function toggleView(){
    VIEW = VIEW === 'splits' ? 'tabs' : 'splits';
    localStorage.setItem('mh_ws_view', VIEW);
    buildGrid();
  }

  // --- staged recipes: skills dropped on a pane wait for an explicit Run.
  // Keyed by agent id (or pane id until the pane has an agent); persisted so
  // a composed recipe survives a refresh.
  let STAGED = {};
  try { STAGED = JSON.parse(localStorage.getItem('mh_ws_staged') || '{}') || {}; }
  catch(_) { STAGED = {}; }
  function saveStaged(){ localStorage.setItem('mh_ws_staged', JSON.stringify(STAGED)); }
  function stageKey(n){ return n.agent || n.pid; }

  // --- split-tree layout: leaf {t:'leaf', agent, pid} / split {t:'split', dir, a, b}
  let LAYOUT = null;
  let PSEQ = 1;
  function newLeaf(){ return {t:'leaf', agent:null, pid:'p'+(PSEQ++)}; }
  function loadLayout(){
    try {
      const raw = JSON.parse(localStorage.getItem('mh_ws_layout') || 'null');
      if (raw && raw.t) { LAYOUT = raw; fixSeq(raw); return; }
    } catch(_){}
    LAYOUT = newLeaf();
  }
  function fixSeq(n){
    if (n.t === 'leaf') { const k = parseInt(String(n.pid||'').slice(1)) || 0;
      if (k >= PSEQ) PSEQ = k + 1; return; }
    fixSeq(n.a); fixSeq(n.b);
  }
  function saveLayout(){ localStorage.setItem('mh_ws_layout', JSON.stringify(LAYOUT)); }
  function leaves(n, acc){ acc = acc || [];
    if (n.t === 'leaf') acc.push(n); else { leaves(n.a, acc); leaves(n.b, acc); }
    return acc; }
  function findParent(n, leaf, parent){
    if (n === leaf) return parent;
    if (n.t !== 'split') return null;
    return findParent(n.a, leaf, n) || findParent(n.b, leaf, n);
  }
  function splitPane(pid, dir){
    if (leaves(LAYOUT).length >= 16) { toast('16-pane cap reached', true); return; }
    const leaf = leaves(LAYOUT).find(l => l.pid === pid);
    if (!leaf) return;
    const clone = {t:'leaf', agent:leaf.agent, pid:leaf.pid};
    const fresh = newLeaf();
    const split = {t:'split', dir:dir, a:clone, b:fresh};
    const parent = findParent(LAYOUT, leaf, null);
    if (!parent) LAYOUT = split;
    else if (parent.a === leaf) parent.a = split; else parent.b = split;
    saveLayout(); buildGrid();
  }
  function closePane(pid){
    if (VIEW === 'tabs') {
      const id = pid.indexOf('tab-') === 0 ? pid.slice(4) : null;
      const a = id && AGENTS[id];
      if (!a) return;
      if (a.status === 'running' && !confirm(a.name+' is running. Close and stop it?')) return;
      delete STAGED[id]; saveStaged();
      apiPost('/api/workspace/remove', {id:id}).then(() => {
        delete AGENTS[id];
        if (ACTIVE === id) ACTIVE = null;
        buildGrid();
      });
      return;
    }
    const leaf = leaves(LAYOUT).find(l => l.pid === pid);
    if (!leaf) return;
    const a = leaf.agent && AGENTS[leaf.agent];
    if (a && a.status === 'running' && !confirm(a.name+' is running. Close and stop it?')) return;
    if (leaf.agent) { delete STAGED[leaf.agent]; saveStaged();
      apiPost('/api/workspace/remove', {id: leaf.agent}); }
    const parent = findParent(LAYOUT, leaf, null);
    if (!parent) { leaf.agent = null; }        // last pane: just empty it
    else {
      const sib = parent.a === leaf ? parent.b : parent.a;
      const gp = findParent(LAYOUT, parent, null);
      if (!gp) LAYOUT = sib;
      else if (gp.a === parent) gp.a = sib; else gp.b = sib;
    }
    saveLayout(); buildGrid();
  }
  function togglePane(){
    if (VIEW !== 'splits') return;
    const ls = leaves(LAYOUT);
    splitPane(ls[ls.length-1].pid, ls.length % 2 ? 'h' : 'v');
  }

  // --- render into #grid: split tree or tab strip; PANES maps pid -> {el, cursor, node}
  let PANES = {};
  function buildGrid(){
    const grid = document.getElementById('grid');
    grid.innerHTML = ''; PANES = {};
    document.getElementById('viewbtn').textContent = VIEW === 'splits' ? 'Tabs' : 'Splits';
    document.getElementById('panebtn').style.display = VIEW === 'splits' ? '' : 'none';
    if (VIEW === 'tabs') { grid.classList.add('tabs'); buildTabs(grid); }
    else { grid.classList.remove('tabs'); grid.appendChild(buildNode(LAYOUT)); }
    paintAgents();
  }
  function tabAgents(){
    return Object.values(AGENTS).filter(a => !a.swarm)
      .sort((x,y) => x.created < y.created ? -1 : (x.created > y.created ? 1 : 0));
  }
  function tabSig(){
    return tabAgents().map(a => a.id+':'+a.status+':'+a.name+':'+a.profile).join('|');
  }
  function buildTabs(grid){
    const ags = tabAgents();
    if (ACTIVE && !AGENTS[ACTIVE]) ACTIVE = null;
    if (!ACTIVE && ags.length) ACTIVE = ags[0].id;
    const bar = document.createElement('div'); bar.id = 'tabbar';
    bar.dataset.sig = tabSig();
    ags.forEach(a => {
      const t = document.createElement('div');
      t.className = 'tab' + (a.id === ACTIVE ? ' on' : '');
      t.innerHTML = '<span class="dot '+a.status+'"></span>'+esc(a.name)
        + ' <span class="tprof">'+esc(a.profile)+'</span>';
      t.onclick = () => { ACTIVE = a.id;
        localStorage.setItem('mh_ws_tab', ACTIVE); buildGrid(); };
      bar.appendChild(t);
    });
    const plus = document.createElement('div');
    plus.className = 'tab' + (ACTIVE === null ? ' on' : '');
    plus.textContent = '+';
    plus.title = 'spawn a new agent (its own account profile)';
    plus.onclick = () => { ACTIVE = null;
      localStorage.setItem('mh_ws_tab', ''); buildGrid(); };
    bar.appendChild(plus);
    grid.appendChild(bar);
    const node = ACTIVE ? {t:'leaf', agent:ACTIVE, pid:'tab-'+ACTIVE}
                        : {t:'leaf', agent:null, pid:'tab-new'};
    grid.appendChild(buildNode(node));
  }
  function buildNode(n){
    if (n.t === 'split') {
      const d = document.createElement('div');
      d.className = 'split' + (n.dir === 'v' ? ' v' : '');
      d.appendChild(buildNode(n.a)); d.appendChild(buildNode(n.b));
      return d;
    }
    const p = document.createElement('div');
    p.className = 'pane'; p.dataset.pid = n.pid;
    p.addEventListener('dragover', ev => { ev.preventDefault(); p.classList.add('drop'); });
    p.addEventListener('dragleave', () => p.classList.remove('drop'));
    p.addEventListener('drop', ev => { ev.preventDefault(); p.classList.remove('drop');
      onDrop(n, ev); });
    p.innerHTML = paneHTML(n);
    wirePane(p, n);
    PANES[n.pid] = {el: p, cursor: 0, node: n};
    return p;
  }
  function paneHTML(n){
    const head = '<div class="phead">'
      + '<span class="dot" data-r="dot"></span><span class="nm" data-r="nm">&mdash;</span>'
      + '<span class="sp"></span>'
      + (VIEW === 'splits'
         ? '<button title="split horizontally" data-r="sh">&#8596;</button>'
           + '<button title="split vertically" data-r="sv">&#8597;</button>' : '')
      + '<button title="stop the current run" data-r="stop">&#9632;</button>'
      + '<button class="warn" title="close pane" data-r="close">&#10005;</button></div>';
    if (!n.agent) {
      return head + '<div class="spawn"><div>empty pane</div>'
        + '<input data-r="nmIn" placeholder="agent name" value="Claude '+(PSEQ)+'">'
        + '<select data-r="prof" title="account profile"></select>'
        + '<select data-r="model" title="model for this agent"></select>'
        + '<button data-r="spawn">Spawn agent</button>'
        + '<div class="recipe" data-r="recipe"></div>'
        + '<button data-r="run" style="margin-top:4px">&#9654; Spawn &amp; Run</button>'
        + '<div style="margin-top:6px;font-size:11px">drop skills to stage a recipe'
        + ' &middot; drop a kanban card to dispatch</div></div>';
    }
    return head + '<pre class="pout" data-r="out"></pre>'
      + '<div class="recipe" data-r="recipe"></div>'
      + '<div class="pin"><select data-r="model" title="model for the next run"></select>'
      + '<input data-r="in" placeholder="command for this agent&hellip; (Enter runs)">'
      + '<button data-r="run" title="run the staged skills + command">&#9654; Run</button></div>';
  }
  function modelOptions(sel, current){
    current = current || '';
    const opts = MODELS.map(m => '<option value="'+esc(m.id)+'">'
      + esc(m.label)+'</option>');
    if (!MODELS.some(m => m.id === current))       // keep a free-typed id visible
      opts.push('<option value="'+esc(current)+'">'+esc(current)+'</option>');
    opts.push('<option value="__custom">custom&hellip;</option>');
    sel.innerHTML = opts.join('');
    sel.value = current;
  }
  function pickedModel(sel){
    if (sel.value !== '__custom') return sel.value;
    return (prompt('model id (as the claude CLI --model flag accepts)') || '').trim();
  }
  function modelLabel(id){
    const m = MODELS.find(m => m.id === id);
    return m ? m.label : id;
  }
  async function spawnAgent(name, profile, model){
    const res = await apiPost('/api/workspace/spawn',
      {name: name, role: 'agent', profile: profile || 'default', model: model || ''});
    if (!res.ok) { toast(res.error||'spawn failed', true); return null; }
    AGENTS[res.agent.id] = res.agent;   // seed so paint doesn't see "vanished"
    return res.agent;
  }
  function adoptSpawn(n, agent){
    // migrate any recipe staged on the empty pane to the new agent id
    if (STAGED[n.pid]) { STAGED[agent.id] = STAGED[n.pid];
      delete STAGED[n.pid]; saveStaged(); }
    // a poll's adoptOrphans may have claimed this just-spawned agent for
    // another empty leaf while the spawn POST was in flight — unclaim it so
    // two panes never share one agent
    if (VIEW === 'splits') leaves(LAYOUT).forEach(l => {
      if (l.agent === agent.id) l.agent = null; });
    n.agent = agent.id;
    if (VIEW === 'tabs') { ACTIVE = agent.id;
      localStorage.setItem('mh_ws_tab', ACTIVE); }
    else saveLayout();
  }
  function wirePane(p, n){
    const q = r => p.querySelector('[data-r="'+r+'"]');
    const sh = q('sh'); if (sh) sh.onclick = () => splitPane(n.pid, 'h');
    const sv = q('sv'); if (sv) sv.onclick = () => splitPane(n.pid, 'v');
    q('close').onclick = () => closePane(n.pid);
    q('stop').onclick = () => { if (n.agent) apiPost('/api/workspace/stop', {id:n.agent}); };
    const run = q('run'); if (run) run.onclick = () => runPane(n, p);
    if (!n.agent) {
      const sel = q('prof');
      sel.innerHTML = PROFILES.map(pr => '<option>'+esc(pr.name)+'</option>').join('');
      modelOptions(q('model'), '');
      q('spawn').onclick = async () => {
        const agent = await spawnAgent(q('nmIn').value, sel.value,
                                       pickedModel(q('model')));
        if (!agent) return;
        adoptSpawn(n, agent); buildGrid();
      };
    } else {
      const msel = q('model');
      modelOptions(msel, (AGENTS[n.agent]||{}).model || '');
      msel.onchange = async () => {
        const res = await apiPost('/api/workspace/set_model',
          {id:n.agent, model: pickedModel(msel)});
        if (!res.ok) { toast(res.error||'failed', true);
          modelOptions(msel, (AGENTS[n.agent]||{}).model || ''); return; }
        AGENTS[n.agent] = res.agent;
        modelOptions(msel, res.agent.model);   // reflect a custom-typed id
        toast('model \\u2192 ' + (res.agent.model ? modelLabel(res.agent.model)
                                                  : 'account default'));
      };
      const inp = q('in');
      inp.addEventListener('keydown', e => { if (e.key === 'Enter') runPane(n, p); });
    }
    paintRecipe(p, n);
  }
  function paintRecipe(p, n){
    const el = p.querySelector('[data-r="recipe"]');
    if (!el) return;
    const key = stageKey(n);
    const sk = STAGED[key] || [];
    el.innerHTML = sk.map((s,i) => '<span class="rchip" title="'+esc(s.plugin||'local')+'">'
      + esc(s.name)+'<b data-i="'+i+'" title="unstage">&#10005;</b></span>').join('');
    el.querySelectorAll('b[data-i]').forEach(b => b.onclick = () => {
      sk.splice(parseInt(b.dataset.i), 1);
      if (!sk.length) delete STAGED[key]; else STAGED[key] = sk;
      saveStaged(); paintRecipe(p, n);
    });
    const run = p.querySelector('[data-r="run"]');
    if (run && !n.agent) run.style.display = sk.length ? '' : 'none';
  }
  // one Run = one server-composed pipeline: the staged skills + the typed
  // command go to run_recipe, which enriches them with MindHive's learned
  // history (skill health, prevention rules, ledger recall) before launching
  async function runPane(n, p){
    const q = r => p.querySelector('[data-r="'+r+'"]');
    const cmd = (q('in') ? q('in').value : '').trim();
    if (!(STAGED[stageKey(n)] || []).length && !cmd) {
      toast('stage a skill or type a command first', true); return;
    }
    const wasEmpty = !n.agent;
    if (wasEmpty) {
      const agent = await spawnAgent(q('nmIn') ? q('nmIn').value : '',
                                     q('prof') ? q('prof').value : 'default',
                                     q('model') ? pickedModel(q('model')) : '');
      if (!agent) return;
      adoptSpawn(n, agent);
    }
    // hold the id locally: a concurrent repaint may reset n.agent, and the
    // send must still reach the agent that was just spawned
    const agentId = n.agent;
    const res = await apiPost('/api/workspace/run_recipe',
      {id:agentId, skills: STAGED[agentId] || [], command: cmd});
    if (!res.ok) { toast(res.error||'run failed', true);
      if (wasEmpty) buildGrid(); return; }
    delete STAGED[agentId]; saveStaged();   // recipe consumed by this run
    if (q('in')) q('in').value = '';
    toast('run started');
    if (wasEmpty) buildGrid(); else paintRecipe(p, n);
  }
  async function onDrop(n, ev){
    let data;
    try { data = JSON.parse(ev.dataTransfer.getData('text/plain') || 'null'); }
    catch(_) { data = null; }
    if (!data) return;
    if (data.kind === 'skill') {
      // stage, don't run: the recipe launches only on an explicit ▶ Run
      const key = stageKey(n);
      (STAGED[key] = STAGED[key] || []).push({name:data.name, plugin:data.plugin});
      saveStaged();
      const pane = PANES[n.pid];
      if (pane) paintRecipe(pane.el, n);
      toast(data.name + ' staged \\u2014 press \\u25b6 Run');
      return;
    }
    if (data.kind === 'task') {
      if (!n.agent) {                     // auto-spawn a pane to receive the card
        const agent = await spawnAgent('Claude '+PSEQ, 'default');
        if (!agent) return;
        adoptSpawn(n, agent); buildGrid();
      }
      // hold the id locally: a concurrent repaint may reset n.agent
      const agentId = n.agent;
      const res = await apiPost('/api/workspace/board_dispatch',
        {task_id:data.id, agent_id:agentId});
      toast(res.ok ? 'card dispatched' : (res.error||'dispatch failed'), !res.ok);
      loadBoard();
    }
  }

  // --- live agent state + incremental output ---
  let AGENTS = {};
  async function poll(){
    try {
      const res = await api('/api/workspace/agents');
      AGENTS = {};
      (res.agents||[]).forEach(a => AGENTS[a.id] = a);
      document.getElementById('count').textContent =
        (res.agents||[]).length + '/' + (res.cap||16) + ' agents';
      paintAgents();
      for (const pid in PANES) {
        const pane = PANES[pid];
        const aid = pane.node.agent;
        if (!aid || !AGENTS[aid]) continue;
        const out = pane.el.querySelector('[data-r="out"]');
        if (!out) continue;
        const o = await api('/api/workspace/output?id='+aid+'&since='+pane.cursor);
        if (!o.ok) continue;
        if (o.lines && o.lines.length) {
          const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
          out.textContent += (out.textContent ? '\\n' : '') + o.lines.join('\\n');
          if (stick) out.scrollTop = out.scrollHeight;
        }
        pane.cursor = o.next;
      }
      adoptOrphans();
    } catch(_){}
  }
  // A just-spawned agent can be missing from one poll cycle: a request already
  // in flight when the spawn landed returns a list without it, and treating
  // that single stale miss as "vanished" wiped the pane and made the follow-up
  // skill/card send fail with "unknown agent". Reset a pane only after two
  // consecutive polls agree the agent is gone.
  let MISSES = {};
  function paintAgents(){
    if (VIEW === 'tabs') paintTabs();
    for (const pid in PANES) {
      const pane = PANES[pid], n = pane.node;
      if (VIEW === 'splits' && n.agent && !AGENTS[n.agent] && POLLED) {
        MISSES[n.agent] = (MISSES[n.agent] || 0) + 1;
        if (MISSES[n.agent] < 2) continue;
        delete MISSES[n.agent];
        n.agent = null; saveLayout();
        buildGrid(); return;     // agent vanished server-side — reset pane
      }
      if (n.agent) delete MISSES[n.agent];
      const a = n.agent && AGENTS[n.agent]; if (!a) continue;
      const nm = pane.el.querySelector('[data-r="nm"]');
      const dot = pane.el.querySelector('[data-r="dot"]');
      if (nm) nm.textContent = a.name + ' \\u00b7 ' + a.profile
        + (a.model ? ' \\u00b7 ' + modelLabel(a.model) : '')
        + (a.role !== 'agent' ? ' \\u00b7 ' + a.role : '')
        + (a.last_turns != null ? ' \\u00b7 ' + a.last_turns + ' turns' : '')
        + (a.last_cost != null ? ' \\u00b7 $' + (+a.last_cost).toFixed(2) : '');
      if (dot) dot.className = 'dot ' + a.status;
    }
  }
  function paintTabs(){
    // rebuild the strip only when the agent set / statuses actually changed
    const bar = document.getElementById('tabbar');
    if (bar && bar.dataset.sig !== tabSig()) buildGrid();
  }
  // The split layout lives in this browser while agents live server-side, so
  // agents can orphan (count says 9/16, panes look empty). Every poll, adopt
  // any non-swarm agent no pane shows: fill empty leaves first, then split in
  // new ones up to the 16-pane cap. Tabs render from server truth directly.
  function adoptOrphans(){
    if (VIEW !== 'splits' || !POLLED) return;
    const shown = new Set(leaves(LAYOUT).map(l => l.agent).filter(Boolean));
    const orphans = Object.values(AGENTS).filter(a => !a.swarm && !shown.has(a.id));
    if (!orphans.length) return;
    let changed = false;
    for (const a of orphans) {
      let leaf = leaves(LAYOUT).find(l => !l.agent);
      if (!leaf) {
        const ls = leaves(LAYOUT);
        if (ls.length >= 16) break;
        const last = ls[ls.length-1];
        const clone = {t:'leaf', agent:last.agent, pid:last.pid};
        const fresh = newLeaf();
        const split = {t:'split', dir: ls.length % 2 ? 'h' : 'v', a:clone, b:fresh};
        const parent = findParent(LAYOUT, last, null);
        if (!parent) LAYOUT = split;
        else if (parent.a === last) parent.a = split; else parent.b = split;
        leaf = fresh;
      }
      // an adopted agent inherits any recipe staged on the empty pane
      if (STAGED[leaf.pid] && !STAGED[a.id]) {
        STAGED[a.id] = STAGED[leaf.pid]; delete STAGED[leaf.pid]; saveStaged();
      }
      leaf.agent = a.id; changed = true;
    }
    if (changed) { saveLayout(); buildGrid(); }
  }
  async function clearIdle(){
    if (!confirm('Remove every idle (non-running) agent?')) return;
    const res = await apiPost('/api/workspace/clear_idle', {});
    toast(res.ok ? 'removed ' + res.removed + ' idle agent(s)'
                 : (res.error||'failed'), !res.ok);
  }
  let POLLED = false;

  // --- skills sidebar (drag source) ---
  let SKILLS = [];
  async function loadSkills(){
    try {
      const res = await api('/api/plugins/inventory');
      const inv = res.inventory || {};
      SKILLS = [];
      (inv.local_skills||[]).forEach(s => SKILLS.push({name:s.name, plugin:null}));
      (inv.plugins||[]).forEach(p => (p.skills||[]).forEach(
        s => SKILLS.push({name:s.name, plugin:p.name})));
      paintSkills();
    } catch(_) { document.getElementById('skills').textContent = 'unavailable'; }
  }
  function paintSkills(){
    const q = (document.getElementById('skq').value||'').toLowerCase();
    const el = document.getElementById('skills');
    const hits = SKILLS.filter(s => !q || (s.name+' '+(s.plugin||'')).toLowerCase().includes(q))
      .slice(0, 80);
    el.innerHTML = hits.map((s,i) => '<span class="skchip" draggable="true" data-i="'+i
      +'" title="'+esc(s.plugin||'local')+'">'+esc(s.name)+'</span>').join('')
      || '<span class="lbl">no match</span>';
    el.querySelectorAll('.skchip').forEach(c => c.addEventListener('dragstart', ev => {
      const s = hits[parseInt(c.dataset.i)];
      ev.dataTransfer.setData('text/plain', JSON.stringify(
        {kind:'skill', name:s.name, plugin:s.plugin}));
    }));
  }

  // --- profiles (accounts): auth health + login/trust per profile ---
  let PROFILES = [{name:'default'}];
  let MODELS = [{id:'', label:'account default'}];
  let AUTH = {};
  async function loadProfiles(){
    try {
      const res = await api('/api/workspace/profiles');
      PROFILES = res.profiles || PROFILES;
      if (res.models && res.models.length) MODELS = res.models;
      paintProfiles();
      loadAuth();
    } catch(_){}
  }
  async function loadAuth(){
    try {
      const res = await api('/api/workspace/auth');
      AUTH = {};
      (res.auth||[]).forEach(a => AUTH[a.profile] = a);
      paintProfiles();
    } catch(_){}
  }
  function paintProfiles(){
    document.getElementById('profs').innerHTML = PROFILES.map(p => {
      const a = AUTH[p.name] || {state:'unknown', detail:'checking\\u2026'};
      return '<div class="prof">'
        + '<span class="adot '+esc(a.state)+'" title="'+esc(a.detail)+'"></span>'
        + '<span class="name" title="'+esc(p.config_dir||'system default')
        + ' \\u2014 '+esc(a.detail)+'">'+esc(p.name)+'</span>'
        + '<button title="open a login console for this account (type /login there)"'
        + ' onclick="loginProfile(\\''+esc(p.name)+'\\')">login</button>'
        + '<button title="pre-accept the workspace trust dialog for this project"'
        + ' onclick="trustProfile(\\''+esc(p.name)+'\\')">trust</button>'
        + (p.name==='default' ? '' :
           '<button class="warn" onclick="rmProfile(\\''+esc(p.name)+'\\')">&#10005;</button>')
        + '</div>';
    }).join('');
  }
  async function loginProfile(name){
    const res = await apiPost('/api/workspace/profile_login', {name:name});
    toast(res.ok ? res.detail : (res.error||'failed'), !res.ok);
    setTimeout(loadAuth, 15000);   // re-check after the user logs in
  }
  async function trustProfile(name){
    const res = await apiPost('/api/workspace/profile_trust', {name:name});
    toast(res.ok ? res.detail : (res.error||'failed'), !res.ok);
  }
  async function addProfile(){
    const res = await apiPost('/api/workspace/profile_add',
      {name: document.getElementById('pname').value,
       config_dir: document.getElementById('pdir').value});
    if (!res.ok) { toast(res.error||'failed', true); return; }
    document.getElementById('pname').value=''; document.getElementById('pdir').value='';
    loadProfiles();
  }
  async function rmProfile(name){
    await apiPost('/api/workspace/profile_remove', {name:name}); loadProfiles();
  }

  // --- kanban ---
  const COLS = [['backlog','Backlog'],['running','Running'],['done','Done']];
  let TASKS = [];
  function toggleBoard(){
    document.getElementById('board').classList.toggle('hidden');
    loadBoard();
  }
  async function loadBoard(){
    try { const b = await api('/api/workspace/board'); TASKS = b.tasks||[]; paintBoard(); }
    catch(_){}
  }
  function paintBoard(){
    const cols = document.getElementById('cols');
    cols.innerHTML = COLS.map(c => {
      const cards = TASKS.filter(t => t.status === c[0]);
      return '<div class="col" data-col="'+c[0]+'"><h4>'+c[1]+' ('+cards.length+')</h4>'
        + cards.map(t => '<div class="card" draggable="true" data-id="'+esc(t.id)+'">'
          + esc(t.title)
          + (t.agent_id && AGENTS[t.agent_id] ?
             '<div class="who">\\u25b8 '+esc(AGENTS[t.agent_id].name)+'</div>' : '')
          + (c[0]==='backlog' ? '<button data-d="'+esc(t.id)+'">\\u25b6 dispatch</button>' : '')
          + '</div>').join('')
        + '</div>';
    }).join('');
    cols.querySelectorAll('.card').forEach(el => el.addEventListener('dragstart', ev =>
      ev.dataTransfer.setData('text/plain', JSON.stringify({kind:'task', id:el.dataset.id}))));
    cols.querySelectorAll('.col').forEach(el => {
      el.addEventListener('dragover', ev => { ev.preventDefault(); el.classList.add('drop'); });
      el.addEventListener('dragleave', () => el.classList.remove('drop'));
      el.addEventListener('drop', async ev => {
        ev.preventDefault(); el.classList.remove('drop');
        let d; try { d = JSON.parse(ev.dataTransfer.getData('text/plain')||'null'); }
        catch(_) { return; }
        if (!d || d.kind !== 'task') return;
        const t = TASKS.find(x => x.id === d.id); if (!t) return;
        t.status = el.dataset.col;
        await apiPost('/api/workspace/board_save', {tasks: TASKS});
        loadBoard();
      });
    });
    cols.querySelectorAll('button[data-d]').forEach(b => b.onclick = async () => {
      const res = await apiPost('/api/workspace/board_dispatch', {task_id:b.dataset.d});
      toast(res.ok ? 'dispatched to '+res.agent.name : (res.error||'failed'), !res.ok);
      loadBoard();
    });
  }
  async function addCard(){
    const title = document.getElementById('ctitle').value.trim();
    if (!title) return;
    TASKS.push({title:title, prompt:document.getElementById('cprompt').value.trim()||title,
      status:'backlog'});
    document.getElementById('ctitle').value=''; document.getElementById('cprompt').value='';
    await apiPost('/api/workspace/board_save', {tasks: TASKS});
    loadBoard();
  }

  // boot
  loadLayout(); buildGrid(); loadProfiles(); loadSkills(); loadBoard();
  setInterval(loadAuth, 30000);        // auth health drifts (token expiry)
  (async function loop(){ await poll(); POLLED = true; setTimeout(loop, 1000); })();
</script>
</body></html>"""


# The three status colours are decided in `theme`, not here: this document has its
# own palette, but "done is green, working is amber, failed is red" is the same
# decision in every one of them, and a value repeated across four documents is a
# value that drifts. Substituted at IMPORT rather than per request — a marker that
# is already gone cannot be placed by anything a request carries, which is the
# ordering the token substitution below has to reason about explicitly.
_PAGE = _PAGE.replace("/*__STATUS__*/", theme.STATUS_CSS)


def render_parallel(token: str) -> str:
    # Escape every "<" in the JSON-encoded token so neither "</script>" nor
    # "<!--" can break out of the inline <script> block.
    return _PAGE.replace("__TOKEN__", json.dumps(token).replace("<", "\\u003c"))
