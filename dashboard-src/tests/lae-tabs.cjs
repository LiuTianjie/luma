const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const test = require('node:test');
const ts = require('typescript');

// Run the real component's hooks and effects, keeping its JSX as inspectable
// objects. Deferred requests deliberately ignore abort so stale-result guards
// are exercised independently of a cooperative network implementation.
function harness(token = 'account-a') {
  const previousWindow = global.window;
  const timers = new Map();
  let timerId = 0;
  global.window = {
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  };
  const slots = [];
  const effects = [];
  const calls = [];
  let cursor = 0;
  let dirty = false;
  let tree;
  let props = { lang: 'en', token };
  const changed = (a, b) => !a || !b || a.length !== b.length || b.some((v, i) => !Object.is(v, a[i]));
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!slots[index]) {
        const slot = { value: typeof initial === 'function' ? initial() : initial };
        slot.set = next => {
          const value = typeof next === 'function' ? next(slot.value) : next;
          if (!Object.is(value, slot.value)) { slot.value = value; dirty = true; }
        };
        slots[index] = slot;
      }
      return [slots[index].value, slots[index].set];
    },
    useRef(initial) { const index = cursor++; return slots[index] ||= { current: initial }; },
    useMemo(fn, deps) {
      const index = cursor++;
      if (!slots[index] || changed(slots[index].deps, deps)) slots[index] = { deps, value: fn() };
      return slots[index].value;
    },
    useCallback(fn, deps) { return react.useMemo(() => fn, deps); },
    useEffect(fn, deps) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || changed(previous.deps, deps)) {
        const slot = { deps, cleanup: previous?.cleanup };
        slots[index] = slot;
        effects.push(() => { slot.cleanup?.(); slot.cleanup = fn(); });
      }
    },
  };
  const element = (type, props) => ({ type, props: props || {} });
  const named = names => Object.fromEntries(names.map(name => [name, name]));
  const imports = {
    react,
    'react/jsx-runtime': { jsx: element, jsxs: element, Fragment: 'Fragment' },
    '@/components/ui/button': named(['Button']),
    '@/components/ui/badge': named(['Badge']),
    '@/components/ui/card': named(['Card', 'CardContent', 'CardDescription', 'CardFooter', 'CardHeader', 'CardTitle']),
    '@/components/ui/empty': named(['Empty', 'EmptyDescription', 'EmptyHeader', 'EmptyMedia', 'EmptyTitle']),
    '@/components/ui/skeleton': named(['Skeleton']),
    '@/components/ui/spinner': named(['Spinner']),
    '@/components/ui/table': named(['Table', 'TableBody', 'TableCell', 'TableHead', 'TableHeader', 'TableRow']),
    '@/components/ui/tabs': named(['Tabs', 'TabsContent', 'TabsList', 'TabsTrigger']),
    '@/components/ui/alert': named(['Alert', 'AlertDescription', 'AlertTitle']),
    'lucide-react': named(['Boxes', 'MapPinned', 'RefreshCw', 'ScrollText', 'UsersRound', 'WalletCards', 'AlertCircle']),
    '../components/primitives': named(['Badge', 'CodeCell', 'PrimaryCell', 'StatePill']),
    './PageHeader': named(['PageHeader']),
    '../laeAdminApi': {
      fetchLaeAdmin(resource, token, signal) {
        return new Promise((resolve, reject) => calls.push({ resource, token, signal, resolve, reject }));
      },
    },
  };
  const filename = path.resolve(__dirname, '../src/pages/LaeAdminPage.tsx');
  const mod = new Module(filename, module);
  mod.paths = module.paths;
  mod.require = id => imports[id] || require(id);
  mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, filename);

  function render(nextProps = {}) {
    props = { ...props, ...nextProps };
    dirty = true;
    let remaining = 30;
    while (dirty) {
      assert.ok(remaining-- > 0, 'component effects should settle');
      dirty = false;
      cursor = 0;
      tree = mod.exports.LaeAdminPage(props);
      effects.splice(0).forEach(effect => effect());
    }
    return tree;
  }
  function nodes(value = tree) {
    if (Array.isArray(value)) return value.flatMap(nodes);
    if (!value || typeof value !== 'object' || !value.props) return [];
    return [value, ...nodes(value.props.children ?? null)];
  }
  function text(value) {
    if (Array.isArray(value)) return value.map(text).join('');
    if (value && typeof value === 'object') return text(value.props?.children);
    return value == null || typeof value === 'boolean' ? '' : String(value);
  }
  function tab(label) {
    const match = nodes().find(node => node.type === 'TabsTrigger' && [].concat(node.props.children).includes(label));
    assert.ok(match, `tab ${label} exists`);
    return match;
  }
  function refresh() {
    const header = nodes().find(node => node.type === 'PageHeader');
    assert.ok(header?.props.meta.action, 'refresh action exists');
    return header.props.meta.action;
  }
  const runner = {
    calls, render, nodes, text, tab, refresh,
    clickTab(label) {
      const tabs = nodes().find(node => node.type === 'Tabs');
      assert.ok(tabs, 'controlled Tabs root exists');
      tabs.props.onValueChange(tab(label).props.value);
      render();
    },
    titles() { return nodes().filter(node => node.type === 'PrimaryCell').map(node => node.props.title); },
    count(label) { return text(tab(label)).slice(label.length); },
    async flush() { for (let i = 0; i < 6; i++) { await Promise.resolve(); render(); } },
    close() { slots.forEach(slot => slot?.cleanup?.()); global.window = previousWindow; },
  };
  render();
  return runner;
}

