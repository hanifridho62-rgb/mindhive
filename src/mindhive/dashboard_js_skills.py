# src/mindhive/dashboard_js_skills.py
"""The Skill & Plugin section: the viewing panel over the skill database (U7) —
the status strip, the sort/filter controls, the per-skill cards with their
measured completion rates, and the invoke/reflect actions.

It breaks the page's naming pattern and `docs/session-legend.md` says so:
`renderPlugins`/`paintPlugins`, entered via `showSkillPanel()`. There is no
`showPlugins`.

Extracted verbatim from ``dashboard_page.py`` when that file reached 2396 lines,
more than twice the project's 1000-line cap. Pure text: this module holds one
string constant and imports nothing. ``dashboard_page.render_page`` splices it
back in at ``/*__JS_SKILLS__*/``, in source order, so the served page is
byte-identical to the one the single file produced.

Not independently loadable in a browser: the whole page is ONE ``<script>``, so
every fragment shares one scope and function declarations hoist across the seams.
The split is for readers and for the cap, and it buys no isolation — a function
here may be called from any other fragment, and is.
"""
JS = """\
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
    // The cross-section discard sits ABOVE the cache writes, not just around
    // the paint. It used to guard only paintPlugins(), so a cycle that resolved
    // after the user had left still overwrote seven module caches with the
    // answers to questions they had stopped asking — and every later repaint
    // that does NOT refetch (a filter toggle, an expand, invokeSkill's own
    // repaint) rendered from them. The node harness could not see it: its
    // inventory fixture deserialised to exactly PLUG_INV's initial value, so
    // the write left no trace to detect.
    if (!shouldPaint(liveFn, renderPlugins)) return;
    PLUG_INV = (res[0] && res[0].inventory) || {plugins: [], local_skills: [], counts: {}};
    if (res[1]) { PLUG_STATUS = res[1].status || {}; PLUG_COUNTS = res[1].counts || {}; }
    if (res[2] && res[2].stats) PLUG_STATS = res[2].stats;
    if (res[3] && res[3].recommendations) PLUG_REC = res[3];
    if (res[4] && res[4].auto) indexSkillRules(res[4].auto);
    expanded.forEach((n, i) => { if (res[5+i]) PLUG_HIST[n] = res[5+i]; });
    paintPlugins();
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
      return ' <span class="pill" style="color:var(--ok);border-color:var(--ok-edge)" '
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
        if (shouldPaint(liveFn, renderPlugins)) paintPlugins();
      }).catch(() => {});
    }
    if (!PLUG_HIST[name]){
      apiGet('/api/skills/history?name='+encodeURIComponent(name)+'&limit=25')
        .then(h => { PLUG_HIST[name] = h || []; if (shouldPaint(liveFn, renderPlugins)) paintPlugins(); })
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
    if (el) el.scrollIntoView({behavior: REDUCE_MOTION ? 'auto' : 'smooth',
                               block: 'center'});
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
    if (shouldPaint(liveFn, renderPlugins)) paintPlugins();
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
    if (shouldPaint(liveFn, renderPlugins)) paintPlugins();
  }
  // Learn from a failed call on demand: reflect the skill:<name> incident into
  // a prevention rule immediately (bypassing the recurrence gate). Spawns the
  // claude CLI once, so it's behind a confirm; the result lands in the card's
  // Needed-changes area in place.
  async function reflectFailure(name){
    if (PLUG_REFLECTING.has(name)) return;
    if (!confirm('Reflect the "'+name+'" failure into a prevention rule now? Runs the claude CLI once.')) return;
    PLUG_REFLECTING.add(name);
    if (shouldPaint(liveFn, renderPlugins)) paintPlugins();
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
    if (shouldPaint(liveFn, renderPlugins)) paintPlugins();
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
    run('loading skill & plugin', renderPlugins, 'rows');
  }
  function doExport(){ window.location = u('/api/export'); }

  // A server-side refusal is `{ok:false, error:"…"}` with HTTP 200 — the six
  // one-at-a-time locks answer that way on purpose, so the page can render it.
  // Rendering it was ALL that happened: neither caller inspected `ok`, so run()
  // finished normally and both the status line and the live region announced
  // "ready" for an operation the server had declined to start. With the nav
  // lock gone, a second click is easy to make and this was its only feedback.
  //
  // Keyed on `error` rather than on `ok` alone, because ok:false is also a
  // legitimate RESULT here: `/api/verify` reports a broken hash chain that way,
  // carrying `reason`, and that operation really did run.
  function refusal(r){
    return r && r.ok === false && typeof r.error === 'string' ? r.error : null;
  }
  function doAction(path, label, method){
    controls.innerHTML=''; liveFn = null;
    run(label+'…', async (current) => {
      const r = method === 'GET' ? await apiGet(path) : await apiPost(path, {});
      if (current()) showText(r);      // liveFn is null here; run()'s generation guards it
      const why = refusal(r);
      if (why) throw new Error(why);   // reaches run()'s catch -> "failed", announced
    });
  }"""
