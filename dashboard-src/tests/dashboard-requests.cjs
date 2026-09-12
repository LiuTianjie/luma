const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const test = require('node:test');
const ts = require('typescript');
function load(name, imports = {}) {
  const filename = path.resolve(__dirname, '../src', name + '.ts');
  const mod = new Module(filename, module);
  mod.paths = module.paths;
  mod.require = (id) => imports[id] || require(id);
  mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
  return mod.exports;
}
test('independent pages skip fleet queries and node consumers use the light scope', () => {
  const { dashboardScopeForPage: scope } = load('dashboardScope');
  for (const page of ['deployments', 'registry', 'credentials', 'lae']) assert.equal(scope(page), 'none');
  assert.equal(scope('builder'), 'nodes');
  for (const path of ['/fleet/join', '/fleet/regions', '/fleet/maintenance', '/terminal/node/manager']) assert.equal(scope('nodes', path), 'nodes');
  assert.equal(scope('nodes', '/fleet'), 'fleet');
  assert.equal(scope('nodes', '/fleet/nodes/manager'), 'full');
  assert.equal(scope('nodes', '/fleet/network'), 'network');
  assert.equal(scope('setup'), 'setup');
  for (const [page, expected] of [['overview', 'overview'], ['applications', 'applications'], ['deploy', 'none'], ['storage', 'storage'], ['observability', 'directory']]) assert.equal(scope(page), expected);
  assert.equal(scope('applications', '/apps/my-app/metrics'), 'application');
  assert.equal(scope('storage', '/storage/governance'), 'none');
  assert.equal(scope('deploy', '/create/image'), 'deploy');
  assert.equal(scope('observability', '/observe/metrics'), 'metrics');
  const { dashboardQueryForRoute: query } = load('dashboardScope');
  assert.equal(query('application', '/apps/app%2Fone/overview', ''), 'app=app%2Fone');
  assert.equal(query('applications', '/apps', '?q=api&offset=50&tab=ignored'), 'q=api&offset=50');
});
test('alert tabs fetch only displayed resources, preserving auth and cancellation', async () => {
  const calls = [];
  const { fetchAlertTab } = load('alertingApi', { './apiClient': { apiGet: async (...args) => { calls.push(args); return { items: [] }; } } });
  const signal = new AbortController().signal;
  for (const [tab, paths] of [
    ['alerts', ['overview', 'incidents?limit=50&status=firing']],
    ['rules', ['overview', 'presets', 'rules', 'channels']],
    ['notifications', ['overview', 'channels', 'deliveries?limit=50']],
  ]) {
    calls.length = 0;
    await fetchAlertTab(tab, 'token', 'firing', signal);
    assert.deepEqual(calls.map(c => c[0]), paths.map(p => '/v1/alerting/' + p));
    assert.ok(calls.every(c => c[1] === 'token' && c[2] === signal));
  }
});

// Small hook runner: exercises effects, cleanup and pending network responses
// without a browser or real timers; no production hook logic is reproduced.
function hookRunner() {
  const slots = []; let cursor = 0; const effects = [];
  const react = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], v => { slots[i] = typeof v === 'function' ? v(slots[i]) : v; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial }; },
    useCallback(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((v, n) => v !== old.deps[n])) slots[i] = { deps, fn }; return slots[i].fn; },
    useEffect(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((v, n) => v !== old.deps[n])) effects.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; }); },
  };
  const { useDashboardData } = load('useDashboardData', { react });
  return { render(scope, query) { cursor = 0; const value = useDashboardData(scope, query); effects.splice(0).forEach(fn => fn()); return value; }, close() { slots.forEach(s => s?.cleanup?.()); } };
}
test('dashboard deduplicates refreshes, aborts old scope, ignores stale responses, skips independent pages', async () => {
  const previous = { window: global.window, document: global.document, localStorage: global.localStorage, fetch: global.fetch };
  const timers = new Map(); let timerId = 0; const calls = [];
  global.window = { location: { hostname: 'example.com' }, setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); } };
  global.document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
  global.localStorage = { getItem: () => 'token', setItem() {}, removeItem() {} };
  global.fetch = (url, options) => new Promise(resolve => calls.push({ url, options, resolve }));
  const runner = hookRunner();
  try {
    const first = runner.render('full');
    const waiting = first.loadDashboard();
    assert.equal(first.loadDashboard(), waiting);
    assert.equal(calls.length, 1);
    const second = runner.render('nodes');
    assert.ok(calls[0].options.signal.aborted);
    assert.equal(calls[1].url, '/v1/dashboard?scope=nodes');
    const current = second.loadDashboard();
    calls[1].resolve({ ok: true, text: async () => JSON.stringify({ nodes: [{ name: 'new' }] }) });
    await current;
    calls[0].resolve({ ok: true, text: async () => JSON.stringify({ nodes: [{ name: 'old' }] }) });
    await waiting;
    assert.equal(runner.render('nodes').payload.nodes[0].name, 'new');
    const list = runner.render('applications', 'offset=0');
    const listRequest = list.loadDashboard();
    calls[2].resolve({ ok: true, text: async () => JSON.stringify({ services: [{ name: 'first' }] }) });
    await listRequest;
    const filtered = runner.render('applications', 'q=next');
    assert.equal(filtered.payload.services[0].name, 'first');
    const filteredRequest = filtered.loadDashboard();
    assert.ok(calls[3].url.endsWith('&q=next'));
    calls[3].resolve({ ok: true, text: async () => JSON.stringify({ services: [{ name: 'next' }] }) });
    await filteredRequest;
    assert.equal(runner.render('applications', 'q=next').payload.services[0].name, 'next');
    const independent = runner.render('none');
    await independent.loadDashboard();
    assert.equal(calls.length, 4);
    assert.equal(independent.payload, null);
  } finally { runner.close(); Object.assign(global, previous); }
});
