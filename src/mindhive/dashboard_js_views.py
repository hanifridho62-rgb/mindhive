# src/mindhive/dashboard_js_views.py
"""The thin sections: Log, SOP, State, Briefing, Recall, Graph, Workspace and
Append. Each is a few lines of controls over `run()` and one fetch, which is
why they share a module instead of getting eight.

Extracted verbatim from ``dashboard_page.py`` when that file reached 2396 lines,
more than twice the project's 1000-line cap. Pure text: this module holds one
string constant and imports nothing. ``dashboard_page.render_page`` splices it
back in at ``/*__JS_VIEWS__*/``, in source order, so the served page is
byte-identical to the one the single file produced.

Not independently loadable in a browser: the whole page is ONE ``<script>``, so
every fragment shares one scope and function declarations hoist across the seams.
The split is for readers and for the cap, and it buys no isolation — a function
here may be called from any other fragment, and is.
"""
JS = """\
  // --- mandatory code change-log (Log view) ---
  function logBadge(cat){
    const colors = {update:'var(--cat-update)', minor_change:'var(--cat-minor)', deletion:'var(--cat-deletion)',
                    reduction:'var(--cat-reduction)', major_change:'var(--cat-major)', replacement:'var(--cat-replacement)'};
    const c = colors[cat] || 'var(--cat-other)';
    return '<span class="pill" style="color:'+c+';border-color:'+c+'">'+esc(cat||'?')+'</span>';
  }
  async function renderLog(){
    const res = await apiGet('/api/changelog');
    if (!shouldPaint(liveFn, renderLog)) return;       // newer section owns the panel
    if (!res || !Array.isArray(res.entries)) throw new Error((res && res.error) || 'changelog fetch failed');
    const pend = res.pending && res.pending.files && res.pending.files.length
      ? '<div class="row"><span class="pill" style="color:#e0a030;border-color:#a67a20">'
        + '\\u26a0 ' + res.pending.files.length + ' unlogged file(s) \\u2014 record the mandatory entry: '
        + '<code>hive log &lt;category&gt; "brief explanation"</code></span></div>'
      : '';
    if (!res.entries.length){
      showHTML(pend + '<h2 class="h">No change-log entries yet \\u2014 every change to '
        + 'mindhive code must be recorded with '
        + '<code>hive log &lt;category&gt; "brief explanation"</code> ('
        + esc((res.categories || []).join(', ')) + ').</h2>');
      return;
    }
    const showProj = (PROJECT === '*');
    showHTML(pend + '<h2 class="h">'+res.entries.length+' change-log entr'
      + (res.entries.length===1?'y':'ies')+'</h2>'
      + '<ul class="hits">' + res.entries.map(e =>
        '<li><div class="file">'+logBadge(e.category)+' '+esc(e.text)+'</div>'
        + ((e.files||[]).length ? '<div class="snip">'+(e.files||[]).map(esc).join(' \\u00b7 ')+'</div>' : '')
        + '<div class="snip dup">'+esc(fmtTs(e.ts))+' \\u00b7 session '+esc(e.session||'')
        + (showProj && e.project ? ' \\u00b7 '+esc(e.project) : '') +'</div></li>').join('')
      + '</ul>');
  }
  function showLog(){ controls.innerHTML=''; liveFn = renderLog; run('loading change log', renderLog, 'rows'); }

  function sopBadge(action){
    const colors = {add:'var(--cat-minor)', update:'var(--cat-update)', 'delete':'var(--cat-deletion)'};
    const c = colors[action] || 'var(--cat-other)';
    return '<span class="pill" style="color:'+c+';border-color:'+c+'">'+esc(action||'?')+'</span>';
  }
  async function renderSops(){
    const res = await apiGet('/api/sops');
    if (!shouldPaint(liveFn, renderSops)) return;      // newer section owns the panel
    if (!res || !Array.isArray(res.active)) throw new Error((res && res.error) || 'sop fetch failed');
    const showProj = (PROJECT === '*');
    if (!res.active.length && !res.history.length){
      showHTML('<h2 class="h">No SOPs yet \\u2014 record standing procedures with '
        + '<code>hive sop add &lt;id&gt; --title "\\u2026" --file &lt;body.md&gt;</code>. '
        + 'Active SOPs auto-load into every session via STATE.md / MEMORY.md.</h2>');
      return;
    }
    const act = res.active.map((r, i) =>
      '<li><div class="file">SOP '+(i+1)+': '+esc(r.title)
      + ' <span class="pill">'+esc(r.sop_id)+' v'+r.version+'</span>'
      + (showProj && r.project ? ' <span class="pill">'+esc(r.project)+'</span>' : '')
      + '</div><div class="snip" style="white-space:pre-wrap">'+esc(r.text)+'</div>'
      + '<div class="snip dup">created '+esc(fmtTs(r.created))+' \\u00b7 updated '+esc(fmtTs(r.updated))+'</div></li>').join('');
    const hist = res.history.map(e =>
      '<li><div class="file">'+sopBadge(e.action)+' '+esc(e.sop_id)
      + (e.title ? ' \\u2014 '+esc(e.title) : '') + '</div>'
      + '<div class="snip dup">'+esc(fmtTs(e.ts))+' \\u00b7 session '+esc(e.session||'')
      + (showProj && e.project ? ' \\u00b7 '+esc(e.project) : '') + '</div></li>').join('');
    showHTML('<h2 class="h">'+res.active.length+' active SOP'
      + (res.active.length===1?'':'s')+' (auto-loaded each session)</h2>'
      + '<ul class="hits">'+act+'</ul>'
      + '<h2 class="h">History ('+res.history.length+' lifecycle event'
      + (res.history.length===1?'':'s')+')</h2>'
      + '<ul class="hits">'+hist+'</ul>');
  }
  function showSops(){ controls.innerHTML=''; liveFn = renderSops; run('loading SOPs', renderSops, 'rows'); }

  async function renderState(){
    const s = await apiGet('/api/state');
    if (!shouldPaint(liveFn, renderState)) return;     // newer section owns the panel
    showText(s);
  }
  function showState(){ controls.innerHTML=''; liveFn = renderState; run('loading state', renderState, 'block'); }
  function showBriefing(){
    controls.innerHTML=''; liveFn = null;
    // liveFn is null here, so shouldPaint cannot guard this one — run()'s
    // generation does. Without it, /api/briefing (1.2-1.9s on the 31k store)
    // resolving after the user clicked another section painted Briefing over it.
    run('loading briefing', async (current) => {
      const t = await apiGet('/api/briefing');
      if (current()) showText(t);
    }, 'block');
  }

  function showRecall(){
    liveFn = null;
    controls.innerHTML = '<div class="row"><input id="q" placeholder="search query" style="min-width:260px">'
      + '<label><input type="checkbox" id="sem"> semantic</label>'
      + '<button onclick="runRecall()">Search</button></div>';
    showText('Enter a query and search.');
    // Panel swaps that never reach run() still have to announce themselves —
    // run() is where the other sections get their announcement.
    announce('recall \\u2014 ready');
    const q = document.getElementById('q');
    q.focus();
    q.addEventListener('keydown', e => { if (e.key === 'Enter') runRecall(); });
  }
  function runRecall(){
    const q = document.getElementById('q').value;
    const sem = document.getElementById('sem').checked ? '1' : '0';
    run('searching', async (current) => {
      const hits = await apiGet('/api/recall?q='+encodeURIComponent(q)+'&semantic='+sem);
      // Recall sets liveFn = null, and a second search while the first is in
      // flight is the common case here, not an edge one.
      if (!current()) return;
      if (!hits.length) { showText('No matches for "'+q+'".'); return; }
      showHTML('<h2 class="h">'+hits.length+' result'+(hits.length>1?'s':'')+'</h2><ul class="hits">'
        + hits.map(h => '<li><div class="file">'+esc(h.file||'')+'</div><div class="snip">'+esc(h.snippet||'')+'</div></li>').join('')
        + '</ul>');
    }, 'rows');
  }

  // Graph is a one-shot heavy render (not live-refreshed): fetch once and hand
  // the self-contained page to the iframe. Semantic links are the expensive,
  // opt-in build (an embedding search per event) so they stay off by default;
  // ticking the box reloads with ?semantic=1.
  function showGraph(){
    liveFn = null;
    let semantic = false;
    controls.innerHTML = '<label class="ctl" style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">'
      + '<input type="checkbox" id="gsem"> semantic links</label>';
    const load = () => run('building graph', async (current) => {
      const html = await apiGet('/api/graph' + (semantic ? '?semantic=1' : ''));
      // liveFn is null for Graph, so run()'s generation is the only guard. It
      // also covers re-entry: toggling the semantic checkbox calls load() again,
      // and the slower of the two answers must not win.
      if (current()) { only(graph); graph.srcdoc = html; }
    });
    const cb = document.getElementById('gsem');
    if (cb) cb.addEventListener('change', () => { semantic = cb.checked; load(); });
    load();
  }

  // Workspace views (MindParallel / MindSwarm / Self-Reliant) are full pages
  // served by the server and loaded by URL — srcdoc must be cleared first or
  // it wins over src. 'research' lives under its own /api/research family.
  function showWorkspace(view){
    controls.innerHTML=''; liveFn = null;
    only(graph);
    graph.removeAttribute('srcdoc');
    graph.src = u(withProj(view === 'research' ? '/api/research/page'
                                               : '/api/workspace/'+view));
    announce(view + ' workspace \\u2014 loading');
  }

  // Self-Reliant (an iframe) can ask the parent to open the Memory view filtered
  // to a PRD's backing events — the section's link into the memory it wrote. Only
  // trust messages from our own iframe on the same origin (never arbitrary posts).
  window.addEventListener('message', function(ev){
    if (ev.origin !== location.origin) return;
    if (!graph || ev.source !== graph.contentWindow) return;
    const d = ev.data;
    if (!d || d.type !== 'mh-self-reliant' || d.action !== 'show-memory') return;
    MEM_QUERY = String(d.query == null ? '' : d.query).slice(0, 200);
    showMemory();
  });

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
    announce('append memory \\u2014 ready');
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
"""
