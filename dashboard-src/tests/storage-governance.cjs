const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/components/storageGovernanceModel.ts");
const mod = new Module(filename, module);
mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
const { storageTime, storageBytes, cleanupPlanGate, storageTaskFinished } = mod.exports;
test("unknown usage is distinct from zero and negative growth must be formatted separately", () => {
  for (const unknown of [undefined, null, NaN, Infinity, -1]) assert.equal(storageBytes(unknown, "unknown"), "unknown");
  assert.equal(storageBytes(0, "unknown"), "0 B");
  assert.equal(storageBytes(1536, "unknown"), "1.5 KiB");
});
test("grace, expiration and protected conditions block a plan at exact boundaries", () => {
  const plan = { planId: "reviewed", eligibleAfter: 1700000000, expiresAt: 1700003600 };
  assert.equal(cleanupPlanGate(null), "missing");
  assert.equal(cleanupPlanGate({ planId: "no-time-bound" }), "missing");
  assert.equal(cleanupPlanGate({ ...plan, expiresAt: "invalid" }), "missing");
  assert.equal(cleanupPlanGate(plan, 1699999999999), "grace");
  assert.equal(cleanupPlanGate(plan, 1700000000000), "ready");
  assert.equal(cleanupPlanGate(plan, 1700003600000), "expired");
  assert.equal(cleanupPlanGate({ ...plan, blockedReasons: ["running build"] }, 1700000000000), "blocked");
});
test("timestamp variants and terminal statuses are handled without treating queued as success", () => {
  assert.equal(storageTime(1700000000), 1700000000000);
  assert.equal(storageTime(1700000000000), 1700000000000);
  assert.equal(storageTime("invalid"), null);
  assert.equal(storageTaskFinished("queued"), false);
  assert.equal(storageTaskFinished("running"), false);
  assert.equal(storageTaskFinished("succeeded"), true);
  assert.equal(storageTaskFinished("failed"), true);
});
