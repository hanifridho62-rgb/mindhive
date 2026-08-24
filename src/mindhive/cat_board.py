# src/mindhive/cat_board.py
"""MN-13 — the faculty cat board as ONE fragment, usable on any page.

The founding ask was for a cat on "setiap section yang berjalan pada mindNetwork",
so an operator can "melacak apa saja yang menyala dan apa saja yang tidak menyala".
The board shipped on the MindSwarm page only — which is not the page the operator
actually lives on, so the signal was absent exactly where it was wanted.

The obvious fix — paste the strip into `dashboard_page.py` too — would put a second
copy of `CAT_GRID`, `CAT_PALETTES`, `CAT_LABEL`, `catSVG`, the panel and the click
handler into the tree. That is what invariant 10 forbids, and the drift would be
invisible: one page would keep six palettes, or the old age-only vanish rule, and
nothing would say which page was lying. So this module owns the fragment and both
pages interpolate it.

Two things deliberately stay OUT of here:

- `#mn-cats`'s own box. The swarm page wants a full-width strip under its header;
  the dashboard wants an inline cluster in its header. Only the cat and panel rules
  are page-independent, so only those live here — each page positions its own
  container. A shared rule with two page-specific overrides would be the same
  duplication wearing a hat.
- WHEN a cat renders. `ui_cats.board` decides that server-side and ships its
  thresholds in the payload; this fragment renders what arrives, so it cannot
  disagree with the tests that pin the bands.
"""
from __future__ import annotations

import json

# Pixel-cat grid, drawn as an inline SVG rect grid — no image files, so a page stays
# offline-self-contained. Grid chars: b body, p patch, e eye, n nose, . empty.
CAT_GRID = [
    ".b........b.",
    ".bb......bb.",
    ".bpb....bpb.",
    ".bbbbbbbbbb.",
    "bbbbbbbbbbbb",
    "bbeebbbbeebb",
    "bbbbbnnbbbbb",
    ".bbbbbbbbbb.",
    ".bpbbbbbbpb.",
    ".bbbbbbbbbb.",
    ".bbbbbbbbbb.",
    "..bb.bb.bb..",
]

# Seven, not six: `telemetry.FACULTIES` has seven members and the board gives each
# one its own cat, so a six-entry table would make two faculties render identically
# — the exact thing the board exists to distinguish. Every modulo in the JS reads
# this table's length rather than a literal, so an eighth palette cannot leave a
# hardcoded 7 behind.
CAT_PALETTES = [
    {"name": "white", "b": "#f2f2f0", "p": "#e3e3df", "e": "#4aa3ff"},
    {"name": "calico", "b": "#f2f2f0", "p": "#e8863a", "e": "#3fb950"},
    {"name": "orange", "b": "#e8863a", "p": "#c9641f", "e": "#3fb950"},
    {"name": "tabby", "b": "#9aa2ad", "p": "#6b7280", "e": "#e8b64c"},
    {"name": "siamese", "b": "#e8dcc8", "p": "#5b4636", "e": "#58a6ff"},
    {"name": "black", "b": "#262a33", "p": "#16181d", "e": "#e8b64c"},
    {"name": "cream", "b": "#f0d9a8", "p": "#d8b878", "e": "#3fb950"},
]

# Faculty -> the words printed above the cat's head.
CAT_LABEL = {
    "skill-plugin": "Skills", "sop": "SOPs", "improvements": "Improvements",
    "history": "History", "task-graph": "Tasks",
    "compatibility": "Compatibility", "pr-green": "PR green",
}

