# tests/test_static_dashboard_js.py — blueprint P5 Pillar 4: `static/dashboard.js`.
"""The paint-signature helper and the latched poller, as one static file.

`src/mindhive/static/dashboard.js` defines `window.MindHive = { paintInto,
pollOverview, liveInFlight, catsInFlight }` (spec §10.5.3) and is reachable
two ways, both pinned here:

* served on its own at `GET /static/dashboard.js`, a v1 route
  (`dashboard_get_v1.GetRoutesV1`) behind the same token check as every route
  but `/`;
* inlined at the START of the page's one `<script>` block. NOT a second
  `<script src>`: `tests/test_paint_guard_node.py` splits the page on exactly
  one script block and would silently test half of it, so the page stays one
  block and the file is read once and spliced in -- read through
  `importlib.resources`, never through a path computed from `__file__`, the
  rule `pgmirror` set for its migrations (the frozen build has no source tree).

Inlined first means it shares one scope with every page fragment after it, and
that shapes the file: it declares nothing at the top level (a `let
liveInFlight` there is a SyntaxError against the core's own, and a top-level
`function paintInto` is silently replaced by the core's hoisted one), it touches
no DOM while loading, and it carries none of the strings the page's source
probes and the node harness's mutants search for -- being first, it would be
found first.

The spec's `pollOverview` cannot run on this page as written: it fetches
`/api/overview` with no token (this server answers 403), paints an
`#overview-container` and calls a `renderOverviewView` that do not exist, signs
with `project_name`/`head_hash`/`event_count`, which the payload does not carry
(the signature would be the constant "undefined_undefined_undefined", so the
first paint would be the last), and starts itself with `setInterval` at load. So
here the caller starts it and supplies what the page cannot guess:
`pollOverview(intervalMs = 2500, {target, render, headers, url})`, returning a
`stop()`; the element is looked up by id at each tick, `url` defaults to
`/api/overview`, and the signature is derived from what the poll would paint.

The executed cases need node and carry the `node` marker (deselected by
default; `pyproject.toml` explains why that fence is not cosmetic). Run them
deliberately:

    pytest -m node tests/test_static_dashboard_js.py
"""
import glob
import json
import re
import shutil
import subprocess
import threading
import tomllib
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import pytest

from mindhive import dashboard, dashboard_page, taskview
from mindhive.dashboard_get import GetRoutes
from mindhive.dashboard_get_v1 import GetRoutesV1

REPO = Path(__file__).resolve().parents[1]
PKG_DIR = REPO / "src" / "mindhive"
STATIC = PKG_DIR / "static" / "dashboard.js"
ROUTE = "/static/dashboard.js"
TOKEN = "test-token-static"
SPDX = "// SPDX-License-Identifier: Apache-2.0"
NODE_TIMEOUT_S = 60


def _static_js() -> str:
    assert STATIC.is_file(), f"{STATIC.relative_to(REPO).as_posix()} does not exist"
    return STATIC.read_text(encoding="utf-8")


def _page() -> str:
    return dashboard_page.render_page("test-token", tasks_css=taskview.CARDS_CSS,
                                      tasks_js=taskview.CARDS_JS)


