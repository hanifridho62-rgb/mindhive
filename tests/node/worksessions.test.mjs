// tests/node/worksessions.test.mjs — driven by tests/test_worksessions_node.py.
//
// argv[2] is a file holding the Oversee work-session functions exactly as the
// served page carries them (extracted by the Python side, never retyped here).
// They run in a vm context whose globals stand in for the page's module state:
// `WS_DATA`, `PROJECT` and `fmtTs`. Every case writes its rendered HTML into one
// JSON report the Python side asserts on.
import fs from 'node:fs';
import vm from 'node:vm';

const src = fs.readFileSync(process.argv[2], 'utf8');
const ctx = { PROJECT: 'test', WS_DATA: null, fmtTs: s => 'T(' + s + ')' };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const call = (expr) => vm.runInContext(expr, ctx);
const out = {};

// 1. no data at all: every helper renders nothing
out.none = { usage: call("wsUsage('test')"), pills: call('wsPills({})'),
             inits: call('wsInitiatives(false)') };

// 2. a namespace's usage, truncated digest
ctx.WS_DATA = { ok: true, usage: {
  test: { context_budget_tokens: 200000, safe_budget_tokens: 160000,
          tokens_est: 50000, truncated: true, measured: true },
  beta: { context_budget_tokens: 200000, safe_budget_tokens: 160000,
          tokens_est: 900000, truncated: false, measured: true },
  zero: { context_budget_tokens: 0, safe_budget_tokens: 160000, tokens_est: 5,
          measured: true },
  bare: { context_budget_tokens: 200000, measured: true },
  unmeasured: { context_budget_tokens: 200000, safe_budget_tokens: 160000,
                tokens_est: 0, truncated: false, measured: false },
  legacy: { context_budget_tokens: 200000, safe_budget_tokens: 160000, tokens_est: 10 },
}, initiatives: [] };
out.usage = call("wsUsage('test')");
out.overflow = call("wsUsage('beta')");
out.zeroBudget = call("wsUsage('zero')");
out.bare = call("wsUsage('bare')");
out.defaultProject = call('wsUsage(undefined)');
out.unmeasured = call("wsUsage('unmeasured')");
out.legacy = call("wsUsage('legacy')");

// 3. HIVE MIND: a card names its own namespace; no namespace means nothing
ctx.PROJECT = '*';
out.hiveMindOwn = call("wsUsage('beta')");
out.hiveMindNone = call('wsUsage(undefined)');
ctx.PROJECT = 'test';

// 4. pills, with hostile ledger text
out.pillsOnline = call(`wsPills({phase: '<img src=x onerror=alert(1)>', online: true,
  project: 'test', initiatives: [{tag: '"><script>alert(1)</script>', reason: 'r'},
                                 {tag: '', reason: 'plain'}]})`);
out.pillsOffline = call("wsPills({phase: 'kanban', online: false, project: 'nowhere'})");

// 5. the Autonomous Initiatives group
ctx.WS_DATA = { ok: true, usage: {}, initiatives: Array.from({ length: 10 }, (_, i) => ({
  session_id: 's<' + i + '>', project: 'p&' + i, reason: 'reason <b>' + i + '</b>',
  ref: i % 2 ? 'inc:' + i : '', tag: '[INITIATIVE: r' + i + ']',
  step_id: i === 3 ? 'step-<3>' : '',
  ts: '2026-09-17T0' + (i % 10) + ':00:00Z' })) };
out.initsHive = call('wsInitiatives(true)');
out.initsOne = call('wsInitiatives(false)');

process.stdout.write(JSON.stringify(out));
