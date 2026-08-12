/*
 * tests/node/paint_guard.test.mjs — the paint-guard decision, EXECUTED.
 *
 * Driven by `tests/test_paint_guard_node.py` (marker `node`, deselected from
 * the default suite — see that file for why that fence is not cosmetic).
 *
 * WHY this exists at all, when `tests/test_dashboard.py` already asserts the
 * guard's source text. The plan's failure-modes table classifies this codepath
 * as SILENT: the idiom is applied to some renders and not others, and nothing
 * about the page's shape says which. A source-string assertion can only ask
 * "does the text `shouldPaint(liveFn, renderX)` appear near renderX" — it
 * cannot ask the two questions that decide whether the bug is actually fixed:
 *
 *   1. Is the comparison re-read AFTER the fetch resolves? A guard evaluated
 *      before the await reads identically in the source and catches nothing,
 *      because at that instant liveFn IS the renderer that is starting.
 *   2. Does a discarded render really write nothing — not the panel, not its
 *      module cache, and not an error through run()'s catch?
 *
 * Both are questions about ordering and effects, so both need the code to run.
 * The scenario driven here is the one the user reported, verbatim: a poll of
 * the section they were on is still in flight, they click another section, and
 * the older response lands last.
 *
 * WHY no jsdom / no test runner. `pyproject.toml` keeps `dependencies = []`,
 * and that is a property worth more than the convenience: this repo ships as a
 * single PyInstaller binary. So the page runs against a hand-rolled DOM stub in
 * `node:vm` — enough surface for the emitted script to boot, and nothing else.
 * The stub is deliberately dumb: it records writes rather than interpreting
 * them, so it cannot drift into agreeing with a broken page.
 *
 * WHY the mutant. Every discard case here is also run against a copy of the
 * page with that renderer's guard mechanically removed, and is REQUIRED to
 * fail there. A test that passes both with and without the thing it tests is
 * measuring nothing; this file refuses to report a pass it cannot also make
 * fail. That check runs on every invocation, not once at authoring time.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [PAGE_JS_PATH, GUARD_JS_PATH] = process.argv.slice(2);
const PAGE_JS = readFileSync(PAGE_JS_PATH, 'utf8');
const GUARD_JS = readFileSync(GUARD_JS_PATH, 'utf8');

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

// One payload per endpoint the page can reach, shaped enough that the winning
// renderer paints something. `/api/projects` is the exception and is NEVER
// answered: the page's boot IIFE awaits it before deciding whether to land on
// the overview, so leaving it in flight keeps boot inert and every scenario's
// panel writes attributable to the scenario itself.
const PAYLOADS = {
  '/api/overview': {
    project: 'overview-fixture', events: 2, by_type: { decision: 2 },
    chain_ok: true, memory_events: 2, auto_events: 0, sessions: 1,
    last_ts: '2026-01-01T00:00:00Z',
  },
  '/api/memory': [{
    type: 'decision', ts: '2026-01-01T00:00:00Z', session: 's1',
    text: 'memory-fixture', tags: [],
  }],
  '/api/improvements': { ok: true, auto: [], manual: [] },
  '/api/sessions': [],
  '/api/oversee': { ok: true, runs: [], sessions: [] },
  '/api/enrich/settings': { enabled: true },
  '/api/changelog': { ok: true, entries: [], categories: ['update'], pending: { files: [] } },
  '/api/sops': { ok: true, active: [], history: [] },
  '/api/state': 'state-fixture',
  '/api/plugins/inventory': { ok: true, inventory: { plugins: [], local_skills: [], counts: {} } },
  '/api/skills/status': { ok: true, status: {}, counts: {} },
  '/api/skills/stats': { ok: true, stats: {} },
  '/api/skills/recommend': { ok: true, recommendations: [] },
  '/api/mindnet/cats': { cats: [], sessions: [] },
};
const DEFAULT_PAYLOAD = { ok: true };

// What the user perceives as "the section", which is not only what they can
// see. A render "painted" exactly when one of these was written — #out covers
// run()'s catch path too, since fail() writes there, so "discarded without
// throwing" needs no separate probe.
//
// #ann and #status were NOT in this set when the guard first shipped, and that
// omission made 16 `discards_late_paint:*` cases certify "wrote nothing" while
// run() announced the STALE section's completion into the live region with no
// guard at all. A screen-reader user got exactly the clobber the visual guard
// exists to prevent, and the harness reported it as clean. A panel probe that
// only watches the pixels measures half the interface.
const PANEL_IDS = new Set(['view', 'out', 'ann', 'status']);

// The module-level cache each renderer writes on the far side of its await. A
// stale response that skips the paint but still writes here reintroduces the
// bug on the next repaint, so the caches are asserted separately.
const CACHES = {
  renderMemory: 'MEM_ITEMS',
  renderImprovements: 'IMP_ITEMS',
  renderOversee: 'OV_DATA',
  renderPlugins: 'PLUG_INV',
  renderTaskCards: 'TASK_SESSIONS',
  renderSessions: 'TASK_SESSIONS',
};

// Sessions is the one section whose paint the page cannot guard: renderSessions
// delegates to renderTaskCards in taskview.py, which awaits /api/sessions and
// paints on the far side of that await, in a module the page does not own. The
// page says so in a comment rather than pretending otherwise. This list is a
// CEILING, not a pin — closing the gap in taskview.py makes the set of
// clobbering renderers smaller, which still satisfies the subset check below.
const ALLOWED_UNGUARDED = new Set(['renderSessions', 'renderTaskCards']);

// A renderer's guard compares against the identity of the SECTION it belongs
// to, which for almost every renderer is itself. renderTaskCards is the one
// exception: it is the paint half of renderSessions, and taskview.py guards it
// with `shouldPaint(liveFn, renderSessions)` deliberately, because the section
// the user picked is Sessions — renderTaskCards is never something you can
// navigate to. Discovery here is by name pattern (`async function render\w+`),
// so it gets picked up as if it were its own section; driving it that way sets
// liveFn to a value no guard on the page ever compares against, every scenario
// discards, and the anti-vacuity control below reports "nothing painted".
// That was a modelling artifact in this harness, not a defect in the page —
// but it was reported as 41 cases with ok:false while the python driver, which
// enumerated only the 40 case names it knew, printed "40 passed".
const OWNING_SECTION = { renderTaskCards: 'renderSessions' };
const sectionOf = (name) => OWNING_SECTION[name] || name;

// ---------------------------------------------------------------------------
// the DOM stub
// ---------------------------------------------------------------------------

function makeElement(id, onPanelWrite) {
  const classes = new Set();
  const el = {
    id,
    _innerHTML: '',
    _textContent: '',
    style: {},
    dataset: {},
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return Object.hasOwn(this.attrs, k) ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener() {},
    appendChild() {},
    focus() {},
    scrollIntoView() {},
    // Handlers are wired by `view.querySelectorAll(...).forEach(...)` after
    // every paint. Returning an empty list is faithful enough: this file is
    // about which render reaches the paint, not about what the paint wires up.
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
    },
  };
  const record = (prop, value) => {
    if (PANEL_IDS.has(id)) onPanelWrite({ id, prop, value: String(value).slice(0, 160) });
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._innerHTML; },
    set(v) { el._innerHTML = String(v); record('innerHTML', v); },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return el._textContent; },
    set(v) { el._textContent = String(v); record('textContent', v); },
  });
  return el;
}

/** A page loaded into its own vm context, with every fetch held open. */
function newWorld(scriptSrc) {
  const panelWrites = [];
  const elements = new Map();
  const pending = [];          // every fetch issued and not yet answered
  const timers = new Map();    // recorded, never fired — see below
  let timerId = 0;

  const getElementById = (id) => {
    if (!elements.has(id)) {
      elements.set(id, makeElement(id, (w) => panelWrites.push(w)));
    }
    return elements.get(id);
  };

  const documentStub = {
    hidden: false,
    activeElement: null,
    body: makeElement('body', () => {}),
    getElementById,
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => makeElement('<' + tag + '>', () => {}),
    addEventListener() {},
  };

  function respond(payload) {
    const isText = typeof payload === 'string';
    return {
      ok: true,
      status: 200,
      headers: { get: () => (isText ? 'text/plain; charset=utf-8' : 'application/json') },
      json: async () => payload,
      text: async () => (isText ? payload : JSON.stringify(payload)),
    };
  }

  // Every request is parked. Nothing resolves until a scenario says so, which
  // is what makes "still in flight" an exact state rather than a hoped-for
  // timing window — no sleeps, no flake.
  const fetchStub = (url) => {
    const path = String(url).split('?')[0];
    const rec = { url: String(url), path, done: false };
    rec.promise = new Promise((resolve, reject) => {
      rec.resolve = resolve;
      rec.reject = reject;
    });
    pending.push(rec);
    return rec.promise;
  };

  const sandbox = {
    console: {
      // stdout carries the JSON report; page noise goes to stderr.
      log: (...a) => process.stderr.write('[page] ' + a.join(' ') + '\n'),
      warn: (...a) => process.stderr.write('[page] ' + a.join(' ') + '\n'),
      error: (...a) => process.stderr.write('[page] ' + a.join(' ') + '\n'),
    },
    document: documentStub,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { clipboard: { writeText: async () => {} } },
    fetch: fetchStub,
    confirm: () => false,
    // Recorded, never fired. Nothing on the paint path depends on a timer
    // elapsing (run()'s two timers only stage loading feedback), so a clock
    // that never advances removes a whole class of timing flake from these
    // assertions while changing no decision under test.
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: (id) => { timers.delete(id); },
    setInterval: (fn, ms) => { timers.set(++timerId, { fn, ms, repeating: true }); return timerId; },
    clearInterval: (id) => { timers.delete(id); },
    addEventListener() {},
    removeEventListener() {},
  };
  // `window` is the same object as the global, as it is in a browser — the page
  // reaches setTimeout bare and matchMedia through `window.`, and both have to
  // find the same stub.
  sandbox.window = sandbox;
  sandbox.location = { href: '', toString: () => '' };
  sandbox.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });

  const context = vm.createContext(sandbox);
  vm.runInContext(scriptSrc, context, { filename: 'dashboard-page.js' });

  const world = {
    context,
    panelWrites,
    pending,
    getElementById,
    /** Read or run anything in the page's own scope (`liveFn` is a top-level
     *  `let`, so it is reachable from a later script in the same context). */
    eval: (src) => vm.runInContext(src, context, { filename: 'harness.js' }),
    /** Requests issued since the given mark. */
    since: (mark) => pending.slice(mark),
    answer: (recs) => {
      for (const rec of recs) {
        if (rec.done) continue;
        rec.done = true;
        rec.resolve(respond(Object.hasOwn(PAYLOADS, rec.path) ? PAYLOADS[rec.path] : DEFAULT_PAYLOAD));
      }
    },
    /** Drain the microtask queue. setImmediate is a host macrotask, so every
     *  promise continuation the page queued has run by the time it fires. */
    settle: async () => {
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
    },
  };
  return world;
}

