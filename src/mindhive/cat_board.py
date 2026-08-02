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
  #mn-cats .lbl { color:#8b949e; font-size:10.5px; letter-spacing:.6px;
    text-transform:uppercase; flex:0 0 auto; align-self:center; }
  #mn-cats .none { color:#6e7681; font-size:11.5px; align-self:center; }
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
  }
  #mn-cat-panel { position:fixed; z-index:60; display:none; min-width:210px;
    background:#161b22; border:1px solid #30363d; border-radius:8px; color:#e6e8ef;
    padding:8px 10px; font-size:11.5px; box-shadow:0 6px 22px #0009; }
  #mn-cat-panel h3 { margin:0 0 5px; font-size:12px; color:#e8b64c; }
  #mn-cat-panel .kv { display:flex; gap:6px; }
  #mn-cat-panel .kv span:first-child { color:#8b949e; min-width:58px; }
  #mn-cat-panel .warn { margin-top:5px; color:#ff9e64; }
"""

# The strip's own markup. The initial "no activity" text is what the page shows
# before the first poll answers; `paintCats` replaces the whole strip after that.
HTML = ('<div id="mn-cats"><span class="lbl">faculties</span>'
        '<span class="none">no MindNetwork activity</span></div>'
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
  function catPanel(cat, el){
    const p = document.getElementById('mn-cat-panel');
    if (!p) return;
    // textContent throughout: task_id and phase are ledger-supplied strings and
    // this is the one place they reach the DOM.
    p.innerHTML = '';
    const h = document.createElement('h3');
    h.textContent = CAT_LABEL[cat.faculty] || cat.faculty;
    p.appendChild(h);
    const rows = [['task', cat.task_id || '\\u2014'], ['phase', cat.phase || '\\u2014'],
                  ['skill', cat.skill || '\\u2014'], ['state', cat.status],
                  ['last seen', catAge(cat.age_secs) + ' ago']];
    for (const [k, v] of rows){
      const d = document.createElement('div'); d.className = 'kv';
      const a = document.createElement('span'); a.textContent = k;
      const b = document.createElement('span'); b.textContent = v;
      d.appendChild(a); d.appendChild(b); p.appendChild(d);
    }
    // The reason this board exists: awake but not moving is a stuck parallel run,
    // and the user asked to be able to see that at a glance.
    if (cat.status === 'active' && cat.age_secs >= 900){
      const w = document.createElement('div'); w.className = 'warn';
      w.textContent = 'active with no update for ' + catAge(cat.age_secs)
        + ' \\u2014 possible silent hang';
      p.appendChild(w);
    }
    // Show it first, then measure: a display:none panel has no width, so clamping
    // against its own size has to happen after it is laid out.
    p.style.display = 'block';
    const r = el.getBoundingClientRect();
    p.style.top = Math.round(r.bottom + 6) + 'px';
    // Clamp to the viewport. Left-aligning to the cat is fine on the swarm page,
    // where the strip starts at the left edge — but the dashboard puts the strip at
    // the END of its header, so a cat near the right edge pushed the panel off
    // screen entirely. Found by opening the real page, not by a unit test: nothing
    // in a string-comparison test can see a rectangle leave the window.
    const w = p.getBoundingClientRect().width || 210;
    const max = Math.max(4, document.documentElement.clientWidth - w - 8);
    p.style.left = Math.round(Math.min(Math.max(4, r.left), max)) + 'px';
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
      if (panel) panel.style.display = 'none';
      return;
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
        bar.querySelectorAll('.mn-cat.sel').forEach(x => x.classList.remove('sel'));
        if (already){ if (panel) panel.style.display = 'none'; }
        else { el.classList.add('sel'); catPanel(cat, el); }
      };
      // Re-open the panel the poll just replaced, so a refresh does not close a
      // panel the user is reading.
      if (cat.faculty === openFaculty){ el.classList.add('sel'); }
      bar.appendChild(el);
      if (cat.faculty === openFaculty){ catPanel(cat, el); }
    }
  }
  document.addEventListener('click', () => {
    const p = document.getElementById('mn-cat-panel');
    if (p) p.style.display = 'none';
    document.querySelectorAll('#mn-cats .mn-cat.sel')
      .forEach(x => x.classList.remove('sel'));
  });
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
