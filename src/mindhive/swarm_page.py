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

from . import cat_board, pane, theme

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MindSwarm</title>
<style>
/*__STATUS__*/
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
  button.stop { color:var(--bad); border-color:#5a2e2e; }
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
  .chip .dot.running { background:var(--warn); animation:pulse 1s infinite; }
  .chip .dot.done { background:var(--ok); } .chip .dot.error { background:#ff6b6b; }
  /* The node cat bobs while its agent is actually running — the same motion the
     faculty strip uses, from the SAME keyframes. `mn-bob` arrives below with
     `cat_board.CSS`; declaring a second copy here is precisely the drift
     invariant 10 forbids, and it would be invisible until one page's cats bobbed
     at a different rate than the other's. The status comes off the agent
     snapshot, so "moving" means "the server says this agent is running" and
     never a guess the page made on its own. */
  .chip.running .mn-cat-sprite { animation:mn-bob 1.6s ease-in-out infinite; }
  /* The hover dance reaches every OTHER cat on this page for free, because
     `cat_board.CSS` keys it on `.mn-cat-sprite` and this page interpolates that
     sheet. It cannot reach a RUNNING chip's cat: the bob rule directly above is
     three classes to the shared rule's two, so it wins, and the one cat an
     operator is most likely to point at would be the one cat that ignored them.
     Naming it here rather than in `cat_board.CSS` keeps the ownership the reduce
     block below already states — `.chip` is this page's furniture, and the
     shared sheet does not know it exists.

     The three keyframe sets and the reasoning behind them live in
     `cat_board.CSS`, which this page interpolates whole; only the SELECTOR is
     this page's business. Copying the tracks here would be the drift the note
     above the bob rule already warns about — and would silently fork the
     choreography the first time either copy is tuned. */
  .chip.running .mn-cat-sprite:hover {
    transform-origin:center bottom;
    animation:mn-dance-step 620ms ease-in-out infinite,
              mn-dance-tilt 780ms ease-in-out infinite,
              mn-dance-squash 520ms ease-in-out infinite; }
  /* This page's OWN motion, silenced by the same preference. `cat_board.CSS`
     carries the guard for every cat sprite, but it cannot name `.dot`/`.badge` —
     those are this page's furniture and do not exist on the dashboard. Without
     this block a reduced-motion user would keep the pulsing dots and the pulsing
     badge and lose only the animation this change added, which is the opposite
     of honouring the preference. `running` survives without motion either way:
     the dot stays amber against grey for every other state. */
  @media (prefers-reduced-motion: reduce) {
    .chip .dot.running, .badge.run { animation:none; }
  }
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
  /* This page wants the board as a full-width strip under the header; the main
     dashboard wants it inline in its own header. That box is the only part that
     differs, so it is the only part still written here — every cat and panel rule
     comes from `cat_board.CSS`. */
  #mn-cats { flex:0 0 auto; display:flex; align-items:center; gap:6px;
    padding:5px 14px; background:#12161d; border-bottom:1px solid #30363d;
    min-height:44px; overflow-x:auto; }
  #mn-cats .lbl { margin-right:2px; }
  /* The session strip is this page's own box, like `#mn-cats` — `cat_board.CSS`
     owns the cats and pills, each page owns where they sit. Its own band under
     the faculty strip so the two are never read as one list. */
  #mn-sessions { flex:0 0 auto; display:flex; align-items:center; gap:6px;
    padding:4px 14px; background:#0f1319; border-bottom:1px solid #30363d;
    min-height:32px; overflow-x:auto; }
  #mn-sessions .lbl { margin-right:2px; }
/*__CAT_CSS__*/
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
<!--__CAT_HTML__-->
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
  // The cats, their palettes and the whole faculty board come from
  // `cat_board.script()` — one copy, shared with the main dashboard. This page adds
  // only its own consumer: a node's cat is picked deterministically from its agent
  // id, so it is random per new node and stable across refreshes.