// ---------------------------------------------------------------------------
// the mutant: the same page with one renderer's guard removed
// ---------------------------------------------------------------------------

// Both shapes the guard is written in — `if (!shouldPaint(liveFn, renderX))
// return;` at the head of a paint, and the inverted `if (shouldPaint(liveFn,
// renderX)) paintX();` renderPlugins uses — plus the bare `liveFn !== renderX`
// form they were extracted from, so the mutator keeps working if the helper is
// ever inlined again. A renderer whose guard the mutator cannot find is
// REPORTED, never silently treated as unguarded.
function removeGuard(src, name) {
  const negative = new RegExp(
    'if\\s*\\(\\s*(?:!\\s*shouldPaint\\(\\s*liveFn\\s*,\\s*' + name +
    '\\s*\\)|liveFn\\s*!==\\s*' + name + ')\\s*\\)\\s*return\\s*;', 'g');
  const positive = new RegExp(
    'if\\s*\\(\\s*(?:shouldPaint\\(\\s*liveFn\\s*,\\s*' + name +
    '\\s*\\)|liveFn\\s*===\\s*' + name + ')\\s*\\)\\s*', 'g');
  let sites = 0;
  let out = src.replace(negative, () => { sites++; return '/* guard removed */'; });
  out = out.replace(positive, () => { sites++; return ''; });
  return { src: out, sites };
}

