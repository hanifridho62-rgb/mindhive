/**
 * Runs the injection row's own JavaScript -- the exact bytes of
 * `cat_board.script()`, handed over on argv, never retyped here.
 *
 * `tests/test_injection_cats.py` asserts that three verdict classes exist in
 * the CSS and that their rule bodies differ. That is a check on the stylesheet;
 * it cannot see whether `paintInjections` ever REACHES those classes, whether a
 * click replays the right turn, or whether the block arrives as text. A DOM is
 * needed for those, so this file builds a small one and executes the decisions.
 *
 * Prints one JSON report; every verdict lives on the Python side, so a new case
 * is a Python edit rather than a change in two files.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [srcPath, casesPath] = process.argv.slice(2);
const source = readFileSync(srcPath, 'utf8');
const spec = JSON.parse(readFileSync(casesPath, 'utf8'));

// --- the DOM stub -----------------------------------------------------------
// Only what `paintInjections` -> `mnOpen` -> `injPanel` -> `mnPanel` touch.
// Children are RECORDED rather than dropped: what the row renders is the whole
// question here, and the stub `paint_guard.test.mjs` carries answers a
// different one (which render reaches the paint) so its appendChild is a no-op.

function makeWorld() {
  const all = [];
  const byId = new Map();

  function makeEl(tag) {
    const classes = new Set();
    const el = {
      tag,
      children: [],
      _html: '',
      _text: '',
      style: {},
      dataset: {},
      title: '',
      handlers: {},
      get className() { return [...classes].join(' '); },
      set className(v) {
        classes.clear();
        String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
      },
      classList: {
        add: (c) => classes.add(c),
        remove: (c) => classes.delete(c),
        contains: (c) => classes.has(c),
      },
      appendChild(c) { this.children.push(c); return c; },
      addEventListener(kind, fn) { (this.handlers[kind] ||= []).push(fn); },
      // A panel is positioned against a rectangle; any finite one will do.
      getBoundingClientRect: () => ({ top: 10, bottom: 30, left: 40, width: 210 }),
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = String(v); if (v === '') this.children = []; },
      get textContent() { return this._text; },
      set textContent(v) { this._text = String(v); },
    };
    all.push(el);
    return el;
  }

  const getElementById = (id) => {
    if (!byId.has(id)) { const e = makeEl('#' + id); e.id = id; byId.set(id, e); }
    return byId.get(id);
  };

  const document = {
    getElementById,
    createElement: makeEl,
    documentElement: { clientWidth: 1280 },
    addEventListener() {},
    // The one selector the board actually asks for. Matching it exactly, rather
    // than parsing CSS, keeps the stub honest about what it can answer.
    querySelectorAll(sel) {
      if (sel !== '.mn-cat.sel, .mn-sess.sel, .mn-inj.sel') {
        throw new Error('stub cannot answer selector: ' + sel);
      }
      return all.filter((e) => e.classList.contains('sel')
        && ['mn-cat', 'mn-sess', 'mn-inj'].some((c) => e.classList.contains(c)));
    },
  };
  return { document, getElementById, all, makeEl };
}

// --- reporting --------------------------------------------------------------

function describeCat(el) {
  const n = el.children.find((c) => c.classList.contains('n'));
  return {
    className: el.className,
    title: el.title,
    sprite: el._html,
    badge: n ? n.textContent : null,
    selected: el.classList.contains('sel'),
  };
}

function describePanel(p) {
  const pre = p.children.find((c) => c.tag === 'pre');
  return {
    display: p.style.display || '',
    heading: (p.children.find((c) => c.tag === 'h3') || {})._text || '',
    rows: p.children.filter((c) => c.classList.contains('kv'))
      .map((d) => d.children.map((s) => s._text)),
    warning: (p.children.find((c) => c.classList.contains('warn')) || {})._text || '',
    bodyText: pre ? pre._text : null,
    bodyHTML: pre ? pre._html : null,
    bodyClass: pre ? pre.className : null,
  };
}

// --- run --------------------------------------------------------------------

const out = { cases: {} };
try {
  for (const c of spec.cases) {
    const w = makeWorld();
    const ctx = vm.createContext({ document: w.document, window: {}, console });
    vm.runInContext(source + '\nglobalThis.__paint = paintInjections;',
                    ctx, { filename: 'cat_board.js' });

    const box = w.getElementById('mn-inject');
    const panel = w.getElementById('mn-cat-panel');

    for (const payload of c.paints) ctx.__paint(payload);

    // `clicks` names cat indexes to click, in order, so one case can express
    // "open A, then open B" and "click A twice".
    for (const idx of (c.clicks || [])) {
      const cat = box.children.filter((e) => e.classList.contains('mn-inj'))[idx];
      if (!cat) throw new Error('case ' + c.label + ': no cat at index ' + idx);
      for (const fn of (cat.handlers.click || [])) fn({ stopPropagation() {} });
    }

    const cats = box.children.filter((e) => e.classList.contains('mn-inj'));
    out.cases[c.label] = {
      sig: box.dataset.sig || '',
      label: (box.children.find((e) => e.classList.contains('lbl')) || {})._text || '',
      empty: (box.children.find((e) => e.classList.contains('none')) || {})._text || '',
      cats: cats.map(describeCat),
      selected: cats.filter((e) => e.classList.contains('sel')).length,
      panel: describePanel(panel),
    };
  }
} catch (e) {
  out.harness_error = String((e && e.stack) || e);
}
process.stdout.write(JSON.stringify(out));
