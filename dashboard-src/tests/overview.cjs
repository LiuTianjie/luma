const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/pages/overviewModel.ts");
const mod = new Module(filename, module);
mod.filename = filename;
mod.paths = module.paths;
mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
const { groupOverviewIssues, nodePressure } = mod.exports;

test("overview groups explicit application and node evidence without losing raw diagnostics", () => {
  const app = { stack: "api", services: [{ fullName: "api-web" }, { fullName: "api-worker" }] };
  const node = { name: "manager" };
  const issues = [
    { kind: "service-pending", target: "api-web", severity: "warning", message: "Pending" },
    { kind: "service-failed", target: "api-worker", severity: "critical", message: "Failed" },
    { kind: "node-memory", target: "manager", severity: "warning", message: "Memory" },
    { kind: "agent", target: "manager", severity: "warning", message: "Heartbeat" },
  ];
  const groups = groupOverviewIssues(issues, [app], [node]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].app, app);
  assert.equal(groups[0].severity, "critical");
  assert.deepEqual(groups[0].issues, issues.slice(0, 2));
  assert.equal(groups[1].node, node);
  assert.deepEqual(groups[1].issues, issues.slice(2));
});

test("overview does not guess root causes or associate ambiguous service names", () => {
  const apps = [{ stack: "a", services: [{ name: "web" }] }, { stack: "b", services: [{ name: "web" }] }];
  const issues = [
    { kind: "service-failed", target: "web", message: "Timeout" },
    { kind: "network", target: "web", message: "Timeout" },
    { kind: "node-state", target: "other", message: "Timeout" },
  ];
  const groups = groupOverviewIssues(issues, apps, []);
  assert.equal(groups.length, 3);
  assert.ok(groups.every((group) => !group.app && !group.node));
  assert.equal(groups.flatMap((group) => group.issues).length, issues.length);
});

test("disk pressure is included when prioritizing node capacity", () => {
  assert.equal(nodePressure({ metrics: { cpuPercent: 0, loadPercent: 99, memoryUsedPercent: 10, diskUsedPercent: 96 } }), 96);
  assert.equal(nodePressure({}), 0);
});