// ---------------------------------------------------------------------------
// the scenario
// ---------------------------------------------------------------------------

/**
 * Run the reported race and report what the late render did.
 *
 * `victim` is the section the user is on when its load starts; `switchAway`
 * decides whether they then pick another one. The switch happens strictly
 * between the victim's fetch being ISSUED and that fetch being ANSWERED, which
 * is the whole point: at the moment the victim was entered, liveFn was the
 * victim, so a guard evaluated before the await passes and paints anyway. Only
 * a comparison re-read after the await can discard this.
 *
 * `via: 'poll'` starts the victim the way refreshLive() does — the path the
 * user actually hit, and the one a run()-scoped generation counter cannot see.
 * `via: 'run'` starts it the way every show*() does, through run(), where a
 * stale throw would reach fail() and paint an error over the new section.
 */
async function race(scriptSrc, victim, { switchAway = true, via = 'run' } = {}) {
  const w = newWorld(scriptSrc);
  await w.settle();                                  // let boot issue /api/projects
  const winner = victim === 'renderState' ? 'renderOverview' : 'renderState';

  // 1. the user is on `victim`; its load starts and its fetch goes out
  const mark = w.pending.length;
  const victimSection = sectionOf(victim);
  w.eval('liveFn = ' + victimSection + ';');
  const victimRun = via === 'poll'
    ? w.eval('refreshLive()')
    : w.eval("run('loading " + victim + "', " + victim + ')');
  await w.settle();
  const victimFetches = w.since(mark);
  if (!victimFetches.length) {
    return { error: victim + ' issued no request, so nothing was ever in flight —' +
                    ' this scenario cannot say anything about it' };
  }

  // The ordering claim, measured instead of argued: at the instant this fetch
  // went out, liveFn WAS the victim — so a guard evaluated here, before the
  // await, would have let the render through. Everything the discard case
  // proves rests on this being true first.
  const ownedPanelAtStart = w.eval('liveFn') === w.eval(victimSection);

  const cacheVar = CACHES[victim];
  const cacheBefore = cacheVar ? w.eval('JSON.stringify(' + cacheVar + ')') : null;

  // 2. mid-flight, the user picks another section
  if (switchAway) {
    const mark2 = w.pending.length;
    w.eval('liveFn = ' + winner + ';');
    const winnerRun = w.eval("run('loading " + winner + "', " + winner + ')');
    await w.settle();
    w.answer(w.since(mark2));                        // the new section answers first
    await winnerRun;
    await w.settle();
    if (w.eval('liveFn') === w.eval(victimSection)) {
      return { error: 'the switch did not take: liveFn still points at ' + victimSection };
    }
  }

  // 3. the older response lands last
  const mark3 = w.panelWrites.length;
  w.answer(victimFetches);
  await victimRun;
  await w.settle();

  const late = w.panelWrites.slice(mark3);
  const cacheAfter = cacheVar ? w.eval('JSON.stringify(' + cacheVar + ')') : null;
  return {
    painted: late.length > 0,
    writes: late,
    ownedPanelAtStart,
    cacheVar,
    cacheWritten: cacheVar ? cacheBefore !== cacheAfter : false,
    // The panel the user is actually looking at, so a failure report says what
    // they would have seen rather than only that something happened.
    panel: {
      view: w.getElementById('view')._innerHTML.slice(0, 200),
      out: (w.getElementById('out')._textContent || w.getElementById('out')._innerHTML).slice(0, 200),
    },
  };
}

