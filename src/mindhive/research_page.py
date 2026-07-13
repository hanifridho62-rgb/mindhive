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

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Self-Reliant</title>
<style>
  :root { color-scheme: dark; }
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
  .prof button { padding:0 5px; font-size:10px; }
  .adot { width:8px; height:8px; border-radius:50%; background:#3a4054; flex:0 0 auto; }
  .adot.ok { background:#46c46a; } .adot.expired { background:#e0b341; }
  .adot.none { background:#ff6b6b; } .adot.unknown { background:#8b949e; }
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
  .rchip b { cursor:pointer; color:#ff8a8a; font-weight:400; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:6px; }
  .row input[type=number] { width:90px; }
  #goal { width:100%; min-height:44px; resize:vertical; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; background:#3a4054; }
  .dot.running { background:#e0b341; animation:pulse 1s infinite; }
  .dot.ended { background:#46c46a; } .dot.stopped { background:#ff6b6b; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:4px 8px; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
  tr.best td { color:#7ee2a8; }
  td.st-ok { color:#46c46a; } td.st-oom { color:#e0b341; }
  td.st-crash, td.st-timeout, td.st-aborted { color:#ff8a8a; }
  #log { flex:1 1 auto; min-height:120px; overflow-y:auto; margin:0; white-space:pre-wrap;
    word-break:break-word; font:12px/1.5 ui-monospace, Consolas, monospace; color:#c9d1d9; }
  #toast { position:fixed; bottom:14px; left:50%; transform:translateX(-50%);
    background:#21262d; border:1px solid #3d6; border-radius:8px; padding:6px 14px;
    display:none; z-index:9; }
</style></head>
<body>
<header>
  <h1>SELF-RELIANT</h1>
  <span class="lbl"><span class="dot" id="dot"></span> <span id="stline">idle</span></span>
  <span class="lbl" style="margin-left:auto">autonomous autoresearch — 5-minute capped
    experiments; runs only on an explicit Start</span>
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
    <div class="lbl" style="margin-top:6px">Each account is a separate Claude Code login
      (its own CLAUDE_CONFIG_DIR). Add one, then <b>login</b> to authenticate it in a
      console. The run below runs under whichever account you pick.</div>
  </div>
  <div id="center">
    <div class="panel">
      <textarea id="goal"
        placeholder="research goal, e.g. reach the lowest val_bpb a 6 GB RTX 3050 can hit in 5 minutes"></textarea>
      <div id="recipe"></div>
      <div class="row">
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
        <button class="warn" onclick="stopRun()">&#9632; Stop</button>
      </div>
    </div>
    <div class="panel">
      <b style="font-size:12px">LEADERBOARD</b>
      <span class="lbl" id="bestline"></span>
      <table><thead><tr><th>#</th><th>val_bpb</th><th>status</th><th>change</th>
        <th>skills</th></tr></thead><tbody id="lb"></tbody></table>
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
  let AUTH = {};
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
      paintProfiles(); loadAuth();
    } catch(_){}
  }
  async function loadAuth(){
    try {
      const res = await api('/api/workspace/auth');
      AUTH = {}; (res.auth||[]).forEach(a => AUTH[a.profile] = a);
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
        + ' \\u00b7 ' + (st.experiments||0) + ' experiments'
        + (st.best != null ? ' \\u00b7 best val_bpb ' + st.best : '');
      document.getElementById('startbtn').disabled = st.state === 'running';
      const lb = await api('/api/research/leaderboard');
      paintBoard(lb);
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
  function paintBoard(lb){
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
      || '<tr><td colspan="5" class="lbl">no experiments yet \\u2014 Start a run</td></tr>';
  }

  // boot
  paintRecipe(); loadSkills(); loadProfiles();
  (async function loop(){ await poll(); setTimeout(loop, 1000); })();
</script>
</body></html>"""


def render_research(token: str) -> str:
    # Escape every "<" in the JSON-encoded token so neither "</script>" nor
    # "<!--" can break out of the inline <script> block.
    return _PAGE.replace("__TOKEN__", json.dumps(token).replace("<", "\\u003c"))
