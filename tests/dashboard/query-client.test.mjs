import assert from 'node:assert/strict';
import fs from 'node:fs';
import Module, { createRequire } from 'node:module';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
import { ApiError, UNAUTHORIZED_EVENT } from '../../dashboard-src/src/apiClient.ts';
import {
  createDashboardQueryClient,
  DASHBOARD_STALE_TIME_MS,
  invalidateDashboardQueries,
  RequestTimeoutError,
  shouldRetryDashboardQuery,
} from '../../dashboard-src/src/queryClient.ts';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

function loadDashboardTs(name, imports = {}) {
  const filename = path.resolve(__dirname, '../../dashboard-src/src', `${name.replace(/\.ts$/, '')}.ts`);
  const mod = new Module(filename);
  mod.filename = filename;
  mod.paths = Module._nodeModulePaths(path.dirname(filename));
  mod.require = (id) => imports[id] || imports[id.replace(/\.ts$/, '')] || require(id);
  const transpiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: filename,
  }).outputText;
  mod._compile(transpiled, filename);
  return mod.exports;
}

test('createDashboardQueryClient sets focus/background-pause/staleTime defaults', () => {
  const client = createDashboardQueryClient();
  try {
    const defaults = client.getDefaultOptions().queries;
    assert.equal(defaults.refetchOnWindowFocus, true);
    assert.equal(defaults.refetchIntervalInBackground, false);
    assert.equal(defaults.staleTime, DASHBOARD_STALE_TIME_MS);
    assert.equal(defaults.staleTime, 10_000);
    assert.equal(defaults.retry, shouldRetryDashboardQuery);
  } finally {
    client.clear();
  }
});

test('shouldRetryDashboardQuery never retries 401, 403, 404 or timeouts and caps transient retries at 1', async () => {
  for (const status of [401, 403, 404]) {
    assert.equal(shouldRetryDashboardQuery(0, new ApiError(`HTTP ${status}`, status)), false);
    assert.equal(shouldRetryDashboardQuery(0, { status }), false);
  }
  assert.equal(shouldRetryDashboardQuery(0, new RequestTimeoutError()), false);

  // Transient server or network errors retry at most once (failureCount 0 -> true, 1 -> false).
  assert.equal(shouldRetryDashboardQuery(0, new ApiError('Bad Gateway', 502)), true);
  assert.equal(shouldRetryDashboardQuery(1, new ApiError('Bad Gateway', 502)), false);
  assert.equal(shouldRetryDashboardQuery(0, new Error('Failed to fetch')), true);
  assert.equal(shouldRetryDashboardQuery(1, new Error('Failed to fetch')), false);

  // End-to-end verification through a real QueryClient.
  const client = createDashboardQueryClient();
  client.setDefaultOptions({
    queries: { ...client.getDefaultOptions().queries, retryDelay: 0 },
  });
  try {
    for (const status of [401, 403, 404]) {
      let attempts = 0;
      await assert.rejects(
        client.fetchQuery({
          queryKey: ['test-no-retry', status],
          queryFn: async () => {
            attempts += 1;
            throw new ApiError(`HTTP ${status}`, status);
          },
        }),
        (err) => err instanceof ApiError && err.status === status,
      );
      assert.equal(attempts, 1, `HTTP ${status} must not be retried`);
    }

    let transientAttempts = 0;
    await assert.rejects(
      client.fetchQuery({
        queryKey: ['test-transient-retry'],
        queryFn: async () => {
          transientAttempts += 1;
          throw new ApiError('Service Unavailable', 503);
        },
      }),
      (err) => err instanceof ApiError && err.status === 503,
    );
    assert.equal(transientAttempts, 2, 'transient errors retry at most once (2 total attempts)');
  } finally {
    client.clear();
  }
});

test('invalidateDashboardQueries invalidates matching caches and broadcasts luma:refresh on global refresh', async () => {
  const prevWindow = globalThis.window;
  const prevCustomEvent = globalThis.CustomEvent;
  const events = [];
  globalThis.CustomEvent = class CustomEvent {
    constructor(type) {
      this.type = type;
    }
  };
  globalThis.window = {
    dispatchEvent(event) {
      events.push(event.type);
      return true;
    },
  };
  const client = createDashboardQueryClient();
  try {
    client.setQueryData(['dashboard', 'overview', '', 'session:1'], { ok: true });
    client.setQueryData(['alerting', 'tab', 'alerts', 'firing', '', 'session:1'], { overview: {} });

    await invalidateDashboardQueries(['alerting'], client);
    assert.deepEqual(events, [], 'scoped invalidation does not emit global luma:refresh');
    assert.equal(client.getQueryState(['alerting', 'tab', 'alerts', 'firing', '', 'session:1'])?.isInvalidated, true);
    assert.equal(client.getQueryState(['dashboard', 'overview', '', 'session:1'])?.isInvalidated, false);

    await invalidateDashboardQueries(undefined, client);
    assert.deepEqual(events, ['luma:refresh'], 'global refresh emits luma:refresh for unmigrated panels');
    assert.equal(client.getQueryState(['dashboard', 'overview', '', 'session:1'])?.isInvalidated, true);
  } finally {
    client.clear();
    globalThis.window = prevWindow;
    globalThis.CustomEvent = prevCustomEvent;
  }
});

