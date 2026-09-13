# src/mindhive/taskview.py
"""Backend + page fragments for the Sessions notification center.

Three disclosure levels: L1 per-session tallies (the summary chip), L2 the
ordered activity rows capped to the last ITEM_CAP entries, and L3 granular
detail (full diffs / command output) parsed lazily from the Claude Code
transcript so the append-only ledger never stores heavy payloads. Nothing
here deletes anything — the Sessions page is a deliberately auditable track
record. Also owns the cards' CSS/JS fragments, injected into the page by
dashboard.py so dashboard_page.py stays sibling-free."""
import calendar
import difflib
import json
import re
import time
from pathlib import Path

from . import events as ev_mod

ACTIVE_WINDOW_SECS = 600      # newest event younger than this -> "ongoing"
ITEM_CAP = 100                # locked: detail history shows the last 100 rows
DIFF_CAP_LINES = 400
OUTPUT_CAP_CHARS = 10_000
# `events` owns the rule; this used to recompile it, which made a FOURTH
# statement of a whitelist the anti-drift test enumerates three of -- and the
# copy had already drifted. It was anchored with `$`, which Python matches
# immediately BEFORE a trailing newline, so `"abc\n"` passed the guard whose
# own docstring promises path traversal is rejected. `events` uses `fullmatch`,
# which has no such behaviour; importing the pattern is how the fourth
# statement stops being a statement.
_NOTE_TYPES = ("caveat", "decision", "experiment_result",
               "next_step", "open_question")


def _epoch(ts):
    if not isinstance(ts, str):
        return None
    try:
        return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def status_of(last_ts, now=None) -> str:
    """"ongoing" when the session's newest event is fresher than
    ACTIVE_WINDOW_SECS, else "completed". Unparseable -> completed."""
    e = _epoch(last_ts)
    if e is None:
        return "completed"
    return "ongoing" if (now or time.time()) - e < ACTIVE_WINDOW_SECS else "completed"


def tally(evs) -> dict:
    """L1 aggregate for one session: row-kind counts plus summed line stats.
    Zero-valued keys are kept — the JS omits them from the chip."""
    t = {"files": 0, "plus": 0, "minus": 0, "commands": 0,
         "notes": 0, "failures": 0, "loops": 0}
    seen_files = set()
    for ev in evs:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.type == "file_touched":
            seen_files.add(p.get("text"))
            t["plus"] += int(p.get("plus") or 0)
            t["minus"] += int(p.get("minus") or 0)
        elif ev.type == "command_run":
            t["commands"] += 1
        elif ev.type in _NOTE_TYPES:
            t["notes"] += 1
        elif ev.type == "tool_failed":
            t["failures"] += 1
        elif ev.type == "loop_iteration":
            t["loops"] += 1
    t["files"] = len(seen_files)
    return t


def items(evs, cap=ITEM_CAP) -> dict:
    """L2 rows in ledger order, capped to the LAST ``cap``. The ledger keeps
    everything; ``truncated`` tells the UI how many older rows exist."""
    rows = []
    for ev in evs:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.type == "file_touched":
            row = {"kind": "touched", "text": p.get("text", ""), "ts": ev.ts}
            for k in ("plus", "minus", "tool"):
                if k in p:
                    row[k] = p[k]
        elif ev.type == "command_run":
            row = {"kind": "ran", "text": p.get("text", ""), "ts": ev.ts}
            if p.get("desc"):
                row["desc"] = p["desc"]
        elif ev.type in _NOTE_TYPES:
            row = {"kind": ev.type, "text": p.get("text", ""), "ts": ev.ts}
        elif ev.type == "tool_failed":
            row = {"kind": "failed", "ts": ev.ts,
                   "text": p.get("text") or p.get("signature", "")}
        elif ev.type == "loop_iteration":
            row = {"kind": "loop", "ts": ev.ts,
                   "text": p.get("summary") or f"iteration {p.get('iteration')}",
                   "iteration": p.get("iteration"),
                   "progress": p.get("progress"),
                   "breaker_state": p.get("breaker_state"),
                   "cost_usd": p.get("cost_usd"),
                   "total_tokens": p.get("total_tokens"),
                   "context_fill_ratio": p.get("context_fill_ratio")}
        else:
            continue
        rows.append(row)
    total = len(rows)
    return {"items": rows[-cap:], "total": total,
            "truncated": max(0, total - cap)}


