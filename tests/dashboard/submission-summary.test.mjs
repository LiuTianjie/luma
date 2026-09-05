import test from 'node:test';
import assert from 'node:assert/strict';
import { submissionSummary } from '../../dashboard-src/src/deploy/submissionSummary.ts';

test('deployment confirmation follows edited YAML target, not the original form', () => {
  const summary = submissionSummary('service', 'name: changed-app\nimage: registry/app:v2\nregion: cn\nexposure: cn-edge\ndomain: app.example.com\nport: 8080\nstorage:\n  data: {storageClass: shared}\n');
  assert.equal(summary.name, 'changed-app');
  assert.equal(summary.region, 'cn');
  assert.deepEqual(summary.images, ['changed-app: registry/app:v2']);
  assert.deepEqual(summary.volumes, ['data']);
  assert.match(summary.ingress[0], /app.example.com:8080/);
});
test('compose impact uses actual compose services and sidecar placement', () => {
  const summary = submissionSummary('compose', 'name: app\nregion: home\nservices:\n  web: {exposure: none}\n', 'services:\n  web: {image: web:v2}\n  worker: {image: worker:v2}\n');
  assert.deepEqual(summary.services, ['web', 'worker']);
  assert.deepEqual(summary.images, ['web: web:v2', 'worker: worker:v2']);
  assert.deepEqual(summary.ingress, []);
});
test('invalid or incomplete edited YAML cannot yield a deployable target', () => {
  for (const yaml of ['', '[]', 'name: [', 'image: nginx']) assert.throws(() => submissionSummary('service', yaml));
  assert.throws(() => submissionSummary('compose', 'name: app', 'volumes: {}'));
});
