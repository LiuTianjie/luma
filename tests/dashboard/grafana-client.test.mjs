import assert from 'node:assert/strict';
import test from 'node:test';
import { grafanaGet, GrafanaReadError } from '../../dashboard-src/src/apiClient.ts';

const path = '/grafana/api/search?tag=luma-app%3Aworkers';
const rotationRequired = () => Response.json({ messageId: 'session.token.rotate' }, { status: 401 });

test('Grafana session rotation recovers the same read under one deadline', async (t) => {
  const controller = new AbortController();
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, options });
    if (requests.length === 1) return rotationRequired();
    if (requests.length === 2) return Response.json({ message: 'Token rotated' });
    return Response.json([{ uid: 'task-dashboard' }]);
  });
  assert.deepEqual(await grafanaGet(path, 'session:browser', controller.signal), [{ uid: 'task-dashboard' }]);
  assert.deepEqual(requests.map(({ url, options }) => [url, options.method || 'GET']), [
    [path, 'GET'], ['/grafana/api/user/auth-tokens/rotate', 'POST'], [path, 'GET'],
  ]);
  assert.ok(requests.every(({ options }) => options.signal === controller.signal));
  assert.ok(requests.every(({ options }) => !options.headers.Authorization));
});

test('ordinary authorization, overload and upstream failures are not retried', async (t) => {
  for (const status of [401, 403, 429, 500]) {
    const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ message: 'Unavailable' }, { status }));
    await assert.rejects(grafanaGet(path, 'management-token', new AbortController().signal),
      (error) => error instanceof GrafanaReadError && error.status === status);
    assert.equal(fetch.mock.callCount(), 1);
    assert.equal(fetch.mock.calls[0].arguments[1].headers.Authorization, 'Bearer management-token');
    fetch.mock.restore();
  }
});

test('a failed rotation stops; a second rotation request is not looped', async (t) => {
  for (const rotationFails of [true, false]) {
    let calls = 0;
    const fetch = t.mock.method(globalThis, 'fetch', async () => {
      calls += 1;
      return calls === 2 && !rotationFails ? Response.json({}) : rotationRequired();
    });
    await assert.rejects(grafanaGet(path, 'session:browser', new AbortController().signal),
      (error) => error instanceof GrafanaReadError && error.status === 401);
    assert.equal(calls, rotationFails ? 2 : 3);
    fetch.mock.restore();
  }
});

test('cancellation during rotation prevents the follow-up read', async (t) => {
  const controller = new AbortController();
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    calls += 1;
    if (calls === 1) return rotationRequired();
    controller.abort();
    options.signal.throwIfAborted();
  });
  await assert.rejects(grafanaGet(path, 'session:browser', controller.signal), { name: 'AbortError' });
  assert.equal(calls, 2);
});
