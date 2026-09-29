/**
 * Runs the job pane's own text builder -- `jobText`, the exact bytes lifted
 * out of `dashboard_js_actions.JS`, handed over on argv, never retyped here.
 *
 * `jobText` is a pure function of a job snapshot, an output tail and a dropped
 * count, so it needs none of the DOM stub `paint_guard.test.mjs` builds: a vm
 * context with nothing in it is enough. The Python side slices the function
 * and the one constant it reads (`JOB_TAIL_LINES`) out of the fragment, so
 * the boot IIFE and the poll timer at the fragment's tail never run here.
 *
 * Prints one JSON object of label -> rendered text (or `THREW: <message>`).
 * Every verdict lives on the Python side, so a new case is a Python edit
 * rather than a change in two files.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [srcPath, casesPath] = process.argv.slice(2);
const source = readFileSync(srcPath, 'utf8');
const cases = JSON.parse(readFileSync(casesPath, 'utf8'));

const context = vm.createContext({});
vm.runInContext(source + '\nglobalThis.jobText = jobText;',
                context, { filename: 'jobbar.js' });

// JSON cannot carry NaN or an infinity; the cases spell them as strings and
// they become the numbers here, so the pane meets the values it would meet
// from a malformed snapshot rather than a string that merely names them.
function revive(job) {
  const out = { ...job };
  if (out.progress_pct === 'NaN') out.progress_pct = NaN;
  if (out.progress_pct === 'Infinity') out.progress_pct = Infinity;
  if (out.progress_pct === '-Infinity') out.progress_pct = -Infinity;
  return out;
}

const out = {};
for (const c of cases) {
  try {
    out[c.label] = context.jobText(revive(c.job), c.output || '', c.dropped || 0);
  } catch (e) {
    out[c.label] = 'THREW: ' + (e && e.message);
  }
}
process.stdout.write(JSON.stringify(out));
