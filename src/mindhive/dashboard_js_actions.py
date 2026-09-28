# src/mindhive/dashboard_js_actions.py
"""The six long operations as 202-accepted polled jobs (T13), the MN-13 faculty
cat board's poll, and the boot sequence.

Last, and it has to be: `setInterval(pollCats, …)` and the boot IIFE are the
only two statements on this page that EXECUTE at parse time rather than
declaring a function. Everything they touch must already be declared above
them, so this fragment is concatenated last.

Extracted verbatim from ``dashboard_page.py`` when that file reached 2396 lines,
more than twice the project's 1000-line cap. Pure text: this module holds one
string constant and imports nothing. ``dashboard_page.render_page`` splices it
back in at ``/*__JS_ACTIONS__*/``, in source order, so the served page is
byte-identical to the one the single file produced.

Not independently loadable in a browser: the whole page is ONE ``<script>``, so
every fragment shares one scope and function declarations hoist across the seams.
The split is for readers and for the cap, and it buys no isolation — a function
here may be called from any other fragment, and is.
"""
JS = """\
  // --- the six long operations: 202 accepted, then polled (T13) -------------
  //
  // These six used to hold one fetch open for the whole run — a pytest run, an
  // embedding pass, a claude call per incident. Past the fetch timeout the page
  // gave up on work that was still running and would still finish, and reported
  // a success as a failure. Now the POST returns a job id and this follows it.
  //
  // Polling, not SSE: the page already has a 2.5s poll loop and its own
  // discard-stale-paint idiom, so a second transport with its own reconnect
  // semantics would be a new failure mode bought for the same result.
  const JOB_POLL_MS = 900;
  const JOB_TAIL_LINES = 400;      // what the pane keeps; the server caps the rest
  function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

  // Polls ONE job by id. Deliberately not `apiGet`: that guards against a
  // project switch landing a stale paint, and a job id is not ambiguous about
  // which namespace it belongs to — it was started for exactly one. Switching the
  // picker while a four-minute suite runs must not turn it into an error.
  async function apiJob(id, offset){
    const r = await fetch(u('/api/job?id=' + encodeURIComponent(id)
        + '&offset=' + encodeURIComponent(offset)),
      { headers: { 'X-Hive-Token': TOKEN } });
    return r.json();
  }

  // What the pane says. The outcome is named, not implied: "nothing to do" and
  // "did work" are both successes and the user is entitled to know which one
  // happened.
  function jobText(job, output, dropped){
    const head = (job.state === 'finished')
      ? (job.summary || job.outcome || 'done')
        + '\\n' + (job.outcome || '') + ' \\u00b7 ' + (job.seconds || 0) + 's'
      : (job.label || 'working') + '\\u2026 ' + (job.seconds || 0) + 's';
    const lines = String(output || '').split('\\n');
    const tail = lines.length > JOB_TAIL_LINES
      ? lines.slice(-JOB_TAIL_LINES).join('\\n') : String(output || '');
    // Truncation the reader cannot see is output it will misread.
    const gap = dropped
      ? '[' + dropped + ' earlier bytes dropped \\u2014 the buffer is capped]\\n\\n'
      : '';
    // The bar is drawn from the NUMBER, never from the tail. A `\\r` frame
    // never reaches the byte stream the tail is built from -- the ring holds
    // the live one back and reports it as `progress_pct` (jobs.OutputRing),
    // precisely so a reader does not have to re-parse carriage returns it
    // cannot see. A finished job has no live frame, so it gets no bar.
    const pct = (job.state !== 'finished'
                 && typeof job.progress_pct === 'number') ? job.progress_pct : null;
    const filled = (pct === null) ? 0 : Math.round(pct / 5);
    const bar = (pct === null) ? ''
      : '[' + '\\u2588'.repeat(filled) + '\\u00b7'.repeat(20 - filled)
        + '] ' + pct + '%\\n\\n';
    return head + '\\n\\n' + bar + gap + tail;
  }

  // Start the work and follow it to its end. Returns the finished job; throws
  // when the server refused to start it at all.
  async function awaitJob(path, body, current, paint){
    const started = await apiPost(path, body || {});
    const why = refusal(started);
    if (why) throw new Error(why);   // reaches run()'s catch -> "failed", announced
    let job = started.job;
    if (!job || !job.id)
      throw new Error('the server accepted the work but named no job to follow');
    let output = job.output || '', offset = job.offset || 0, dropped = job.dropped || 0;
    while (job.state !== 'finished') {
      if (paint && current()) paint(job, output, dropped);
      await sleep(JOB_POLL_MS);
      const r = await apiJob(job.id, offset);
      const gone = refusal(r);
      if (gone) throw new Error(gone);
      job = r.job;
      // Only what is new: the offset the last poll handed back is what keeps a
      // 2.5s tick from re-sending the whole buffer (T16).
      output += (job.output || '');
      offset = job.offset;
      dropped += (job.dropped || 0);
    }
    job.fullOutput = output;
    job.droppedBytes = dropped;
    return job;
  }

  function runJob(path, label, body){
    controls.innerHTML=''; liveFn = null;
    run(label, async (current) => {
      const job = await awaitJob(path, body, current,
        (j, out, dropped) => showText(jobText(j, out, dropped)));
      const text = jobText(job, job.fullOutput, job.droppedBytes);
      if (current()) showText(text);
      if (job.outcome === 'failed') {
        // Carries the pane so `fail` can keep the output while stating the
        // reason — a failed suite's own output is the point.
        const e = new Error(job.summary || 'the job failed');
        e.pane = text;
        throw e;
      }
    });
  }

  // The Heavy confirm (these cost tokens or minutes) plus the 202 handoff.
  function confirmJob(path, label, body){
    if(!confirm(label + ' \\u2014 this can take a while. Continue?')) return;
    runJob(path, label, body);
  }

  // Embed asks a second question the generic confirm cannot: whether to force
  // past LIVE_LOCK. That answer is refused SYNCHRONOUSLY by the server, so it
  // arrives as an ordinary ok:false rather than as a job outcome.
  function runEmbed(){
    if(!confirm('Embed \\u2014 this can take a while. Continue?')) return;
    const body = confirm('Force past LIVE_LOCK? (only if the box is idle)')
      ? { force: true } : {};
    runJob('/api/embed', 'Embedding', body);
  }

  //__TASKS_JS__

  // --- MN-13: the faculty cat board ---------------------------------------
  // Injected from `cat_board.script()` — the same implementation the swarm page
  // renders, not a copy of it.
/*__CAT_JS__*/
  // Its own interval rather than a hook into refreshLive(): refreshLive returns
  // early whenever the visible section is not live-refreshable, when a manual
  // operation is running, or before any section has registered a liveFn — and the
  // cat board's whole job is to be readable exactly then, when something is slow or
  // stuck. Sharing that timer would blank the board at the moment it matters.
  // Errors are swallowed for the same reason the swarm page keeps this call outside
  // its main try: a failing board must not take out the page, and a failing page
  // must not take out the board.
  // In-flight guard, same reason as refreshLive — see the measurement in its comment.
  // This timer is the worse offender of the two: it has no busy gate at all, so it
  // fires against /api/mindnet/cats every 2500 ms whatever section is open, and that
  // endpoint reads the whole ledger (or every namespace in HIVE MIND) on each call.
  // Measured alone it costs 0.87-0.92 s and returns 503 bytes; under real load it took
  // 6 s for the same 503 bytes, which is queueing, not payload.
  let catsInFlight = false;
  async function pollCats(){
    if (!liveOn || document.hidden) return;
    if (catsInFlight) return;   // skip this tick, never queue it
    catsInFlight = true;
    // ONE fetch feeds both strips — see the swarm page's poll for why.
    try {
      const board = await apiGet('/api/mindnet/cats');
      paintCats(board.cats || []);
      paintSessions(board);
    } catch(_){}
    finally { catsInFlight = false; }
  }
  setInterval(pollCats, LIVE_MS);

  // populate the project picker, then land on the overview — unless the user
  // already picked a view while the picker was still loading.
  (async () => { await loadProjects(); if (!viewPicked) showOverview(); pollCats(); })();"""
