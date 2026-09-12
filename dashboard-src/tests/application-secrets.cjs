const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const test = require('node:test');
const ts = require('typescript');
const filename = path.resolve(__dirname, '../src/applicationSecrets.ts');
const mod = new Module(filename, module);
mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
const { applicationSecretRows, applicationSecretScope } = mod.exports;
test('application secrets preserve scope isolation and override global values', () => {
  assert.equal(applicationSecretScope(' My_App.v2 '), 'my-app-v2');
  const rows = applicationSecretRows(['app/DB_PASSWORD', 'other/PRIVATE_TOKEN', 'DB_PASSWORD', 'SHARED', 'UNRELATED', 'app/UNUSED'], 'app', ['password: ${DB_PASSWORD}\ntoken: ${SHARED}\nmissing: ${NEW_VALUE}']);
  assert.deepEqual(rows, [
    {name:'DB_PASSWORD',source:'application',referenced:true},
    {name:'NEW_VALUE',source:'missing',referenced:true},
    {name:'SHARED',source:'global',referenced:true},
    {name:'UNUSED',source:'application',referenced:false},
  ]);
});
test('config failure can still show owned secrets without listing unrelated globals or scope prefixes', () => {
  assert.deepEqual(applicationSecretRows(['app/A','app-extra/B','GLOBAL'], 'app', []), [{name:'A',source:'application',referenced:false}]);
});
