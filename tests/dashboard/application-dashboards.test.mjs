import assert from 'node:assert/strict';
import test from 'node:test';
import {
  applicationDashboardSearchPath,
  applicationDashboardUrl,
  readApplicationDashboards,
} from '../../dashboard-src/src/components/applicationDashboardModel.ts';

const app = 'orders-workers';
const dashboard = (uid, title, extra = {}) => ({
  uid, title, type: 'dash-db', tags: [`luma-app:${app}`], ...extra,
});

test('dashboard discovery encodes the exact application tag as one bounded search', () => {
  const name = 'team/app & region=cn';
  const url = new URL(applicationDashboardSearchPath(name), 'https://luma.example.net');
  assert.equal(url.pathname, '/grafana/api/search');
  assert.deepEqual([...url.searchParams], [
    ['type', 'dash-db'], ['tag', `luma-app:${name}`], ['limit', '100'],
  ]);
});

test('discovery accepts only dashboards explicitly associated with this application', () => {
  const payload = [
    dashboard('tasks', 'Tasks'),
    dashboard('http', ' HTTP '),
    dashboard('tasks', 'Duplicate'),
    dashboard('other', 'Other', { tags: ['luma-app:another-app'] }),
    dashboard('prefix', 'Prefix', { tags: [`luma-app:${app}-test`] }),
    dashboard('folder', 'Folder', { type: 'dash-folder' }),
    dashboard('untagged', 'Untagged', { tags: undefined }),
    dashboard('invalid', '   '),
    dashboard('../explore', 'Unsafe UID'),
    dashboard('x'.repeat(41), 'Too long'),
    null, 'invalid', {},
  ];
  assert.deepEqual(readApplicationDashboards(payload, app), [
    { uid: 'http', title: 'HTTP' }, { uid: 'tasks', title: 'Tasks' },
  ]);
  assert.deepEqual(readApplicationDashboards([], app), []);
  assert.throws(() => readApplicationDashboards({ message: 'Unavailable' }, app), /Invalid Grafana/);
});

test('embedded and standalone links stay on the authenticated Grafana origin', () => {
  const [entry] = readApplicationDashboards([
    dashboard('tasks_v2-1', 'Tasks', { url: 'https://untrusted.example/d/tasks' }),
  ], app);
  const name = 'orders & var-app=other';
  const embedded = new URL(applicationDashboardUrl(entry.uid, name), 'https://luma.example.net');
  assert.equal(embedded.origin, 'https://luma.example.net');
  assert.equal(embedded.pathname, '/grafana/d/tasks_v2-1');
  assert.equal(embedded.searchParams.get('var-app'), name);
  assert.equal(embedded.searchParams.get('kiosk'), 'tv');
  const standalone = new URL(applicationDashboardUrl(entry.uid, name, false), embedded.origin);
  assert.equal(standalone.searchParams.has('kiosk'), false);
  assert.equal(standalone.searchParams.get('var-app'), name);
  for (const uid of ['', '../other', 'foo?orgId=2', 'foo#bar', '//other', '%2e%2e']) {
    assert.throws(() => applicationDashboardUrl(uid, app), /Invalid Grafana dashboard UID/);
  }
});
