/**
 * Runs the heartbeat fragment -- the exact bytes `server/js.heartbeat_script`
 * produces, handed over on argv, never retyped here.
 *
 * The fragment is not a pure function like `clock.test.mjs`'s: it is an IIFE
 * that reaches for `fetch`, `Worker`, `Blob`, `URL` and the page's own `u()`
 * and `TOKEN`. So this harness builds those, runs the fragment in a fresh vm
 * context per scenario, then drives the seams the browser would drive -- the
 * worker's `onmessage` tick, its `onerror`, the fallback interval -- and
 * records what the page DID.
 *
 * Every verdict lives on the Python side. This file only observes.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [srcPath, casesPath] = process.argv.slice(2);
const source = readFileSync(srcPath, 'utf8');
const cases = JSON.parse(readFileSync(casesPath, 'utf8'));

function run(spec) {
  const rec = {
    fetches: [],        // one entry per beat the page attempted
    intervals: [],      // ms of every main-thread setInterval it registered
    workerSource: null, // the script it handed the Worker
    workerUrl: null,
    threw: null,        // a syntax error, or anything the IIFE let escape
  };
  const timers = [];
  const workers = [];

  function WorkerStub(url) {
    if (spec.noWorker) { throw new Error('Worker is not available'); }
    rec.workerUrl = url;
    workers.push(this);
  }

  const sandbox = {
    TOKEN: spec.token,
    // The page's own helper, in the shape `dashboard_js_core` defines it.
    u: (path) => path + '?token=' + encodeURIComponent(spec.token),
    setInterval: (fn, ms) => { rec.intervals.push(ms); timers.push(fn);
                               return timers.length; },
    clearInterval: () => {},
    // Present and TRUE in the hidden scenario: a page that consults this is
    // the bug the Python side asserts against.
    document: { hidden: !!spec.hidden },
    Blob: function (parts, opts) {
      this.parts = parts;
      this.type = opts && opts.type;
    },
    URL: {
      createObjectURL: (blob) => {
        rec.workerSource = (blob.parts || []).join('');
        return 'blob:stub-url';
      },
    },
    Worker: WorkerStub,
    fetch: (url, init) => {
      rec.fetches.push({
        url,
        method: init && init.method,
        keepalive: init && init.keepalive,
        body: init && init.body,
        headers: Object.assign({}, (init && init.headers) || {}),
      });
      if (spec.fetchThrows) { throw new Error('fetch exploded'); }
      if (spec.fetchRejects) { return Promise.reject(new Error('offline')); }
      return Promise.resolve({ ok: true });
    },
  };

  const context = vm.createContext(sandbox);
  try {
    vm.runInContext(source, context, { filename: 'heartbeat.js' });
  } catch (e) {
    rec.threw = 'LOAD: ' + (e && e.message);
    return rec;
  }
  rec.afterLoad = rec.fetches.length;

  try {
    for (let i = 0; i < (spec.fireOnError || 0); i++) {
      if (workers.length) { workers[0].onerror(); }
    }
    rec.afterOnError = rec.fetches.length;
    for (let i = 0; i < (spec.fireOnMessage || 0); i++) {
      if (workers.length) { workers[0].onmessage(); }
    }
    rec.afterOnMessage = rec.fetches.length;
    for (let i = 0; i < (spec.fireTimers || 0); i++) {
      for (const fn of timers) { fn(); }
    }
    rec.afterTimers = rec.fetches.length;
  } catch (e) {
    rec.threw = 'DRIVE: ' + (e && e.message);
  }
  rec.workerCount = workers.length;
  return rec;
}

const out = {};
for (const spec of cases) { out[spec.name] = run(spec); }
process.stdout.write(JSON.stringify(out));
