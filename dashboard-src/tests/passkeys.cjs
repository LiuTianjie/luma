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
  mod.require = (id) => imports[id] || require(id);
  mod._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
  return mod.exports;
}
const apiClient = load('apiClient');
const auth = load('authApi', { './apiClient': apiClient });
const bytes = (buffer) => [...new Uint8Array(buffer)];
const b64url = (values) => Buffer.from(values).toString('base64url');

test('server options become WebAuthn buffers without losing other fields', () => {
  const creation = auth.creationOptions({
    rp: { id: 'luma.example.com', name: 'Luma' },
    user: { id: b64url([1, 2, 3]), name: 'luma@luma.example.com', displayName: 'Luma' },
    challenge: b64url([250, 251, 252, 253]),
    pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
    excludeCredentials: [{ id: b64url([9, 8]), type: 'public-key', transports: ['internal'] }],
    authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
  });
  assert.deepEqual(bytes(creation.challenge), [250, 251, 252, 253]);
  assert.deepEqual(bytes(creation.user.id), [1, 2, 3]);
  assert.equal(creation.user.name, 'luma@luma.example.com');
  assert.deepEqual(bytes(creation.excludeCredentials[0].id), [9, 8]);
  assert.deepEqual(creation.excludeCredentials[0].transports, ['internal']);
  assert.equal(creation.authenticatorSelection.userVerification, 'required');

  const request = auth.requestOptions({ challenge: b64url([7]), rpId: 'luma.example.com', allowCredentials: [{ id: b64url([255, 254]), type: 'public-key' }] });
  assert.deepEqual(bytes(request.challenge), [7]);
  assert.deepEqual(bytes(request.allowCredentials[0].id), [255, 254]);
  assert.equal(request.rpId, 'luma.example.com');
});

test('browser credentials serialize to the base64url JSON Control verifies', () => {
  const buffer = (values) => new Uint8Array(values).buffer;
  const base = { id: 'AQI', rawId: buffer([1, 2]), type: 'public-key', authenticatorAttachment: 'platform', getClientExtensionResults: () => ({}) };
  const registration = auth.registrationJson({
    ...base,
    response: { clientDataJSON: buffer([123]), attestationObject: buffer([0xfb, 0xff]), getTransports: () => ['internal', 'hybrid'] },
  });
  assert.deepEqual(registration, {
    id: 'AQI', rawId: 'AQI', type: 'public-key', clientExtensionResults: {}, authenticatorAttachment: 'platform',
    response: { clientDataJSON: 'ew', attestationObject: '-_8', transports: ['internal', 'hybrid'] },
  });
  const assertion = auth.authenticationJson({
    ...base,
    authenticatorAttachment: null,
    response: { clientDataJSON: buffer([123]), authenticatorData: buffer([1]), signature: buffer([2]), userHandle: null },
  });
  assert.equal('authenticatorAttachment' in assertion, false, 'a null attachment is omitted, not sent as null');
  assert.deepEqual(assertion.response, { clientDataJSON: 'ew', authenticatorData: 'AQ', signature: 'Ag', userHandle: null });
});

test('the token is exchanged once and never sent with later session requests', async () => {
  const previous = global.fetch;
  const calls = [];
  global.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return { ok: true, status: 200, text: async () => JSON.stringify({ session: { id: 's1', method: 'token' }, passkeys: [] }) };
  };
  try {
    await auth.exchangeToken('  luma_secret  ');
    assert.equal(calls[0].url, '/v1/auth/session');
    assert.equal(calls[0].options.headers.Authorization, 'Bearer luma_secret');
    await auth.listPasskeys(apiClient.sessionCredential());
    assert.equal(calls[1].options.headers.Authorization, undefined);
  } finally { global.fetch = previous; }
});

test('signed-out and missing sign-in API are told apart', async () => {
  const previous = global.fetch;
  try {
    global.fetch = async () => ({ ok: false, status: 401, text: async () => JSON.stringify({ error: 'unauthorized' }) });
    assert.equal(await auth.fetchCurrentSession(), null);
    global.fetch = async () => ({ ok: false, status: 404, text: async () => 'Not Found' });
    await assert.rejects(auth.fetchCurrentSession(), auth.SignInUnavailable);
  } finally { global.fetch = previous; }
});
