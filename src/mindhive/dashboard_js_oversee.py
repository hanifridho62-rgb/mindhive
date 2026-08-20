# src/mindhive/dashboard_js_oversee.py
"""The Sessions and Oversee sections: live Claude work — loop runs, interactive
sessions, roadmap panels — plus the prompt-enrichment panel that rides
Oversee's controls.

Extracted verbatim from ``dashboard_page.py`` when that file reached 2396 lines,
more than twice the project's 1000-line cap. Pure text: this module holds one
string constant and imports nothing. ``dashboard_page.render_page`` splices it
back in at ``/*__JS_OVERSEE__*/``, in source order, so the served page is
byte-identical to the one the single file produced.

Not independently loadable in a browser: the whole page is ONE ``<script>``, so
every fragment shares one scope and function declarations hoist across the seams.
The split is for readers and for the cap, and it buys no isolation — a function
here may be called from any other fragment, and is.
"""
JS = """\
  // --- Sessions: rendered by the notification-center fragment (taskview.py).
  // This is the one section whose paint does not happen in this module, and
  // therefore the one whose guard does not either: renderTaskCards awaits
  // /api/sessions and calls paintTaskCards() on the far side of that await,
  // inside taskview.py. The guard lives there, at both the paint and the cache
  // write, and it compares against renderSessions — the section the user picked
  // — because renderTaskCards is not somewhere you can navigate to. A guard
  // placed HERE would run before the await and catch nothing, which would read
  // as protection while providing none.
  async function renderSessions(){ return renderTaskCards(); }
  function showSessions(){ controls.innerHTML=''; liveFn = renderSessions; run('loading sessions', renderSessions, 'rows'); }
  function openSession(sid){ SESSION = sid; showMemory(); }

  // --- Oversee: live Claude work — loop runs AND interactive sessions ---
  // Cards mirror the Improvements view (glyph + pills + expandable body);
  // expansion state survives the live refresh via OV_EXPANDED. The expanded
  // detail is a Claude-style task checklist (\\u2713 done, pulsing \\u25d0
  // doing, \\u25cb pending) in the hive palette.
  let OV_DATA = null;
  const OV_EXPANDED = new Set();
  // Interactive roadmap panel state: which cards have the roadmap open, and
  // which step nodes are expanded — both Sets so the 2.5 s live refresh
  // never collapses what the user is reading (same idiom as OV_EXPANDED).
  const RM_OPEN = new Set();
  const RM_NODE_OPEN = new Set();
  function ovStatus(r){
    if (r.kind === 'chat') return ['\\u25c9', r.online ? 'sok' : 'snone', 'chat session'];
    if (r.status === 'running') return ['\\u25b6', r.online ? 'sok' : 'snone', 'running'];
    if (r.status === 'completed') return ['\\u2714', 'sok', 'completed'];
    if (r.status === 'breaker_open') return ['\\u26a1', 'sbad', 'tripped'];
    return ['\\u25a0', 'snone', r.status || 'ended'];
  }
  // Provenance pill, shared by ovRow and rmPanel so the two surfaces cannot
  // drift into describing the same fact differently.
  const OV_NEW_PILL = '<span class="pill rmnew" title="not in the original plan '
    + '\\u2014 added because the record proved it outstanding">new</span> ';
  function ovRow(glyph, cls, item, dim){
    // Records now; bare strings historically, and `r.doing` is still one. A
    // renderer that assumed either shape would print [object Object] on half the
    // ledger, so normalise instead of branching at every call site.
    const rec = (item && typeof item === 'object') ? item : {text: item};
    return '<div class="snip'+(dim?' dup':'')+'"><span class="'+cls+'">'+glyph
      + '</span> '+(rec.new ? OV_NEW_PILL : '')+esc(rec.text || '')+'</div>';
  }
  function ovGroup(items, glyph, cls, dim){
    return (items || []).map(x => ovRow(glyph, cls, x, dim)).join('');
  }
  // Skill trail on chat cards: which skills fired, when (HH:MM + relative), the
  // outcome, and the recorded "why" — the args the skill was invoked with, else
  // a prompt-gate direction, else the skill's intended use, plus the prompt it
  // was serving ("during"). Ledger facts only, never synthesized. The skill
  // name links to its Skill & Plugin detail.
  function skillAgo(ts){
    const t = Date.parse(ts || '');
    if (!t) return '';
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return s + 's ago';
    const m = Math.floor(s / 60); if (m < 60) return m + 'm ago';
    const h = Math.floor(m / 60); if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }
  function ovOpenSkill(name, plugin){
    if (!name) return;
    showSkillPanel();
    showSkillDetail(name, plugin || null);
  }
  function ovSkillRow(k){
    const name = (k.plugin ? k.plugin + ':' : '') + (k.name || '');
    const why = [k.why, k.during ? 'during: ' + k.during : '']
      .filter(Boolean).join(' \\u00b7 ');
    const ago = skillAgo(k.ts);
    return '<div class="snip ovskill"><span class="'+(k.ok === false ? 'sbad' : 'sok')
      + '">\\u2726</span> <span class="dup">'+esc(fmtHm(k.ts))
      + (ago ? ' \\u00b7 '+esc(ago) : '')+'</span> '
      + '<span class="ovsklink" style="cursor:pointer;text-decoration:underline" '
      + 'data-skill="'+escAttr(k.name || '')+'" data-plugin="'+escAttr(k.plugin || '')
      + '">'+esc(name)+'</span>'
      + (k.ok === false ? ' <span class="sbad">failed</span>' : '')
      + (why ? '<div class="dup" style="margin-left:18px">'+esc(why)+'</div>' : '')
      + '</div>';
  }
  function ovSkills(r){
    const ks = r.skills || [];
    if (!ks.length) return '';
    const hidden = (r.skills_total || ks.length) - ks.length;
    // Compact distinct-skill summary with xN counts (first-appearance order).
    const counts = {}, order = [];
    ks.forEach(k => {
      const n = (k.plugin ? k.plugin + ':' : '') + (k.name || '');
      if (!(n in counts)){ counts[n] = 0; order.push(n); }
      counts[n]++;
    });
    const summary = order.map(n => esc(n) + (counts[n] > 1 ? ' \\u00d7'+counts[n] : ''))
      .join(' \\u00b7 ');
    return '<div class="snip dup">skills used: '+summary+'</div>'
      + ks.map(ovSkillRow).join('')
      + (hidden > 0 ? '<div class="snip dup">\\u2026 '+hidden+' earlier</div>' : '');
  }
  // The roadmap panel's data source (KTD5): an explicit roadmap plan when
  // the ledger has one for this run/session id — it carries the true
  // original ask and step statuses — else a client-side synthesis from the
  // card's own progress groups, so the roadmap button is never dead.
  function rmFromCard(r){
    const steps = [];
    (r.done || []).forEach(t => steps.push({text:t, status:'done', note:''}));
    if (r.doing) steps.push({text:r.doing, status:'active', note:''});
    (r.planned || []).forEach(t => steps.push({text:t, status:'pending', note:''}));
    (r.not_yet || []).forEach(t => steps.push({text:t, status:'skipped', note:''}));
    const done = steps.filter(s => s.status === 'done').length;
    // `r.task` ahead of the title: the synthesised panel labels this field
    // `Asked:`, and the task is the closest thing a chat session has to a
    // declared ask. The title is a prompt headline, which is a fair
    // fallback and a poor first choice.
    return {plan_id:r.run_id, prompt:r.task || r.title || r.run_id, title:'',
            status:'active', cause:'', synthesized:true, steps:steps,
            progress:{done:done, total:steps.length}};
  }
  function rmGlyph(st){
    if (st === 'done') return ['\\u2714', 'rmdone'];
    if (st === 'active') return ['\\u25d0', 'rmactive'];
    if (st === 'skipped') return ['\\u25cb', 'rmskip'];
    return ['\\u25cb', 'rmpend'];
  }
  function rmPanel(rm, rid){
    const steps = rm.steps || [];
    const p = rm.progress || {done:0, total:steps.length};
    const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
    let html = '<div class="rmwrap">'
      + '<div class="rmask"><span class="dup">Asked:</span> '
      + esc(rm.prompt || '')+'</div>';
    if (rm.status === 'interrupted'){
      html += '<div class="rmint">\\u26a0 interrupted'
        + (rm.cause ? ' \\u2014 '+esc(rm.cause) : '')
        + ' \\u00b7 resume: <code>hive roadmap resume '+esc(rm.plan_id)
        + '</code></div>';
    }
    html += '<div class="rmbar"><div class="rmfill" style="width:'+pct+'%"></div></div>'
      + '<div class="dup" style="font-size:11px;margin:2px 0 6px">'
      + p.done+'/'+p.total+' steps done \\u00b7 '+pct+'%'
      + (rm.synthesized ? ' \\u00b7 live view \\u2014 no declared roadmap' : '')
      + '</div>';
    html += steps.map((s, i) => {
      const g = rmGlyph(s.status);
      const key = rid+':'+i;
      const edge = (i === 0 ? ' rmfirst' : '') + (i === steps.length-1 ? ' rmlast' : '');
      return '<div class="rmstep'+edge+'" data-key="'+escAttr(key)
        + '" title="click for step detail">'
        + '<span class="rmnode '+g[1]+'">'+g[0]+'</span>'
        // Provenance, not recency: this step was not in the plan the work
        // started from — the replenishment injector added it because the record
        // proved it outstanding. It stays marked after the step is done.
        + (s.new ? OV_NEW_PILL : '')
        + '<span'+(s.status === 'skipped' ? ' class="dup"' : '')+'>'+esc(s.text)+'</span>'
        + (RM_NODE_OPEN.has(key)
            ? '<div class="rmdetail">status: '+esc(s.status)
              + (s.note ? ' \\u00b7 '+esc(s.note) : '')+'</div>' : '')
        + '</div>';
    }).join('');
    return html + '</div>';
  }
  // Interrupted (or card-less) plans keep their roadmap on the board even
  // after the run/session that owned them left it — the out-of-usage marker.
  function rmPlanCard(p, showProj){
    const bad = p.status === 'interrupted';
    return '<li><div class="file"><span class="statusDot '+(bad?'sbad':'snone')+'">'
      + (bad ? '\\u26a0' : '\\u25c9')+'</span> '
      + (showProj && p.project ? esc(p.project)+' \\u00b7 ' : '')
      + esc(p.title || p.plan_id)
      + ' <span class="pill">'+esc(p.status)+'</span></div>'
      + rmPanel(p, 'plan-'+p.plan_id)+'</li>';
  }
  function ovCard(r, showProj){
    const st = ovStatus(r);
    const open = OV_EXPANDED.has(r.run_id);
    const head = '<span class="statusDot '+st[1]+'">'+st[0]+'</span>'
      + (showProj && r.project ? esc(r.project)+' \\u00b7 ' : '')
      + '<span class="ovtitle" style="cursor:pointer" title="click for progress detail">'
      + esc(r.title || r.run_id)+'</span>'
      + srPill(r.origin)
      + '<button class="mini ovrm" data-rid="'+escAttr(r.run_id)
      + '" title="the ask, the plan, and how far along it is">roadmap</button>'
      + '<button class="mini ovdel" data-rid="'+escAttr(r.run_id)
      + '" data-proj="'+escAttr(r.project||'')+'">delete</button>';
    const chat = r.kind === 'chat';
    const pills = '<span class="pill">'+esc(st[2])+'</span> '
      + '<span class="pill">'+(r.iterations||0)+(chat
          ? ' command'+((r.iterations||0)===1?'':'s')
          : ' iteration'+((r.iterations||0)===1?'':'s'))+'</span>'
      + (chat && r.skills_total ? ' <span class="pill">'+r.skills_total
          + ' skill'+(r.skills_total===1?'':'s')+'</span>' : '')
      + (chat ? '' : ' <span class="pill">$'+(r.total_cost_usd||0).toFixed(2)+'</span>')
      // What the run added to its OWN plan. Absent when it added nothing, so a
      // normal run is not decorated with a zero — the pill exists to make the
      // loop growing its own work visible, and a cost the operator can see.
      + (r.injected_count ? ' <span class="pill rmnew" title="work the injector '
          + 'added to this run\\u2019s plan from the record">'+r.injected_count
          + ' injected</span>' : '')
      + (r.online ? ' <span class="pill" style="color:var(--ok);border-color:var(--ok-edge)">'
                    + (chat ? 'online \\u2014 working' : 'online')+'</span>' : '');
    let body = '';
    if (open){
      // The task FIRST, because it is the question the card exists to
      // answer -- what is this session working on. It is not the title: the
      // title is the prompt headline, and collapsing the two is what left
      // the task with nowhere to appear.
      if (r.task) body += '<div class="snip"><span class="ovtask">'
        + 'working on</span> '+esc(r.task)+'</div>';
      body += ovGroup(r.done, '\\u2713', 'ovdone');
      if (r.doing) body += ovRow('\\u25d0', 'ovspin', r.doing);
      body += ovGroup(r.planned, '\\u25cb', 'ovpend')
            + ovGroup(r.not_yet, '\\u25cb', 'ovpend', true);
      // Open questions, last and undimmed: they are the one group the
      // reader is scanning for when the answer to 'how far along' is
      // 'stuck'.
      body += ovGroup(r.blockers, '\\u26a0', 'ovblock');
      body += ovSkills(r);
      body += '<div class="snip dup">started '+esc(fmtTs(r.started))
            + ' \\u00b7 last event '+esc(fmtTs(r.last_event_ts))+'</div>';
    }
    if (RM_OPEN.has(r.run_id)){
      body += rmPanel(r.roadmap || rmFromCard(r), r.run_id);
    }
    return '<li data-rid="'+escAttr(r.run_id)+'"><div class="file">'+head+'</div>'
      + '<div style="margin:4px 0">'+pills+'</div>'+body+'</li>';
  }
  // --- Prompt enrichment (Oversee): improve a draft prompt before spending a
  // session on it — asks the user (clarifying questions), checks the source
  // code (candidate files), injects skills, and composes an Anthropic-SOP
  // shaped prompt. Default-ON server-side; the draft, answers, and last
  // result survive the 2.5 s live refresh via module state (OV_EXPANDED
  // idiom), and the refresh skips repaints while the user is typing here.
  let EN_SET = null;       // /api/enrich/settings result
  let EN_RESULT = null;    // last /api/enrich/preview result
  let EN_PROMPT = '';      // draft prompt text
  const EN_ANSWERS = {};   // question id -> typed answer
  function enOn(){ return !EN_SET || EN_SET.enabled !== false; }
  function enChip(met){
    return met ? '<span class="enok">\\u2713</span>'
               : '<span class="enbad">\\u2717</span>';
  }
  function enResult(r){
    let html = '<div class="ensec">'
      + (r.sop||[]).map(c => '<span class="pill" title="'+escAttr(c.hint||'')+'">'
          + enChip(c.met)+' '+esc(c.label)+'</span>').join(' ')+'</div>';
    if ((r.questions||[]).length){
      html += '<div class="ensec"><b>Asking you</b> \\u2014 answers fold into '
        + 'the composed prompt (re-enrich after answering):</div>'
        + r.questions.map(q => '<div class="enq">'+esc(q.text)
          + '<br><input class="enans" data-qid="'+escAttr(q.id)
          + '" placeholder="answer\\u2026" value="'+escAttr(EN_ANSWERS[q.id]||'')
          + '"></div>').join('');
    }
    if ((r.sources||[]).length){
      html += '<div class="ensec"><b>From the source code</b>: '
        + r.sources.map(s => '<span class="skchip" title="'+escAttr(s.why||'')
            + '">'+esc(s.path)+'</span>').join(' ')+'</div>';
    }
    const sk = ((r.skills||{}).directives||[]).concat((r.skills||{}).suggestions||[]);
    if (sk.length){
      html += '<div class="ensec"><b>Skills to inject</b>: '
        + sk.map(s => '<span class="pill'+(s.provenance==='measured'?'':' est')+'">'
            + esc(s.skill)+(s.plugin?' \\u00b7 '+esc(s.plugin):'')
            + ' \\u2014 '+esc(s.provenance)+'</span>').join(' ')+'</div>';
    }
    return html
      + '<div class="ensec"><b>Enriched prompt</b> (Anthropic-SOP shaped)'
      + ' <button id="encopy" class="mini">copy</button></div>'
      + '<pre class="enout">'+esc(r.enriched_prompt||'')+'</pre>';
  }
  function enPanel(){
    if (PROJECT === '*'){
      // Never vanish silently: under HIVE MIND the panel stays visible as a
      // stub saying where the feature lives (enrichment reads ONE project
      // namespace — ledger, settings sidecar, trained manifest).
      return '<div id="enpanel" class="enwrap"><div class="file">Enrichment'
        + ' <span class="dup">pick a concrete project in the header to use '
        + 'it \\u2014 enrichment works per project</span></div></div>';
    }
    const on = enOn();
    let html = '<div id="enpanel" class="enwrap"><div class="file">'
      + 'Enrichment'
      + '<button class="mini" id="entoggle" title="on by default; the choice '
      + 'persists per project">'+(on ? 'on \\u2014 turn off' : 'off \\u2014 turn on')
      + '</button>'
      + (on ? '' : ' <span class="dup">disabled \\u2014 the hook adds nothing '
                   + 'and this panel is idle</span>')
      + '</div>';
    if (on){
      html += '<textarea id="enprompt" rows="3" placeholder="Paste a draft '
        + 'prompt \\u2014 enrichment asks you, checks the source code, injects '
        + 'skills, and composes an Anthropic-SOP prompt">'+esc(EN_PROMPT)
        + '</textarea>'
        + '<div style="margin:6px 0"><button id="enrun">Enrich</button></div>'
        + (EN_RESULT ? enResult(EN_RESULT) : '');
    }
    return html + '</div>';
  }
  async function enrichNow(){
    const ta = view.querySelector('#enprompt');
    if (ta) EN_PROMPT = ta.value;
    if (!EN_PROMPT.trim()) return;
    await run('enriching prompt\\u2026', async () => {
      const res = await apiPost('/api/enrich/preview',
                                {prompt: EN_PROMPT, answers: EN_ANSWERS});
      if (!res.ok) { showText(res); return; }
      EN_RESULT = res.result;
      paintOversee();
    });
  }
  function wireEnrich(){
    const t = view.querySelector('#entoggle');
    if (t) t.onclick = async () => {
      const res = await apiPost('/api/enrich/toggle', {enabled: !enOn()});
      if (!res.ok) { showText(res); return; }
      EN_SET = res.settings;
      if (!EN_SET.enabled) EN_RESULT = null;
      paintOversee();
    };
    const go = view.querySelector('#enrun');
    if (go) go.onclick = () => enrichNow();
    const cp = view.querySelector('#encopy');
    if (cp) cp.onclick = async () => {
      if (!EN_RESULT) return;
      try { await navigator.clipboard.writeText(EN_RESULT.enriched_prompt); }
      catch(_){ /* clipboard may be unavailable; the text stays visible */ }
      // record the act, fire-and-forget: copying is real use, previews are not
      apiPost('/api/enrich/preview',
              {prompt: EN_PROMPT, answers: EN_ANSWERS, record: true});
      cp.textContent = 'copied \\u2713';
    };
    view.querySelectorAll('.enans').forEach(el => {
      el.oninput = () => { EN_ANSWERS[el.dataset.qid] = el.value; };
    });
    const ta = view.querySelector('#enprompt');
    if (ta) ta.oninput = () => { EN_PROMPT = ta.value; };
  }
  function paintOversee(){
    if (!OV_DATA) return;
    // Online/offline strip: shown ONLY while Claude is working on a session.
    const online = OV_DATA.any_online
      ? '<div class="row"><span class="pill" style="color:var(--ok);border-color:var(--ok-edge)">'
        + '<span class="blip">\\u25cf</span> online \\u2014 Claude is working</span></div>'
      : '';
    const showProj = (PROJECT === '*');
    // Card-less roadmaps (interrupted markers, plans whose run left the
    // board) render above the session list so resumable work stays visible.
    const plans = OV_DATA.plans || [];
    const strip = plans.length
      ? '<h2 class="h">'+plans.length+' roadmap'
        + (plans.length===1?'':'s')+' awaiting resume</h2>'
        + '<ul class="hits">'+plans.map(p => rmPlanCard(p, showProj)).join('')+'</ul>'
      : '';
    if (!OV_DATA.runs.length && !plans.length){
      showHTML(enPanel() + online + '<h2 class="h">No work sessions \\u2014 interactive '
        + 'Claude sessions appear here as their telemetry flows, and '
        + '<code>hive loop</code> runs join them. Finished sessions are '
        + 'removed automatically.</h2>');
      wireEnrich();
      return;
    }
    showHTML(enPanel() + online + strip
      + (OV_DATA.runs.length
        ? '<h2 class="h">'+OV_DATA.runs.length+' work session'
          + (OV_DATA.runs.length===1?'':'s')
          + (OV_DATA.dismissed ? ' <span class="dup">\\u00b7 '+OV_DATA.dismissed
             +' finished &amp; removed</span>' : '')+'</h2>'
          + '<ul class="hits">'+OV_DATA.runs.map(r => ovCard(r, showProj)).join('')+'</ul>'
        : ''));
    wireEnrich();
    view.querySelectorAll('.ovtitle').forEach(el => el.onclick = () => {
      const rid = el.closest('li').dataset.rid;
      if (OV_EXPANDED.has(rid)) OV_EXPANDED.delete(rid); else OV_EXPANDED.add(rid);
      paintOversee();
    });
    view.querySelectorAll('.ovdel').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation(); deleteRun(el.dataset.rid, el.dataset.proj);
    });
    view.querySelectorAll('.ovrm').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation();
      const rid = el.dataset.rid;
      if (RM_OPEN.has(rid)) RM_OPEN.delete(rid); else RM_OPEN.add(rid);
      paintOversee();
    });
    view.querySelectorAll('.rmstep').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation();
      const key = el.dataset.key;
      if (RM_NODE_OPEN.has(key)) RM_NODE_OPEN.delete(key); else RM_NODE_OPEN.add(key);
      paintOversee();
    });
    view.querySelectorAll('.ovsklink').forEach(el => el.onclick = (ev) => {
      ev.stopPropagation();
      ovOpenSkill(el.dataset.skill, el.dataset.plugin);
    });
  }
  async function renderOversee(){
    // Skip this refresh tick while the user is typing in the enrichment
    // panel — a repaint would eat the caret and the click-in-progress.
    const ae = document.activeElement;
    if (ae && ae.closest && ae.closest('#enpanel')) return;
    const [res, es] = await Promise.all([apiGet('/api/oversee'),
                                         apiGet('/api/enrich/settings')]);
    // The clobber the user reported was this exact render landing late. Discard
    // before the throw too — see renderImprovements for why.
    if (!shouldPaint(liveFn, renderOversee)) return;   // newer section owns the panel
    if (!res || res.ok === false) throw new Error((res && res.error) || 'oversee fetch failed');
    OV_DATA = res;
    EN_SET = (es && typeof es.enabled === 'boolean') ? es : {enabled: true};
    paintOversee();
  }
  async function deleteRun(rid, proj){
    if (!confirm('Delete this session from Oversee? Its events stay in the '
        + 'ledger (append-only), it just stops being shown.')) return;
    await run('deleting session\\u2026', async () => {
      const body = {run_id: rid};
      if (PROJECT === '*' && proj) body.project = proj;
      const res = await apiPost('/api/oversee/dismiss', body);
      if (!res.ok) { showText(res); return; }
      await renderOversee();
    });
  }
  function showOversee(){ controls.innerHTML=''; liveFn = renderOversee; run('loading oversee', renderOversee, 'rows'); }
  function showOverview(){ controls.innerHTML=''; liveFn = renderOverview; run('loading overview', renderOverview, 'cards'); }
"""