const page = total => ({ limit: 100, offset: 0, total });
const application = name => ({ id: name, tenantId: 'tenant-1', name, slug: name, kind: 'web', desiredState: 'running', observedState: 'running', serviceCount: 1, requestedVolumeBytes: 0 });
const user = email => ({ id: email, email, status: 'active' });
const tenant = { id: 'tenant-1', name: 'Tenant One', slug: 'tenant-one', ownerEmail: 'owner@example.com', status: 'active' };
const request = (runner, resource, start = 0) => {
  const match = runner.calls.slice(start).find(call => call.resource === resource);
  assert.ok(match, `request for ${resource} exists`);
  return match;
};

test('LAE loads the selected resource, cancels tab work, ignores late data and reuses loaded tabs', async () => {
  const h = harness();
  try {
    assert.deepEqual(h.calls.map(call => call.resource).sort(), ['applications', 'tenants']);
    assert.ok(h.calls.every(call => call.token === 'account-a' && call.signal instanceof AbortSignal));
    for (const label of ['Apps', 'Placement', 'Users', 'Tenants', 'Operations', 'Usage']) assert.equal(h.count(label), '—');
    const abandonedApps = request(h, 'applications');
    const abandonedTenants = request(h, 'tenants');
    h.clickTab('Users');
    assert.ok(abandonedApps.signal.aborted);
    assert.ok(abandonedTenants.signal.aborted);
    assert.deepEqual(h.calls.slice(2).map(call => call.resource), ['users']);
    abandonedApps.resolve({ applications: [application('stale-app')], page: page(99) });
    abandonedTenants.resolve({ tenants: [tenant], page: page(99) });
    await h.flush();
    assert.equal(h.count('Apps'), '—');
    assert.equal(h.count('Tenants'), '—');
    request(h, 'users').resolve({ users: [user('current@example.com')], page: page(7) });
    await h.flush();
    assert.ok(h.titles().includes('current@example.com'));
    assert.equal(h.count('Users'), '7');

    const appStart = h.calls.length;
    h.clickTab('Apps');
    assert.deepEqual(h.calls.slice(appStart).map(call => call.resource).sort(), ['applications', 'tenants']);
    request(h, 'applications', appStart).resolve({ applications: [application('current-app')], page: page(3) });
    await h.flush();
    assert.ok(h.titles().includes('current-app'), 'app rows do not wait for tenant labels');
    assert.equal(h.count('Apps'), '3');
    assert.notEqual(h.nodes().find(node => node.type === 'section')?.props.hidden, true);
    request(h, 'tenants', appStart).resolve({ tenants: [tenant], page: page(2) });
    await h.flush();
    assert.ok(h.titles().includes('Tenant One'));
    const loadedCalls = h.calls.length;
    h.clickTab('Users');
    assert.ok(h.titles().includes('current@example.com'));
    h.clickTab('Apps');
    assert.ok(h.titles().includes('current-app'));
    assert.equal(h.calls.length, loadedCalls, 'loaded tabs reuse their own cached rows and page counts');
  } finally { h.close(); }
});

