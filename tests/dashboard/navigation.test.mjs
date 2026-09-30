import assert from 'node:assert/strict';
import test from 'node:test';
import { activeNavChild, buildNavGroups, navWorkspace } from '../../dashboard-src/src/navItems.ts';
import { ROUTE_BY_PAGE, legacyRedirect, pageForPath } from '../../dashboard-src/src/routes.ts';

const vm = { issueCounts: { critical: 0, warning: 0, info: 0 } };
const items = buildNavGroups('zh', vm).flatMap((group) => group.items);
const item = (id) => items.find((entry) => entry.id === id);
const childFor = (path) => activeNavChild(item(navWorkspace(pageForPath(path))), path)?.href;

test('every secondary destination resolves to a page highlighted under its own workspace', () => {
  for (const entry of items) {
    for (const child of entry.children || []) {
      const page = pageForPath(child.href);
      assert.notEqual(page, 'notfound', child.href);
      assert.equal(navWorkspace(page), entry.id, child.href);
      assert.equal(legacyRedirect(child.href), null, child.href);
    }
  }
});

test('each destination appears once and no two entries share a label', () => {
  const hrefs = items.flatMap((entry) => (entry.children || []).map((child) => child.href));
  assert.equal(new Set(hrefs).size, hrefs.length);
  const labels = items.flatMap((entry) => [entry.label, ...(entry.children || []).map((child) => child.label)]);
  assert.equal(new Set(labels).size, labels.length);
});

test('only the most specific secondary page is active', () => {
  assert.equal(childFor('/observe'), '/observe');
  assert.equal(childFor('/observe/rules'), '/observe/rules');
  assert.equal(childFor('/observe/logs'), '/observe/apps');
  assert.equal(childFor('/fleet'), '/fleet');
  assert.equal(childFor('/fleet/join'), '/fleet');
  assert.equal(childFor('/fleet/nodes/manager'), '/fleet');
  assert.equal(childFor('/terminal/node/manager'), '/fleet');
  assert.equal(childFor('/fleet/regions'), '/fleet/regions');
  assert.equal(childFor('/storage/governance'), '/storage');
  assert.equal(childFor('/registry/policy'), '/registry');
  assert.equal(childFor('/settings/secrets/new'), '/settings/secrets');
  assert.equal(childFor('/settings/maintenance'), '/settings/maintenance');
});

test('application creation belongs to applications, maintenance to platform settings', () => {
  assert.equal(navWorkspace(pageForPath('/create')), 'applications');
  assert.equal(navWorkspace(pageForPath('/create/image')), 'applications');
  assert.equal(navWorkspace(pageForPath('/builds')), 'applications');
  assert.equal(pageForPath(ROUTE_BY_PAGE.maintenance), 'maintenance');
  assert.equal(navWorkspace('maintenance'), 'credentials');
});

test('moved destinations redirect instead of rendering a missing page', () => {
  assert.equal(legacyRedirect('/fleet/maintenance'), '/settings/maintenance');
  assert.equal(legacyRedirect('/settings/storage'), '/storage');
  for (const path of ['/fleet', '/fleet/join', '/storage', '/settings/secrets', '/observe/logs']) assert.equal(legacyRedirect(path), null, path);
});
