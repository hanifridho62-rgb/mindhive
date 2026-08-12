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
  // Deliberately NOT the empty list. An empty payload deserialises to exactly
  // the value `TASK_SESSIONS` already holds, so the cache probe could not tell
  // a stale write from no write at all — and `paintTaskCards` takes its "No
  // sessions yet." branch, which exercises none of the card path. One row fixes
  // both. `status: 'completed'` keeps `renderTaskCards` off the per-card
  // `/api/task` fetches, which only fire for OPEN or ongoing cards.
  '/api/sessions': [{
    project: 'sessions-fixture', session_id: 's-late', status: 'completed',
    origin: 'manual', activity: { files: 1 },
    first_ts: '2026-01-01T00:00:00Z', last_ts: '2026-01-01T00:01:00Z',
  }],
  '/api/oversee': { ok: true, runs: [], sessions: [] },
  '/api/enrich/settings': { enabled: true },
  '/api/changelog': { ok: true, entries: [], categories: ['update'], pending: { files: [] } },
  '/api/sops': { ok: true, active: [], history: [] },
  '/api/state': 'state-fixture',
  // Same reason as `/api/sessions`: the empty inventory IS `PLUG_INV`'s initial
  // value, so a stale cache write was invisible to the probe.
  '/api/plugins/inventory': { ok: true, inventory: {
    plugins: [{ name: 'plugins-fixture', version: '0.0.0', marketplace: 'fixture',
                present: true, skills: [{ name: 'fixture-skill', trigger: 'never' }] }],
    local_skills: [], counts: { skills_total: 1 } } },
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

// Renderers excused from the per-renderer discard cases. It is EMPTY, and that
// is the point: it used to excuse renderSessions and renderTaskCards on the
// grounds that the paint happens inside taskview.py, past an await this module
// does not own. taskview.py had already shipped that guard in the same commit —
// so the one guard living outside dashboard_page.py was the only guard in the
// change with no discard case, no anti-vacuity case and no mutant, and deleting
// it would have kept every test green. A ceiling nobody rechecks is a hole.
const ALLOWED_UNGUARDED = new Set([]);

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

