# src/mindhive/workspace_page.py
"""MindParallel — the Workspace's parallel-terminal view.

A self-contained HTML page (served inside the dashboard's iframe, like the
graph view) showing up to 16 agent panes at once. Panes live in a binary split
tree — any pane can split horizontally or vertically — and each pane is one
workspace agent: a headless Claude Code run whose stream is polled from
/api/workspace/output. Drag a skill chip from the sidebar onto a pane and that
agent picks it up; the kanban drawer dispatches cards to free agents.

Pure presentation: token in, HTML string out. All state lives server-side in
workspace.py except the split layout, which is the browser's own (localStorage).
"""
import json

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MindParallel</title>
<style>
  :root { color-scheme: dark; }
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
  button.warn:hover { color:#ff8a8a; }
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
  #grid { flex:1 1 auto; display:flex; min-width:0; padding:6px; gap:0; }
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
  .dot.running { background:#e0b341; animation:pulse 1s infinite; }
  .dot.done { background:#46c46a; } .dot.error { background:#ff6b6b; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  .pout { flex:1; overflow-y:auto; padding:8px 10px; margin:0; white-space:pre-wrap;
    word-break:break-word; font:12px/1.5 ui-monospace, Consolas, monospace; color:#c9d1d9; }
  .pin { display:flex; gap:6px; padding:6px; border-top:1px solid #30363d; flex:0 0 auto; }
  .pin input { flex:1; min-width:0; }
  .spawn { margin:auto; text-align:center; color:#8b949e; }
  .spawn input, .spawn select { display:block; margin:6px auto; width:180px; }
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
  .err { color:#ff8a8a; }
</style></head>
<body>
<header>
  <h1>MINDPARALLEL</h1>
  <span class="lbl" id="count">0/16 agents</span>
  <button onclick="togglePane()">&#43; pane</button>
  <button onclick="toggleBoard()">Kanban</button>
  <span class="lbl" style="margin-left:auto">drag a skill onto a pane &middot; drag cards between columns</span>
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
      login (its own CLAUDE_CONFIG_DIR), so accounts run in parallel.</div>
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
    const leaf = leaves(LAYOUT).find(l => l.pid === pid);
    if (!leaf) return;
    const a = leaf.agent && AGENTS[leaf.agent];
    if (a && a.status === 'running' && !confirm(a.name+' is running. Close and stop it?')) return;
    if (leaf.agent) apiPost('/api/workspace/remove', {id: leaf.agent});
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
    const ls = leaves(LAYOUT);
    splitPane(ls[ls.length-1].pid, ls.length % 2 ? 'h' : 'v');
  }

  // --- render the tree into nested flex divs; PANES maps pid -> {out, cursor}
  let PANES = {};
  function buildGrid(){
    const grid = document.getElementById('grid');
    grid.innerHTML = ''; PANES = {};
    grid.appendChild(buildNode(LAYOUT));
    paintAgents();
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
    PANES[n.pid] = {el: p, cursor: 0, agent: n.agent};
    return p;
  }
  function paneHTML(n){
    const head = '<div class="phead">'
      + '<span class="dot" data-r="dot"></span><span class="nm" data-r="nm">&mdash;</span>'
      + '<span class="sp"></span>'
      + '<button title="split horizontally" data-r="sh">&#8596;</button>'
      + '<button title="split vertically" data-r="sv">&#8597;</button>'
      + '<button title="stop the current run" data-r="stop">&#9632;</button>'
      + '<button class="warn" title="close pane" data-r="close">&#10005;</button></div>';
    if (!n.agent) {
      return head + '<div class="spawn"><div>empty pane</div>'
        + '<input data-r="nmIn" placeholder="agent name" value="Claude '+(PSEQ)+'">'
        + '<select data-r="prof"></select>'
        + '<button data-r="spawn">Spawn agent</button>'
        + '<div style="margin-top:6px;font-size:11px">or drop a skill / kanban card here</div></div>';
    }
    return head + '<pre class="pout" data-r="out"></pre>'
      + '<div class="pin"><input data-r="in" placeholder="prompt this agent&hellip; (Enter)">'
      + '<button data-r="send">&#9654;</button></div>';
  }
  function wirePane(p, n){
    const q = r => p.querySelector('[data-r="'+r+'"]');
    q('sh').onclick = () => splitPane(n.pid, 'h');
    q('sv').onclick = () => splitPane(n.pid, 'v');
    q('close').onclick = () => closePane(n.pid);
    q('stop').onclick = () => { if (n.agent) apiPost('/api/workspace/stop', {id:n.agent}); };
    if (!n.agent) {
      const sel = q('prof');
      sel.innerHTML = PROFILES.map(pr => '<option>'+esc(pr.name)+'</option>').join('');
      q('spawn').onclick = async () => {
        const res = await apiPost('/api/workspace/spawn',
          {name: q('nmIn').value, role: 'agent', profile: sel.value});
        if (!res.ok) { toast(res.error||'spawn failed', true); return; }
        AGENTS[res.agent.id] = res.agent;   // seed so paint doesn't see "vanished"
        n.agent = res.agent.id; saveLayout(); buildGrid();
      };
    } else {
      const inp = q('in');
      const send = async () => {
        const v = inp.value.trim(); if (!v) return;
        const res = await apiPost('/api/workspace/send', {id:n.agent, prompt:v});
        if (!res.ok) { toast(res.error||'send failed', true); return; }
        inp.value = '';
      };
      q('send').onclick = send;
      inp.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
    }
  }
  async function onDrop(n, ev){
    let data;
    try { data = JSON.parse(ev.dataTransfer.getData('text/plain') || 'null'); }
    catch(_) { data = null; }
    if (!data) return;
    if (!n.agent) {                       // auto-spawn a pane to receive the drop
      const res = await apiPost('/api/workspace/spawn',
        {name:'Claude '+PSEQ, role:'agent', profile:'default'});
      if (!res.ok) { toast(res.error||'spawn failed', true); return; }
      AGENTS[res.agent.id] = res.agent;   // seed so paint doesn't see "vanished"
      n.agent = res.agent.id; saveLayout(); buildGrid();
    }
    // hold the id locally: a concurrent repaint may reset n.agent, and the
    // follow-up send must still reach the agent that was just spawned
    const agentId = n.agent;
    if (data.kind === 'skill') {
      const src = data.plugin ? ' (from the '+data.plugin+' plugin)' : '';
      const res = await apiPost('/api/workspace/send', {id:agentId,
        prompt:'Use the '+data.name+' skill'+src+' now. Follow it to completion.'});
      toast(res.ok ? data.name+' \\u2192 pane' : (res.error||'busy'), !res.ok);
    } else if (data.kind === 'task') {
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
        if (!pane.agent || !AGENTS[pane.agent]) continue;
        const out = pane.el.querySelector('[data-r="out"]');
        if (!out) continue;
        const o = await api('/api/workspace/output?id='+pane.agent+'&since='+pane.cursor);
        if (!o.ok) continue;
        if (o.lines && o.lines.length) {
          const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
          out.textContent += (out.textContent ? '\\n' : '') + o.lines.join('\\n');
          if (stick) out.scrollTop = out.scrollHeight;
        }
        pane.cursor = o.next;
      }
    } catch(_){}
  }
  // A just-spawned agent can be missing from one poll cycle: a request already
  // in flight when the spawn landed returns a list without it, and treating
  // that single stale miss as "vanished" wiped the pane and made the follow-up
  // skill/card send fail with "unknown agent". Reset a pane only after two
  // consecutive polls agree the agent is gone.
  let MISSES = {};
  function paintAgents(){
    leaves(LAYOUT).forEach(n => {
      const pane = PANES[n.pid]; if (!pane) return;
      if (n.agent && !AGENTS[n.agent] && POLLED) {
        MISSES[n.agent] = (MISSES[n.agent] || 0) + 1;
        if (MISSES[n.agent] < 2) return;
        delete MISSES[n.agent];
        n.agent = null; saveLayout();
        buildGrid(); return;     // agent vanished server-side — reset pane
      }
      if (n.agent) delete MISSES[n.agent];
      const a = n.agent && AGENTS[n.agent]; if (!a) return;
      const nm = pane.el.querySelector('[data-r="nm"]');
      const dot = pane.el.querySelector('[data-r="dot"]');
      if (nm) nm.textContent = a.name + ' \\u00b7 ' + a.profile
        + (a.role !== 'agent' ? ' \\u00b7 ' + a.role : '');
      if (dot) dot.className = 'dot ' + a.status;
    });
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

  // --- profiles ---
  let PROFILES = [{name:'default'}];
  async function loadProfiles(){
    try {
      const res = await api('/api/workspace/profiles');
      PROFILES = res.profiles || PROFILES;
      document.getElementById('profs').innerHTML = PROFILES.map(p =>
        '<div class="prof"><span class="name" title="'+esc(p.config_dir||'system default')
        +'">'+esc(p.name)+'</span>'
        + (p.name==='default' ? '' :
           '<button class="warn" onclick="rmProfile(\\''+esc(p.name)+'\\')">&#10005;</button>')
        +'</div>').join('');
    } catch(_){}
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
  (async function loop(){ await poll(); POLLED = true; setTimeout(loop, 1000); })();
</script>
</body></html>"""


def render_parallel(token: str) -> str:
    # Escape every "<" in the JSON-encoded token so neither "</script>" nor
    # "<!--" can break out of the inline <script> block.
    return _PAGE.replace("__TOKEN__", json.dumps(token).replace("<", "\\u003c"))
