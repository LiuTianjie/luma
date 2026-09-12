const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const test = require('node:test');
const ts = require('typescript');
const filename = path.resolve(__dirname, '../src/components/trafficTopology.ts');
const mod = new Module(filename, module);
mod.filename = filename;
mod.paths = module.paths;
mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
const { buildTopology, normalizePathSegments, routeElementIds } = mod.exports;
const paths = [
  { id: 'same-id', kind: 'cn-edge', domain: 'a.example.com', segments: ['DNS', 'proxy', 'service-a', 'node-a'] },
  { id: 'same-id', kind: 'cn-edge', domain: 'b.example.com', segments: ['DNS', 'proxy', 'service-b', 'node-b'] },
];
const graph = buildTopology(paths);
const node = label => graph.elements.find(element => element.data.label === label || element.data.label?.endsWith('\n' + label));
test('hovering any hop traces the entire matching route, without leaking through a shared proxy', () => {
  for (const hop of ['a.example.com', 'service-a', 'node-a']) {
    const ids = routeElementIds(graph.elements, node(hop).data.id);
    assert.ok(ids.includes(node('a.example.com').data.id));
    assert.ok(ids.includes(node('node-a').data.id));
    assert.ok(!ids.includes(node('b.example.com').data.id));
    assert.ok(!ids.includes(node('node-b').data.id));
    assert.equal(graph.edges.filter(edge => ids.includes(edge.id)).length, 4);
  }
});
test('shared hops trace all participating paths and duplicate common links merge', () => {
  assert.equal(routeElementIds(graph.elements, node('proxy').data.id).length, graph.elements.length);
  assert.equal(graph.edges.length, 7);
  const commonEdge = graph.edges.find(edge => edge.source === node('DNS').data.id && edge.target === node('proxy').data.id);
  assert.deepEqual(commonEdge.routeKeys, ['0', '1']);
  assert.equal(routeElementIds(graph.elements, commonEdge.id).length, graph.elements.length);
});
test('all legacy hops are retained, including internal and tunnel destinations', () => {
  for (const kind of ['cn-edge', 'external-edge', 'cloudflare-tunnel', 'internal']) {
    assert.deepEqual(normalizePathSegments({ kind, segments: ['a', 'b', 'c', 'd', 'e'] }), ['a', 'b', 'c', 'd', 'e']);
  }
});
test('destination deduplication only removes exact trailing endpoints, preserving earlier ingress and similar names', () => {
  assert.deepEqual(normalizePathSegments({ domain: 'app.example.com', segments: ['10.0.0.1', 'my-node-service:8080', 'node'], destinations: [{ node: 'node', nodeAddress: '10.0.0.1' }] }), ['app.example.com', '10.0.0.1', 'my-node-service:8080']);
});
test('each replica is part of its route and unknown hover targets cannot highlight unrelated paths', () => {
  const replicaGraph = buildTopology([{ domain: 'app.example.com', segments: ['proxy'], destinations: [{ node: 'one', state: 'running' }, { node: 'two', state: 'running' }] }]);
  assert.equal(routeElementIds(replicaGraph.elements, replicaGraph.nodes[0].id).length, replicaGraph.elements.length);
  assert.deepEqual(routeElementIds(replicaGraph.elements, 'missing'), []);
});