// The function the nav button calls, per section. The race USED to set
// `liveFn = renderX` itself, which stubbed out the precondition the whole
// design rests on — "showOverview()/showOversee()/… each assign it
// SYNCHRONOUSLY before starting their fetch" — and turned the harness's own
// `ownedPanelAtStart` claim into a tautology it constructed. Entering through
// the real function makes that claim a measurement: nothing is awaited between
// the call and the read.
const SHOW_OF = {
  renderOverview: 'showOverview',
  renderMemory: 'showMemory',
  renderImprovements: 'showImprovements',
  renderOversee: 'showOversee',
  renderLog: 'showLog',
  renderSops: 'showSops',
  renderState: 'showState',
  renderPlugins: 'showSkillPanel',
  renderSessions: 'showSessions',
};

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
  // A clock the scenario moves by hand. Timers here never elapse on their own,
  // so anything the page decides from wall-clock deltas (the stuck-poll check)
  // would otherwise be untestable without sleeping.
  let clock = 1767225600000;   // 2026-01-01T00:00:00Z, fixed so runs compare

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
    Date: class extends Date {
      constructor(...a) { if (a.length) super(...a); else super(clock); }
      static now() { return clock; }
    },
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
    /** Answer with a body the fixture table does not carry — a server-side
     *  refusal is a 200 with `ok:false`, which no path-keyed payload models. */
    answerWith: (recs, payload) => {
      for (const rec of recs) {
        if (rec.done) continue;
        rec.done = true;
        rec.resolve(respond(payload));
      }
    },
    /** The other way a request can end. run()'s catch is a paint site too. */
    reject: (recs, msg) => {
      for (const rec of recs) {
        if (rec.done) continue;
        rec.done = true;
        rec.reject(new Error(msg || 'the request died'));
      }
    },
    /** Fire every one-shot timer currently armed and forget it.
     *
     *  The default of never firing is right for the race cases (see the stub
     *  comment) but it is not free: it makes run()'s skeleton unreachable, and
     *  the skeleton is a paint. Repeating timers are deliberately left alone —
     *  the two 2.5 s pollers would issue requests no scenario here answers. */
    /** Move the page's wall clock forward without waiting for it. */
    advance: (ms) => { clock += ms; },
    fireTimers: () => {
      const once = [...timers.entries()].filter(([, t]) => !t.repeating);
      for (const [id, t] of once) { timers.delete(id); t.fn(); }
      return once.length;
    },
    /** Drain the microtask queue. setImmediate is a host macrotask, so every
     *  promise continuation the page queued has run by the time it fires. */
    settle: async () => {
      for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r));
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
  const victimSection = sectionOf(victim);
  const enter = SHOW_OF[victimSection];
  if (!enter) return { error: 'no nav entry point mapped for ' + victimSection };

  // 1. the user picks `victim`, through the same function its nav button calls
  let mark = w.pending.length;
  w.eval(enter + '()');
  // The ordering claim, MEASURED: nothing has been awaited between the call
  // above and this read, so if liveFn already points at the section, the
  // assignment really is synchronous and really does precede the fetch. That is
  // the property the whole mechanism rests on — a guard re-read after an await
  // is only meaningful because the assignment landed before one.
  const ownedPanelAtStart = w.eval('liveFn') === w.eval(victimSection);

  if (via === 'poll') {
    // Let the section finish, THEN let the poller start the in-flight load —
    // the path the user actually hit, and the one run()'s counter cannot see.
    await w.settle();
    w.answer(w.since(mark));
    await w.settle();
    mark = w.pending.length;
    w.eval('refreshLive()');
  }
  await w.settle();
  const victimFetches = w.since(mark);
  if (!victimFetches.length) {
    return { error: victim + ' issued no request, so nothing was ever in flight —' +
                    ' this scenario cannot say anything about it' };
  }

  const cacheVar = CACHES[victim];
  const cacheBefore = cacheVar ? w.eval('JSON.stringify(' + cacheVar + ')') : null;

  // 2. mid-flight, the user picks another section — again through its button
  if (switchAway) {
    const mark2 = w.pending.length;
    w.eval(SHOW_OF[winner] + '()');
    await w.settle();
    w.answer(w.since(mark2));                        // the new section answers first
    await w.settle();
    if (w.eval('liveFn') === w.eval(victimSection)) {
      return { error: 'the switch did not take: liveFn still points at ' + victimSection };
    }
  }

  // 3. the older response lands last
  const mark3 = w.panelWrites.length;
  w.answer(victimFetches);
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
    //
    // The cache half of that same argument, and it is not hypothetical: the
    // `/api/plugins/inventory` fixture used to deserialise to exactly the value
    // `PLUG_INV` already held, so `cacheBefore !== cacheAfter` was false whether
    // or not the stale render wrote it. The probe read as coverage and measured
    // nothing. Requiring the KEPT run to move the cache proves the fixture can
    // tell the two apart at all — for every cached renderer, including ones
    // added after this was written.
    const kept = await race(PAGE_JS, name, { switchAway: false });
    const probeBlind = CACHES[name] && !kept.error && !kept.cacheWritten;
    record(`paints_when_the_user_stayed:${name}`,
           !kept.error && kept.painted && !probeBlind,
           kept.error || (probeBlind
             ? `the ${CACHES[name]} probe cannot see a write: this renderer painted ` +
               'with the user still on it and the cache did not change, so the ' +
               'fixture must already equal the initial value. Every cache claim ' +
               `for ${name} above is unproven until the fixture differs.`
             : kept.painted ? 'painted as expected, and moved its cache'
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
    // Mutate the guard of the SECTION this renderer belongs to: renderTaskCards
    // is guarded by `shouldPaint(liveFn, renderSessions)`, because Sessions is
    // what the user picked. Searching for its own name would find nothing and
    // report a missing guard that is right there.
    const { src, sites } = removeGuard(PAGE_JS, sectionOf(name));
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

// --- tier 4: the run()-scoped generation, invisible to the race above --------
//
// `shouldPaint` answers one question: is this render's SECTION still the live
// one. Everything run() does AROUND the render is outside that question —
// the skeleton it arms, the "still loading" status, the closing announce, and
// fail()'s error panel — and two loads of the SAME section make shouldPaint
// answer yes for both, so it cannot help there at all. That is what RUN_GEN is
// for, and none of it is reachable through the race scenario: the skeleton half
// needs a timer to elapse, and the same-section half needs two loads of one
// section, which race() never performs.

/** A superseded run's timers fire late. They must write nothing. */
async function supersededTimers(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  const mark = w.pending.length;
  w.eval('liveFn = renderState;');
  const stale = w.eval("run('loading state', renderState, true)");
  await w.settle();
  if (!w.since(mark).length) return { error: 'renderState issued no request' };

  const mark2 = w.pending.length;
  w.eval('liveFn = renderOverview;');
  const fresh = w.eval("run('loading overview', renderOverview, true)");
  await w.settle();
  w.answer(w.since(mark2));
  await fresh;
  await w.settle();

  // Only the stale run's one-shots are still armed: the winner cleared its own
  // in run()'s finally, and boot's /api//projects fetch is never answered.
  const before = w.panelWrites.length;
  const fired = w.fireTimers();
  await w.settle();
  return { fired, late: w.panelWrites.slice(before) };
}

/** The same section loaded twice; the older load then FAILS. */
async function staleSameSectionError(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  w.eval('liveFn = renderState;');
  const mark = w.pending.length;
  const first = w.eval("run('loading state', renderState, true)");
  await w.settle();
  const firstFetches = w.since(mark);
  if (!firstFetches.length) return { error: 'renderState issued no request' };

  const mark2 = w.pending.length;
  const second = w.eval("run('loading state', renderState, true)");
  await w.settle();
  if (!w.since(mark2).length) {
    return { error: 'the second load of the same section issued no request, so ' +
                    'there are not two runs in flight and nothing is being tested' };
  }
  w.answer(w.since(mark2));
  await second;
  await w.settle();

  const before = w.panelWrites.length;
  w.reject(firstFetches, 'the older request died');
  await first;
  await w.settle();
  return { late: w.panelWrites.slice(before) };
}

// The single point RUN_GEN is read through. Neutralising it is the page as it
// was before the counter existed, which is what the mutant halves below require
// each pass above to be makeable back into.
function withoutRunGeneration(src) {
  const from = 'const current = () => gen === RUN_GEN;';
  const out = src.replace(from, 'const current = () => true;');
  return { src: out, changed: out !== src };
}

async function runGenerationCases() {
  const mutant = withoutRunGeneration(PAGE_JS);
  record('the_run_generation_is_read_through_one_expression', mutant.changed,
         mutant.changed
           ? 'found `current()` and can neutralise it'
           : 'run() no longer defines `current` as a comparison against RUN_GEN, ' +
             'so the two mutant cases below cannot be constructed and the two ' +
             'real ones are unproven');

  const t = await supersededTimers(PAGE_JS);
  record('a_superseded_runs_timers_paint_nothing',
         !t.error && t.fired > 0 && t.late.length === 0,
         t.error || (t.fired === 0
           ? 'no one-shot timer was armed, so nothing was tested — run() no ' +
             'longer stages its loading feedback through setTimeout'
           : t.late.length
             ? "the superseded run's timers wrote " + JSON.stringify(t.late[0]) +
               ' over the section the user actually picked'
             : t.fired + ' late timer(s) fired and wrote nothing'));

  const e = await staleSameSectionError(PAGE_JS);
  record('an_older_load_of_the_same_section_cannot_paint_its_error',
         !e.error && e.late.length === 0,
         e.error || (e.late.length
           ? 'the older load failed and painted ' + JSON.stringify(e.late[0]) +
             ' over the result the newer load had already delivered. shouldPaint ' +
             'cannot catch this: both loads ARE the live section.'
           : 'the older failure was discarded'));

  if (!mutant.changed) return;
  const tm = await supersededTimers(mutant.src);
  record('removing_the_run_generation_lets_the_stale_timers_paint',
         !tm.error && tm.late.length > 0,
         tm.error || (tm.late.length
           ? 'without the generation check the skeleton lands on the new section'
           : 'the generation check was removed and NOTHING changed — the case ' +
             'above does not discriminate, so treat its pass as unproven'));
  const em = await staleSameSectionError(mutant.src);
  record('removing_the_run_generation_lets_the_stale_error_paint',
         !em.error && em.late.length > 0,
         em.error || (em.late.length
           ? 'without the generation check the dead request paints its error'
           : 'the generation check was removed and NOTHING changed — the case ' +
             'above does not discriminate, so treat its pass as unproven'));
}

// --- tier 5: the poller's in-flight latch, when the request never settles ----
//
// `liveInFlight` is released in a `finally`, so every way a fetch can END is
// covered. A fetch that never ends is not one of them: `fetch` has no default
// timeout, so a wedged server holds the latch forever, every tick returns
// early, and the "live" chip keeps asserting freshness over a view that stopped
// updating. The harness parks requests by construction, which makes that state
// exact rather than a timing window.

async function stuckPollGoesStale(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  const isStale = () => w.eval("liveEl.classList.contains('stale')");
  w.eval('liveFn = renderState;');
  const mark = w.pending.length;
  w.eval('refreshLive()');
  await w.settle();
  const issued = w.pending.length - mark;
  if (!issued) return { error: 'refreshLive() issued no request' };
  const staleImmediately = isStale();

  let ticks = 0;
  while (ticks < 6 && !isStale()) {
    w.advance(6000);            // well past LIVE_STUCK_MS, latch still held
    w.eval('refreshLive()');
    await w.settle();
    ticks++;
  }
  return { stale: isStale(), staleImmediately, ticks,
           issued: w.pending.length - mark };
}

function withoutTheStuckCheck(src) {
  const from = 'if (Date.now() - liveSince > LIVE_STUCK_MS) markLiveHealth(false);';
  const out = src.replace(from, '');
  return { src: out, changed: out !== src };
}

async function stuckPollCases() {
  const r = await stuckPollGoesStale(PAGE_JS);
  record('a_poll_that_never_returns_stops_the_chip_claiming_live',
         !r.error && r.stale && !r.staleImmediately,
         r.error || (r.staleImmediately
           ? 'the chip was already stale before any tick was missed, so this ' +
             'says nothing about the stuck poll'
           : r.stale
             ? `the chip went stale after ${r.ticks} skipped tick(s)`
             : 'six skipped ticks with the request still in flight and the chip ' +
               'still claims the view is live'));

  // The other half, and the reason this is not simply a timeout: a stuck poll
  // must not spawn a replacement every tick. Piling requests onto a server that
  // is merely slow is the failure the latch exists to prevent.
  record('a_stuck_poll_never_issues_a_second_request',
         !r.error && r.issued === 1,
         r.error || (r.issued === 1
           ? 'six ticks were skipped and the one stuck request was never joined'
           : `${r.issued} requests went out while one was already in flight — ` +
             'the latch is retrying instead of skipping'));

  const m = withoutTheStuckCheck(PAGE_JS);
  record('removing_the_stuck_check_lets_the_chip_go_on_lying', m.changed,
         m.changed ? 'the check is present and removable'
                   : 'no stuck-poll check found in refreshLive() — the case ' +
                     'above cannot be made to fail, so treat it as unproven');
  if (!m.changed) return;
  const rm = await stuckPollGoesStale(m.src);
  record('the_chip_stays_green_without_the_stuck_check',
         !rm.error && !rm.stale,
         rm.error || (rm.stale
           ? 'the chip went stale with the check removed, so something else is ' +
             'producing that signal and the case above does not isolate it'
           : 'check removed → six skipped ticks and the chip still says live'));
}

// --- tier 6: the OTHER poller, which had no executed cover at all ------------
//
// `pollCats` carries the same in-flight latch as `refreshLive` and was asserted
// only by substring-matching the served page — including a whitespace-exact
// `finally { catsInFlight = false; }`. That check cannot tell a latch that
// works from one that is merely spelled correctly, and this is the poller with
// the shorter fold and therefore the one most likely to be judged unimportant.

async function catsPollTicks(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  const mark = w.pending.length;
  const out = (n) => w.pending.length - mark;

  w.eval('pollCats()');
  await w.settle();
  const first = out();

  w.eval('pollCats()');            // two more ticks while the first is parked
  w.eval('pollCats()');
  await w.settle();
  const afterSkips = out();

  w.answer(w.since(mark));         // the request finally lands...
  await w.settle();
  w.eval('pollCats()');            // ...and the next tick must go out
  await w.settle();
  return { first, afterSkips, afterRelease: out() };
}

function withoutCatsLatch(src) {
  const from = 'if (catsInFlight) return;';
  const out = src.replace(from, '');
  return { src: out, changed: out !== src };
}

function withoutCatsRelease(src) {
  const from = 'finally { catsInFlight = false; }';
  const out = src.replace(from, 'finally { }');
  return { src: out, changed: out !== src };
}

async function catsPollCases() {
  const r = await catsPollTicks(PAGE_JS);
  record('the_cats_poll_issues_one_request_and_then_skips',
         r.first === 1 && r.afterSkips === 1,
         r.first !== 1
           ? `the first tick issued ${r.first} request(s), so the rest of this ` +
             'case is not measuring what it claims'
           : r.afterSkips === 1
             ? 'two further ticks fired while the first was in flight and neither queued'
             : `${r.afterSkips - 1} extra requests went out while one was in flight`);
  record('the_cats_poll_resumes_once_its_request_lands',
         r.afterRelease === 2,
         r.afterRelease === 2
           ? 'the latch released and the next tick issued its own request'
           : `${r.afterRelease - 1} request(s) after the answer arrived — the latch ` +
             'was never released, so this poller is wedged for the life of the page');

  const a = withoutCatsLatch(PAGE_JS);
  const b = withoutCatsRelease(PAGE_JS);
  record('both_halves_of_the_cats_latch_are_removable', a.changed && b.changed,
         a.changed && b.changed
           ? 'the skip and the release are both present and can be neutralised'
           : `not found: ${[!a.changed && 'the skip', !b.changed && 'the release']
                .filter(Boolean).join(' and ')} — the cases above cannot be made ` +
             'to fail, so treat them as unproven');
  if (a.changed) {
    const ra = await catsPollTicks(a.src);
    record('removing_the_cats_skip_lets_the_ticks_queue', ra.afterSkips > 1,
           ra.afterSkips > 1
             ? `skip removed → ${ra.afterSkips} concurrent requests`
             : 'skip removed and nothing changed — the case does not discriminate');
  }
  if (b.changed) {
    const rb = await catsPollTicks(b.src);
    record('removing_the_cats_release_wedges_the_poller', rb.afterRelease === 1,
           rb.afterRelease === 1
             ? 'release removed → no tick ever goes out again'
             : 'release removed and the poller kept going — the case does not discriminate');
  }
}

// --- tier 7: scope, which section identity cannot express --------------------
//
// `shouldPaint` compares WHICH SECTION is live. Changing the project changes
// neither the section nor the renderer: `onProjChange` re-enters Overview, so a
// user already on Overview has liveFn === renderOverview on both sides of the
// switch and an answer fetched for the previous namespace compares equal. It
// paints numbers from another project under this project's label, with nothing
// on screen that looks wrong. Driven through `onProjChange` rather than by
// assigning PROJECT, so it is the real path and not a constructed one.

async function projectSwitchDiscardsTheOldAnswer(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  w.eval("PROJECT = 'alpha';");
  const mark = w.pending.length;
  w.eval('showOverview()');
  await w.settle();
  const old = w.since(mark);
  if (!old.length) return { error: 'showOverview issued no request' };
  const sameSectionBefore = w.eval('liveFn') === w.eval('renderOverview');

  const mark2 = w.pending.length;
  w.eval("document.getElementById('proj').value = 'beta';");
  w.eval('onProjChange()');
  await w.settle();
  if (!w.since(mark2).length) {
    return { error: 'onProjChange issued no request, so no newer answer exists' };
  }
  w.answer(w.since(mark2));
  await w.settle();
  const sameSectionAfter = w.eval('liveFn') === w.eval('renderOverview');

  const before = w.panelWrites.length;
  w.answer(old);                   // project alpha's overview lands last
  await w.settle();
  return { late: w.panelWrites.slice(before), scope: w.eval('PROJECT'),
           sameSection: sameSectionBefore && sameSectionAfter };
}

function withoutTheScopeCheck(src) {
  const from = 'if (PROJECT !== scope) throw StaleScope(scope);';
  const out = src.replace(from, '');
  return { src: out, changed: out !== src };
}

async function scopeCases() {
  const r = await projectSwitchDiscardsTheOldAnswer(PAGE_JS);
  record('a_render_started_for_another_project_paints_nothing',
         !r.error && r.scope === 'beta' && r.sameSection && r.late.length === 0,
         r.error || (r.scope !== 'beta'
           ? `the picker did not move (PROJECT is ${JSON.stringify(r.scope)}), so ` +
             'no scope change happened and nothing was tested'
           : !r.sameSection
             ? 'liveFn changed across the project switch, so shouldPaint could have ' +
               'caught this on its own and the case proves nothing about scope'
             : r.late.length
               ? "project alpha's answer painted " + JSON.stringify(r.late[0]) +
                 " under project beta's label"
               : "the previous project's answer was discarded, and liveFn was " +
                 'identical on both sides of the switch — only the scope check ' +
                 'could have made that decision'));

  const m = withoutTheScopeCheck(PAGE_JS);
  record('the_scope_check_is_one_removable_expression', m.changed,
         m.changed ? 'found the scope comparison in apiGet'
                   : 'no scope comparison found in apiGet — the case above cannot ' +
                     'be made to fail, so treat it as unproven');
  if (!m.changed) return;
  const rm = await projectSwitchDiscardsTheOldAnswer(m.src);
  record('removing_the_scope_check_paints_the_other_projects_data',
         !rm.error && rm.late.length > 0,
         rm.error || (rm.late.length
           ? 'check removed → the previous project\'s rows land on the new project'
           : 'check removed and NOTHING changed — the case above does not ' +
             'discriminate, so treat its pass as unproven'));
}

// --- tier 8: honesty of the surrounding report ------------------------------
//
// Three separate ways the page told the user something that was not so. None of
// them is a paint, so none is reachable through the race scenario, and all
// three are exactly the class of defect this whole change exists to remove.

/** A refused action must not be announced as a completed one. */
async function refusedActionReportsFailure(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  const mark = w.pending.length;
  w.eval("doAction('/api/harvest','Catch up')");
  await w.settle();
  const reqs = w.since(mark);
  if (!reqs.length) return { error: 'doAction issued no request' };
  w.answerWith(reqs, { ok: false, error: 'a harvest is already running' });
  await w.settle();
  return {
    status: w.getElementById('status')._textContent,
    announced: w.getElementById('ann')._textContent,
    body: w.getElementById('out')._textContent || w.getElementById('out')._innerHTML,
  };
}

/** A tick whose render was discarded must not stamp the page as updated. */
async function discardedTickDoesNotStamp(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  let mark = w.pending.length;
  w.eval('showOversee()');
  await w.settle();
  w.answer(w.since(mark));
  await w.settle();

  mark = w.pending.length;
  w.eval('refreshLive()');                    // a poll of Oversee goes out
  await w.settle();
  const poll = w.since(mark);
  if (!poll.length) return { error: 'refreshLive issued no request' };
  const stampedBefore = w.getElementById('synced')._textContent;

  const mark2 = w.pending.length;
  w.eval('showState()');                      // the user leaves mid-flight
  await w.settle();
  w.answer(w.since(mark2));
  await w.settle();
  w.answer(poll);                             // the discarded answer lands last
  await w.settle();
  return { stampedBefore, stampedAfter: w.getElementById('synced')._textContent };
}

/** Pausing outranks a health message about a poll that is no longer running. */
async function pausedChipDropsTheStaleMessage(scriptSrc) {
  const w = newWorld(scriptSrc);
  await w.settle();
  w.eval('liveFails = 3; markLiveHealth(false);');
  const staleTitle = w.eval('liveEl.title');
  const staleClass = w.eval("liveEl.classList.contains('stale')");
  w.eval('toggleLive();');                    // the user pauses
  return { staleTitle, staleClass, pausedTitle: w.eval('liveEl.title'),
           pausedClass: w.eval("liveEl.classList.contains('paused')") };
}

// One mutation each, and each is the whole mechanism: with `refusal` blind both
// callers stop noticing, with the liveFn re-read gone every tick stamps, with
// the paused branch gone the health message outlives the pause.
const HONESTY_MUTANTS = {
  refusal: (s) => s.replace(
    "return r && r.ok === false && typeof r.error === 'string' ? r.error : null;",
    'return null;'),
  stamp: (s) => s.replace(
    'if (liveFn === fn) { markSynced(); markLiveHealth(true); }',
    'markSynced(); markLiveHealth(true);'),
  paused: (s) => s.replace(/if \(!liveOn\) return '[^']*';/, ''),
};

async function honestyCases() {
  for (const [name, mutate] of Object.entries(HONESTY_MUTANTS)) {
    record(`the_${name}_check_is_removable`, mutate(PAGE_JS) !== PAGE_JS,
           mutate(PAGE_JS) !== PAGE_JS
             ? 'found and neutralisable'
             : `the ${name} check is no longer in the shape this file can remove, ` +
               'so its case below cannot be made to fail and is unproven');
  }

  const a = await refusedActionReportsFailure(PAGE_JS);
  record('a_refused_action_is_not_announced_as_ready',
         !a.error && /failed/i.test(a.status || '') && /failed/i.test(a.announced || ''),
         a.error || `status=${JSON.stringify(a.status)} announced=` +
           `${JSON.stringify(a.announced)} — a 200 with ok:false is how the server ` +
           'declines to start work; neither caller inspected it, so the status ' +
           'line and the live region both said the operation was done');
  record('the_refusal_reason_still_reaches_the_panel',
         !a.error && String(a.body).includes('already running'),
         a.error || 'the panel shows ' + JSON.stringify(String(a.body).slice(0, 120)) +
           " — reporting failure must not cost the user the server's reason");

  const d = await discardedTickDoesNotStamp(PAGE_JS);
  record('a_discarded_live_tick_does_not_stamp_the_page_as_updated',
         !d.error && d.stampedAfter === d.stampedBefore,
         d.error || (d.stampedAfter === d.stampedBefore
           ? 'the tick was discarded and "updated HH:MM:SS" did not move'
           : `the stamp moved to ${JSON.stringify(d.stampedAfter)} for a render that ` +
             'never painted — that stamp is the one widget a user checks to decide ' +
             'whether what they are looking at is current'));

  const p = await pausedChipDropsTheStaleMessage(PAGE_JS);
  record('pausing_replaces_the_stale_message_it_can_no_longer_update',
         /not refreshing/i.test(p.staleTitle || '') && p.staleClass &&
         /paused/i.test(p.pausedTitle || '') && !/not refreshing/i.test(p.pausedTitle || ''),
         !p.staleClass
           ? 'the chip never went stale, so the pause case starts from the wrong state'
           : /not refreshing/i.test(p.pausedTitle || '')
             ? 'the paused chip still reads ' + JSON.stringify(p.pausedTitle) +
               ' — markLiveHealth is unreachable while paused, so nothing would ' +
               'ever correct it'
             : 'stale before, ' + JSON.stringify(p.pausedTitle) + ' after pausing');

  // the canaries: each pass above must be makeable to fail
  const am = await refusedActionReportsFailure(HONESTY_MUTANTS.refusal(PAGE_JS));
  record('without_the_refusal_check_the_status_says_ready',
         !am.error && !/failed/i.test(am.status || ''),
         am.error || 'the refusal check was removed and the status still read ' +
           JSON.stringify(am.status) + ' — the case above does not discriminate');
  const dm = await discardedTickDoesNotStamp(HONESTY_MUTANTS.stamp(PAGE_JS));
  record('without_the_liveFn_recheck_a_discarded_tick_stamps',
         !dm.error && dm.stampedAfter !== dm.stampedBefore,
         dm.error || 'the re-read was removed and the stamp still did not move — ' +
           'the case above does not discriminate');
  const pm = await pausedChipDropsTheStaleMessage(HONESTY_MUTANTS.paused(PAGE_JS));
  record('without_the_paused_branch_the_stale_message_survives_the_pause',
         /not refreshing/i.test(pm.pausedTitle || ''),
         'the paused branch was removed and the chip still read ' +
           JSON.stringify(pm.pausedTitle) + ' — the case above does not discriminate');
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
  await runGenerationCases();
  await stuckPollCases();
  await catsPollCases();
  await scopeCases();
  await honestyCases();

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
