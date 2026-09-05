const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const cache = new Map();
function load(relative) {
  const filename = path.resolve(__dirname, "../src", relative);
  if (cache.has(filename)) return cache.get(filename).exports;
  const mod = new Module(filename, module);
  cache.set(filename, mod);
  mod.filename = filename;
  mod.paths = module.paths;
  mod.require = (name) => name.startsWith(".") ? load(path.relative(path.resolve(__dirname, "../src"), path.resolve(path.dirname(filename), name + ".ts"))) : require(name);
  mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }, fileName: filename }).outputText, filename);
  return mod.exports;
}
const { channelRequest, ruleRequest, newAlertRule, mergeAlertPages, alertStatusLabel, localizeAlertPreset, getAlerting, postAlerting } = load("alertingApi.ts");

test("app bot channel edits preserve the write-only App Secret and require three connection fields for creation", () => {
  assert.deepEqual(channelRequest("channel-1", " Ops ", true, "cli_app", "", "oc_chat"), { id: "channel-1", name: "Ops", type: "feishu", enabled: true, appId: "cli_app", chatId: "oc_chat" });
  assert.deepEqual(channelRequest("channel-1", "Ops", true, " cli_app ", " new-secret ", " oc_chat "), { id: "channel-1", name: "Ops", type: "feishu", enabled: true, appId: "cli_app", appSecret: "new-secret", chatId: "oc_chat" });
  assert.equal(channelRequest("", "", true, "cli_app", "new-secret", "oc_chat").name, "飞书告警");
  assert.throws(() => channelRequest("", "Ops", true, "cli_app", "", "oc_chat"), /App Secret/);
  assert.throws(() => channelRequest("", "Ops", true, "", "secret", "oc_chat"), /App ID/);
  assert.throws(() => channelRequest("channel-1", "Ops", true, "cli_app", "", ""), /chat ID/);
});

test("zero-duration build-failure preset survives rule form serialization", () => {
  const preset = { metric: "build.failed", name: "Build failed", threshold: 0, forSeconds: 0, unit: "count", description: "" };
  const rule = newAlertRule(preset);
  assert.equal(rule.threshold, 0);
  assert.equal(rule.forSeconds, 0);
  rule.channelIds = ["ops", "ops", "team"];
  const saved = ruleRequest(rule);
  assert.equal(saved.id, undefined);
  assert.deepEqual(saved.channelIds, ["ops", "team"]);
  assert.equal(saved.noData, "keep");
  for (const [field, value] of [["threshold", NaN], ["forSeconds", -1], ["repeatSeconds", Infinity]]) assert.throws(() => ruleRequest({ ...rule, [field]: value }));
});

test("pagination merges boundary duplicates without losing updated incidents", () => {
  assert.deepEqual(mergeAlertPages([{ id: 9, status: "firing" }, { id: 8, status: "pending" }], [{ id: 8, status: "resolved" }, { id: 7, status: "resolved" }]), [{ id: 9, status: "firing" }, { id: 8, status: "resolved" }, { id: 7, status: "resolved" }]);
});

test("queued and retry states never present delivery as sent", () => {
  assert.equal(alertStatusLabel("pending", false), "Pending");
  assert.equal(alertStatusLabel("retry", false), "Retrying");
  assert.equal(alertStatusLabel("sending", true), "发送中");
  assert.equal(alertStatusLabel("sent", true), "已发送");
  assert.equal(alertStatusLabel("closed", true), "管理关闭");
  assert.notEqual(alertStatusLabel("closed", true), alertStatusLabel("resolved", true));
});

test("English preset text preserves metric identity and threshold values", () => {
  const preset = { metric: "node.disk", name: "磁盘空间紧张", description: "文件系统", threshold: 90, forSeconds: 300, unit: "percent" };
  const translated = localizeAlertPreset(preset, false);
  assert.equal(translated.name, "Disk space pressure");
  assert.equal(translated.metric, preset.metric);
  assert.equal(translated.threshold, 90);
  assert.equal(translated.unit, "%");
  assert.equal(preset.name, "磁盘空间紧张");
});

test("alert requests retain auth, cancellation, and server error details", async () => {
  const original = global.fetch;
  const calls = [];
  const signal = new AbortController().signal;
  global.fetch = async (url, options) => {
    calls.push({ url, options });
    if (options.method === "POST") return new Response(JSON.stringify({ error: "wait 30 seconds between channel tests" }), { status: 400 });
    return new Response(JSON.stringify({ items: [] }));
  };
  try {
    assert.deepEqual(await getAlerting("incidents?status=firing", "management-token", signal), { items: [] });
    assert.equal(calls[0].url, "/v1/alerting/incidents?status=firing");
    assert.equal(calls[0].options.headers.Authorization, "Bearer management-token");
    assert.equal(calls[0].options.signal, signal);
    await assert.rejects(postAlerting("channels/ops/test", "management-token", {}, signal), /wait 30 seconds/);
  } finally { global.fetch = original; }
});


test("observed values use metric units, bounded precision and explicit missing-data states", () => {
  // Compile the actual pure formatter from the component, without mounting React.
  const filename = path.resolve(__dirname, "../src/components/AlertingPanel.tsx");
  const source = ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const formatter = source.statements.find((node) => ts.isFunctionDeclaration(node) && node.name?.text === "formatAlertObservedValue");
  assert.ok(formatter, "formatter is exported by the rendered component");
  const mod = new Module(filename, module);
  mod._compile(ts.transpileModule(formatter.getText(source), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
  const format = mod.exports.formatAlertObservedValue;
  assert.equal(format("node.offline", 14.673213958740234, false, true), "14.7 秒");
  assert.equal(format("task.queue_age", 601.234567, false, false), "601.2 s");
  for (const metric of ["node.cpu", "node.memory", "node.disk", "node.inode"]) assert.equal(format(metric, 91.234567, false, true), "91.2%");
  assert.equal(format("node.cpu", 0, false, false), "0%");
  assert.equal(format("build.failed", 1, false, true), "失败");
  assert.equal(format("build.failed", 0, false, false), "Not failed");
  for (const value of [null, undefined, NaN, Infinity]) assert.equal(format("node.disk", value, false, true), "缺少采样");
  assert.equal(format("node.disk", 90, true, false), "No data");
});
