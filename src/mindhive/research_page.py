# src/mindhive/research_page.py
"""Self-Reliant — the autonomous autoresearch section (plan U7).

A self-contained HTML page served inside the dashboard's iframe, sibling to
MindParallel and lifting its interaction grammar: a skills sidebar whose chips
drag-stage into the run's recipe (guidance for the researcher agent, KTD4),
a research-goal box, optional caps + profile + failover controls, an explicit
Start/Stop, a live leaderboard ranked by val_bpb, and the per-experiment log
stream polled from /api/research/output.

Pure presentation: token in, HTML string out. All run state lives server-side
in research.py; only the staged recipe persists in localStorage.
"""
import json

from . import theme

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Self-Reliant</title>
<style>
/*__STATUS__*/
  * { box-sizing: border-box; }
  body { margin:0; font:13px/1.45 system-ui, sans-serif; background:#0e1116; color:#e6edf3;
    height:100vh; display:flex; flex-direction:column; overflow:hidden; }
  header { display:flex; align-items:center; gap:12px; padding:8px 14px;
    background:#161b22; border-bottom:1px solid #30363d; flex:0 0 auto; }
  header h1 { margin:0; font-size:14px; letter-spacing:1px; }
  .lbl { color:#8b949e; font-size:12px; }
  button { background:#21262d; color:#e6edf3; border:1px solid #30363d; border-radius:6px;
    padding:4px 10px; cursor:pointer; font:inherit; }
  button:hover { background:#30363d; }
  button.go { border-color:#2ea043; color:#7ee2a8; }
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
  .prof { display:flex; align-items:center; gap:6px; margin:6px 0 0; font-size:12px;
    flex-wrap:wrap; }
  .prof .name { flex:1; overflow:hidden; text-overflow:ellipsis; }
  .prof button { padding:0 6px; font-size:10px; }
  .prof button.primary { border-color:#2ea043; color:#7ee2a8; }
  .adot { width:8px; height:8px; border-radius:50%; background:#3a4054; flex:0 0 auto; }
  .adot.ok { background:var(--ok); } .adot.expired { background:var(--warn); }
  .adot.none { background:#ff6b6b; } .adot.unknown, .adot.error { background:#8b949e; }
  .prof-id { font-size:10px; color:#8b949e; margin:0 0 4px 14px; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }
  .pill { display:inline-block; font-size:10px; border:1px solid #30363d;
    border-radius:999px; padding:0 7px; vertical-align:middle; }
  .pill.sr { color:#c9a6ff; border-color:#5b3f8a; background:#191227; }
  .srcchip { display:inline-block; color:#8fd3c7; background:#10201c;
    border:1px solid #2a5045; border-radius:6px; padding:1px 7px;
    margin:3px 3px 0 0; font-size:11px; }
  #center { flex:1 1 auto; display:flex; flex-direction:column; min-width:0; padding:10px;
    gap:10px; overflow-y:auto; }
  .panel { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:10px; }
  #recipe { display:flex; flex-wrap:wrap; gap:4px; min-height:30px; border:1px dashed #30363d;
    border-radius:6px; padding:4px 6px; margin:6px 0; }
  #recipe.drop { border-color:#3d6; }
  #recipe:empty::before { content:'drop skills here — they guide the researcher (optional)';
    color:#8b949e; font-size:11px; }
  .rchip { display:inline-flex; align-items:center; gap:5px; color:#9ecbff;
    background:#12182a; border:1px solid #2a3550; border-radius:999px;
    padding:1px 8px; font-size:11px; }
  .rchip b { cursor:pointer; color:var(--bad); font-weight:400; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:6px; }
  .row input[type=number] { width:90px; }
  #goal { width:100%; min-height:44px; resize:vertical; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; background:#3a4054; }
  .dot.running { background:var(--warn); animation:pulse 1s infinite; }
  .dot.ended { background:var(--ok); } .dot.stopped { background:#ff6b6b; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  /* D8: the sheet that turns an animation ON owns switching it off, so a
     second consumer of this CSS inherits the guard instead of quietly
     losing it. Repeating motion is a trigger for some people, not a
     preference. This page is served into the dashboard's own iframe, so
     the host page's guard does not reach it. */
  @media (prefers-reduced-motion: reduce) {
    .dot.running { animation:none !important; }
  }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:4px 8px; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  tr.best td { color:#7ee2a8; }
  td.st-ok { color:var(--ok); } td.st-oom { color:var(--warn); }
  td.st-crash, td.st-timeout, td.st-aborted { color:var(--bad); }
  #log { flex:1 1 auto; min-height:120px; overflow-y:auto; margin:0; white-space:pre-wrap;
    word-break:break-word; font:12px/1.5 ui-monospace, Consolas, monospace; color:#c9d1d9; }
  #prds { display:flex; flex-direction:column; gap:6px; margin-top:6px; }
  .prd { border:1px solid #30363d; border-radius:6px; background:#0e1116; }
  .prd > .head { display:flex; flex-direction:column; gap:2px; padding:7px 10px; cursor:pointer; }
  .prd > .head:hover { background:#161b22; }
  .prd .title { color:#e6edf3; font-weight:600; font-size:12px; }
  .prd .title::before { content:'\\25B8 '; color:#8b949e; }
  .prd.open .title::before { content:'\\25BE '; }
  .prd .sum { color:#8b949e; font-size:11px; }
  .prd .body { display:none; border-top:1px solid #21262d; padding:8px 10px; }
  .prd.open .body { display:block; }
  .prd pre { margin:0 0 8px; white-space:pre-wrap; word-break:break-word; max-height:320px;
    overflow:auto; font:12px/1.5 ui-monospace, Consolas, monospace; color:#c9d1d9; }
  .prd .mem { margin-top:6px; }
  .prd .mem .hit { color:#9ecbff; font-size:11px; margin:3px 0; padding-left:8px;
    border-left:2px solid #2a3550; white-space:pre-wrap; word-break:break-word; }
  #toast { position:fixed; bottom:14px; left:50%; transform:translateX(-50%);
    background:#21262d; border:1px solid #3d6; border-radius:8px; padding:6px 14px;
    display:none; z-index:9; }
</style></head>
<body>
<header>
  <h1>SELF-RELIANT</h1>
  <span class="lbl"><span class="dot" id="dot"></span> <span id="stline">idle</span></span>
  <span class="lbl" style="margin-left:auto">autonomous autoresearch — runs only on an
    explicit Start; PRD runs may read the trusted sources listed left; every output is
    labeled <span class="pill sr">&#10022; self-reliant</span> across Memory, Sessions,
    Improvements and Oversee</span>
</header>
<div id="main">
  <div id="side">
    <h3>Skills (drag into the recipe)</h3>
    <input id="skq" placeholder="search skills&hellip;" style="width:100%"
      oninput="paintSkills()">
    <div id="skills" class="lbl">loading&hellip;</div>
    <div class="lbl" style="margin-top:10px">Staged skills are strategy guidance for the
      researcher agent — they steer how it edits train.py, they never execute
      inside the training loop.</div>
    <h3>Accounts</h3>
    <div id="profs"></div>
    <input id="pname" placeholder="account name" style="width:100%;margin-top:4px">
    <input id="pdir" placeholder="CLAUDE_CONFIG_DIR path" style="width:100%;margin-top:4px">
    <button style="margin-top:4px" onclick="addProfile()">Add account</button>
    <div class="lbl" style="margin-top:6px">Accounts connect automatically: the page
      verifies the login your Claude app already has and shows who it is. If one isn't
      connected, press <b>connect</b> — your browser opens and one Approve click signs
      it in. Nothing to type, no terminal.</div>
    <h3>Trusted sources</h3>
    <div id="sources" class="lbl">loading&hellip;</div>
  </div>
  <div id="center">
    <div class="panel">
      <textarea id="goal"
        placeholder="research goal, e.g. reach the lowest val_bpb a 6 GB RTX 3050 can hit in 5 minutes"></textarea>
      <div id="recipe"></div>
      <div class="row">
        <label class="lbl" title="PRD: the agent studies train.py + history and writes a
          buildable Product Requirement Document — description, requirements, and an SOP
          composed from your installed skills — for you to build in a fresh Claude Code
          session (read-only, never touches GitHub). improve: the agent edits train.py and
          runs experiments.">mode
          <select id="mode">
            <option value="learn">PRD (spec &amp; SOP)</option>
            <option value="improve">improve (edit &amp; run)</option>
          </select></label>
        <label class="lbl">max experiments <input id="capn" type="number" min="1"
          placeholder="&infin;"></label>
        <label class="lbl">time budget (min) <input id="capt" type="number" min="1"
          placeholder="&infin;"></label>
        <label class="lbl">account <select id="prof"></select></label>
        <label class="lbl" title="the model the researcher agent edits train.py with;
          'account default' forces no --model flag">model
          <select id="model" onchange="onModelChange()"></select></label>
        <label class="lbl" title="on usage exhaustion, continue under the next
          authenticated account"><input id="failover" type="checkbox"> account failover</label>
        <span style="flex:1"></span>
        <button class="go" id="startbtn" onclick="startRun()">&#9654; Start</button>
        <button class="warn" id="stopbtn" disabled onclick="stopRun()">&#9632; Stop</button>
      </div>
    </div>
    <div class="panel">
      <b style="font-size:12px">LEADERBOARD</b>
      <span class="lbl" id="bestline"></span>
      <table><thead><tr><th>#</th><th>val_bpb</th><th>status</th><th>change</th>
        <th>skills</th></tr></thead><tbody id="lb"></tbody></table>
    </div>
    <div class="panel" id="prdpanel" style="display:none">
      <b style="font-size:12px">PRDS — WHAT TO BUILD NEXT</b>
      <span class="lbl">read-only PRDs from this run — press one to reveal its memory,
        then build it in a fresh Claude Code session</span>
      <div id="prds"></div>
    </div>
    <div class="panel" style="flex:1 1 auto; display:flex; flex-direction:column; min-height:0">
      <b style="font-size:12px; flex:0 0 auto">RUN LOG</b>
      <pre id="log"></pre>
    </div>
  </div>
</div>
<div id="toast"></div>
<script>
  const TOKEN = __TOKEN__;
  const PROJ = new URLSearchParams(location.search).get('project') || '';
  function withP(path){ return (PROJ && PROJ !== '*')
    ? path + (path.includes('?')?'&':'?') + 'project=' + encodeURIComponent(PROJ) : path; }
  function api(path){ return fetch(withP(path) + (path.includes('?')?'&':'?') + 'token='
      + encodeURIComponent(TOKEN), {headers:{'X-Hive-Token':TOKEN}}).then(r=>r.json()); }
  function apiPost(path, body){
    const b = Object.assign({}, body||{});
    if (PROJ && PROJ !== '*') b.project = PROJ;
    return fetch(withP(path)+'?token='+encodeURIComponent(TOKEN), {method:'POST',
      headers:{'Content-Type':'application/json','X-Hive-Token':TOKEN},
      body:JSON.stringify(b)}).then(r=>r.json());
  }
  function esc(s){ return String(s).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  let TID = null;
  function toast(msg, bad){
    const t = document.getElementById('toast');
    t.textContent = msg; t.style.borderColor = bad ? '#a55' : '#3d6';
    t.style.display = 'block';
    clearTimeout(TID); TID = setTimeout(()=>{ t.style.display='none'; }, 3200);
  }

  // --- staged recipe: skills wait for an explicit Start (same grammar as
  // MindParallel; persisted so a composed recipe survives a refresh) ---
  let STAGED = [];
  try { STAGED = JSON.parse(localStorage.getItem('mh_sr_staged') || '[]') || []; }
  catch(_) { STAGED = []; }
  function saveStaged(){ localStorage.setItem('mh_sr_staged', JSON.stringify(STAGED)); }
  function paintRecipe(){
    const el = document.getElementById('recipe');
    el.innerHTML = STAGED.map((s,i) => '<span class="rchip" title="'+esc(s.plugin||'local')
      +'">'+esc(s.name)+'<b data-i="'+i+'" title="unstage">&#10005;</b></span>').join('');
    el.querySelectorAll('b[data-i]').forEach(b => b.onclick = () => {
      STAGED.splice(parseInt(b.dataset.i), 1); saveStaged(); paintRecipe();
    });
  }
  const drop = document.getElementById('recipe');
  drop.addEventListener('dragover', ev => { ev.preventDefault(); drop.classList.add('drop'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drop'));
  drop.addEventListener('drop', ev => {
    ev.preventDefault(); drop.classList.remove('drop');
    let d; try { d = JSON.parse(ev.dataTransfer.getData('text/plain')||'null'); }
    catch(_) { return; }
    if (!d || d.kind !== 'skill') return;
    STAGED.push({name:d.name, plugin:d.plugin}); saveStaged(); paintRecipe();
    toast(d.name + ' staged');
  });

  // --- skills sidebar (drag source; same inventory as MindParallel) ---
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
  // --- accounts (profiles) + model picker: one account = one Claude Code login
  // (its own CLAUDE_CONFIG_DIR); reuse the tested /api/workspace/* routes so the
  // whole switching flow lives in-section. The run picks one account + model. ---
  let PROFILES = [{name:'default'}];
  let MODELS = [{id:'', label:'account default'}];
  let AUTH = {};      // cheap file-heuristic state, painted first
  let VERIFY = {};    // authoritative CLI verification (adds email + plan)
  function modelOptions(sel, current){
    current = current || '';
    const opts = MODELS.map(m => '<option value="'+esc(m.id)+'">'+esc(m.label)+'</option>');
    if (!MODELS.some(m => m.id === current))       // keep a free-typed id visible
      opts.push('<option value="'+esc(current)+'">'+esc(current)+'</option>');
    opts.push('<option value="__custom">custom&hellip;</option>');
    sel.innerHTML = opts.join('');
    sel.value = current;
  }
  function onModelChange(){
    const sel = document.getElementById('model');
    let v = sel.value;
    if (v === '__custom') {
      v = (prompt('model id (as the claude CLI --model flag accepts)') || '').trim();
      modelOptions(sel, v);          // pin the free-typed id as the selection
    }
    localStorage.setItem('mh_sr_model', v);
  }
  async function loadProfiles(){
    try {
      const res = await api('/api/workspace/profiles');
      PROFILES = res.profiles || PROFILES;
      if (res.models && res.models.length) MODELS = res.models;
      document.getElementById('prof').innerHTML =
        PROFILES.map(p => '<option>'+esc(p.name)+'</option>').join('');
      modelOptions(document.getElementById('model'),
                   localStorage.getItem('mh_sr_model') || '');
      paintProfiles(); loadAuth(); verifyAll();
    } catch(_){}
  }
  // Trusted-sources transparency panel: exactly what a PRD run's enforced
  // allowlist will grant (same helper server-side), so what the agent may
  // read is never a surprise.
  async function loadSources(){
    const el = document.getElementById('sources');
    try {
      const res = await api('/api/research/sources');
      if (!res.ok) { el.textContent = 'unavailable'; return; }
      el.innerHTML = (res.enabled && (res.domains||[]).length)
        ? '<div class="lbl">PRD runs may search the web and read these domains '
          + '(read-only \\u2014 GitHub writes stay blocked):</div>'
          + res.domains.map(d => '<span class="srcchip">'+esc(d)+'</span>').join('')
        : '<div class="lbl">web sources are disabled (MINDHIVE_RESEARCH_WEB=0) '
          + '\\u2014 PRD runs read local files only</div>';
    } catch(_) { el.textContent = 'unavailable'; }
  }
  async function loadAuth(){
    try {
      const res = await api('/api/workspace/auth');
      AUTH = {}; (res.auth||[]).forEach(a => AUTH[a.profile] = a);
      paintProfiles();
    } catch(_){}
  }
  // Auto-connect: each account is verified with the CLI itself (cached
  // server-side), so the login your Claude app already holds simply shows up
  // as "connected as <email>" — no terminal, nothing to type, nothing to do.
  async function verifyProfile(name, force){
    try {
      const res = await apiPost('/api/workspace/auth_verify',
        {name:name, force:!!force});
      if (res.ok) { VERIFY[name] = res; paintProfiles(); }
    } catch(_){}
  }
  function verifyAll(){ PROFILES.forEach(p => verifyProfile(p.name)); }
  function paintProfiles(){
    document.getElementById('profs').innerHTML = PROFILES.map(p => {
      const a = VERIFY[p.name] || AUTH[p.name]
        || {state:'unknown', detail:'checking\\u2026'};
      const connected = a.state === 'ok';
      const id = connected
        ? esc(a.email || '') + (a.subscription ? ' \\u00b7 '+esc(a.subscription) : '')
        : esc(a.detail || '');
      return '<div class="prof">'
        + '<span class="adot '+esc(a.state)+'" title="'+esc(a.detail||'')+'"></span>'
        + '<span class="name" title="'+esc(p.config_dir||'system default')
        + ' \\u2014 '+esc(a.detail||'')+'">'+esc(p.name)+'</span>'
        + '<button class="primary" title="verify this account; if it is not '
        + 'connected, your browser opens for a one-click sign-in (nothing to type)"'
        + ' onclick="connectProfile(\\''+esc(p.name)+'\\')">'
        + (connected ? 're-check' : 'connect')+'</button>'
        + '<button title="pre-accept the workspace trust dialog for this project"'
        + ' onclick="trustProfile(\\''+esc(p.name)+'\\')">trust</button>'
        + (p.name==='default' ? '' :
           '<button class="warn" onclick="rmProfile(\\''+esc(p.name)+'\\')">&#10005;</button>')
        + '</div>'
        + (id ? '<div class="prof-id" title="'+id+'">'+id+'</div>' : '');
    }).join('');
  }
  async function connectProfile(name){
    toast('verifying '+name+'\\u2026');
    let res;
    try { res = await apiPost('/api/workspace/auth_verify', {name:name, force:true}); }
    catch(_) { res = null; }
    if (res && res.ok && res.state === 'ok') {
      VERIFY[name] = res; paintProfiles();
      toast(res.detail || 'connected'); return;
    }
    const lg = await apiPost('/api/workspace/profile_login', {name:name});
    toast(lg.ok ? 'browser sign-in opened \\u2014 approve it there, nothing to type'
                : (lg.error||'failed'), !lg.ok);
    if (lg.ok) setTimeout(() => verifyProfile(name, true), 20000);
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

  // --- start / stop ---
  async function startRun(){
    const goal = document.getElementById('goal').value.trim();
    if (!goal) { toast('type a research goal first', true); return; }
    const capn = parseInt(document.getElementById('capn').value) || null;
    const capt = parseFloat(document.getElementById('capt').value) || null;
    const res = await apiPost('/api/research/start', {
      goal: goal, skills: STAGED,
      max_experiments: capn,
      time_budget_s: capt ? capt * 60 : null,
      failover: document.getElementById('failover').checked,
      profile: document.getElementById('prof').value || 'default',
      mode: document.getElementById('mode').value || 'learn',
      model: (m => m === '__custom' ? '' : m)(document.getElementById('model').value)});
    if (!res.ok) { toast(res.error || 'start failed', true); return; }
    toast('run ' + res.run_id + ' started');
  }
  async function stopRun(){
    const res = await apiPost('/api/research/stop', {});
    toast(res.ok ? 'stop requested' : (res.error||'failed'), !res.ok);
  }

  // --- live status + leaderboard + incremental log ---
  let CURSOR = 0;
  async function poll(){
    try {
      const st = await api('/api/research/status');
      const dot = document.getElementById('dot');
      dot.className = 'dot ' + esc(st.state||'');
      document.getElementById('stline').textContent =
        (st.state||'idle') + (st.reason ? ' ('+st.reason+')' : '')
        + ' \\u00b7 ' + (st.experiments||0)
        + (st.mode === 'learn' ? ' rounds' : ' experiments')
        + (st.best != null ? ' \\u00b7 best val_bpb ' + st.best : '');
      document.getElementById('startbtn').disabled = st.state === 'running';
      document.getElementById('stopbtn').disabled = st.state !== 'running';
      const lb = await api('/api/research/leaderboard');
      paintBoard(lb, st.mode);
      if (st.mode === 'learn') {
        const pr = await api('/api/research/prds');
        paintPRDs((pr.ok && pr.prds) || []);
      } else {
        document.getElementById('prdpanel').style.display = 'none';
      }
      const o = await api('/api/research/output?since=' + CURSOR);
      if (o.ok && o.lines && o.lines.length) {
        const log = document.getElementById('log');
        const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
        log.textContent += (log.textContent ? '\\n' : '') + o.lines.join('\\n');
        if (stick) log.scrollTop = log.scrollHeight;
      }
      if (o.ok) CURSOR = o.next;
    } catch(_){}
  }
  function paintBoard(lb, mode){
    const rows = lb.experiments || [];
    const bestId = lb.best && lb.best.id;
    document.getElementById('bestline').textContent = lb.best
      ? ' \\u2014 best val_bpb ' + lb.best.val_bpb : ' \\u2014 no scored experiment yet';
    document.getElementById('lb').innerHTML = rows.map((e,i) =>
      '<tr'+(e.id===bestId?' class="best"':'')+'><td>'+(i+1)+'</td>'
      + '<td>'+(e.val_bpb != null ? e.val_bpb : '\\u2014')+'</td>'
      + '<td class="st-'+esc(e.status)+'">'+esc(e.status)+'</td>'
      + '<td title="'+esc(e.change||'')+'">'+esc((e.change||'').slice(0,90))+'</td>'
      + '<td>'+esc((e.skills||[]).join(', '))+'</td></tr>').join('')
      || '<tr><td colspan="5" class="lbl">'+(mode === 'improve'
          ? 'no experiments yet \\u2014 Start a run'
          : 'PRD runs analyze without training \\u2014 their output appears in the PRDS panel below')+'</td></tr>';
  }

  // --- PRD cards: each PRD-mode run yields buildable specs. Press a card to
  // expand the full PRD; 'show memory' recalls its backing ledger events inline,
  // and 'View in Memory' asks the parent dashboard to open the Memory section
  // filtered to it — so Self-Reliant stays connected to the memory it wrote. ---
  let PRD_OPEN = {};                 // title -> expanded, so a repaint keeps state
  function paintPRDs(prds){
    const panel = document.getElementById('prdpanel');
    panel.style.display = prds.length ? '' : 'none';
    if (!prds.length) return;
    const box = document.getElementById('prds');
    box.innerHTML = prds.map((p,i) => {
      const key = p.title || ('PRD ' + (p.n||i+1));
      const open = PRD_OPEN[key] ? ' open' : '';
      return '<div class="prd'+open+'" data-key="'+esc(key)+'" data-i="'+i+'">'
        + '<div class="head"><span class="title">'+esc(p.title||('PRD '+(p.n||i+1)))
        + ' <span class="pill sr" title="produced by a Self-Reliant run \\u2014 '
        + 'its memory carries the same label">&#10022; self-reliant</span></span>'
        + '<span class="sum">'+esc(p.summary||'')+'</span>'
        + (p.goal ? '<span class="sum">goal: '+esc(p.goal)+'</span>' : '')
        + '</div>'
        + '<div class="body"><pre>'+esc(p.body||'')+'</pre>'
        + '<div class="acts"><button class="copy-btn" title="copy the full PRD '
        + 'to paste into a fresh Claude Code session">copy PRD</button> '
        + '<button class="mem-btn">show memory</button> '
        + '<button class="jump-btn">View in Memory \\u2192</button></div>'
        + '<div class="mem"></div></div></div>';
    }).join('');
    box.querySelectorAll('.prd').forEach(card => {
      const key = card.dataset.key;
      card.querySelector('.head').onclick = () => {
        PRD_OPEN[key] = !PRD_OPEN[key]; card.classList.toggle('open');
      };
      card.querySelector('.copy-btn').onclick =
        (ev) => copyPRD(prds[parseInt(card.dataset.i)], ev.target);
      card.querySelector('.mem-btn').onclick = () => showBacking(card, key);
      card.querySelector('.jump-btn').onclick = () => jumpToMemory(key);
    });
  }
  async function copyPRD(p, btn){
    if (!p) return;
    const text = 'TITLE: '+(p.title||'')+'\\nSUMMARY: '+(p.summary||'')
      + '\\n\\n'+(p.body||'');
    try { await navigator.clipboard.writeText(text); }
    catch(_) {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta);
      ta.select(); document.execCommand('copy'); ta.remove();
    }
    const old = btn.textContent; btn.textContent = 'copied \\u2713';
    setTimeout(() => { btn.textContent = old; }, 1200);
  }
  async function showBacking(card, query){
    const mem = card.querySelector('.mem');
    mem.innerHTML = '<span class="lbl">recalling\\u2026</span>';
    try {
      const hits = await api('/api/recall?q='+encodeURIComponent(query)+'&semantic=0');
      mem.innerHTML = (hits && hits.length)
        ? hits.slice(0,5).map(h => '<div class="hit">'+esc((h.snippet||'').slice(0,240))
            +'</div>').join('')
        : '<span class="lbl">no backing memory yet</span>';
    } catch(_) { mem.innerHTML = '<span class="lbl">recall failed</span>'; }
  }
  function jumpToMemory(query){
    try { window.parent.postMessage(
      {type:'mh-self-reliant', action:'show-memory', query:query}, location.origin); }
    catch(_) { toast('could not open Memory', true); }
  }

  // boot
  paintRecipe(); loadSkills(); loadProfiles(); loadSources();
  (async function loop(){ await poll(); setTimeout(loop, 1000); })();
</script>
</body></html>"""


# The three status colours are decided in `theme`, not here: this document has its
# own palette, but "done is green, working is amber, failed is red" is the same
# decision in every one of them, and a value repeated across four documents is a
# value that drifts. Substituted at IMPORT rather than per request — a marker that
# is already gone cannot be placed by anything a request carries, which is the
# ordering the token substitution below has to reason about explicitly.
_PAGE = _PAGE.replace("/*__STATUS__*/", theme.STATUS_CSS)


def render_research(token: str) -> str:
    # Escape every "<" in the JSON-encoded token so neither "</script>" nor
    # "<!--" can break out of the inline <script> block.
    return _PAGE.replace("__TOKEN__", json.dumps(token).replace("<", "\\u003c"))
