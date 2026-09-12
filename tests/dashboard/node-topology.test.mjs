import test from 'node:test';
import assert from 'node:assert/strict';
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  layoutNodeTopology,
  nodeTopologySnapshot,
} from '../../dashboard-src/src/components/nodeTopologyModel.ts';

function fixture() {
  return {
    nodes: [
      {
        name: 'host-a', displayName: 'Primary host', region: 'cn', leader: true,
        state: 'ready', agentStatus: 'connected', agentVersion: '1.0',
        agentLastSeen: 100, metrics: { cpuPercent: 10, memoryUsedPercent: 20 },
        capacity: { cpus: 4, memoryBytes: 8192 },
      },
      { name: 'host-b', region: 'cn', leader: false, state: 'ready' },
    ],
    services: [
      {
        name: 'api', fullName: 'app-api', stack: 'app', region: 'cn', exposure: 'none',
        nodes: ['host-a', 'host-b'], image: 'api:v1', running: 2, desired: 2,
        resources: { actual: { cpuPercent: 10, memoryUsageBytes: 1024 } },
        tasks: [{ id: 'task-a', node: 'host-a', state: 'running', cpuPercent: 10 }],
      },
    ],
  };
}

function connections(topology) {
  return topology.edges.map(({ source, target }) => `${source}->${target}`).sort();
}

function assertValidLayout(topology) {
  const nodes = new Map(topology.nodes.map((node) => [node.id, node]));
  assert.equal(nodes.size, topology.nodes.length, 'node IDs are unique');
  assert.ok(Number.isFinite(NODE_WIDTH) && NODE_WIDTH > 0);
  assert.ok(Number.isFinite(NODE_HEIGHT) && NODE_HEIGHT > 0);
  for (const node of topology.nodes) {
    assert.ok(Number.isFinite(node.x), `${node.id} has a finite x coordinate`);
    assert.ok(Number.isFinite(node.y), `${node.id} has a finite y coordinate`);
  }
  assert.equal(new Set(connections(topology)).size, topology.edges.length, 'connections are unique');
  for (const edge of topology.edges) {
    assert.ok(nodes.has(edge.source), `source ${edge.source} exists`);
    assert.ok(nodes.has(edge.target), `target ${edge.target} exists`);
    assert.ok(nodes.get(edge.source).x < nodes.get(edge.target).x, 'edges follow the left-to-right layout');
  }
  const elements = new Map(topology.elements.map((element) => [element.data.id, element]));
  assert.equal(elements.size, topology.nodes.length + topology.edges.length);
  for (const node of topology.nodes) {
    assert.deepEqual(elements.get(node.id).data, { id: node.id, label: node.label, kind: node.kind });
    assert.deepEqual(elements.get(node.id).position, { x: node.x, y: node.y });
    assert.equal(elements.get(node.id).classes, node.kind);
  }
  for (const edge of topology.edges) {
    assert.deepEqual(elements.get(edge.id).data, edge);
  }
}

test('fresh polling objects and runtime-only changes preserve the topology snapshot', () => {
  const original = fixture();
  const expected = nodeTopologySnapshot(original.nodes, original.services, 'en');
  const refreshed = structuredClone(original);
  assert.equal(nodeTopologySnapshot(refreshed.nodes, refreshed.services, 'en'), expected);

  Object.assign(refreshed.nodes[0], {
    displayName: 'Renamed display alias', hostname: 'changed-hostname', role: 'worker',
    state: 'down', availability: 'drain', agentStatus: 'disconnected', agentOs: 'linux',
    agentVersion: '2.0', agentLastSeen: 200, terminalConnected: false,
    terminalStatus: 'offline', storageCapabilities: ['shared'],
    metrics: { cpuPercent: 90, memoryUsedPercent: 80 }, capacity: { cpus: 8, memoryBytes: 16384 },
  });
  Object.assign(refreshed.services[0], {
    image: 'api:v2', status: 'pending', running: 0, desired: 3, pending: 3, failed: 1,
    health: 'unhealthy', diagnostics: ['waiting for resources'],
    resources: { actual: { cpuPercent: 90, memoryUsageBytes: 8192 } },
    tasks: [{ id: 'task-b', node: 'host-b', state: 'pending', cpuPercent: 0 }],
  });
  assert.equal(nodeTopologySnapshot(refreshed.nodes, refreshed.services, 'en'), expected);
});

const graphChanges = [
  ['host name', ({ nodes }) => { nodes[0].name = 'host-renamed'; }],
  ['host leadership', ({ nodes }) => { nodes[0].leader = false; }],
  ['host region', ({ nodes }) => { nodes[0].region = 'home'; }],
  ['host addition', ({ nodes }) => { nodes.push({ name: 'host-c', region: 'home' }); }],
  ['service placement', ({ services }) => { services[0].nodes = ['host-b']; }],
  ['service becoming unplaced', ({ services }) => { services[0].nodes = []; }],
  ['service name', ({ services }) => { services[0].name = 'web'; }],
  ['service stack', ({ services }) => { services[0].stack = 'other-app'; }],
  ['service exposure', ({ services }) => { services[0].exposure = 'cn-edge'; }],
  ['service identity', ({ services }) => { services[0].fullName = 'app-api-new'; }],
  ['service removal', ({ services }) => { services.splice(0, 1); }],
];