# --- L3: lazy granular detail from the Claude Code transcript --------------

def transcripts_root() -> Path:
    return Path.home() / ".claude" / "projects"


def find_transcript(session_id, root=None):
    """Locate ``<root>/<any dir>/<session_id>.jsonl``. The id becomes a
    filename, so anything but a strict token is rejected (path traversal)."""
    if not isinstance(session_id, str) \
            or not ev_mod.SESSION_ID_RE.fullmatch(session_id):
        return None
    root = Path(root) if root else transcripts_root()
    if not root.exists():
        return None
    for d in sorted(root.iterdir()):
        f = d / f"{session_id}.jsonl"
        if d.is_dir() and f.exists():
            return f
    return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _diff(old, new, path) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=path, tofile=path, lineterm=""))
    if len(lines) > DIFF_CAP_LINES:
        extra = len(lines) - DIFF_CAP_LINES
        lines = lines[:DIFF_CAP_LINES] + [f"… +{extra} more lines"]
    return "\n".join(lines)


def _detail_for_use(block):
    name = block.get("name")
    inp = block.get("input") or {}
    if name in ("Edit", "Write", "NotebookEdit"):
        fp = inp.get("file_path")
        if not fp:
            return None
        d = {"kind": "touched", "text": fp, "tool": name}
        old, new = inp.get("old_string"), inp.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            d["diff"] = _diff(old, new, fp)
        elif name == "Write" and isinstance(inp.get("content"), str):
            head = inp["content"].splitlines()[:DIFF_CAP_LINES]
            d["diff"] = "\n".join("+" + ln for ln in head)
        return d
    if name in ("Bash", "PowerShell"):
        cmd = inp.get("command")
        if not cmd:
            return None
        d = {"kind": "ran", "text": cmd[:180], "command": cmd[:2000]}
        desc = inp.get("description")
        if isinstance(desc, str) and desc.strip():
            d["desc"] = desc.strip()[:120]
        return d
    return None