def _page_script() -> str:
    page = _page()
    assert page.count("<script>") == 1 and page.count("</script>") == 1
    return page.split("<script>", 1)[1].split("</script>", 1)[0]


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(TOKEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _fetch(base, path, token=TOKEN):
    """(status, content-type, body) -- an error status is an answer too."""
    url = base + path
    if token is not None:
        url += ("&" if "?" in path else "?") + "token=" + token
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read().decode("utf-8")


# --- the file ------------------------------------------------------------------

def test_the_file_opens_with_its_licence_line():
    assert _static_js().splitlines()[0] == SPDX


def test_the_file_is_reachable_through_the_package():
    """Through `importlib.resources`, the way an installed build finds it."""
    ref = resources.files("mindhive") / "static" / "dashboard.js"
    assert ref.is_file(), "mindhive/static/dashboard.js is not in the package"
    assert ref.read_text(encoding="utf-8") == _static_js()


# --- how the page reads it -------------------------------------------------------

_PROBE_TEXT = "/* what the fake package root answers */"


class _FakeRoot:
    """`importlib.resources.files(...)` inside `_probe`: a package root that
    answers every read with `_PROBE_TEXT` and records where it was asked."""

    def __init__(self, reads, where):
        self._reads, self._where = reads, where

    def joinpath(self, *names):
        return _FakeRoot(self._reads, "/".join([self._where, *map(str, names)]))

    __truediv__ = joinpath

    def read_text(self, encoding=None):
        self._reads.append((self._where, encoding))
        return _PROBE_TEXT


def _probe(module_path, monkeypatch):
    """Execute *module_path* as a fresh module of the `mindhive` package while
    `importlib.resources.files` is `_FakeRoot`; return (module, reads). The
    module the rest of the suite imported is untouched: this is a second module
    object under its own name."""
    import importlib.util
    reads = []
    monkeypatch.setattr(resources, "files", lambda anchor: _FakeRoot(
        reads, getattr(anchor, "__name__", anchor).replace(".", "/")))
    spec = importlib.util.spec_from_file_location("mindhive._static_js_probe", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, reads


def test_the_page_reads_the_file_once_through_importlib_resources(monkeypatch):
    """Executed, not grepped: the page module is run against a fake package root
    and must inline what the ROOT hands it -- a reader that computes its path
    from `__file__` never asks and inlines the checkout's copy instead. Once, at
    import, as UTF-8: two renders, one read."""
    module, reads = _probe(PKG_DIR / "dashboard_page.py", monkeypatch)
    for _ in range(2):
        script = module.render_page("test-token").split("<script>", 1)[1]
        assert script.lstrip().startswith(_PROBE_TEXT)
    assert reads == [("mindhive/static/dashboard.js", "utf-8")]


def test_the_probe_would_catch_a_module_that_reads_through_dunder_file(tmp_path,
                                                                       monkeypatch):
    """CANARY for the test above: a `Path(__file__)` reader, probed the same
    way, gets the file on disk and leaves the fake root unasked."""
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "dashboard.js").write_text("/* the copy on disk */",
                                                      encoding="utf-8")
    reader = tmp_path / "reader.py"
    reader.write_text("from pathlib import Path\n"
                      "DASHBOARD_JS = (Path(__file__).parent / 'static' / 'dashboard.js')"
                      ".read_text(encoding='utf-8')\n", encoding="utf-8")
    module, reads = _probe(reader, monkeypatch)
    assert (module.DASHBOARD_JS, reads) == ("/* the copy on disk */", [])


def test_neither_module_that_holds_the_file_names_dunder_file():
    """The mechanism, structurally, as `tests/test_pg_packaging.py` asserts it
    for the migrations: a `Path(__file__)` fallback BESIDE the resources read
    passes the probe in a checkout and still hands the frozen build a path into
    nothing. Over the AST, so a docstring that names the rule is not the rule
    broken."""
    import ast
    for name in ("dashboard_page.py", "dashboard_get_v1.py"):
        tree = ast.parse((PKG_DIR / name).read_text(encoding="utf-8"))
        assert "__file__" not in {n.id for n in ast.walk(tree)
                                  if isinstance(n, ast.Name)}, name


# --- the route -----------------------------------------------------------------

def test_the_route_serves_the_file_as_javascript(server):
    status, ctype, body = _fetch(server, ROUTE)
    assert status == 200, body
    media, _, params = (ctype or "").partition(";")
    assert media.strip() == "application/javascript", ctype
    assert "charset=utf-8" in params.replace(" ", "").lower(), ctype
    assert body == _static_js()
    assert "dataset.paint" in body


def test_the_route_refuses_a_missing_or_wrong_token_exactly_as_the_api_does(server):
    """REGRESSION PIN before the route exists (the token check runs ahead of
    route lookup); after, it catches a static branch placed ahead of that
    check, which would hand the file to any local page that asked."""
    forbidden = {"ok": False, "error": "forbidden"}
    baseline = _fetch(server, "/api/state", token=None)          # the canary
    assert (baseline[0], json.loads(baseline[2])) == (403, forbidden)
    for token in (None, "not-the-token"):
        status, _ctype, body = _fetch(server, ROUTE, token=token)
        assert (status, json.loads(body)) == (403, forbidden), (token, body)


def test_the_route_is_a_v1_route_and_only_a_v1_route():
    assert ROUTE in GetRoutesV1._GET_ROUTES_V1
    assert ROUTE not in GetRoutes._GET_ROUTES
    assert ROUTE in dashboard.make_handler("t")._GET_ROUTES


# --- the page ------------------------------------------------------------------

def test_the_page_defines_the_namespace():
    assert "window.MindHive" in _page()


def test_the_page_still_has_exactly_one_script_block():
    """REGRESSION PIN: the file arrives inside the block, never as a second."""
    page = _page()
    assert page.count("<script") == 1
    assert page.count("</script>") == 1


def test_the_file_is_inlined_verbatim_at_the_start_of_that_block():
    js, body = _static_js().strip(), _page_script()
    assert body.lstrip().startswith(js), \
        "the page's script does not open with static/dashboard.js, byte for byte"
    assert body.count(js) == 1


# JavaScript ends a `//` comment at ANY of these, and the HTML parser turns a
# lone CR into LF before the script engine sees it. A `\r` written inside a
# non-raw Python string is therefore not an escape in the page: it is a line
# break that ends the comment early and hands the rest of the line to the
# parser as code.
_JS_LINE_TERMINATORS = {"\r": "CR", " ": "LINE SEPARATOR",
                        " ": "PARAGRAPH SEPARATOR"}


def _bare_line_terminators(script) -> list:
    return [f"{name} on script line {script.count(chr(10), 0, at) + 1}: "
            f"{(script[max(0, at - 40):at].splitlines() or [''])[-1].strip()!r}"
            for ch, name in _JS_LINE_TERMINATORS.items()
            for at in (m.start() for m in re.finditer(re.escape(ch), script))]


def test_the_page_script_carries_no_bare_line_terminator():
    """The whole page is ONE script, so one stray terminator is not a local
    bug: it made every line of the dashboard's JavaScript unparseable (a
    `\\r` in a comment of `dashboard_js_actions.JS`, P1h), which the default
    suite could not see because nothing in it runs the script."""
    assert _bare_line_terminators(_page_script()) == []


def test_the_terminator_check_would_catch_an_escape_that_leaked():
    """CANARY for the test above."""
    script = "  const a = 1;\n  // a `\r` frame\n  const b = 2;"
    assert _bare_line_terminators(script) == ["CR on script line 2: '// a `'"]


# --- what being inlined first forbids -------------------------------------------

_PLACEHOLDERS = ("__TOKEN__", "/*__", "//__", "<!--__")
_FN_DECL = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")
# `tests/test_dashboard.py`'s own probes, copied: a two-space `function` is a
# top-level page function to `_js_block`/`_enclosing_function`, and a pane write
# outside a registered writer fails its paint-stamp census.
_TOP_LEVEL_FN_RE = re.compile(r"\n  (?:async )?function (\w+)\s*\(")
_PANE_WRITE_RE = re.compile(
    r"(?:\b(?:out|view)|getElementById\(\s*['\"](?:out|view)['\"]\s*\))"
    r"\.(?:innerHTML|textContent)\s*=[^=]")


def _hazards(js, rest) -> list:
    """Why `js`, spliced ahead of `rest`, would break the page or its probes."""
    found = [f"placeholder {p}" for p in _PLACEHOLDERS if p in js]
    found += [f"tag {t}" for t in ("<script", "</script") if t in js.lower()]
    if "var(--" in js or re.search(r"^\s*--[a-z0-9-]+:", js, re.M):
        found.append("a CSS custom property the theme audit would count")
    found += [f"declares {n}(), which the page declares too"
              for n in sorted(set(_FN_DECL.findall(js)) & set(_FN_DECL.findall(rest)))]
    found += [f"two-space function {n}()" for n in _TOP_LEVEL_FN_RE.findall(js)]
    if _PANE_WRITE_RE.search(js):
        found.append("writes a pane (out/view) directly")
    return found


def test_the_inlined_file_is_safe_to_splice_ahead_of_the_page():
    js = _static_js()
    rest = _page_script().replace(js.strip(), "", 1)
    assert _hazards(js, rest) == []


def test_the_splice_check_would_catch_a_file_that_looked_like_page_code():
    """CANARY for the test above."""
    bad = ("(function () {\n  function fail(e){ out.textContent = String(e); }\n"
           "  const t = __TOKEN__; /* var(--line) */\n})();")
    assert _hazards(bad, "function fail(e){}") == [
        "placeholder __TOKEN__",
        "a CSS custom property the theme audit would count",
        "declares fail(), which the page declares too",
        "two-space function fail()",
        "writes a pane (out/view) directly",
    ]


_HARNESS = Path(__file__).parent / "node" / "paint_guard.test.mjs"
_ANCHOR_RE = re.compile(r"(?:const from = |\.replace\(\s*)(['\"])(.+?)\1")
# The regex mutants key on these identifiers rather than on a fixed string.
_ANCHOR_IDENTIFIERS = ("liveFn", "liveOn", "shouldPaint")


def _harness_anchors() -> list:
    return sorted({m.group(2) for m in _ANCHOR_RE.finditer(
        _HARNESS.read_text(encoding="utf-8"))})


def test_the_inlined_file_contains_no_anchor_the_node_harness_mutates():
    """The harness mutates the page with `String.replace`, which rewrites the
    FIRST occurrence. Inlined at the top, a copy of an anchor here would take
    the mutation and leave the real guard standing: every mutant would pass."""
    js = _static_js()
    assert [a for a in _harness_anchors() if a in js] == []
    assert [i for i in _ANCHOR_IDENTIFIERS if re.search(r"\b%s\b" % i, js)] == []


def test_the_anchor_list_is_read_from_the_harness_and_every_anchor_is_real():
    """CANARY for the test above: an extractor that found nothing would pass it."""
    anchors = _harness_anchors()
    assert len(anchors) >= 9, anchors
    script = _page_script()
    assert [a for a in anchors if a not in script] == []
    for ident in _ANCHOR_IDENTIFIERS:
        assert re.search(r"\b%s\b" % ident, script), ident


def _code_lines(js) -> list:
    return [s for s in (line.strip() for line in js.splitlines())
            if len(s) >= 24 and not s.startswith(("//", "/*", "*"))]


def _copied(js, text) -> int:
    return sum(1 for line in _code_lines(js) if line in text)


def test_no_python_module_carries_a_second_copy_of_the_file():
    """One home. The page reads the file; a Python constant holding the same
    JS would be the copy that drifts while the tests read the other one."""
    js = _static_js()
    assert len(_code_lines(js)) >= 3, "too few code lines to recognise a copy"
    # the canary: a module that pasted the file is recognised as one
    assert _copied(js, 'JS = """\n' + js + '\n"""') == len(_code_lines(js))
    copies = {p.relative_to(PKG_DIR).as_posix(): n for p in PKG_DIR.rglob("*.py")
              if (n := _copied(js, p.read_text(encoding="utf-8", errors="replace"))) >= 3}
    assert copies == {}


# --- packaging -----------------------------------------------------------------

def _shipped() -> set:
    """What setuptools would ship from `mindhive`'s directory: each declared
    pattern globbed against it, exactly as `package-data` is resolved -- a
    `*` that does not cross a `/`, which `fnmatch` would let through."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8")) \
        ["tool"]["setuptools"].get("package-data", {})
    patterns = list(data.get("mindhive", [])) + list(data.get("*", []))
    return {Path(hit).as_posix() for pattern in patterns
            for hit in glob.glob(pattern, root_dir=PKG_DIR, recursive=True)}


def test_the_static_script_is_declared_as_package_data():
    """`packages.find` ships `.py` files only; without this the wheel carries a
    page that inlines a file it does not have."""
    assert "static/dashboard.js" in _shipped()


def test_every_script_under_static_is_shipped():
    on_disk = sorted(p.relative_to(PKG_DIR).as_posix()
                     for p in (PKG_DIR / "static").glob("*.js"))
    assert on_disk == ["static/dashboard.js"]
    assert [n for n in on_disk if n not in _shipped()] == []


# --- executed under node ---------------------------------------------------------

HARNESS_JS = r"""// Written by tests/test_static_dashboard_js.py for one run; never committed.
import { existsSync, readFileSync } from 'node:fs';
import vm from 'node:vm';

const [STATIC_PATH, PAGE_PATH] = process.argv.slice(2);
const SRC = existsSync(STATIC_PATH) ? readFileSync(STATIC_PATH, 'utf8') : null;
const report = {};

const PROBE = ['paintInto', 'pollOverview', 'liveInFlight', 'catsInFlight'];
const LEAK_PROBE = 'JSON.stringify(' + JSON.stringify(PROBE)
  + ".filter((n) => eval('typeof ' + n) !== 'undefined'))";

let serial = 0;

function makeWorld() {
  const log = { domReads: [], timers: [], cleared: [], fetches: [], writes: [], focus: [] };
  const els = new Map();
  let timerId = 0;
  function makeEl(id) {
    const node = { id, serial: ++serial, kids: new Set(), html: '' };
    // A DOMStringMap stores strings: a number goes in and '7' comes out.
    node.dataset = new Proxy({}, { set(t, k, v) { t[k] = String(v); return true; } });
    node.contains = (other) => other === node || node.kids.has(other);
    node.focus = () => { log.focus.push(node.serial); };
    Object.defineProperty(node, 'innerHTML', {
      get() { return node.html; },
      set(v) {
        node.html = String(v);
        log.writes.push(id);
        // Parsing replaces every descendant: an id in new markup names a NEW node.
        node.kids = new Set();
        for (const m of node.html.matchAll(/\bid="([^"]+)"/g)) {
          const child = makeEl(m[1]);
          els.set(m[1], child);
          node.kids.add(child);
        }
      },
    });
    return node;
  }
  const doc = {
    activeElement: null,
    getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  };
  // Every read the script makes of `document` is logged, so "no DOM access
  // while loading" is an observation, not a reading of the source.
  const documentSeen = new Proxy(doc, {
    get(t, k) { log.domReads.push(String(k)); return Reflect.get(t, k); },
  });
  const sandbox = {
    console: { log() {}, info() {}, debug() {}, warn() {}, error() {} },
    document: documentSeen,
    fetch(url, init) {
      const rec = { url: String(url), init: init || null };
      rec.promise = new Promise((resolve, reject) => { rec.resolve = resolve; rec.reject = reject; });
      log.fetches.push(rec);
      return rec.promise;
    },
    setInterval(fn, ms) { const id = ++timerId; log.timers.push({ id, fn, ms, kind: 'interval' }); return id; },
    clearInterval(id) { log.cleared.push(id); },
    setTimeout(fn, ms) { const id = ++timerId; log.timers.push({ id, fn, ms, kind: 'timeout' }); return id; },
    clearTimeout(id) { log.cleared.push(id); },
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(SRC, context, { filename: 'dashboard.js' });
  return { context, log, doc, run: (s) => vm.runInContext(s, context, { filename: 'probe.js' }) };
}

const headersOf = (type) => ({ get: (k) => (String(k).toLowerCase() === 'content-type' ? type : null) });
const answer = (payload) => ({ ok: true, status: 200, headers: headersOf('application/json'),
  json: async () => payload, text: async () => JSON.stringify(payload) });
const refusal = (status) => ({ ok: false, status, headers: headersOf('application/json'),
  json: async () => ({ ok: false, error: 'forbidden' }),
  text: async () => JSON.stringify({ ok: false, error: 'forbidden' }) });
// A macrotask: every microtask the page queued has run by the time it fires.
const settle = () => new Promise((resolve) => setImmediate(resolve));
const tokenOf = (rec) => {
  const h = rec && rec.init && rec.init.headers;
  if (!h) return null;
  return typeof h.get === 'function' ? h.get('X-Hive-Token') : (h['X-Hive-Token'] ?? null);
};

async function record(name, fn, needsStatic = true) {
  try {
    if (needsStatic && SRC === null) throw new Error('static file missing: ' + STATIC_PATH);
    report[name] = { ok: true, value: await fn() };
  } catch (e) {
    report[name] = { ok: false, error: String((e && e.stack) || e).split('\n').slice(0, 3).join(' | ') };
  }
}

await record('leak_canary', () => {
  const context = vm.createContext({});
  vm.runInContext('function paintInto() {}\nlet liveInFlight = false;', context);
  return JSON.parse(vm.runInContext(LEAK_PROBE, context));
}, false);

await record('load', () => {
  const w = makeWorld();
  const ns = w.context.MindHive;
  return {
    domReads: w.log.domReads.slice(), timers: w.log.timers.length, fetches: w.log.fetches.length,
    namespace: typeof ns,
    members: ns ? [typeof ns.paintInto, typeof ns.pollOverview, ns.liveInFlight, ns.catsInFlight] : null,
    leaked: JSON.parse(w.run(LEAK_PROBE)),
  };
});

await record('paint', () => {
  const w = makeWorld();
  const pane = w.doc.getElementById('pane');
  const paint = (html, sig) => w.context.MindHive.paintInto(pane, html, sig);
  const state = () => ({ html: pane.innerHTML, stamp: pane.dataset.paint ?? null, writes: w.log.writes.length });
  const first = paint('<b>one</b>', 'sig-1'); const afterFirst = state();
  const same = paint('<b>two</b>', 'sig-1'); const afterSame = state();
  const changed = paint('<b>three</b>', 'sig-2'); const afterChanged = state();
  return { first, afterFirst, same, afterSame, changed, afterChanged };
});

await record('numeric_signature', () => {
  const w = makeWorld();
  const pane = w.doc.getElementById('pane');
  const first = w.context.MindHive.paintInto(pane, '<b>a</b>', 7);
  const again = w.context.MindHive.paintInto(pane, '<b>b</b>', 7);
  return { first, again, writes: w.log.writes.length, stamp: pane.dataset.paint ?? null };
});

await record('no_target', () => {
  const w = makeWorld();
  const out = {};
  for (const [label, target] of [['null', null], ['undefined', undefined]]) {
    try { out[label] = { result: w.context.MindHive.paintInto(target, '<b>x</b>', 'sig'), threw: false }; }
    catch (e) { out[label] = { threw: String(e) }; }
  }
  out.writes = w.log.writes.length;
  return out;
});

await record('focus', () => {
  const w = makeWorld();
  const M = w.context.MindHive;
  const pane = w.doc.getElementById('pane');
  M.paintInto(pane, '<input id="q">', 'sig-1');
  const typedInto = w.doc.getElementById('q');
  w.doc.activeElement = typedInto;                 // the reader is typing in the pane
  M.paintInto(pane, '<input id="q"><p>more</p>', 'sig-2');
  const rebuilt = w.doc.getElementById('q');
  const inside = { calls: w.log.focus.slice(), rebuilt: rebuilt.serial, old: typedInto.serial };
  w.log.focus.length = 0;
  w.doc.activeElement = w.doc.getElementById('proj');   // focus outside the pane
  M.paintInto(pane, '<input id="q"><p>again</p>', 'sig-3');
  w.doc.activeElement = w.doc.getElementById('q');      // inside, but nothing changes
  M.paintInto(pane, '<input id="q"><p>again</p>', 'sig-3');
  return { inside, untouched: w.log.focus.slice() };
});

await record('poll', async () => {
  const w = makeWorld();
  const M = w.context.MindHive;
  const rendered = [];
  const stop = M.pollOverview(1000, {
    target: 'ov',
    url: '/api/overview?project=alpha',
    headers: { 'X-Hive-Token': 'tok-1' },
    render: (data) => { rendered.push(data.events); return '<p>' + data.events + ' events</p>'; },
  });
  const reqs = w.log.fetches;
  const out = {};
  out.started = { timers: w.log.timers.map((t) => [t.kind, t.ms]), fetches: reqs.length,
                  domReads: w.log.domReads.length, stop: typeof stop };
  const tick = async () => { w.log.timers[0].fn(); await settle(); };
  await tick();
  out.first = { fetches: reqs.length, url: reqs[0] ? reqs[0].url : null, token: tokenOf(reqs[0]),
                live: M.liveInFlight, cats: M.catsInFlight };
  await tick(); await tick();
  out.overlapping = { fetches: reqs.length, live: M.liveInFlight };
  reqs[0].resolve(answer({ events: 5 })); await settle();
  const ov = w.doc.getElementById('ov');
  out.answered = { live: M.liveInFlight, writes: w.log.writes.length, html: ov.innerHTML,
                   rendered: rendered.slice() };
  await tick(); reqs[1].resolve(answer({ events: 5 })); await settle();
  out.unchanged = { fetches: reqs.length, writes: w.log.writes.length, live: M.liveInFlight };
  await tick(); reqs[2].resolve(answer({ events: 6 })); await settle();
  out.changed = { writes: w.log.writes.length, html: ov.innerHTML };
  await tick(); reqs[3].reject(new Error('network down')); await settle();
  out.networkError = { live: M.liveInFlight, writes: w.log.writes.length };
  await tick(); reqs[4].resolve(refusal(403)); await settle();
  out.refused = { live: M.liveInFlight, writes: w.log.writes.length };
  await tick();
  out.resumed = { fetches: reqs.length };
  reqs[5].resolve(answer({ events: 6 })); await settle();
  stop();
  out.stopped = { cleared: w.log.cleared.slice(), interval: w.log.timers[0].id };
  return out;
});

await record('poll_defaults', async () => {
  const w = makeWorld();
  w.context.MindHive.pollOverview(undefined, { target: 'ov', render: () => '<p></p>' });
  const timers = w.log.timers.map((t) => [t.kind, t.ms]);
  w.log.timers[0].fn(); await settle();
  return { timers, url: w.log.fetches[0] ? w.log.fetches[0].url : null };
});

await record('page_compiles', () => {
  new vm.Script(readFileSync(PAGE_PATH, 'utf8'), { filename: 'page.js' });
  return true;
}, false);

process.stdout.write(JSON.stringify(report));
"""


@pytest.fixture(scope="module")
def node_report(tmp_path_factory):
    """Run the harness once against the real file and the real page script."""
    node = shutil.which("node")
    if not node:                                        # pragma: no cover
        pytest.skip("node is not on PATH -- these cases were REQUESTED (`-m node`) "
                    "and did not run; nothing here passed")
    work = tmp_path_factory.mktemp("static-js")
    harness = work / "static_js.test.mjs"
    harness.write_text(HARNESS_JS, encoding="utf-8")
    page_js = work / "page.js"
    page_js.write_text(_page_script(), encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(STATIC), str(page_js)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=NODE_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return json.loads(proc.stdout)
    except ValueError:                                  # pragma: no cover
        pytest.fail(f"node produced no report (exit {proc.returncode})\n"
                    f"--- stdout ---\n{proc.stdout[:4000]}\n"
                    f"--- stderr ---\n{proc.stderr[:4000]}")


def _value(report, name):
    case = report[name]
    assert case["ok"], f"{name}: {case['error']}"
    return case["value"]


@pytest.mark.node
def test_loading_the_file_touches_no_dom_starts_no_timer_and_fetches_nothing(node_report):
    load = _value(node_report, "load")
    assert (load["domReads"], load["timers"], load["fetches"]) == ([], 0, 0)


@pytest.mark.node
def test_the_namespace_carries_both_helpers_and_two_idle_latches(node_report):
    load = _value(node_report, "load")
    assert load["namespace"] == "object"
    assert load["members"] == ["function", "function", False, False]


@pytest.mark.node
def test_nothing_but_the_namespace_escapes_into_the_page_scope(node_report):
    """Inlined first, a top-level `let liveInFlight` is a SyntaxError against
    the core's own, and a top-level `function paintInto` is silently replaced
    by the core's hoisted one. Neither name may be visible outside."""
    assert _value(node_report, "load")["leaked"] == []


@pytest.mark.node
def test_the_leak_probe_would_catch_a_top_level_declaration(node_report):
    """CANARY for the test above."""
    assert _value(node_report, "leak_canary") == ["paintInto", "liveInFlight"]


@pytest.mark.node
def test_the_first_paint_writes_and_stamps(node_report):
    paint = _value(node_report, "paint")
    assert paint["first"] is True
    assert paint["afterFirst"] == {"html": "<b>one</b>", "stamp": "sig-1", "writes": 1}


@pytest.mark.node
def test_an_identical_signature_does_not_write_innerhtml(node_report):
    """The signature decides, not the markup: same signature, no write -- even
    though the HTML handed in this time differs."""
    paint = _value(node_report, "paint")
    assert paint["same"] is False
    assert paint["afterSame"] == {"html": "<b>one</b>", "stamp": "sig-1", "writes": 1}


@pytest.mark.node
def test_a_new_signature_repaints_and_restamps(node_report):
    paint = _value(node_report, "paint")
    assert paint["changed"] is True
    assert paint["afterChanged"] == {"html": "<b>three</b>", "stamp": "sig-2", "writes": 2}


@pytest.mark.node
def test_a_numeric_signature_is_compared_as_the_string_the_dom_stores(node_report):
    """`dataset` hands back '7' for a 7; compared as given, every poll of a
    count-based signature would repaint."""
    assert _value(node_report, "numeric_signature") == \
        {"first": True, "again": False, "writes": 1, "stamp": "7"}


@pytest.mark.node
def test_a_missing_target_paints_nothing_and_does_not_throw(node_report):
    assert _value(node_report, "no_target") == {
        "null": {"result": False, "threw": False},
        "undefined": {"result": False, "threw": False},
        "writes": 0,
    }


@pytest.mark.node
def test_a_repaint_gives_focus_back_to_the_rebuilt_field(node_report):
    """The write destroys the focused input and parses a new one with the same
    id; focus goes to the NEW node, found by id after the write. Refocusing the
    old reference would focus a detached element."""
    inside = _value(node_report, "focus")["inside"]
    assert inside["rebuilt"] != inside["old"]
    assert inside["calls"] == [inside["rebuilt"]]


@pytest.mark.node
def test_focus_outside_the_pane_or_under_a_skipped_paint_is_left_alone(node_report):
    assert _value(node_report, "focus")["untouched"] == []


@pytest.mark.node
def test_starting_a_poll_schedules_one_interval_and_reads_no_dom_yet(node_report):
    started = _value(node_report, "poll")["started"]
    assert started == {"timers": [["interval", 1000]], "fetches": 0,
                       "domReads": 0, "stop": "function"}


@pytest.mark.node
def test_the_poll_interval_defaults_to_2500_ms_and_the_url_to_the_overview(node_report):
    assert _value(node_report, "poll_defaults") == \
        {"timers": [["interval", 2500]], "url": "/api/overview"}


@pytest.mark.node
def test_a_tick_asks_the_callers_url_with_the_callers_token(node_report):
    first = _value(node_report, "poll")["first"]
    assert first == {"fetches": 1, "url": "/api/overview?project=alpha",
                     "token": "tok-1", "live": True, "cats": False}


@pytest.mark.node
def test_a_tick_while_a_poll_is_in_flight_issues_no_request(node_report):
    assert _value(node_report, "poll")["overlapping"] == {"fetches": 1, "live": True}


@pytest.mark.node
def test_an_answer_paints_the_target_and_releases_the_latch(node_report):
    assert _value(node_report, "poll")["answered"] == {
        "live": False, "writes": 1, "html": "<p>5 events</p>", "rendered": [5]}


@pytest.mark.node
def test_an_unchanged_overview_is_not_repainted_and_a_changed_one_is(node_report):
    poll = _value(node_report, "poll")
    assert poll["unchanged"] == {"fetches": 2, "writes": 1, "live": False}
    assert poll["changed"] == {"writes": 2, "html": "<p>6 events</p>"}


@pytest.mark.node
def test_a_network_error_and_a_refusal_release_the_latch_and_paint_nothing(node_report):
    poll = _value(node_report, "poll")
    assert poll["networkError"] == {"live": False, "writes": 2}
    assert poll["refused"] == {"live": False, "writes": 2}
    assert poll["resumed"] == {"fetches": 6}


@pytest.mark.node
def test_stop_clears_the_interval_it_started(node_report):
    stopped = _value(node_report, "poll")["stopped"]
    assert stopped["cleared"] == [stopped["interval"]]


@pytest.mark.node
def test_the_page_script_with_the_file_inlined_still_compiles(node_report):
    """A top-level `let` clash is an early SyntaxError: the whole page, not
    just this file, would stop running."""
    assert _page_script().lstrip().startswith(_static_js().strip()), \
        "the page's script does not open with static/dashboard.js"
    assert _value(node_report, "page_compiles") is True
