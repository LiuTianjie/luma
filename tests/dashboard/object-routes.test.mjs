import assert from 'node:assert/strict';
import test from 'node:test';
import { nodePath, servicePath, updatePath, terminalPath, parseObjectRoute } from '../../dashboard-src/src/objectRoutes.ts';
test('object and shell routes preserve identities containing reserved URL characters', () => {
  const name='stack/service + #1';
  assert.deepEqual(parseObjectRoute(nodePath(name)), {kind:'node',name});
  assert.deepEqual(parseObjectRoute(servicePath(name)), {kind:'service',name});
  assert.deepEqual(parseObjectRoute(updatePath(name)), {kind:'update',name});
  assert.deepEqual(parseObjectRoute(terminalPath('service',name,'stack').split('?')[0]), {kind:'service-terminal',name});
});
test('infrastructure tasks are not mistaken for node identities', () => {
  for (const route of ['/fleet/join','/fleet/network','/fleet/maintenance','/apps/demo/services']) assert.equal(parseObjectRoute(route),null);
});
test('malformed encoded object identity fails without throwing', () => {
  assert.deepEqual(parseObjectRoute('/terminal/node/%E0%A4%A'),{kind:'node-terminal',name:''});
});
