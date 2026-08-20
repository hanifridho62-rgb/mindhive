/**
 * Runs the dashboard's own clock helper -- the exact bytes of
 * `dashboard_js_core.CLOCK_JS`, handed over on argv, never retyped here.
 *
 * Both clock forms are pure functions of a string, so they need none of the
 * DOM stub
 * `paint_guard.test.mjs` builds: a vm context with nothing in it is enough, and
 * that is the whole reason the helper was pulled out into its own constant.
 *
 * Prints one JSON object of input -> output. Every verdict lives on the Python
 * side, so a new case is a Python edit rather than a change in two files.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [srcPath, casesPath] = process.argv.slice(2);
const source = readFileSync(srcPath, 'utf8');
const cases = JSON.parse(readFileSync(casesPath, 'utf8'));

const context = vm.createContext({});
vm.runInContext(source
                + '\nglobalThis.fmtTs = fmtTs; globalThis.fmtHm = fmtHm;',
                context, { filename: 'clock.js' });

const out = {};
for (const c of cases) {
  // The label, not the value: `null` and the empty string are both cases and
  // both would collapse to the same JSON key.
  try {
    // The case names its own clock form. Defaulting would let a
    // mistyped `fn` silently test fmtTs twice and report green.
    const fn = context[c.fn];
    if (typeof fn !== 'function') {
      out[c.label] = 'NO SUCH FN: ' + c.fn;
      continue;
    }
    out[c.label] = fn(c.input);
  } catch (e) {
    out[c.label] = 'THREW: ' + (e && e.message);
  }
}
process.stdout.write(JSON.stringify(out));
