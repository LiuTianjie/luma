const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");

// Execute the actual pure TypeScript models without a new test runner or build output.
function load(relativePath) {
  const filename = path.resolve(__dirname, "../src", relativePath);
  const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    fileName: filename,
  }).outputText;
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  mod._compile(output, filename);
  return mod.exports;
}

const { applicationEndpoints } = load("components/applicationEndpoints.ts");
const { activeDismissals, dismissalStorageKey, DISMISS_DURATION_MS, issueKey } = load("issueDismissals.ts");

test("mixed applications retain HTTP links and display TCP published ports without links", () => {
  assert.deepEqual(applicationEndpoints([
    { domain: "app.example.com", exposure: "cn-edge" },
    { domain: "db.example.com", exposure: "tcp-relay", publishPort: "15432", targetPort: "5432" },
    { domain: "db.example.com", exposure: "tcp-relay", publishPort: "15432", targetPort: "5432" },
    { domain: "hidden.example.com", exposure: "none" },
  ]), [
    { address: "app.example.com", protocol: "http", href: "https://app.example.com/" },
    { address: "db.example.com:15432", protocol: "tcp" },
  ]);
});

test("TCP default ports and existing ports are preserved, including IPv6", () => {
  assert.deepEqual(applicationEndpoints([
    { domain: "db.example.com", exposure: "tcp-relay", targetPort: "5432" },
    { domain: "db.example.com:15432", exposure: "tcp-relay", targetPort: "5432" },
    { domain: "[2001:db8::1]", exposure: "tcp-relay", publishPort: "15432" },
    { domain: "2001:db8::1", exposure: "tcp-relay", targetPort: "5432" },
  ]).map((item) => item.address), ["db.example.com:5432", "db.example.com:15432", "[2001:db8::1]:15432", "[2001:db8::1]:5432"]);
});

test("only valid web addresses become navigable links", () => {
  assert.deepEqual(applicationEndpoints([
    { domain: "http://local.example.com:8080", exposure: "tailscale-relay" },
    { domain: "javascript:alert(1)", exposure: "external-edge" },
    { domain: "ftp://example.com", exposure: "external-edge" },
    { domain: "https://user:secret@example.com", exposure: "external-edge" },
  ]), [{ address: "http://local.example.com:8080", protocol: "http", href: "http://local.example.com:8080/" }]);
});

test("dismissals expire and recovered issues reappear on recurrence", () => {
  const now = 10_000;
  const key = issueKey({ severity: "warning", kind: "service", target: "app", message: "Missing replica" });
  const stored = { [key]: now + DISMISS_DURATION_MS };
  assert.deepEqual(activeDismissals(stored, new Set([key]), now), stored);
  assert.deepEqual(activeDismissals(stored, new Set([key]), now + DISMISS_DURATION_MS), {});
  const afterRecovery = activeDismissals(stored, new Set(), now + 1);
  assert.deepEqual(activeDismissals(afterRecovery, new Set([key]), now + 2), {});
});

test("changed faults, invalid persistence and legacy permanent dismissals cannot hide risks", () => {
  const key = issueKey({ target: "app", message: "warning" });
  const next = issueKey({ target: "app", message: "critical" });
  assert.deepEqual(activeDismissals({ [key]: 500 }, new Set([next]), 100), {});
  for (const invalid of [null, [key], { [key]: "500" }, { [key]: Infinity }, { [key]: DISMISS_DURATION_MS + 101 }]) {
    assert.deepEqual(activeDismissals(invalid, new Set([key]), 100), {});
  }
});

test("issue identities and storage scopes cannot collide", () => {
  assert.notEqual(issueKey({ kind: "a|b", target: "c" }), issueKey({ kind: "a", target: "b|c" }));
  assert.notEqual(dismissalStorageKey("cn", "https://one"), dismissalStorageKey("global", "https://one"));
  assert.notEqual(dismissalStorageKey("cn", "https://one"), dismissalStorageKey("cn", "https://two"));
});


test("image identity abbreviates immutable digests without confusing registry ports with tags", () => {
  const { formatImageIdentity } = load("format.ts");
  assert.equal(formatImageIdentity(`registry.example:5000/app@sha256:${"abcdef01".repeat(8)}`), "sha256:abcdef01abcd");
  assert.equal(formatImageIdentity("registry.example:5000/team/app:v2.3"), "v2.3");
  assert.equal(formatImageIdentity("registry.example:5000/team/app"), "app");
  assert.equal(formatImageIdentity(), "");
});