def detail(session_id, root=None) -> dict:
    """L3: ordered granular details parsed from the session transcript.
    Items carry the same (kind, text) identity as the L2 rows so the UI can
    zip them in order. A missing transcript degrades — old sessions must
    render, not 500."""
    f = find_transcript(session_id, root=root)
    if f is None:
        return {"error": "transcript_unavailable", "items": []}
    try:
        lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {"error": "transcript_unavailable", "items": []}
    uses, order = {}, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        content = (o.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if o.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    d = _detail_for_use(b)
                    if d is not None:
                        uses[b.get("id")] = d
                        order.append(b.get("id"))
        elif o.get("type") == "user":
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                d = uses.get(b.get("tool_use_id"))
                if d is not None and d["kind"] == "ran":
                    d["output"] = _result_text(b.get("content"))[:OUTPUT_CAP_CHARS]
    out = [uses[i] for i in order if i in uses]
    return {"items": out[-ITEM_CAP:], "total": len(out)}


# --- page fragments (injected into dashboard_page by dashboard.py) ----------

CARDS_CSS = """
  .tcard { background:var(--sunken); border:1px solid var(--line); border-radius:10px;
    padding:10px 14px; margin:10px 0; }
  .tcard .thead { display:flex; gap:10px; align-items:center; cursor:pointer;
    flex-wrap:wrap; }
  .tstat { font-size:11px; border-radius:999px; padding:1px 8px;
    border:1px solid var(--line); white-space:nowrap; }
  .tstat.ongoing { color:var(--warn); animation: pulse 1.6s infinite; }
  /* This fragment's own motion, guarded here rather than in the host page —
     same rule cat_board follows: the sheet that turns an animation ON owns
     switching it off, so a second consumer of CARDS_CSS inherits the guard
     instead of quietly losing it. `!important` because `.tstat.ongoing` is as
     specific as this rule and a media query adds no specificity of its own.
     The `pulse` keyframes come from the host page; only the rule is ours. */
  @media (prefers-reduced-motion: reduce) {
    .tstat.ongoing { animation:none !important; }
  }
  .tstat.done { color:var(--ok); }
  .tsid { font-size:12px; color:var(--link); word-break:break-all; }
  .tchip { font-size:12px; color:var(--text); }
  .tspan { font-size:11px; color:var(--dim); margin-left:auto; }
  .trows { list-style:none; padding:0; margin:8px 0 0; }
  .trow { border-top:1px solid var(--raised); padding:5px 2px; }
  /* Both of these carry `cursor:pointer` and neither answered it. No
     transition on purpose: these sit inside a card that repaints on a poll, and
     an instant tint cannot be caught half-applied by a rebuild the way a
     travelling one can. */
  .tlab { cursor:pointer; border-radius:4px; }
  .tlab:hover { background:var(--hover-tint); }
  .tcard .thead:hover { background:var(--hover-tint); }
  .tpath { color:var(--link); } .tplus { color:var(--ok); } .tminus { color:var(--bad); }
  .tbad { color:var(--bad); } .tnote { color:var(--warn); }
  .tbody { margin:6px 0 4px 16px; }
  .tdiff { background:#0a0c11; border:1px solid var(--raised); border-radius:6px;
    padding:8px 10px; max-height:40vh; overflow:auto; white-space:pre-wrap;
    font-size:12px; }
  .dplus { color:var(--ok); } .dminus { color:var(--bad); } .dctx { color:var(--muted); }
  .tdet td { padding:1px 10px 1px 0; color:var(--muted); }
  .tdet td+td { color:var(--text); }
  .tmem { color:var(--link-bright); font-size:12px; }
"""


CARDS_JS = """
  // --- Sessions notification center (injected by taskview.py) ---
  // Spliced into the SAME page scope as dashboard_js_core, so the shared
  // helpers -- esc, escAttr, and the fmtTs/fmtHm clock -- are in scope here
  // and every stamp this file renders is expected to use them.
  // Expansion state and fetched levels live at module level so the 2.5s live
  // re-render never collapses what the user opened (recorded lesson). There is
  // deliberately NO delete affordance anywhere in this view: the history is an
  // auditable track record.
  const TASK_OPEN = new Set();       // session keys with the card expanded
  const TASK_ROW_OPEN = new Set();   // key + '#' + row index
  const TASK_ITEMS = {};             // key -> /api/task response
  const TASK_DETAIL = {};            // key -> /api/task_detail (null = loading)
  let TASK_SESSIONS = [];
  function taskKey(s){ return (s.project||'') + '|' + s.session_id; }
  function chipText(a){
    const seg = [];
    if (a.files) seg.push('edited ' + a.files + ' file' + (a.files>1?'s':'')
      + ((a.plus||a.minus) ? ' (+'+(a.plus||0)+' \\u2212'+(a.minus||0)+')' : ''));
    if (a.commands) seg.push('ran ' + a.commands + ' command' + (a.commands>1?'s':''));
    if (a.notes) seg.push(a.notes + ' note' + (a.notes>1?'s':''));
    if (a.failures) seg.push(a.failures + ' failure' + (a.failures>1?'s':''));
    if (a.loops) seg.push(a.loops + ' loop iteration' + (a.loops>1?'s':''));
    return seg.join(' \\u00b7 ') || 'no tracked activity';
  }
  function rowLabel(r){
    if (r.kind === 'touched'){
      const base = r.text.split(/[\\\\/]/).pop();
      const stats = (r.plus!=null)
        ? ' <span class="tplus">+'+r.plus+'</span> <span class="tminus">\\u2212'+r.minus+'</span>' : '';
      return 'touched <span class="tpath" title="'+escAttr(r.text)+'">'+esc(base)+'</span>'+stats;
    }
    if (r.kind === 'ran') return 'ran '+esc(r.desc || r.text);
    if (r.kind === 'failed') return '<span class="tbad">[failed]</span> '+esc(r.text);
    if (r.kind === 'loop'){
      const glyph = r.breaker_state === 'OPEN' ? '\\u26a0 '
        : (r.progress ? '\\u2713 ' : '\\u25cb ');
      return glyph+'iter '+r.iteration+' \\u00b7 '+esc(r.text);
    }
    return '<span class="tnote">['+esc(r.kind)+']</span> '+esc(r.text);
  }
  function rowDetailHTML(r, det){
    if (r.kind === 'loop'){
      return '<table class="tdet">'
        + '<tr><td>progress</td><td>'+(r.progress ? 'advanced' : 'no progress')+'</td></tr>'
        + '<tr><td>breaker</td><td>'+esc(r.breaker_state||'\\u2014')+'</td></tr>'
        + '<tr><td>tokens</td><td>'+(r.total_tokens!=null ? r.total_tokens : '\\u2014')+'</td></tr>'
        + '<tr><td>cost</td><td>'+(r.cost_usd!=null ? '$'+r.cost_usd : '\\u2014')+'</td></tr>'
        + '<tr><td>context fill</td><td>'+(r.context_fill_ratio!=null
            ? Math.round(r.context_fill_ratio*100)+'%' : '\\u2014')+'</td></tr>'
        + '</table>';
    }
    if (r.kind === 'touched'){
      if (!det || !det.diff)
        return '<div class="lbl">'+esc(r.text)
          + ' \\u2014 line-level detail unavailable (transcript gone)</div>';
      return '<pre class="tdiff">'+det.diff.split('\\n').map(l =>
        '<span class="'+(l[0]==='+'?'dplus':l[0]==='-'?'dminus':'dctx')+'">'
        +esc(l)+'</span>').join('\\n')+'</pre>';
    }
    if (r.kind === 'ran'){
      let h = '<pre class="tdiff dctx">$ '+esc((det&&det.command)||r.text)+'</pre>';
      if (det && det.output) h += '<pre class="tdiff">'+esc(det.output)+'</pre>';
      else h += '<div class="lbl">output unavailable (transcript gone)</div>';
      return h;
    }
    return '<div class="snip">'+esc(r.text)+'</div>';
  }
  function matchDetails(rows, detItems){
    // zip L2 rows to transcript details in order, keyed by kind+text
    const q = {};
    (detItems||[]).forEach(d => {
      const k = d.kind+'|'+d.text; (q[k]=q[k]||[]).push(d); });
    return rows.map(r => {
      const k = r.kind+'|'+r.text;
      return (q[k]&&q[k].length) ? q[k].shift() : null; });
  }
  async function loadTaskItems(key, s){
    const extra = (PROJECT === '*')
      ? '&session_project='+encodeURIComponent(s.project) : '';
    TASK_ITEMS[key] = await apiGet(
      '/api/task?session='+encodeURIComponent(s.session_id)+extra);
  }
  async function toggleTaskCard(key, s){
    if (TASK_OPEN.has(key)) TASK_OPEN.delete(key);
    else {
      TASK_OPEN.add(key);
      if (!TASK_ITEMS[key]) await loadTaskItems(key, s);
    }
    paintTaskCards();
  }
  async function toggleTaskRow(key, idx, s){
    const rk = key+'#'+idx;
    if (TASK_ROW_OPEN.has(rk)){ TASK_ROW_OPEN.delete(rk); paintTaskCards(); return; }
    TASK_ROW_OPEN.add(rk);
    const data = TASK_ITEMS[key];
    const row = data && data.items ? data.items[idx] : null;
    if (row && (row.kind==='touched'||row.kind==='ran')
        && TASK_DETAIL[key] === undefined){
      TASK_DETAIL[key] = null; paintTaskCards();
      try {
        TASK_DETAIL[key] = await apiGet(
          '/api/task_detail?session='+encodeURIComponent(s.session_id));
      } catch(_){ TASK_DETAIL[key] = {error:'fetch_failed', items:[]}; }
    }
    paintTaskCards();
  }
  async function renderTaskCards(){
    const rows = await apiGet('/api/sessions');
    // The cache write is INSIDE the guard, not before it. paintTaskCards
    // already refuses to paint for a section the user left, but the assignment
    // ran first — so a discarded poll still replaced TASK_SESSIONS, and every
    // later repaint that does not refetch (toggleTaskCard, toggleTaskRow) drew
    // from it. Discarding a render means writing nothing, caches included.
    if (!shouldPaint(liveFn, renderSessions)) return;
    TASK_SESSIONS = rows;
    for (const s of TASK_SESSIONS){
      const key = taskKey(s);
      if (TASK_OPEN.has(key) && (s.status === 'ongoing' || !TASK_ITEMS[key]))
        await loadTaskItems(key, s);   // ongoing cards refresh on each tick
    }
    paintTaskCards();
  }
  function cardHTML(s){
    const key = taskKey(s);
    const open = TASK_OPEN.has(key);
    const badge = s.status === 'ongoing'
      ? '<span class="tstat ongoing">\\u25cf ongoing</span>'
      : '<span class="tstat done">\\u2713 completed</span>';
    let body = '';
    if (open){
      const data = TASK_ITEMS[key];
      if (!data) body = '<div class="lbl">loading\\u2026</div>';
      else {
        const dets = matchDetails(data.items,
          TASK_DETAIL[key] && TASK_DETAIL[key].items);
        body = (data.truncated
            ? '<div class="lbl">'+data.truncated+' older entr'
              +(data.truncated>1?'ies':'y')
              +' beyond the last 100 \\u2014 the full history stays in the ledger</div>'
            : '')
          + '<ul class="trows">' + data.items.map((r,i) => {
              const rk = key+'#'+i, ropen = TASK_ROW_OPEN.has(rk);
              return '<li class="trow" data-k="'+escAttr(key)+'" data-i="'+i+'">'
                + '<div class="tlab">'+(ropen?'\\u25be':'\\u203a')+' '+rowLabel(r)+'</div>'
                + (ropen ? '<div class="tbody">'+rowDetailHTML(r, dets[i])+'</div>' : '')
                + '</li>';
            }).join('') + '</ul>'
          + '<div class="row"><a href="#" class="tmem" data-sid="'
          + escAttr(s.session_id)+'">open in Memory \\u2192</a></div>';
      }
    }
    return '<div class="tcard'+(open?' open':'')+'" data-k="'+escAttr(key)+'">'
      + '<div class="thead">'+badge
      + '<span class="tsid">'+(PROJECT==='*'&&s.project ? esc(s.project)+' \\u00b7 ' : '')
      + esc(s.session_id)+'</span>'
      + srPill(s.origin)
      + '<span class="tchip">'+chipText(s.activity||{})+'</span>'
      + '<span class="tspan">'+esc(fmtTs(s.first_ts||''))+' \\u2192 '+esc(fmtTs(s.last_ts||''))+'</span>'
      + '</div>' + body + '</div>';
  }
  function paintTaskCards(){
    // Cross-section discard, applied HERE because this is the paint site.
    // renderTaskCards awaits /api/sessions and calls us on the FAR SIDE of that
    // await, so the same guard placed in renderSessions (dashboard_page.py)
    // would run before the await and catch nothing — it would read as
    // protection while providing none. Measured before this line existed:
    // renderSessions and renderTaskCards were the only renderers still able to
    // paint over a section the user had already left.
    // The three callers are all post-await (renderTaskCards, toggleTaskCard,
    // toggleTaskRow), so one check here covers every path into showHTML.
    // This couples the fragment to its host on purpose: CARDS_JS is inlined by
    // exactly one page (dashboard.py -> render_page(tasks_js=...)), which is
    // where liveFn, shouldPaint and renderSessions live. A future second
    // consumer without them gets a loud ReferenceError here rather than a
    // silently unguarded paint.
    if (!shouldPaint(liveFn, renderSessions)) return;
    if (!TASK_SESSIONS.length){ showText('No sessions yet.'); return; }
    showHTML('<h2 class="h">'+TASK_SESSIONS.length+' task'
      +(TASK_SESSIONS.length>1?'s':'')+'</h2>'
      + '<div class="legend">Append-only, auditable history \\u2014 entries cannot'
      + ' be deleted. Expanded cards show the last 100 actions.</div>'
      + TASK_SESSIONS.map(cardHTML).join(''));
    view.querySelectorAll('.tcard .thead').forEach(el => {
      const key = el.parentElement.dataset.k;
      const s = TASK_SESSIONS.find(x => taskKey(x) === key);
      el.onclick = () => toggleTaskCard(key, s);
    });
    view.querySelectorAll('.trow .tlab').forEach(el => {
      const li = el.parentElement, key = li.dataset.k, idx = +li.dataset.i;
      const s = TASK_SESSIONS.find(x => taskKey(x) === key);
      el.onclick = (ev) => { ev.stopPropagation(); toggleTaskRow(key, idx, s); };
    });
    view.querySelectorAll('.tmem').forEach(el => el.onclick = (ev) => {
      ev.preventDefault(); openSession(el.dataset.sid);
    });
  }
"""
