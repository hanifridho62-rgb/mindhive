/*
 * tests/node/graph_banner.test.mjs — the Graph page's semantic-failure banner,
 * EXECUTED.
 *
 * Driven by `tests/test_graph_banner_node.py` (marker `node`, deselected from
 * the default suite — see `tests/test_paint_guard_node.py` for why).
 *
 * `tests/test_graphview.py` checks the banner's TEXT in the renderer; this runs
 * the block the page ships against a recording `document` and checks what
 * reaches the stage: a failed semantic build puts exactly one `#semerr` alert
 * there, its message set as text (a `<b>` in it stays the characters `<b>`),
 * and a build that did not fail puts nothing.
 *
 * The mutant: every run also executes a copy with the `appendChild` removed and
 * REQUIRES the failure case to go wrong there. A case that passes with and
 * without the thing it tests is measuring nothing.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [BLOCK_PATH] = process.argv.slice(2);
const BLOCK = readFileSync(BLOCK_PATH, 'utf8');

/** An element that records what the block does to it. */
function element(tag) {
  const el = {
    tag, id: '', attrs: {}, children: [], text: null, markup: null,
    setAttribute(k, v) { this.attrs[k] = String(v); },
    appendChild(c) { this.children.push(c); return c; },
  };
  Object.defineProperty(el, 'textContent', {
    set(v) { el.text = String(v); }, get() { return el.text; } });
  Object.defineProperty(el, 'innerHTML', {
    set(v) { el.markup = String(v); }, get() { return el.markup; } });
  return el;
}

/** Run the block once with `data`; report what the stage ended up holding. */
function render(src, data) {
  const stage = element('div');
  const context = vm.createContext({
    data,
    document: {
      createElement: (tag) => element(tag),
      getElementById: (id) => (id === 'stage' ? stage : null),
    },
  });
  let threw = null;
  try { vm.runInContext(src, context, { filename: 'banner.js' }); }
  catch (e) { threw = String(e); }
  return { stage, threw };
}

function withoutTheAppend(src) {
  const out = src.replace(/[^\n]*appendChild\(semerr\);[^\n]*\n?/, '');
  return { src: out, changed: out !== src };
}

const cases = [];
const record = (name, ok, detail) => cases.push({ name, ok: !!ok, detail });
const describe = (r) => r.threw ? 'threw ' + r.threw
  : 'stage holds ' + JSON.stringify(r.stage.children.map(
      (c) => ({ id: c.id, attrs: c.attrs, text: c.text, markup: c.markup })));

const MESSAGE = "semantic query failed on the 'ledger' index: <b>hnsw</b>";

/** Exactly one alert on the stage, carrying `message` as text and no markup. */
function oneAlert(r, message) {
  const kids = r.stage.children;
  return !r.threw && kids.length === 1 && kids[0].id === 'semerr' &&
    kids[0].attrs.role === 'alert' && kids[0].markup === null &&
    kids[0].text === 'Semantic links unavailable: ' + message;
}

const failed = render(BLOCK, { nodes: [], edges: [], semantic_error: MESSAGE });
record('a_failed_build_shows_its_error_as_text', oneAlert(failed, MESSAGE),
       describe(failed));

const fine = render(BLOCK, { nodes: [], edges: [] });
record('a_build_that_did_not_fail_shows_nothing',
       !fine.threw && fine.stage.children.length === 0, describe(fine));

const m = withoutTheAppend(BLOCK);
record('the_banners_append_is_removable', m.changed,
       m.changed ? 'found the appendChild in the block'
                 : 'no `appendChild(semerr);` in the block — the failure case ' +
                   'above cannot be made to fail, so treat it as unproven');
if (m.changed) {
  const mf = render(m.src, { nodes: [], edges: [], semantic_error: MESSAGE });
  record('without_the_append_nothing_is_shown', !oneAlert(mf, MESSAGE),
         oneAlert(mf, MESSAGE)
           ? 'append removed and the alert still reached the stage — the case ' +
             'above does not discriminate'
           : 'append removed → ' + describe(mf));
}

process.stdout.write(JSON.stringify({ cases }));
process.exit(cases.every((c) => c.ok) ? 0 : 1);
