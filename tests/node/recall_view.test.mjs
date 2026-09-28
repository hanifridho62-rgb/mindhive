/*
 * tests/node/recall_view.test.mjs — the Recall view's failure path, EXECUTED.
 *
 * Driven by `tests/test_recall_view_node.py` (marker `node`, deselected from
 * the default suite — see `tests/test_paint_guard_node.py` for why).
 *
 * `/api/recall` answers a failed search with `{ok: false, error}`, and `apiGet`
 * never looks at the status, so `runRecall` has to tell an error body from a
 * hit list itself. `tests/test_recall.py` checks the guard's TEXT; this runs
 * the function the page ships, with `apiGet` answering each shape the server
 * can answer, and records every paint and announcement. A broken index must
 * end as a FAILED run -- "Recall failed" painted once, and the run announced
 * "— failed" -- never as a text paint under a run announced "— ready", the
 * status chip reading ready over an error.
 *
 * `run()` is emulated in the one part a render depends on, the part
 * `tests/test_dashboard.py` pins in the real one: a render that throws is
 * painted by `fail(e)` and announced failed; one that returns is announced
 * ready.
 *
 * The mutants: every run also executes the guard painting by hand instead of
 * throwing (the shape it had), and no guard at all, and REQUIRES the failure
 * case to go wrong in each. A case that passes with and without the thing it
 * tests is measuring nothing.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [FN_PATH] = process.argv.slice(2);
const RUN_RECALL = readFileSync(FN_PATH, 'utf8');

/** Run `runRecall` once with `apiGet` answering `answer`; report the paints. */
async function search(src, answer) {
  const painted = [];
  const announced = [];
  const runs = [];
  const context = vm.createContext({
    document: { getElementById: (id) => ({ q: { value: 'postgres' },
                                           sem: { checked: true } })[id] },
    encodeURIComponent,
    esc: (s) => String(s),
    showText: (t) => painted.push({ kind: 'text', value: String(t) }),
    showHTML: (h) => painted.push({ kind: 'html', value: String(h) }),
    apiGet: async () => answer,
    // This search is never superseded, so current() always answers true.
    run: (label, fn) => {
      runs.push((async () => {
        try {
          await fn(() => true);
          announced.push(label + ' — ready');
        } catch (e) {
          painted.push({ kind: 'fail', value: (e && e.message) || String(e) });
          announced.push(label + ' — failed');
        }
      })());
    },
  });
  vm.runInContext(src, context, { filename: 'runRecall.js' });
  vm.runInContext('runRecall()', context);
  await Promise.all(runs);
  return { painted, announced };
}

const GUARD = /if \(!Array\.isArray\(hits\)\) throw new Error\(([^\n]*)\);/;

/** The guard as it was: the failure painted by hand, the render returning. */
function paintingTheFailure(src) {
  const out = src.replace(GUARD, 'if (!Array.isArray(hits)) { showText($1); return; }');
  return { src: out, changed: out !== src };
}

function withoutTheGuard(src) {
  const out = src.replace(GUARD, '');
  return { src: out, changed: out !== src };
}

const cases = [];
const record = (name, ok, detail) => cases.push({ name, ok: !!ok, detail });

/** Exactly one paint, of this kind, starting with this text. */
function onePaint(r, kind, start) {
  return r.painted.length === 1 && r.painted[0].kind === kind &&
    r.painted[0].value.startsWith(start);
}

/** The run ended as `outcome`, and said so once. */
const ended = (r, outcome) =>
  r.announced.length === 1 && r.announced[0] === 'searching — ' + outcome;

/** One failure paint starting with `start`, under a run announced failed. */
const failedWith = (r, start) => onePaint(r, 'fail', start) && ended(r, 'failed');

const describe = (r) => 'painted ' + JSON.stringify(r.painted) +
  ', announced ' + JSON.stringify(r.announced);

const ERROR_BODY = { ok: false, error: "semantic count failed on the 'events' index" };
const FAILED = "Recall failed: semantic count failed on the 'events' index";

async function main() {
  const failed = await search(RUN_RECALL, ERROR_BODY);
  record('a_failed_search_says_it_failed', failedWith(failed, FAILED),
         describe(failed));

  const silent = await search(RUN_RECALL, null);
  record('a_search_with_no_answer_says_it_failed',
         failedWith(silent, 'Recall failed: no answer from the server'),
         describe(silent));

  const none = await search(RUN_RECALL, []);
  record('an_empty_search_says_no_matches',
         onePaint(none, 'text', 'No matches for "postgres".') && ended(none, 'ready'),
         describe(none));

  const hits = await search(RUN_RECALL, [{ file: 'decisions.md', snippet: 'chose postgres' }]);
  record('hits_are_listed',
         onePaint(hits, 'html', '<h2 class="h">1 result</h2>') &&
           hits.painted[0].value.includes('chose postgres') && ended(hits, 'ready'),
         describe(hits));

  // HIVE MIND: a namespace whose index could not be read answers
  // {project, error} in place of its hits; the others' hits still stand.
  const partial = await search(RUN_RECALL, [
    { project: 'bad', error: "semantic count failed on the 'events' index of project 'bad'" },
    { project: 'good', file: 'decisions.md', snippet: 'chose postgres' }]);
  const p = partial.painted[0] || { value: '' };
  record('a_partial_failure_keeps_the_hits_and_names_the_namespace',
         onePaint(partial, 'html', '<div class="err">Could not search bad: ') &&
           p.value.includes('<h2 class="h">1 result</h2>') &&
           p.value.includes('chose postgres') && ended(partial, 'ready'),
         describe(partial));

  const onlyFailed = await search(RUN_RECALL, [
    { project: 'bad', error: "semantic count failed on the 'events' index of project 'bad'" }]);
  const o = onlyFailed.painted[0] || { value: '' };
  record('a_failure_beside_no_matches_says_both',
         onePaint(onlyFailed, 'html', '<div class="err">Could not search bad: ') &&
           o.value.includes('No matches for "postgres"') && ended(onlyFailed, 'ready'),
         describe(onlyFailed));

  const byHand = paintingTheFailure(RUN_RECALL);
  const gone = withoutTheGuard(RUN_RECALL);
  record('the_guard_is_mutable', byHand.changed && gone.changed,
         byHand.changed ? 'found the throwing guard'
                        : 'no `throw new Error(...)` Array.isArray guard found — the ' +
                          'failure cases above cannot be made to fail, so treat them ' +
                          'as unproven');
  if (byHand.changed) {
    const mf = await search(byHand.src, ERROR_BODY);
    record('painting_the_failure_by_hand_announces_ready',
           !failedWith(mf, FAILED),
           failedWith(mf, FAILED)
             ? 'painted by hand and the run still ended failed — the case above ' +
               'does not discriminate'
             : 'painted by hand → ' + describe(mf));
  }
  if (gone.changed) {
    const mg = await search(gone.src, ERROR_BODY);
    record('without_the_guard_the_error_is_lost',
           !failedWith(mg, FAILED),
           failedWith(mg, FAILED)
             ? 'guard removed and the server\'s error still reached the panel — ' +
               'the case above does not discriminate'
             : 'guard removed → ' + describe(mg));
  }
}

try {
  await main();
} catch (e) {
  process.stdout.write(JSON.stringify({ harness_error: String(e && e.stack || e) }));
  process.exit(1);
}
process.stdout.write(JSON.stringify({ cases }));
process.exit(cases.every((c) => c.ok) ? 0 : 1);
