import assert from 'node:assert/strict';
import test from 'node:test';
import { applicationPath, parseApplicationPath, APPLICATION_TABS, visibleApplicationTabs } from '../../dashboard-src/src/components/applicationRoutes.ts';

test('application detail URLs round-trip object names without changing route boundaries', () => {
  for (const stack of ['word2pdf', '应用 空格', 'team/app', 'a?b#c%']) {
    for (const { id } of APPLICATION_TABS) {
      const route = parseApplicationPath(applicationPath(stack, id));
      assert.equal(route.stack, stack);
      assert.equal(route.tab, id);
    }
  }
});

test('service identity survives an encoded slash for direct links and reloads', () => {
  const route = parseApplicationPath(applicationPath('word2pdf', 'services', 'word2pdf/web'));
  assert.deepEqual(route, { stack: 'word2pdf', tab: 'services', service: 'word2pdf/web' });
});

test('legacy short paths default to overview and malformed escape sequences do not crash the dashboard', () => {
  assert.equal(parseApplicationPath('/apps/word2pdf').tab, 'overview');
  assert.equal(parseApplicationPath('/apps').stack, null);
  assert.equal(parseApplicationPath('/apps/%broken/config').stack, '%broken');
  assert.equal(parseApplicationPath('/nodes/manager').stack, null);
});

test('application tabs adapt cleanly to installed addon capabilities', () => {
  const coreTabs = visibleApplicationTabs({ known: true, active: [], capabilities: [], urls: {} }).map((t) => t.id);
  assert.ok(coreTabs.includes('logs'));
  assert.ok(coreTabs.includes('metrics'));
  assert.ok(!coreTabs.includes('dashboards'));
  assert.ok(!coreTabs.includes('eye'));

  const observeTabs = visibleApplicationTabs({ known: true, active: ['observe'], capabilities: ['observe.dashboards'], urls: {} }).map((t) => t.id);
  assert.ok(observeTabs.includes('dashboards'));
  assert.ok(!observeTabs.includes('eye'));

  const eyeUnmatchedTabs = visibleApplicationTabs({ known: true, active: ['eye'], capabilities: ['eye.rum'], urls: {} }).map((t) => t.id);
  assert.ok(!eyeUnmatchedTabs.includes('eye'));
  assert.ok(!eyeUnmatchedTabs.includes('dashboards'));

  const eyeMatchedTabs = visibleApplicationTabs(
    { known: true, active: ['eye'], capabilities: ['eye.rum'], urls: {} },
    { eyeMatched: true },
  ).map((t) => t.id);
  assert.ok(eyeMatchedTabs.includes('eye'));
  assert.ok(!eyeMatchedTabs.includes('dashboards'));

  const eyeDirectTabs = visibleApplicationTabs(
    { known: true, active: ['eye'], capabilities: ['eye.rum'], urls: {} },
    { eyeMatched: false, activeTab: 'eye' },
  ).map((t) => t.id);
  assert.ok(eyeDirectTabs.includes('eye'));

  const eyeWithoutCapability = visibleApplicationTabs(
    { known: true, active: [], capabilities: [], urls: {} },
    { eyeMatched: true, activeTab: 'eye' },
  ).map((t) => t.id);
  assert.ok(!eyeWithoutCapability.includes('eye'));
});