/*__CAT_JS__*/
  // Delegates to the shared derivation above — the same stable-number-from-an-id
  // rule the session cats use for their palette and their bob phase. Kept as a
  // named function because this page's node cats read better as `catFor(a.id)`.
  function catFor(id){ return idCat(id); }
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
  // The markup `#chips` was last painted with. See `paint()` for why this is
  // remembered rather than read back off the DOM.
  let CHIPS_HTML = '';
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
    const chipHTML = swarm.map(a => {
      const p = pos[a.id] || {x:0, y:0}, col = ROLE_STYLE[a.role] || '#888';
      // The agent's status rides on the chip itself, not just on its dot: that
      // is what lets the node cat animate. Safe as a class because `status` is
      // server-owned and drawn from a fixed vocabulary (idle | running | done |
      // error | stopped — workspace.py's Agent), unlike `a.name`, which is
      // escaped below because it is not.
      return '<div class="chip '+a.status+(SEL===a.id?' sel':'')+'" data-id="'+a.id
        +'" style="left:'+p.x+'px;top:'+p.y+'px;border-color:'+col+';color:'+col+'">'
        + catSVG(catFor(a.id), 30)
        + '<span class="row">'+(ROLE_ICON[a.role]||'') + ' ' + esc(a.name)
        + ' <span class="dot '+a.status+'"></span></span></div>';
    }).join('');
    // Only touch the DOM when the markup actually changed. `paint()` runs on
    // every 1.2 s poll and the cat's bob is a 1.6 s cycle, so an unconditional
    // rebuild tore down every sprite and restarted its animation before it could
    // finish — the cat rose and snapped back mid-descent, forever, never
    // completing a bob. The same applies to the dot's 1 s pulse, which fit
    // inside the old window only by luck.
    //
    // Compared against a remembered string rather than reading `chips.innerHTML`
    // back: the browser re-serializes markup it parses, so the round-trip would
    // never compare equal and the guard would silently never fire. Anything that
    // genuinely changes a chip — status, selection, name, or a stage resize
    // moving its coordinates — changes this string and rebuilds.
    if (CHIPS_HTML !== chipHTML) {
      CHIPS_HTML = chipHTML;
      chips.innerHTML = chipHTML;
      chips.querySelectorAll('.chip').forEach(el =>
        el.onclick = ev => { ev.stopPropagation(); inspect(el.dataset.id); });
    }
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
  // The board itself — palettes, sprites, labels, the metadata panel and the
  // outside-click dismiss — is injected above from `cat_board.script()`, so this
  // page and the main dashboard render one implementation. All this page owns is
  // the poll that feeds it.

  async function pollCats(){
    // ONE fetch feeds both strips: the faculty board and the session board are
    // two answers in the same payload, and a second request would double this
    // page's poll rate for data already in hand.
    try {
      const board = await api('/api/mindnet/cats');
      paintCats(board.cats || []);
      paintSessions(board);
    } catch(_){}
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
  /*__PANE__*/
  (async function loop(){ if (!paneHidden()) await poll(); setTimeout(loop, 1200); })();
</script>
</body></html>"""


# The three status colours are decided in `theme`, not here: this document has its
# own palette, but "done is green, working is amber, failed is red" is the same
# decision in every one of them, and a value repeated across four documents is a
# value that drifts. Substituted at IMPORT rather than per request — a marker that
# is already gone cannot be placed by anything a request carries, which is the
# ordering the token substitution below has to reason about explicitly.
_PAGE = _PAGE.replace("/*__STATUS__*/", theme.STATUS_CSS)
# The pane guard rides the same import-time substitution as the palette,
# and for the same reason: it is one decision shared by three documents.
_PAGE = _PAGE.replace(pane.MARKER, pane.PANE_JS)


def render_swarm(token: str) -> str:
    # Escape every "<" in the JSON-encoded token so neither "</script>" nor
    # "<!--" can break out of the inline <script> block.
    #
    # The cat board arrives from `cat_board`, which also injects the faculty legend
    # from `telemetry.FACULTIES` — so the per-faculty cat index derives from that one
    # tuple and cannot drift from it (invariant 10). The board substitutions run
    # BEFORE the token one: they inject page text, and letting a token-derived string
    # be scanned for markers afterwards would let a crafted token place its own.
    return (_PAGE
            .replace("/*__CAT_CSS__*/", cat_board.CSS)
            .replace("<!--__CAT_HTML__-->", cat_board.HTML)
            .replace("/*__CAT_JS__*/", cat_board.script())
            .replace("__TOKEN__",
                     json.dumps(token).replace("<", "\\u003c")))
