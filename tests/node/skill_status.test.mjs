/**
 * Runs the Skill panel's own JavaScript -- the exact bytes of
 * `dashboard_js_skills.JS`, handed over on argv, never retyped here.
 *
 * The question is which of four states the panel reports for a skill, and a
 * grep for the word 'unobserved' cannot answer it: the status is DERIVED
 * (`last_outcome`, else the invoke-channel status map, else a call count),
 * and the headline completion rate is a division whose denominator is the
 * whole point. So the decisions are executed here.
 *
 * No DOM is needed: `plugSkillEntries`, `statusDot`, `plugStrip` and
 * `plugLegend` all return strings from the in-memory caches. The fragment's
 * top level is pure declarations, so it evaluates on its own; a `document`
 * stub stands in for the one function that reaches for it.
 *
 * Prints one JSON report; every verdict lives on the Python side.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// argv: every page fragment to evaluate, in page order, then the cases file.
// The panel calls `esc`/`escAttr` from the core fragment; the served page is
// ONE script, so the fragments are loaded here the same way -- verbatim and in
// order -- rather than the helpers being retyped as stubs.
const argv = process.argv.slice(2);
const casesPath = argv.pop();
const sources = argv.map((p) => [p, readFileSync(p, 'utf8')]);
const cases = JSON.parse(readFileSync(casesPath, 'utf8'));

// The core fragment is the page's bootstrap: it reads a substituted `__TOKEN__`
// and grabs its elements at top level. Give it what the page gives it -- a
// token and a DOM that answers -- rather than skipping the fragment and
// retyping its helpers. Nothing here is under test; the panel is.
const noop = () => {};
const el = () => ({
  innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
  classList: { add: noop, remove: noop, contains: () => false, toggle: noop },
  appendChild: (c) => c, addEventListener: noop, removeEventListener: noop,
  querySelector: () => null, querySelectorAll: () => [], focus: noop, click: noop,
  setAttribute: noop, getAttribute: () => null, remove: noop,
  getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, width: 0 }),
  scrollIntoView: noop, children: [],
});
const doc = {
  getElementById: () => el(), createElement: () => el(),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener: noop, removeEventListener: noop,
  body: el(), documentElement: el(),
};
const sandbox = {
  __TOKEN__: 'test-token',
  document: doc,
  console,
  window: undefined,          // set to the sandbox itself below
  location: { href: 'http://127.0.0.1/', search: '', hash: '', pathname: '/' },
  history: { replaceState: noop, pushState: noop },
  localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  navigator: { userAgent: 'node', clipboard: { writeText: async () => {} } },
  matchMedia: () => ({ matches: false, addEventListener: noop, addListener: noop }),
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' }),
  setTimeout: noop, clearTimeout: noop, setInterval: noop, clearInterval: noop,
  requestAnimationFrame: noop, cancelAnimationFrame: noop,
  EventSource: function EventSource() { return { close: noop, addEventListener: noop }; },
  addEventListener: noop, removeEventListener: noop,
  alert: noop, confirm: () => false, prompt: () => null,
  __CASE: null,
  __OUT: null,
};
const ctx = vm.createContext(sandbox);
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
for (const [path, src] of sources) {
  vm.runInContext(src, ctx, { filename: path.replace(/^.*[\\/]/, '') });
}

// One probe script, re-run per case. Declared with `var` so re-running it in
// the same context does not trip a redeclaration error.
const PROBE = `
  PLUG_INV = __CASE.inv;
  PLUG_STATS = __CASE.stats;
  PLUG_STATUS = __CASE.status || {};
  PLUG_REC = __CASE.rec || {coverage: {}, recommendations: []};
  PLUG_SORT = 'status'; PLUG_DIR = 'asc';
  var __entries = plugSkillEntries();
  __OUT = {
    entries: __entries.map(e => ({name: e.name, status: e.status,
                                  measured: e.measured, estimated: e.estimated})),
    sorted: __entries.slice().sort(plugCompare).map(e => e.name),
    dots: Object.fromEntries(['ok','fail','unobserved','uncalled']
      .map(s => [s, statusDot(s)])),
    strip: plugStrip(),
    legend: plugLegend(),
    rank: PLUG_STATUS_RANK,
  };
`;

const report = {};
for (const [label, spec] of Object.entries(cases)) {
  sandbox.__CASE = spec;
  vm.runInContext(PROBE, ctx, { filename: 'probe.js' });
  report[label] = sandbox.__OUT;
}
process.stdout.write(JSON.stringify(report));
