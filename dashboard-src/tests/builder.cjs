const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const test = require('node:test');
const ts = require('typescript');
function load(name, imports = {}) {
  const filename = path.resolve(__dirname, '../src', name + '.ts');
  const mod = new Module(filename, module);
  mod.paths = module.paths;
  mod.require = id => imports[id] || require(id);
  mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
  return mod.exports;
}
const { buildImportStream } = load('deploy/deployApi', { '../apiClient': load('apiClient') });
test('Git submission uses the independent queue with defaults and follows its exact result', async () => {
  const previous = global.fetch;
  const calls = [], steps = [];
  global.fetch = async (url, options) => {
    calls.push({url, options});
    return new Response(JSON.stringify({build: calls.length === 1 ? {id: 'build-abc', status: 'queued'} : {id: 'build-abc', status: 'succeeded', events: [{seq: 1, stage: 'build', message: 'Runner claimed task'}]}}));
  };
  try {
    const result = await buildImportStream({token: 'session:test', repoUrl: 'https://github.com/acme/app', buildNode: 'obsolete', registryHost: 'localhost:5000', pushHost: 'localhost:5000'}, step => steps.push(step));
    assert.deepEqual(JSON.parse(calls[0].options.body), {repoUrl: 'https://github.com/acme/app'});
    assert.equal(calls[0].url, '/v1/builder/builds');
    assert.equal(calls[1].url, '/v1/builder/builds/build-abc?afterSeq=0');
    assert.equal(calls[0].options.headers.Authorization, undefined);
    assert.equal(result.status, 'succeeded');
    assert.ok(steps.some(step => step.message === 'Runner claimed task'));
  } finally { global.fetch = previous; }
});
test('a failed Builder result never appears as a successful deployment', async () => {
  const previous = global.fetch;
  let count = 0;
  global.fetch = async () => new Response(JSON.stringify({build: {id: 'build-failed', status: ++count === 1 ? 'queued' : 'failed', message: 'ACR push rejected'}}));
  try {
    await assert.rejects(buildImportStream({token: 'session:test', repoUrl: 'https://github.com/acme/app'}, () => {}), /ACR push rejected/);
  } finally { global.fetch = previous; }
});
