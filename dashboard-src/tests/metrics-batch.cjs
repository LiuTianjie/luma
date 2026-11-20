const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const test = require('node:test');
const ts = require('typescript');

function load(name, imports = {}) {
  const filename = path.resolve(__dirname, '../src', `${name.replace(/\.ts$/, '')}.ts`);
  const mod = new Module(filename, module);
  mod.paths = module.paths;
  mod.require = (id) => imports[id] || imports[id.replace(/\.ts$/, '')] || require(id);
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

// Execute the production hook against real TanStack Query QueryClient/QueryObserver
// with deterministic timers and deferred transport, so cleanup/races are tested
// without waiting for real wall-clock time.
// TanStack Query evaluates `isServer = typeof window === "undefined"` at import time;
// seed a browser-like window before importing so client polling intervals run.
const hadWindow = 'window' in global;
const initialWindow = global.window;
if (!global.window) global.window = {};
const rq = require('@tanstack/react-query');
if (!hadWindow) delete global.window; else global.window = initialWindow;
let activeTimers = new Map();
let activeTimerId = 0;
rq.timeoutManager.setTimeoutProvider({
  setTimeout(fn, delay) {
    const id = ++activeTimerId;
    if (!delay) {
      let cancelled = false;
      activeTimers.set(id, { fn, delay: 0, cancel: () => { cancelled = true; } });
      queueMicrotask(() => { if (!cancelled) { activeTimers.delete(id); fn(); } });
      return id;
    }
    activeTimers.set(id, { fn, delay });
    return id;
  },
  clearTimeout(id) { activeTimers.get(id)?.cancel?.(); activeTimers.delete(id); },
  setInterval(fn, delay) { const id = ++activeTimerId; activeTimers.set(id, { fn, delay, interval: true }); return id; },
  clearInterval(id) { activeTimers.delete(id); },
});

function setup() {
  const previous = { window: global.window, document: global.document };
  const timers = new Map();
  activeTimers = timers;
  const windowEvents = new Map(); const documentEvents = new Map(); const calls = [];
  const eventTarget = (events) => ({ addEventListener: (name, fn) => events.set(name, fn), removeEventListener: (name) => events.delete(name) });
  global.window = {
    ...eventTarget(windowEvents),
    setTimeout(fn, delay) { const id = ++activeTimerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  global.document = { ...eventTarget(documentEvents), visibilityState: 'visible' };
  rq.focusManager.setFocused(true);
  const slots = []; let cursor = 0; const effects = [];
  const react = {
    useContext() { return undefined; },
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], v => { slots[i] = typeof v === 'function' ? v(slots[i]) : v; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial }; },
    useMemo(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((v, n) => v !== old.deps[n])) slots[i] = { deps, value: fn() }; return slots[i].value; },
    useCallback(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || deps.some((v, n) => v !== old.deps[n])) slots[i] = { deps, fn }; return slots[i].fn; },
    useEffect(fn, deps) { const i = cursor++; const old = slots[i]; if (!old || !deps || deps.some((v, n) => v !== old.deps[n])) effects.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: fn() }; }); },
  };
  const apiClient = load('apiClient');
  const queryClientModule = load('queryClient', { react, '@tanstack/react-query': rq, './apiClient': apiClient });
  const testClient = queryClientModule.createDashboardQueryClient();
  testClient.mount();
  const observers = [];
  const reactQuery = {
    ...rq,
    useQuery(options, client) {
      const activeClient = client || testClient;
      const defaulted = activeClient.defaultQueryOptions(options);
      const [observer] = react.useState(() => {
        const created = new rq.QueryObserver(activeClient, defaulted);
        observers.push(created);
        return created;
      });
      const result = observer.getOptimisticResult(defaulted);
      react.useEffect(() => {
        observer.setOptions(defaulted);
      }, [defaulted, observer]);
      react.useEffect(() => {
        const unsub = observer.subscribe(() => {});
        return () => unsub();
      }, [observer]);
      return result;
    },
  };
  const api = {
    historyKey: (kind, name) => `${kind}:${name}`,
    fetchMetricsHistoryBatch: (args) => new Promise((resolve, reject) => calls.push({ ...args, resolve, reject })),
  };
  const { useMetricsHistories } = load('useMetricsHistories', {
    react,
    '@tanstack/react-query': reactQuery,
    './metricsApi': api,
    './queryClient': { ...queryClientModule, queryClient: testClient, useDashboardQueryClient: () => testClient },
  });
  return {
    calls, timers,
    render(targets, token = 'token', window = 900) { cursor = 0; const value = useMetricsHistories(token, targets, window); effects.splice(0).forEach(fn => fn()); return value; },
    refresh() { windowEvents.get('luma:refresh')?.(); },
    visibility(value) { document.visibilityState = value; rq.focusManager.setFocused(value !== 'hidden'); },
    timer(delay) {
      const found = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(found, `expected ${delay}ms timer`);
      if (!found[1].interval) timers.delete(found[0]);
      found[1].fn();
    },
    close() {
      slots.forEach(s => s?.cleanup?.());
      observers.forEach(o => { o.destroy(); o.getCurrentQuery()?.destroy(); });
      testClient.unmount();
      testClient.clear();
      timers.clear();
      rq.focusManager.setFocused(undefined);
      Object.assign(global, previous);
    },
  };
}
const flush = async () => { await new Promise((resolve) => setImmediate(resolve)); };
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
    assert.deepEqual([...r.timers.values()].filter(t => t.interval).map(t => t.delay), [30000]);
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

test('hidden tabs pause background polling, resume immediately on focus, timeout and missing targets are retryable', async () => {
  const r = setup();
  try {
    r.render([node, service]);
    assert.equal(r.calls.length, 1);
    r.calls[0].resolve({ results: [payload(node, 10)] }); await flush();
    assert.match(r.render([node, service])['service:app_web'].error, /omitted/);
    r.timer(30000);
    assert.equal(r.calls.length, 2);
    r.timer(20000);
    assert.ok(r.calls[1].signal.aborted);
    r.calls[1].reject(new Error('request timed out')); await flush();
    const state = r.render([node, service]);
    assert.equal(state['node:edge'].payload.series.cpuPercent[0][1], 10);
    assert.match(state['node:edge'].error, /timed out/);
    r.visibility('hidden');
    r.refresh();
    r.timer(30000);
    assert.equal(r.calls.length, 2, 'hidden tabs pause both interval polling and background refresh');
    r.visibility('visible');
    await flush();
    assert.equal(r.calls.length, 3, 'returning to a visible tab resumes immediately');
  } finally { r.close(); }
});
