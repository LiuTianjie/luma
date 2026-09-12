const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const test = require('node:test');
const ts = require('typescript');

function load(name, imports = {}) {
  const filename = path.resolve(__dirname, '../src', `${name}.ts`);
  const mod = new Module(filename, module);
  mod.paths = module.paths;
  mod.require = (id) => imports[id] || require(id);
  mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText, filename);
  return mod.exports;
}

test('batch metrics API sends all target identities with auth, window and abort signal', async () => {
  const calls = [];
  const api = load('metricsApi', { './apiClient': { apiPost: async (...args) => { calls.push(args); return { results: [] }; } } });
  const targets = [{ kind: 'node', name: 'edge' }, { kind: 'service', name: 'app/web + #1' }];
  const signal = new AbortController().signal;
  await api.fetchMetricsHistoryBatch({ token: 'token', targets, window: 900, signal });
  assert.deepEqual(calls, [['/v1/dashboard/metrics/history/batch', 'token', { targets, window: 900 }, signal]]);
});

// Execute the production hook against deterministic state/effect slots and a
// deferred transport, so cleanup/races are tested without waiting for real time.
function setup() {
  const previous = { window: global.window, document: global.document };
  const timers = new Map(); let timerId = 0;
  const windowEvents = new Map(); const documentEvents = new Map(); const calls = [];
  const eventTarget = (events) => ({ addEventListener: (name, fn) => events.set(name, fn), removeEventListener: (name) => events.delete(name) });
  global.window = { ...eventTarget(windowEvents), setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; }, clearTimeout(id) { timers.delete(id); } };
  global.document = { ...eventTarget(documentEvents), visibilityState: 'visible' };
  const slots = []; let cursor = 0; const effects = [];
  const react = {
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], v => { slots[i] = typeof v === 'function' ? v(slots[i]) : v; }]; },
    useEffect(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((v, n) => v !== old.deps[n])) effects.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; }); },
  };
  const api = {
    historyKey: (kind, name) => `${kind}:${name}`,
    fetchMetricsHistoryBatch: (args) => new Promise((resolve, reject) => calls.push({ ...args, resolve, reject })),
  };
  const { useMetricsHistories } = load('useMetricsHistories', { react, './metricsApi': api });
  return {
    calls, timers,
    render(targets, token = 'token', window = 900) { cursor = 0; const value = useMetricsHistories(token, targets, window); effects.splice(0).forEach(fn => fn()); return value; },
    refresh() { windowEvents.get('luma:refresh')?.(); },
    visibility(value) { document.visibilityState = value; documentEvents.get('visibilitychange')?.(); },
    timer(delay) { const found = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(found, `expected ${delay}ms timer`); timers.delete(found[0]); found[1].fn(); },
    close() { slots.forEach(s => s?.cleanup?.()); Object.assign(global, previous); },
  };
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
const node = { kind: 'node', name: 'edge' };
const service = { kind: 'service', name: 'app_web' };
const payload = (target, value) => ({ ...target, payload: { ...target, series: { cpuPercent: [[3000, value]] } } });

test('metrics batches targets, deduplicates refresh, retains successful data through per-target errors', async () => {
  const r = setup();
  try {
    r.render([node, service]); r.refresh(); r.refresh();
    assert.equal(r.calls.length, 1);
    assert.deepEqual(r.calls[0].targets, [node, service]);
    r.calls[0].resolve({ results: [payload(node, 10), payload(service, 20)] }); await flush();
    const first = r.render([node, service]);
    assert.equal(first['node:edge'].payload.series.cpuPercent[0][1], 10);
    r.refresh();
    assert.equal(r.calls.length, 2);
    assert.deepEqual(r.render([node, service]), first, 'manual refresh must retain charts');
    r.calls[1].resolve({ results: [{ ...node, error: 'node unavailable' }, payload(service, 30)] }); await flush();
    const next = r.render([node, service]);
    assert.equal(next['node:edge'].payload.series.cpuPercent[0][1], 10);
    assert.equal(next['node:edge'].error, 'node unavailable');
    assert.equal(next['service:app_web'].payload.series.cpuPercent[0][1], 30);
    assert.deepEqual([...r.timers.values()].map(t => t.delay), [30000]);
  } finally { r.close(); }
});

test('selection, token and time range changes abort old requests and reject stale completion', async () => {
  const r = setup();
  try {
    r.render([node]);
    assert.deepEqual(r.render([service]), {});
    assert.ok(r.calls[0].signal.aborted);
    r.calls[1].resolve({ results: [payload(service, 30)] }); await flush();
    r.calls[0].resolve({ results: [payload(node, 99)] }); await flush();
    assert.equal(r.render([service])['service:app_web'].payload.series.cpuPercent[0][1], 30);
    assert.deepEqual(r.render([service], 'new-token'), {});
    assert.equal(r.calls[2].token, 'new-token');
    r.render([service], 'new-token', 3600);
    assert.ok(r.calls[2].signal.aborted);
    assert.equal(r.calls[3].window, 3600);
    r.render([], 'new-token', 3600);
    assert.ok(r.calls[3].signal.aborted);
  } finally { r.close(); }
});

test('hidden tabs abort and pause polling, resume immediately, timeout and missing targets are retryable', async () => {
  const r = setup();
  try {
    r.visibility('hidden'); r.render([node, service]);
    assert.equal(r.calls.length, 0);
    r.visibility('visible');
    assert.equal(r.calls.length, 1);
    r.visibility('hidden');
    assert.ok(r.calls[0].signal.aborted);
    r.refresh(); assert.equal(r.calls.length, 1);
    r.visibility('visible'); assert.equal(r.calls.length, 2);
    r.calls[0].resolve({ results: [payload(node, 99)] }); await flush();
    r.calls[1].resolve({ results: [payload(node, 10)] }); await flush();
    assert.match(r.render([node, service])['service:app_web'].error, /omitted/);
    r.timer(30000); r.timer(20000);
    assert.ok(r.calls[2].signal.aborted);
    r.calls[2].reject(new Error('request timed out')); await flush();
    const state = r.render([node, service]);
    assert.equal(state['node:edge'].payload.series.cpuPercent[0][1], 10);
    assert.match(state['node:edge'].error, /timed out/);
    r.visibility('hidden');
    assert.equal(r.timers.size, 0);
    r.visibility('visible'); assert.equal(r.calls.length, 4);
  } finally { r.close(); }
});