test('LAE tenant-label failure does not block apps and replaced refreshes retain rows', async () => {
  const h = harness();
  try {
    request(h, 'applications').resolve({ applications: [application('before-refresh')], page: page(4) });
    request(h, 'tenants').reject(new Error('tenant-label lookup failed'));
    await h.flush();
    assert.ok(h.titles().includes('before-refresh'));
    assert.notEqual(h.nodes().find(node => node.type === 'section')?.props.hidden, true);
    assert.equal(h.count('Tenants'), '—');
    const firstStart = h.calls.length;
    h.refresh().props.onClick();
    h.render();
    assert.ok(h.titles().includes('before-refresh'));
    const firstAppRefresh = request(h, 'applications', firstStart);
    const secondStart = h.calls.length;
    h.refresh().props.onClick();
    h.render();
    assert.ok(firstAppRefresh.signal.aborted, 'the replacement refresh aborts the prior request');
    assert.ok(h.titles().includes('before-refresh'), 'refresh keeps the displayed rows');
    firstAppRefresh.resolve({ applications: [application('obsolete-refresh')], page: page(90) });
    await h.flush();
    assert.ok(h.titles().includes('before-refresh'));
    request(h, 'applications', secondStart).resolve({ applications: [application('after-refresh')], page: page(5) });
    await h.flush();
    assert.ok(h.titles().includes('after-refresh'));
    assert.equal(h.count('Apps'), '5');
    assert.ok(!h.titles().includes('obsolete-refresh'));
  } finally { h.close(); }
});

test('LAE token changes clear all prior-account caches and reject pending account results', async () => {
  const h = harness();
  try {
    request(h, 'applications').resolve({ applications: [application('private-account-a-app')], page: page(11) });
    request(h, 'tenants').resolve({ tenants: [tenant], page: page(6) });
    await h.flush();
    h.clickTab('Users');
    request(h, 'users').resolve({ users: [user('account-a@example.com')], page: page(8) });
    await h.flush();
    assert.ok(h.titles().includes('account-a@example.com'));
    const refreshStart = h.calls.length;
    h.refresh().props.onClick();
    h.render();
    const previousAccount = request(h, 'users', refreshStart);
    const changedStart = h.calls.length;
    h.render({ token: 'account-b' });
    assert.ok(previousAccount.signal.aborted);
    assert.deepEqual(h.calls.slice(changedStart).map(call => [call.resource, call.token]), [['users', 'account-b']]);
    assert.ok(!h.titles().includes('account-a@example.com'));
    for (const label of ['Apps', 'Users', 'Tenants']) assert.equal(h.count(label), '—');
    previousAccount.resolve({ users: [user('late-account-a@example.com')], page: page(88) });
    await h.flush();
    assert.ok(!h.titles().includes('late-account-a@example.com'));
    request(h, 'users', changedStart).resolve({ users: [user('account-b@example.com')], page: page(1) });
    await h.flush();
    assert.ok(h.titles().includes('account-b@example.com'));
    assert.equal(h.count('Users'), '1');
    const appStart = h.calls.length;
    h.clickTab('Apps');
    assert.ok(!h.titles().includes('private-account-a-app'));
    assert.deepEqual(h.calls.slice(appStart).map(call => [call.resource, call.token]).sort(), [['applications', 'account-b'], ['tenants', 'account-b']]);
  } finally { h.close(); }
});
