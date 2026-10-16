import test from 'node:test';
import assert from 'node:assert/strict';
import { exposureAllowedInRegion, requiredRegionForExposure } from '../../dashboard-src/src/deploy/options.ts';
import { serviceExposureRegion, updateComposeServiceExposure } from '../../dashboard-src/src/deploy/yaml.ts';

test('relay selection preserves a custom region and its pinned node', () => {
  assert.equal(serviceExposureRegion('tailscale-relay', 'render'), 'render');
  const service = { name: 'api', exposure: 'none', region: 'render', node: 'air1' };
  assert.deepEqual(updateComposeServiceExposure(service, 'tailscale-relay'), { ...service, exposure: 'tailscale-relay' });
  const inherited = { ...service, region: '' };
  assert.equal(updateComposeServiceExposure(inherited, 'tailscale-relay').region, '');
  assert.equal(requiredRegionForExposure('tailscale-relay'), '');
});

test('deployment options follow the Control region catalog', () => {
  const enabled = [{ name: 'render', exposures: ['none', 'tailscale-relay'] }];
  assert.equal(exposureAllowedInRegion('tailscale-relay', 'render', enabled), true);
  assert.equal(exposureAllowedInRegion('tcp-relay', 'render', enabled), false);
  assert.equal(exposureAllowedInRegion('tailscale-relay', 'render'), false);
  assert.equal(exposureAllowedInRegion('none', 'render'), true);
  assert.equal(exposureAllowedInRegion('tailscale-relay', 'render', [{ name: 'render', exposures: ['none'] }]), false);
});

test('built-in default ingress policies stay unchanged', () => {
  assert.equal(exposureAllowedInRegion('tailscale-relay', 'home'), true);
  assert.equal(exposureAllowedInRegion('cn-edge', 'cn'), true);
  assert.equal(exposureAllowedInRegion('external-edge', 'global'), true);
  assert.equal(exposureAllowedInRegion('tailscale-relay', 'cn'), false);
});