for (const [name, change] of graphChanges) {
  test(`${name} changes invalidate the topology snapshot`, () => {
    const data = fixture();
    const before = nodeTopologySnapshot(data.nodes, data.services, 'en');
    change(data);
    assert.notEqual(nodeTopologySnapshot(data.nodes, data.services, 'en'), before);
  });
}

test('language changes invalidate the snapshot and update translated labels', () => {
  const en = nodeTopologySnapshot([{ name: 'unknown-host' }], [], 'en');
  const zh = nodeTopologySnapshot([{ name: 'unknown-host' }], [], 'zh');
  assert.notEqual(zh, en);
  const topology = layoutNodeTopology(zh);
  assert.equal(topology.nodes.find((node) => node.id === 'cluster:root').label, '集群\nCluster');
  assert.equal(topology.nodes.find((node) => node.id === 'region:unknown').label, 'Region\n未知');
});

test('real layout retains every host and placement while deduplicating repeated hosts', () => {
  const { nodes, services } = fixture();
  services[0].nodes.push('host-a', '');
  services[0].exposure = 'cn-edge';
  services[0].region = 'home';
  const topology = layoutNodeTopology(nodeTopologySnapshot(nodes, services, 'en'));
  assertValidLayout(topology);
  assert.deepEqual(topology.nodes.map((node) => node.id).sort(), [
    'cluster:root', 'node:host-a', 'node:host-b', 'region:cn', 'service:app-api',
  ]);
  assert.deepEqual(connections(topology), [
    'cluster:root->region:cn',
    'node:host-a->service:app-api',
    'node:host-b->service:app-api',
    'region:cn->node:host-a',
    'region:cn->node:host-b',
  ]);
  assert.equal(topology.nodes.find((node) => node.id === 'node:host-a').kind, 'leader');
  assert.equal(topology.nodes.find((node) => node.id === 'service:app-api').label, 'Public\napp/api');
  assert.equal(topology.nodes.find((node) => node.id === 'service:app-api').kind, 'exposedService');
});

test('missing hosts and unplaced services remain connected to their declared or unknown region', () => {
  const services = [
    { name: 'placed', fullName: 'placed', region: 'home', nodes: ['missing-host'] },
    { name: 'pending', fullName: 'pending', region: 'cn', nodes: [] },
    { name: 'unknown', fullName: 'unknown' },
    { name: 'blank-placement', fullName: 'blank-placement', region: 'cn', nodes: ['', ''] },
  ];
  const snapshot = nodeTopologySnapshot([], services, 'en');
  const topology = layoutNodeTopology(snapshot);
  assertValidLayout(topology);
  assert.deepEqual(connections(topology), [
    'cluster:root->region:cn',
    'cluster:root->region:home',
    'cluster:root->region:unknown',
    'node:missing-host->service:placed',
    'region:cn->service:blank-placement',
    'region:cn->service:pending',
    'region:home->node:missing-host',
    'region:unknown->service:unknown',
  ]);
  assert.equal(topology.nodes.find((node) => node.id === 'node:missing-host').label, 'Worker\nmissing-host');
  assert.equal(topology.nodes.find((node) => node.id === 'region:unknown').label, 'Region\nunknown');
  const changed = structuredClone(services);
  changed[1].region = 'home';
  assert.notEqual(nodeTopologySnapshot([], changed, 'en'), snapshot, 'unplaced service region affects topology');
  changed[1].region = 'cn';
  changed[0].region = 'cn';
  assert.notEqual(nodeTopologySnapshot([], changed, 'en'), snapshot, 'missing host region follows its service');
});

test('hosts can use display names when their names are unavailable', () => {
  const nodes = [{ displayName: 'fallback-host' }, { region: 'unused-region' }];
  const snapshot = nodeTopologySnapshot(nodes, [], 'en');
  const topology = layoutNodeTopology(snapshot);
  assertValidLayout(topology);
  assert.deepEqual(topology.nodes.map((node) => node.id).sort(), [
    'cluster:root', 'node:fallback-host', 'region:unknown',
  ]);
  nodes[0].displayName = 'renamed-fallback';
  assert.notEqual(nodeTopologySnapshot(nodes, [], 'en'), snapshot);
});

test('an empty cluster still produces a finite root layout', () => {
  const topology = layoutNodeTopology(nodeTopologySnapshot([], [], 'en'));
  assertValidLayout(topology);
  assert.equal(topology.nodes.length, 1);
  assert.equal(topology.nodes[0].id, 'cluster:root');
  assert.deepEqual(topology.edges, []);
});