test('useDashboardAuth clears QueryClient cache on luma:unauthorized and signOut', async () => {
  const prevWindow = globalThis.window;
  const prevLocalStorage = globalThis.localStorage;
  const listeners = new Map();
  globalThis.window = {
    location: { hostname: 'example.com' },
    addEventListener(name, fn) {
      listeners.set(name, fn);
    },
    removeEventListener(name) {
      listeners.delete(name);
    },
  };
  globalThis.localStorage = { getItem: () => null, removeItem() {}, setItem() {} };

  const client = createDashboardQueryClient();
  const slots = [];
  let cursor = 0;
  const effects = [];
  const react = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [
        slots[i],
        (next) => {
          slots[i] = typeof next === 'function' ? next(slots[i]) : next;
        },
      ];
    },
    useCallback(fn, deps) {
      const i = cursor++;
      const prev = slots[i];
      if (!prev || deps.some((v, idx) => v !== prev.deps[idx])) slots[i] = { deps, fn };
      return slots[i].fn;
    },
    useEffect(fn, deps) {
      const i = cursor++;
      const prev = slots[i];
      if (!prev || !deps || deps.some((v, idx) => v !== prev.deps[idx])) {
        effects.push(() => {
          prev?.cleanup?.();
          slots[i] = { deps, cleanup: fn() };
        });
      }
    },
  };

  const apiClient = loadDashboardTs('apiClient');
  const authApiStub = {
    exchangeToken: async () => {},
    fetchAuthConfig: async () => ({ passkeys: 0 }),
    fetchCurrentSession: async () => ({ id: 'sess-1' }),
    signInWithPasskey: async () => {},
    signOutSession: async () => {},
    SignInUnavailable: class SignInUnavailable extends Error {},
  };
  const { useDashboardAuth } = loadDashboardTs('useDashboardAuth', {
    react,
    './apiClient': apiClient,
    './authApi': authApiStub,
    './queryClient': { useDashboardQueryClient: () => client },
  });

  const render = () => {
    cursor = 0;
    const state = useDashboardAuth();
    effects.splice(0).forEach((fn) => fn());
    return state;
  };

  try {
    render();
    await new Promise((resolve) => setImmediate(resolve));
    const signedIn = render();
    assert.equal(signedIn.status, 'signedIn');
    assert.ok(listeners.has(UNAUTHORIZED_EVENT), 'signed-in auth listens for luma:unauthorized');

    client.setQueryData(['dashboard', 'overview', '', signedIn.token], { summary: { totalNodes: 3 } });
    client.setQueryData(['alerting', 'tab', 'alerts', 'firing', '', signedIn.token], { overview: { channels: 1 } });
    assert.equal(client.getQueryCache().getAll().length, 2);

    // 401 -> luma:unauthorized must immediately wipe all cached authenticated queries.
    listeners.get(UNAUTHORIZED_EVENT)();
    assert.equal(client.getQueryCache().getAll().length, 0);
    const afterUnauthorized = render();
    assert.equal(afterUnauthorized.status, 'signedOut');
    assert.equal(afterUnauthorized.expired, true);

    // Sign back in, populate cache, then sign out -> cache must also be wiped.
    afterUnauthorized.adoptSession();
    const reSignedIn = render();
    assert.equal(reSignedIn.status, 'signedIn');
    client.setQueryData(['dashboard', 'fleet', '', reSignedIn.token], { nodes: [{ name: 'n1' }] });
    assert.equal(client.getQueryCache().getAll().length, 1);

    await reSignedIn.signOut();
    assert.equal(client.getQueryCache().getAll().length, 0);
    const afterSignOut = render();
    assert.equal(afterSignOut.status, 'signedOut');
    assert.equal(afterSignOut.expired, false);
  } finally {
    slots.forEach((slot) => slot?.cleanup?.());
    client.clear();
    globalThis.window = prevWindow;
    globalThis.localStorage = prevLocalStorage;
  }
});

test('useDashboardQueryClient falls back cleanly during SSR without QueryClientProvider', () => {
  // Load queryClient against the root React instance so React, ReactDOMServer, and
  // @tanstack/react-query share a single dispatcher (matching Vite's react dedupe).
  const React = require('react');
  const { renderToStaticMarkup } = require('react-dom/server');
  const rq = require('@tanstack/react-query');
  const apiClient = loadDashboardTs('apiClient');
  const qcModule = loadDashboardTs('queryClient', {
    react: React,
    '@tanstack/react-query': rq,
    './apiClient': apiClient,
  });
  let seenWithoutProvider = null;
  let seenWithProvider = null;

  function Probe({ onClient }) {
    onClient(qcModule.useDashboardQueryClient());
    return React.createElement('span', null, 'ok');
  }

  assert.equal(
    renderToStaticMarkup(React.createElement(Probe, { onClient: (c) => { seenWithoutProvider = c; } })),
    '<span>ok</span>',
  );
  assert.equal(seenWithoutProvider, qcModule.queryClient);

  const custom = qcModule.createDashboardQueryClient();
  try {
    assert.equal(
      renderToStaticMarkup(
        React.createElement(
          rq.QueryClientProvider,
          { client: custom },
          React.createElement(Probe, { onClient: (c) => { seenWithProvider = c; } }),
        ),
      ),
      '<span>ok</span>',
    );
    assert.equal(seenWithProvider, custom);
  } finally {
    custom.clear();
    qcModule.queryClient.clear();
  }
});