// ---------------------------------------------------------------------------
// cases
// ---------------------------------------------------------------------------

const cases = [];
const record = (name, ok, detail) => cases.push({ name, ok: !!ok, detail });

// --- tier 1: the decision function itself, executed on the page's own bytes --
function guardCases() {
  const guard = vm.runInNewContext(GUARD_JS + '\n;shouldPaint', {});
  const renderA = async () => {};
  const renderB = async () => {};

  record('guard_paints_for_the_render_the_user_is_waiting_for',
         guard(renderA, renderA) === true,
         'shouldPaint(f, f) must be true');
  record('guard_discards_a_render_the_user_left',
         guard(renderB, renderA) === false,
         'shouldPaint(other, f) must be false');
  record('guard_discards_when_no_section_is_live',
         guard(null, renderA) === false,
         'liveFn === null means the visible view is not auto-refreshable; a ' +
         'renderer holding the panel must not paint into it');
  // Identity, not equality — the property the whole mechanism rests on. Two
  // renderers with byte-identical bodies are still different sections.
  const twinA = async () => { await 0; };
  const twinB = async () => { await 0; };
  record('guard_compares_identity_not_equality',
         String(twinA) === String(twinB) && guard(twinA, twinB) === false,
         'two distinct functions with identical source must not compare equal');
}

