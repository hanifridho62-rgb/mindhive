# src/mindhive/dashboard_js_improve.py
"""The Memory and Improvements sections: the semantic-event view, the lessons
learned from recorded failures, and the efficiency strip over them.

Extracted verbatim from ``dashboard_page.py`` when that file reached 2396 lines,
more than twice the project's 1000-line cap. Pure text: this module holds one
string constant and imports nothing. ``dashboard_page.render_page`` splices it
back in at ``/*__JS_IMPROVE__*/``, in source order, so the served page is
byte-identical to the one the single file produced.

Not independently loadable in a browser: the whole page is ONE ``<script>``, so
every fragment shares one scope and function declarations hoist across the seams.
The split is for readers and for the cap, and it buys no isolation — a function
here may be called from any other fragment, and is.
"""
JS = """\
  // --- Memory: the semantic layer (decisions/caveats/…) — intent & context ---
  // MEM_ITEMS caches the last fetch so search/sort repaint instantly without a
  // round-trip; MEM_QUERY / MEM_SORT live outside the view so the 2.5s live
  // refresh (which only rewrites #view) preserves what you typed and picked.
  let MEM_ITEMS = [];
  let MEM_QUERY = '';
  let MEM_SORT = 'new';            // 'new' = newest first, 'old' = oldest first
  async function renderMemory(){
    const path = '/api/memory' + (SESSION ? '?session='+encodeURIComponent(SESSION) : '');
    const items = await apiGet(path);
    if (!shouldPaint(liveFn, renderMemory)) return;    // newer section owns the panel
    MEM_ITEMS = items;
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
          + esc(m.type)+' · '+esc(fmtTs(m.ts))+' · '+esc(m.session)
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
    markSection('memory');
    controls.innerHTML =
      (SESSION ? '<div class="row">filtered to session <b>'+esc(SESSION)+'</b> '
        + '<button onclick="clearSession()">show all</button></div>' : '')
      + '<div class="row"><input id="memq" placeholder="search memory…" style="min-width:260px" '
        + 'value="'+escAttr(MEM_QUERY)+'" oninput="onMemSearch(this.value)">'
      + '<button id="memsort" onclick="toggleMemSort()">'
        + (MEM_SORT==='new' ? 'newest first' : 'oldest first')+'</button></div>';
    liveFn = renderMemory; run('loading memory', renderMemory, 'rows');
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
    // Discard BEFORE the error check, not after: a stale render that throws
    // would reach run()'s catch and paint its error over the section the user
    // is now looking at — the same clobber by another route.
    if (!shouldPaint(liveFn, renderImprovements)) return;  // newer section owns the panel
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
      + esc(fmtTs(m.ts))+' \\u00b7 <span class="pill">manual</span>'
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
  // Improvements view with an inline result banner instead of `runJob`'s
  // full-panel output pane.
  async function runReflect(){
    if(!confirm('Reflect (learn from failures) \\u2014 runs the claude CLI once '
      + 'per recurring incident (a few per click). Continue?')) return;
    // POST and repaint inside ONE run() so the live timer can't fire in a
    // busy=false gap between the reflect call and the follow-up render.
    liveFn = renderImprovements;
    await run('reflecting\\u2026', async (current) => {
      // The job's own summary IS the banner. It already reads "reflected 2, 1
      // failed, 3 pending" — or, when there was nothing eligible, says that
      // instead of showing three zeroes the user has to interpret.
      const job = await awaitJob('/api/rules/reflect', {}, current, null);
      IMP_BANNER = 'Reflect: ' + job.summary;
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
    await run('resolving\\u2026', async (current) => {
      const job = await awaitJob('/api/rules/resolve', {}, current, null);
      IMP_BANNER = 'Resolve: ' + job.summary;
      await renderImprovements();
    });
  }
  function onImpSearch(v){ IMP_QUERY = v; paintImprovements(); }
  function onImpStatus(v){ IMP_STATUS = v; paintImprovements(); }
  function onImpCat(v){ IMP_CAT = v; paintImprovements(); }
  function showImprovements(clearBanner){
    markSection('improvements');
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
    liveFn = renderImprovements; run('loading improvements', renderImprovements, 'rows');
  }
"""