# Only the page-independent rules. `#mn-cats` itself is each page's business.
CSS = """
  /* Scoped to BOTH strips. These were `#mn-cats`-only, so a descendant selector
     could not reach the session strip and its empty states rendered in default
     body type — which silently cost the whole quiet-vs-blind distinction its
     visible effect, the one thing that distinction exists to have. */
  #mn-cats .lbl, #mn-sessions .lbl { color:#8b949e; font-size:10.5px;
    letter-spacing:.6px; text-transform:uppercase; flex:0 0 auto;
    align-self:center; }
  #mn-cats .none, #mn-sessions .none { color:#6e7681; font-size:11.5px;
    align-self:center; }
  #mn-sessions .more { color:#6e7681; font-size:11px; align-self:center; }
  /* Amber, not grey. "I cannot observe this" is a different claim from "nothing
     is happening", and if the two render the same the operator learns nothing —
     which is exactly how a checkout with no producer read as a quiet one. */
  #mn-cats .blind, #mn-sessions .blind { color:#ff9e64; font-size:11.5px;
    align-self:center; }
  /* The label sits ABOVE the head, as originally asked ("kucing yang diatas
     kepala nya bertulisan Skill and plugin") — column-reverse puts the SVG below
     the text without reordering the DOM, so the label still reads first for a
     screen reader. No width/height on the svg: the size is an attribute set by
     catSVG, because the strip's cats and the swarm's node cats want different
     sizes from the same function. */
  .mn-cat { position:relative; display:flex; flex-direction:column-reverse;
    align-items:center; gap:1px; border:1px solid #30363d; border-radius:7px;
    padding:3px 7px; background:#161b22; cursor:pointer; flex:0 0 auto;
    font-size:10px; line-height:1.15; }
  .mn-cat .cap { white-space:nowrap; letter-spacing:.2px; }
  .mn-cat:hover { border-color:#58a6ff; }
  .mn-cat svg { display:block; }
  .mn-cat.active { color:#7ee787; border-color:#2ea043; }
  .mn-cat.active svg { animation:mn-bob 1.6s ease-in-out infinite; }
  .mn-cat.dim { color:#8b949e; opacity:.55; }
  /* grayscale, not just opacity. The cats used to be a currentColor silhouette,
     so `.mn-cat.dim`'s grey text colour greyed the sprite for free. They are
     per-faculty coloured now, and without an explicit filter a dim cat would
     still look fully alive — losing the dim band, which IS the hang signal this
     board exists to show. */
  .mn-cat.dim svg { animation:none; filter:grayscale(100%); }
  .mn-cat.sel { border-color:#e8b64c; }
  /* The session cat answers the same four questions on click, so it carries
     the same affordance and the same selected border. */
  .mn-sess { cursor:pointer; }
  .mn-sess.sel { border-color:#e8b64c; }
  @keyframes mn-bob { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-2px)} }
  /* Motion is a signal on this board, not decoration — but it is not a signal
     anybody asked to be shaken by. Keyed on `mn-cat-sprite`, which `catSVG`
     stamps on EVERY cat it draws, so one rule covers the faculty strip and the
     swarm page's node chips today and a third page's cats the day it adopts the
     fragment. Named-class, not `.mn-cat svg`: the chip cats are deliberately
     NOT inside a `.mn-cat` box (that box is the strip's furniture), so a
     descendant selector would leave exactly the cats this guard exists for
     still moving. `!important` because the rule that turns each animation ON is
     more specific than this one and a media query adds no specificity itself. */
  @media (prefers-reduced-motion: reduce) {
    .mn-cat-sprite { animation:none !important; }
    /* The entrance rides the CONTAINER, not the sprite, so the sprite-keyed rule
       above does not reach it. Without this line a reduced-motion viewer would
       still get every session cat sliding in. */
    .mn-sess { animation:none !important; }
  }
  /* --- MN-19: session cats -------------------------------------------------
     A different question ("which Claude is working") deserves a different shape,
     or the two strips read as one list and the faculty cats lose their meaning.
     Faculty cats are labelled boxes; these are pills. */
  .mn-sess { display:flex; align-items:center; gap:5px; flex:0 0 auto;
    border:1px solid #30363d; border-radius:999px; padding:2px 9px 2px 4px;
    background:#161b22; font-size:10.5px; line-height:1.15;
    /* Entrance on transform+opacity ONLY — animating layout properties janks —
       and 240ms with a decelerating curve, the band for a small transition. A
       cat that pops into the strip reads as a glitch; one that rises into it
       reads as arriving, which is the orientation cue that matters when a
       session appears mid-poll. */
    animation:mn-sess-in 240ms cubic-bezier(0.16, 1, 0.3, 1) both; }
  @keyframes mn-sess-in { from { opacity:0; transform:translateY(4px) }
                          to { opacity:1; transform:none } }
  .mn-sess .cap { white-space:nowrap; max-width:150px; overflow:hidden;
    text-overflow:ellipsis; }
  /* "Working" is NEVER encoded in motion alone: the colour and the caption say
     it too, so the state survives `prefers-reduced-motion` fully intact rather
     than becoming an unreadable row of identical pills. */
  .mn-sess.working { color:#7ee787; border-color:#2ea043; }
  .mn-sess.working .mn-cat-sprite { animation:mn-bob 1.6s ease-in-out infinite; }
  .mn-sess.resting { color:#8b949e; }
  .mn-sess.resting .mn-cat-sprite { filter:grayscale(100%); opacity:.55; }
  .mn-sess .dotk { width:5px; height:5px; border-radius:50%; flex:0 0 auto;
    background:currentColor; }
  #mn-cat-panel { position:fixed; z-index:60; display:none; min-width:210px;
    background:#161b22; border:1px solid #30363d; border-radius:8px; color:#e6e8ef;
    padding:8px 10px; font-size:11.5px; box-shadow:0 6px 22px #0009; }
  #mn-cat-panel h3 { margin:0 0 5px; font-size:12px; color:#e8b64c; }
  #mn-cat-panel .kv { display:flex; gap:6px; }
  #mn-cat-panel .kv span:first-child { color:#8b949e; min-width:58px; }
  #mn-cat-panel .warn { margin-top:5px; color:#ff9e64; }
  /* The injection row (T5/T6). One cat per TURN, not per injector: seven cats
     times twelve turns is eighty-four sprites and no signal. The turn's cat
     carries the turn's worst state, and the panel breaks it down. */
  #mn-inject { display:flex; flex-wrap:wrap; gap:6px; align-items:center;
    margin:6px 0 10px; }
  #mn-inject .lbl { color:#8b949e; font-size:11px; margin-right:2px; }
  #mn-inject .none { color:#8b949e; font-size:11px; }
  .mn-inj { display:inline-flex; align-items:center; gap:4px; padding:2px 6px;
    border:1px solid #30363d; border-radius:8px; cursor:pointer; font-size:11px;
    color:#e6e8ef; }
  .mn-inj .n { color:#8b949e; }
  .mn-inj.sel { border-color:#e8b64c; }
  /* Three states, three DISTINCT paints. Absence cannot express failure --
     `.blind` above exists because "nothing is happening" and "I cannot know"
     once rendered identically, and this row must not repeat that. */
  .mn-inj.verbatim { border-color:#3fb950; }
  .mn-inj.partial { border-color:#ff9e64; }
  .mn-inj.silent { filter:grayscale(100%); opacity:.5; }
  .mn-inj.verbatim .mn-cat-sprite { animation:mn-bob 1.6s ease-in-out infinite; }
  #mn-cat-panel .mn-body { margin:6px 0 0; max-height:280px; overflow:auto;
    white-space:pre-wrap; word-break:break-word; font-size:11px; color:#c9d1d9;
    background:#0d1117; border:1px solid #30363d; border-radius:6px; padding:6px; }
"""