// --- tier 2: the ordering guarantee, driven through the real page ------------
async function raceCases(renderers) {
  const clobbering = [];
  for (const name of renderers) {
    for (const via of ['run', 'poll']) {
      const r = await race(PAGE_JS, name, { via });
      if (r.error) { record(`race_setup:${name}:${via}`, false, r.error); continue; }
      const ok = !r.painted && !r.cacheWritten;
      if (!ok && !clobbering.includes(name)) clobbering.push(name);
      if (ALLOWED_UNGUARDED.has(name)) continue;     // covered by the subset case
      record(`discards_late_paint:${name}:${via}`, ok && r.ownedPanelAtStart,
             !r.ownedPanelAtStart
               ? name + ' did not own the panel when its fetch went out, so this ' +
                 'run says nothing about WHERE the guard sits — a check made ' +
                 'before the await would have discarded it too'
               : ok ? 'owned the panel when the request went out, and still wrote ' +
                      'nothing when the answer arrived late'
                    : describeClobber(name, r));
    }

    // Anti-vacuity: the same scenario with no switch MUST paint. Without this,
    // a renderer that returned unconditionally would score a clean pass above.
    const kept = await race(PAGE_JS, name, { switchAway: false });
    record(`paints_when_the_user_stayed:${name}`, !kept.error && kept.painted,
           kept.error || (kept.painted ? 'painted as expected'
             : 'nothing painted even though the user never left — the scenario ' +
               'proves nothing about the discard case'));
  }

  const stray = clobbering.filter((n) => !ALLOWED_UNGUARDED.has(n));
  record('no_renderer_outside_the_documented_gap_can_clobber', stray.length === 0,
         stray.length ? 'these renderers paint a section the user already left: ' +
                        stray.join(', ')
                      : 'observed clobbering: {' + clobbering.join(', ') + '} ⊆ ' +
                        'documented gap {' + [...ALLOWED_UNGUARDED].join(', ') + '}');
}

function describeClobber(name, r) {
  const what = [];
  if (r.painted) what.push('painted ' + JSON.stringify(r.writes[0]));
  if (r.cacheWritten) what.push('wrote its cache ' + r.cacheVar);
  return name + ' ' + what.join(' and ') +
         ' after the user had already moved to another section';
}

// --- tier 3: the mutant — every pass above must be makeable to fail ----------
async function mutantCases(renderers) {
  for (const name of renderers) {
    if (ALLOWED_UNGUARDED.has(name)) continue;
    const { src, sites } = removeGuard(PAGE_JS, name);
    if (!sites) {
      // Reported under the case name the test reads, not a separate one: a
      // missing case surfaces as "the harness ran no such case", which says
      // nothing about WHY. This is the why, and it is the loudest signal in
      // the file — no guard to remove means either it was deleted or its shape
      // changed, and until that is resolved every pass above is unproven.
      record(`removing_the_guard_reintroduces_the_clobber:${name}`, false,
             'no guard found to remove for ' + name + ' — it has either been ' +
             'deleted from the page or rewritten into a shape removeGuard() no ' +
             'longer recognises. Until one of those is fixed, the discard cases ' +
             'for ' + name + ' are unproven: nothing shows they can fail.');
      continue;
    }
    const r = await race(src, name, { via: 'poll' });
    record(`removing_the_guard_reintroduces_the_clobber:${name}`,
           !r.error && (r.painted || r.cacheWritten),
           r.error || (r.painted || r.cacheWritten
             ? 'guard removed at ' + sites + ' site(s) → the late render clobbered again'
             : 'guard removed at ' + sites + ' site(s) and NOTHING changed — this ' +
               'scenario does not discriminate, so treat the pass above as unproven'));
  }
}

// --- the census: neither list may drift out from under the other ------------
function censusCases(renderers) {
  // If the regex stops matching the emitted page, every case below it becomes
  // a loop over nothing and this file reports a serene, meaningless pass.
  record('the_page_still_declares_renderers', renderers.length > 0,
         'discovered: ' + renderers.join(', '));
  // The other direction: an allowance for a renderer that no longer exists
  // keeps excusing a name nobody can find, and would go on excusing it if the
  // name were reused for something else later.
  const stale = [...ALLOWED_UNGUARDED].filter((n) => !renderers.includes(n));
  record('the_documented_gap_still_names_real_renderers', stale.length === 0,
         stale.length ? 'ALLOWED_UNGUARDED excuses renderers the page no longer ' +
                        'declares: ' + stale.join(', ')
                      : 'every excused renderer still exists');
}

async function main() {
  // Discovery, not a hardcoded list: a renderer added later is picked up here
  // and has to survive the race like the rest. That is the answer to the
  // SILENT failure mode — "the idiom applied to some renders but not others"
  // cannot hide from a check that enumerates the renders itself.
  const renderers = [...PAGE_JS.matchAll(/async\s+function\s+(render\w+)\s*\(/g)]
    .map((m) => m[1]);

  censusCases(renderers);
  guardCases();
  await raceCases(renderers);
  await mutantCases(renderers);

  const ok = cases.every((c) => c.ok);
  process.stdout.write(JSON.stringify({ ok, renderers, cases }, null, 1));
  process.exitCode = ok ? 0 : 1;
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({
    ok: false, renderers: [], harness_error: String((e && e.stack) || e), cases,
  }, null, 1));
  process.exitCode = 2;
});
