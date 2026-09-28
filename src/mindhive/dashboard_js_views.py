# SPDX-License-Identifier: Apache-2.0
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
        // `two` asks the sheet for the two-column row: the sentence on the
        // left at the board's reading measure, the file list and the stamp
        // beside it instead of stacked under it. This section is the one with
        // the shape for it -- 270 rows of prose plus two lines of metadata,
        // where the metadata is what the reader scans and the prose is what
        // they read. Not asked for by the SOP list two functions below, whose
        // `.snip` holds the procedure's whole body: a 26em side column is the
        // right home for a file list and the wrong one for a paragraph.
        '<li class="two"><div class="file">'+logBadge(e.category)+' '+esc(e.text)+'</div>'
        + ((e.files||[]).length ? '<div class="snip">'+(e.files||[]).map(esc).join(' \\u00b7 ')+'</div>' : '')
        + '<div class="snip dup">'+esc(fmtTs(e.ts))+' \\u00b7 session '+esc(e.session||'')
        + (showProj && e.project ? ' \\u00b7 '+esc(e.project) : '') +'</div></li>').join('')
      + '</ul>');
  }
  function showLog(){ markSection('log'); controls.innerHTML=''; liveFn = renderLog; run('loading change log', renderLog, 'rows'); }

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
  function showSops(){ markSection('sops'); controls.innerHTML=''; liveFn = renderSops; run('loading SOPs', renderSops, 'rows'); }

  async function renderState(){
    const s = await apiGet('/api/state');
    if (!shouldPaint(liveFn, renderState)) return;     // newer section owns the panel
    showText(s);
  }
  function showState(){ markSection('state'); controls.innerHTML=''; liveFn = renderState; run('loading state', renderState, 'block'); }
  function showBriefing(){
    markSection('briefing');
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
    markSection('recall');
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
        // The one row in sixteen whose `.file` really is a file: `path` puts the
        // link blue back on it, which the shared rule stopped being.
        + hits.map(h => '<li><div class="file path">'+esc(h.file||'')+'</div><div class="snip">'+esc(h.snippet||'')+'</div></li>').join('')
        + '</ul>');
    }, 'rows');
  }

  // Graph is a one-shot heavy render (not live-refreshed): fetch once and hand
  // the self-contained page to the iframe. Semantic links are the expensive,
  // opt-in build (an embedding search per event) so they stay off by default;
  // ticking the box reloads with ?semantic=1.
  function showGraph(){
    markSection('graph');
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

  // A frame that answers neither load nor error must not hold the busy chip
  // open for the rest of the session. Long enough that a slow workspace page
  // still gets to announce itself honestly.
  const WS_LOAD_MS = 15000;

  // Workspace views (MindParallel / MindSwarm / Self-Reliant) are full pages
  // served by the server and loaded by URL - srcdoc must be cleared first or
  // it wins over src. 'research' lives under its own /api/research family.
  //
  // Through run() for the GENERATION, not for the busy chip. This was the one
  // section entry that swapped the panel without going through run(), so it
  // never bumped RUN_GEN - and the run belonging to the section being LEFT
  // therefore still passed its own current() check. Its 150 ms skeleton timer
  // then fired into #view, and paintInto's only(view) hid the frame the reader
  // had already chosen: the section button reads ws-parallel while the panel
  // shows Overview's placeholder over a blank frame, a section that never
  // arrives. Measured on the real page in 2 of 4 clicks made inside the
  // skeleton window and 0 of 2 outside it; the variance is the overview memo,
  // which resolves inside 150 ms once warm and fires no skeleton at all.
  //
  // Bumping RUN_GEN by hand was the smaller change and the wrong one: it would
  // retire the stale run without adopting run()'s finally, and that finally is
  // where setBusy(false) lives - the chip would have stuck on forever.
  //
  // No `shape` argument: a skeleton paints into #view, which is precisely the
  // clobber above. The frame is its own placeholder.
  function showWorkspace(view){
    markSection('ws-' + view);
    controls.innerHTML=''; liveFn = null;
    run('loading ' + view + ' workspace', () => new Promise(resolve => {
      only(graph);
      const want = u(withProj(view === 'research' ? '/api/research/page'
                                                  : '/api/workspace/'+view));
      // Already showing this exact page: keep the document rather than loading
      // it again. It holds whatever the reader typed and has not saved, its
      // scroll position and its expanded rows, and re-navigating discarded all
      // of it on every visit - measured with MindParallel's project-name field,
      // which nothing but the reader ever writes: typed, glanced at Overview,
      // came back to an empty box. Keeping it costs no staleness either, since
      // the page's own loop resumes on its next tick now the frame is visible.
      if (!graph.hasAttribute('srcdoc') && graph.getAttribute('src') === want)
        return resolve();
      graph.removeAttribute('srcdoc');
      const done = () => {
        clearTimeout(timer);
        graph.removeEventListener('load', done);
        graph.removeEventListener('error', done);
        resolve();
      };
      const timer = setTimeout(done, WS_LOAD_MS);
      graph.addEventListener('load', done);
      graph.addEventListener('error', done);
      graph.src = want;
    }));
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
    markSection('append');
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