# The strip's own markup. The initial "no activity" text is what the page shows
# before the first poll answers; `paintCats` replaces the whole strip after that.
HTML = ('<div id="mn-cats"><span class="lbl">faculties</span>'
        '<span class="none">no MindNetwork activity</span></div>'
        '<div id="mn-sessions"><span class="lbl">working</span>'
        '<span class="none">no Claude running</span></div>'
        '<div id="mn-cat-panel"></div>')

# Nothing here recomputes a threshold: the 6-hour rule removes a cat from the
# payload entirely and the dim rule arrives as display:"dim", so this loop cannot
# drift from the tests that pin those numbers. `__CAT_*__` / `__FACULTY_ORDER__`
# are filled by `script()`.
_JS = """
  const CAT_GRID = __CAT_GRID__;
  const CAT_PALETTES = __CAT_PALETTES__;
  const CAT_LABEL = __CAT_LABEL__;
  // The canonical faculty order, injected from `telemetry.FACULTIES` — the one
  // legend. A faculty's cat is its INDEX in that tuple, so the mapping is stable
  // across polls and a faculty added to the legend gets its own palette without
  // anybody editing a second list here. A hand-written faculty->colour table would
  // be exactly that second list, and it would drift.
  const FACULTY_ORDER = __FACULTY_ORDER__;
  function facultyCat(faculty){
    const i = FACULTY_ORDER.indexOf(faculty);
    // `ui_cats.board` iterates FACULTIES and drops anything absent, so an unknown
    // faculty cannot reach the payload. Answer 0 rather than -1 anyway: a negative
    // index would pick the wrong palette silently.
    return i < 0 ? 0 : i;
  }
  function catSVG(idx, size){
    const c = CAT_PALETTES[((idx % CAT_PALETTES.length) + CAT_PALETTES.length)
                           % CAT_PALETTES.length];
    const fill = {b:c.b, p:c.p, e:c.e, n:'#ff8fb3'};
    let rects = '';
    CAT_GRID.forEach((row, y) => { for (let x = 0; x < row.length; x++) {
      const col = fill[row[x]];
      if (col) rects += '<rect x="'+x+'" y="'+y+'" width="1" height="1" fill="'+col+'"/>';
    }});
    // `mn-cat-sprite` is the one hook every cat carries, wherever it is drawn:
    // the swarm page's strip, the dashboard's strip, and the swarm page's node
    // chips. The reduced-motion guard in CSS keys on it, so a page that adopts
    // this fragment inherits the guard without having to know it exists.
    return '<svg class="mn-cat-sprite" viewBox="0 0 12 12" width="'+size
      + '" height="'+size+'" shape-rendering="crispEdges">'+rects+'</svg>';
  }
  function catAge(s){
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s/60) + 'm';
    return (s/3600).toFixed(1) + 'h';
  }
  // ONE panel renderer for both strips. The faculty cat and the session cat
  // answer the same four questions -- what task, what phase, which skill,
  // active or not -- off different producers, and two copies of this would be
  // two places for those answers to start being formatted differently.
  const CAT_DASH = '\\u2014';
  const CAT_DOT = ' \\u00b7 ';
  function mnPanel(heading, rows, el, warning, body){
    const p = document.getElementById('mn-cat-panel');
    if (!p) return;
    // textContent throughout: every value here is ledger-supplied prose and
    // this is the one place it reaches the DOM.
    p.innerHTML = '';
    const h = document.createElement('h3');
    h.textContent = heading;
    p.appendChild(h);
    for (const [k, v] of rows){
      const d = document.createElement('div'); d.className = 'kv';
      const a = document.createElement('span'); a.textContent = k;
      const b = document.createElement('span'); b.textContent = v || CAT_DASH;
      d.appendChild(a); d.appendChild(b); p.appendChild(d);
    }
    if (warning){
      const w = document.createElement('div'); w.className = 'warn';
      w.textContent = warning;
      p.appendChild(w);
    }
    // The replayed block is arbitrary ledger text -- prompts, SOP bodies,
    // whatever a pass composed. textContent, never innerHTML: an innerHTML path
    // here would make every injected block a script vector in the operator's
    // own dashboard.
    if (body){
      const b = document.createElement('pre'); b.className = 'mn-body';
      b.textContent = body;
      p.appendChild(b);
    }
    // Show it first, then measure: a display:none panel has no width, so
    // clamping against its own size has to happen after it is laid out.
    p.style.display = 'block';
    const r = el.getBoundingClientRect();
    p.style.top = Math.round(r.bottom + 6) + 'px';
    // Clamp to the viewport. Left-aligning to the cat is fine on the swarm
    // page, where the strip starts at the left edge -- but the dashboard puts
    // its strips at the END of the header, so a cat near the right edge pushed
    // the panel off screen entirely. Found by opening the real page, not by a
    // unit test: nothing in a string comparison can see a rectangle leave the
    // window.
    const w = p.getBoundingClientRect().width || 210;
    const max = Math.max(4, document.documentElement.clientWidth - w - 8);
    p.style.left = Math.round(Math.min(Math.max(4, r.left), max)) + 'px';
  }
  function catPanel(cat, el){
    mnPanel(CAT_LABEL[cat.faculty] || cat.faculty,
            [['task', cat.task_id], ['phase', cat.phase],
             ['skill', cat.skill], ['state', cat.status],
             ['last seen', catAge(cat.age_secs) + ' ago']], el,
            // The reason this board exists: awake but not moving is a stuck
            // parallel run, and the user asked to see that at a glance.
            (cat.status === 'active' && cat.age_secs >= 900)
              ? 'active with no update for ' + catAge(cat.age_secs)
                + ' \\u2014 possible silent hang' : '');
  }
  // The session cat's four answers. Same questions, live producers: `task` is
  // folded from state_set, `phase` is the roadmap plan's active step, `skill`
  // is the tail of the session's own skill trail, and `display` is oversee's
  // liveness verdict -- never a second opinion formed here.
  function sessPanel(s, el){
    mnPanel(s.title || ((s.kind || 'chat') + ' session'),
            [['task', s.task], ['phase', s.phase], ['skill', s.skill],
             ['state', s.display === 'working' ? 'working' : 'quiet'],
             ['doing', s.doing],
             ['last seen', catAge(s.age_secs) + ' ago']], el, '');
  }
  // Which strip the shared panel currently belongs to. Both strips repaint
  // from the SAME poll, so without an owner each one's housekeeping reaches
  // into the other's open panel: the faculty strip's empty-board branch was
  // closing session panels one poll after every click, and an empty faculty
  // board is the common case, not the rare one.
  let PANEL_OWNER = null;              // 'cat' | 'sess' | 'inj' | null
  function mnOpen(owner, el, render){
    // One entry point, so 'exactly one cat is selected anywhere' holds by
    // construction rather than by both handlers remembering to sweep both
    // strips. The faculty handler did not, and two cats read as selected.
    document.querySelectorAll('.mn-cat.sel, .mn-sess.sel, .mn-inj.sel')
      .forEach(x => x.classList.remove('sel'));
    el.classList.add('sel');
    PANEL_OWNER = owner;
    render();
  }
  function mnClosePanel(owner){
    // A strip may only close what it owns. `null` is the outside click,
    // which owns everything.
    if (owner && PANEL_OWNER !== owner) return;
    const p = document.getElementById('mn-cat-panel');
    if (p) p.style.display = 'none';
    PANEL_OWNER = null;
    document.querySelectorAll('.mn-cat.sel, .mn-sess.sel, .mn-inj.sel')
      .forEach(x => x.classList.remove('sel'));
  }
  function paintCats(cats){
    const bar = document.getElementById('mn-cats');
    // A page may carry the script without the strip; painting nothing is the
    // right answer there, not a TypeError that takes out the caller's poll.
    if (!bar) return;
    const open = bar.querySelector('.mn-cat.sel');
    const openFaculty = open ? open.dataset.faculty : null;
    bar.innerHTML = '';
    const lbl = document.createElement('span');
    lbl.className = 'lbl'; lbl.textContent = 'faculties';
    bar.appendChild(lbl);
    const panel = document.getElementById('mn-cat-panel');
    if (!cats.length){
      const n = document.createElement('span');
      n.className = 'none'; n.textContent = 'no MindNetwork activity';
      bar.appendChild(n);
      mnClosePanel('cat');
      return;
    }
    // A faculty that vanished from the board cannot keep describing itself.
    if (openFaculty && !cats.some(c => c.faculty === openFaculty)){
      mnClosePanel('cat');
    }
    for (const cat of cats){
      const el = document.createElement('span');
      el.className = 'mn-cat ' + cat.display;
      el.dataset.faculty = cat.faculty;
      el.innerHTML = catSVG(facultyCat(cat.faculty), 22);
      const t = document.createElement('span');
      t.className = 'cap';
      t.textContent = CAT_LABEL[cat.faculty] || cat.faculty;
      el.appendChild(t);
      el.title = (CAT_LABEL[cat.faculty] || cat.faculty)
        + ' \\u2014 ' + cat.status + ', last seen ' + catAge(cat.age_secs) + ' ago';
      el.onclick = (ev) => {
        ev.stopPropagation();
        const already = el.classList.contains('sel');
        if (already){ mnClosePanel('cat'); }
        else { mnOpen('cat', el, () => catPanel(cat, el)); }
      };
      // Re-open the panel the poll just replaced, so a refresh does not close a
      // panel the user is reading.
      if (cat.faculty === openFaculty){ el.classList.add('sel'); }
      bar.appendChild(el);
      if (cat.faculty === openFaculty){
        PANEL_OWNER = 'cat';
        catPanel(cat, el);
      }
    }
  }
  document.addEventListener('click', () => {
    mnClosePanel(null);               // the outside click owns every panel
  });

  // --- MN-19: the session strip -------------------------------------------
  // A cat per live Claude, from `oversee`'s existing liveness verdict. Nothing
  // here decides whether a session is working — the server already did, and a
  // second opinion in the page would let this strip and the Oversee section
  // disagree about the same session.

  // Spread the bob's phase across its own cycle, derived from the session id so
  // it is stable across repaints and different between sessions. Negative, so a
  // cat starts mid-bob instead of standing still for up to 1.6 s first. Without
  // this, N cats pulse in lockstep and the strip reads as one strobing bar
  // rather than N independent workers.
  function sessPhase(id){
    return -(idSeed(id) % 1600);
  }
  // One derivation of "a stable number from an id", shared by the palette pick
  // and the phase offset, and by the swarm page's node cats through `catFor`.
  // Arbitrary per new id, identical across refreshes — derived from the id, never
  // drawn at random, which would repaint a different cat on every poll.
  // Hashes the WHOLE id (djb2), not its first four characters as hex. The hex
  // slice was written for hex agent ids and silently collapses every other shape
  // to 0 — `ws-b474f44f`, `research-<id>`, any non-hex prefix — so those cats all
  // drew the same palette AND the same zero phase offset, bobbing in perfect
  // lockstep. That is precisely the strobing bar the desync exists to prevent,
  // and it hit hardest in the multi-agent case where several cats share a screen.
  function idSeed(id){
    let h = 5381;
    const s = String(id);
    for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
    return h;
  }
  function idCat(id){ return idSeed(id) % CAT_PALETTES.length; }
  // What was last drawn, per strip. `paintSessions` rebuilds its container, and
  // rebuilding on every 1.2 s poll restarts a 1.6 s bob before it can finish —
  // the same defect fixed for the swarm chips. `age_secs` is deliberately NOT in
  // the signature: it changes every poll and would defeat the guard entirely, so
  // the hover tooltip may lag a poll behind while the animation stays whole.
  // That trade is the right way round — the motion IS the feature.
  let SESS_SIG = null;
  function paintSessions(board){
    const bar = document.getElementById('mn-sessions');
    if (!bar) return;                 // a page may carry the script, not the strip
    const rows = (board && board.sessions) || [];
    const producers = (board && board.producers) || {};
    const hidden = (board && board.sessions_hidden) || 0;
    // task/phase/skill join the signature: they are what an open panel is
    // showing, and a strip that refuses to repaint when they change would
    // leave the reader looking at a stale phase.
    const sig = JSON.stringify([rows.map(s => [s.session_id, s.display, s.kind,
                                               s.title, s.doing, s.task,
                                               s.phase, s.skill]),
                                hidden, producers.session]);
    if (sig === SESS_SIG) return;
    // Which cat the user has open, so a repaint does not close a panel they
    // are reading -- the rule the faculty strip already keeps.
    const openSel = bar.querySelector('.mn-sess.sel');
    const openSid = openSel ? openSel.dataset.session : null;
    // A panel describing a session that has left the poll -- ended, timed
    // out, or dropped below the cap -- is worse than no panel: every field
    // in it is frozen at whatever the last payload said, and nothing on the
    // page says so. Closing covers the empty-rows branch below as well.
    if (openSid && !rows.some(r => r.session_id === openSid)){
      mnClosePanel('sess');
    }
    // Cleared, not set, before the build: assigning the new signature up front
    // meant a throw mid-build left a half-painted strip that every identical
    // poll afterwards refused to repair. Set it only once the strip is whole.
    SESS_SIG = null;
    bar.innerHTML = '';
    const lbl = document.createElement('span');
    lbl.className = 'lbl'; lbl.textContent = 'working';
    bar.appendChild(lbl);
    if (!rows.length){
      const n = document.createElement('span');
      // The distinction that cost this repo an entire investigation. A build
      // whose branch predates the producer rendered EXACTLY like a quiet one, so
      // "nothing is running" and "I cannot know" were indistinguishable.
      // `!== true`, not `=== false`. A payload with no `producers` key at all —
      // an older server behind a long-lived tab, or a 500 that `apiGet` does not
      // throw on — leaves this `undefined`, and `undefined === false` is false,
      // so the confident "no Claude running" was rendered at the one moment the
      // page genuinely could not know. Absence of the answer IS not-knowing.
      if (producers.session !== true){
        n.className = 'blind';
        n.textContent = 'session signal not built into this dashboard';
      } else {
        n.className = 'none';
        n.textContent = 'no Claude running';
      }
      bar.appendChild(n);
      SESS_SIG = sig;
      return;
    }
    for (const s of rows){
      const el = document.createElement('span');
      el.className = 'mn-sess ' + s.display;
      el.innerHTML = catSVG(idCat(s.session_id), 18);
      const sprite = el.querySelector('.mn-cat-sprite');
      if (sprite) sprite.style.animationDelay = sessPhase(s.session_id) + 'ms';
      const dot = document.createElement('span');
      dot.className = 'dotk';
      el.appendChild(dot);
      const t = document.createElement('span');
      t.className = 'cap';
      // textContent: `title` and `doing` are ledger-supplied prose and this is
      // the one place they reach the DOM.
      t.textContent = s.doing || s.title || (s.kind + ' session');
      el.appendChild(t);
      el.title = (s.display === 'working' ? 'working' : 'quiet')
        + ' \\u2014 ' + (s.kind || 'chat') + ' \\u2014 last seen '
        + catAge(s.age_secs) + ' ago \\u2014 click for detail';
      el.dataset.session = s.session_id;
      el.onclick = (ev) => {
        ev.stopPropagation();
        const already = el.classList.contains('sel');
        if (already){ mnClosePanel('sess'); }
        else { mnOpen('sess', el, () => sessPanel(s, el)); }
      };
      if (s.session_id === openSid){ el.classList.add('sel'); }
      bar.appendChild(el);
      if (s.session_id === openSid){
        PANEL_OWNER = 'sess';          // the repaint keeps the ownership
        sessPanel(s, el);
      }
    }
    if (hidden > 0){
      const more = document.createElement('span');
      more.className = 'more';
      more.textContent = '+' + hidden + ' more';
      bar.appendChild(more);
    }
    SESS_SIG = sig;                  // the strip is whole; now remember it
  }
  // --- the injection row (T5/T6) ---------------------------------------------
  // One cat per TURN. Its class is the turn's worst state, so a degraded pass is
  // visible from the strip without opening anything; the panel names which one.
  function turnVerdict(t){
    const vs = (t.injectors || []).map(i => i.verdict);
    if (vs.indexOf('partial') >= 0) return 'partial';
    if (vs.indexOf('verbatim') >= 0) return 'verbatim';
    return 'silent';                 // every pass stayed quiet this turn
  }
  function injPanel(t, el, block){
    const rows = [];
    for (const i of (t.injectors || [])){
      rows.push([i.name, i.verdict + CAT_DOT + i.chars + ' chars'
                 + (i.reason ? CAT_DOT + i.reason : '')]);
    }
    rows.push(['total', t.total_chars + ' chars / ' + t.total_tokens + ' tokens']);
    if (t.repeats > 1) rows.push(['repeated', t.repeats + ' identical turns']);
    // The operator's question is what Claude READ, which is the composed order
    // and not any one pass's fragment -- so the whole turn rides the panel.
    mnPanel('injection ' + (t.ts || ''), rows, el,
            block ? '' : 'whole turn text is kept for the current turn only',
            block || '');
  }
  function paintInjections(data){
    const box = document.getElementById('mn-inject');
    if (!box) return;
    const turns = (data && data.recent) || [];
    const cur = (data && data.current) || {};
    // The repaint guard SESS_SIG carries, keyed on the ELEMENT rather than a
    // module variable. `showHTML` replaces the whole Oversee subtree, so a
    // module-level signature would suppress the repaint of a container that
    // had just been recreated EMPTY -- the row would vanish on the first
    // unchanged poll after any other Oversee repaint. A fresh element carries
    // no dataset, so it always paints; a surviving one is compared as before,
    // and a 2.5 s poll still cannot close a panel someone is reading nor
    // restart a 1.6 s animation mid-bob.
    const sig = JSON.stringify(turns.map(t => [t.signature, t.repeats]));
    if (box.dataset.sig === sig) return;
    if (PANEL_OWNER === 'inj') mnClosePanel('inj');
    box.innerHTML = '';
    const lbl = document.createElement('span');
    lbl.className = 'lbl'; lbl.textContent = 'injection';
    box.appendChild(lbl);
    if (!turns.length){
      const none = document.createElement('span');
      none.className = 'none';
      none.textContent = 'nothing injected yet';
      box.appendChild(none);
      box.dataset.sig = sig;
      return;
    }
    for (const t of turns){
      const spoke = (t.injectors || []).filter(i => i.verdict !== 'silent').length;
      const total = (t.injectors || []).length;
      const el = document.createElement('span');
      el.className = 'mn-inj ' + turnVerdict(t);
      el.innerHTML = catSVG(idCat(t.signature || ''), 18);
      const n = document.createElement('span');
      n.className = 'n';
      n.textContent = t.total_chars + 'c' + (t.repeats > 1 ? ' x' + t.repeats : '');
      el.appendChild(n);
      // Hover gives the title, the click gives the whole thing. Both were asked
      // for by name.
      el.title = 'injection ' + (t.ts || '') + CAT_DOT + turnVerdict(t)
        + CAT_DOT + spoke + ' of ' + total + ' spoke' + CAT_DOT
        + t.total_chars + ' chars';
      const block = (t.signature && t.signature === cur.signature)
        ? (cur.block || '') : '';
      el.addEventListener('click', ev => {
        ev.stopPropagation();
        const already = el.classList.contains('sel');
        if (already){ mnClosePanel('inj'); }
        else { mnOpen('inj', el, () => injPanel(t, el, block)); }
      });
      box.appendChild(el);
    }
    box.dataset.sig = sig;           // the row is whole; now remember it
  }
"""


def script() -> str:
    """The board's JavaScript with the legends interpolated.

    The faculty order comes from `telemetry.FACULTIES` at render time, so a faculty
    added there gets its own palette on every page with no second list to update.
    Imported lazily to keep a page render's "token in, HTML out" purity: drawing a
    dashboard for a project with no mindnet state must not pull the package in."""
    from .mindnet.telemetry import FACULTIES
    return (_JS
            .replace("__CAT_GRID__", json.dumps(CAT_GRID))
            .replace("__CAT_PALETTES__", json.dumps(CAT_PALETTES))
            .replace("__CAT_LABEL__", json.dumps(CAT_LABEL))
            .replace("__FACULTY_ORDER__", json.dumps(list(FACULTIES))))
