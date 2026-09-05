const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const cache = new Map();
function load(relativePath) {
  const filename = path.resolve(__dirname, "../src", relativePath);
  if (cache.has(filename)) return cache.get(filename);
  const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }, fileName: filename }).outputText;
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  const originalRequire = mod.require.bind(mod);
  mod.require = (id) => id.startsWith(".") ? load(path.relative(path.resolve(__dirname, "../src"), path.resolve(path.dirname(filename), `${id}.ts`))) : originalRequire(id);
  mod._compile(output, filename);
  cache.set(filename, mod.exports);
  return mod.exports;
}
const model = load("historyModel.ts");

test("history cursors stay opaque and only server filters enter the list request", () => {
  const url = new URL(model.historyListPath("entryKind=build&entryId=x&app=shop%2Fapi&status=failed&since=2026-09-01T00%3A00%3A00Z", "a+/=&?"), "https://control.example");
  assert.equal(url.pathname, "/v1/history");
  assert.equal(url.searchParams.get("limit"), "50");
  assert.equal(url.searchParams.get("cursor"), "a+/=&?");
  assert.equal(url.searchParams.get("app"), "shop/api");
  assert.equal(url.searchParams.has("entryId"), false);
  assert.equal(url.searchParams.has("entryKind"), false);
});

test("page overlap updates summaries without duplicating or moving them by updatedAt", () => {
  const first = [{ kind: "build", id: "same", createdAt: 30, updatedAt: 30, status: "running" }, { kind: "deployment", id: "same", createdAt: 20 }];
  const incoming = [{ kind: "build", id: "same", createdAt: 30, updatedAt: 200, status: "succeeded" }, { kind: "build", id: "older", createdAt: 10, updatedAt: 500 }];
  const merged = model.mergeHistoryItems(first, incoming);
  assert.equal(merged.length, 3);
  assert.deepEqual(merged.map((item) => [item.kind, item.id]), [["build", "same"], ["deployment", "same"], ["build", "older"]]);
  assert.equal(merged[0].status, "succeeded");
  assert.deepEqual(model.mergeHistoryItems(merged, incoming), merged);
});

test("deep link opening and closing preserve filters and encode arbitrary record IDs", () => {
  const original = "app=shop&source=cli&since=2026-09-01T00%3A00%3A00Z";
  const selection = { kind: "deployment", id: "record/with ?&" };
  const opened = model.historySelectionSearch(original, selection);
  assert.deepEqual(model.historySelection(opened), selection);
  assert.deepEqual([...new URLSearchParams(model.historySelectionSearch(opened, null))], [...new URLSearchParams(original)]);
  assert.equal(model.historySelection("entryKind=unknown&entryId=a"), null);
  assert.equal(model.historyDetailPath(selection, "opaque/="), "/v1/history/deployment/record%2Fwith%20%3F%26?limit=50&cursor=opaque%2F%3D");
});

test("date controls round trip local minutes and reject invalid dates", () => {
  const timestamp = "2026-09-05T10:30:00.000Z";
  assert.equal(model.dateInputTimestamp(model.localDateInput(timestamp)), timestamp);
  assert.equal(model.localDateInput(String(Date.parse(timestamp) / 1000)), model.localDateInput(timestamp));
  assert.equal(model.dateInputTimestamp("not a date"), "");
  assert.equal(model.localDateInput("not a date"), "");
});

test("history API rejects failures and malformed pages instead of pretending history is empty", async () => {
  const { fetchHistory, fetchHistoryDetail } = load("historyApi.ts");
  const previous = global.fetch;
  try {
    global.fetch = async () => new Response(JSON.stringify({ error: "database unavailable" }), { status: 503 });
    await assert.rejects(fetchHistory("token", ""), /database unavailable/);
    global.fetch = async () => new Response(JSON.stringify({ items: [] }));
    await assert.rejects(fetchHistory("token", ""), /invalid history page/);
    global.fetch = async () => new Response(JSON.stringify({ items: [], page: { limit: 50, hasMore: true, nextCursor: null } }));
    await assert.rejects(fetchHistory("token", ""), /invalid history page/);
    global.fetch = async () => new Response(JSON.stringify({ item: { kind: "build", id: "wrong" }, events: [], record: {}, page: { limit: 50, hasMore: false, nextCursor: null } }));
    await assert.rejects(fetchHistoryDetail("token", { kind: "build", id: "requested" }), /invalid history detail/);
  } finally { global.fetch = previous; }
});

test("history API requests only one summary page or one event page with auth and cancellation", async () => {
  const { fetchHistory, fetchHistoryDetail } = load("historyApi.ts");
  const previous = global.fetch;
  const requests = [];
  const page = { limit: 50, hasMore: false, nextCursor: null };
  try {
    global.fetch = async (url, options) => {
      requests.push({ url, options });
      return new Response(JSON.stringify(url.startsWith("/v1/history/build/") ? { item: { kind: "build", id: "b1" }, record: {}, events: [{ name: "build", message: "ok" }], page } : { items: [], page }));
    };
    await fetchHistory("secret", "app=demo", "cursor1");
    await fetchHistoryDetail("secret", { kind: "build", id: "b1" }, "cursor2");
    assert.equal(requests.length, 2);
    assert.equal(new URL(requests[0].url, "https://control").searchParams.get("cursor"), "cursor1");
    assert.equal(new URL(requests[1].url, "https://control").searchParams.get("cursor"), "cursor2");
    assert.equal(requests[0].options.headers.Authorization, "Bearer secret");
    assert.ok(requests[0].options.signal instanceof AbortSignal);
  } finally { global.fetch = previous; }
});


test("retry actions resolve to a distinct child attempt, never the failed parent", () => {
  assert.deepEqual(model.retryBuildSelection({ buildRunId: "child-2" }, "parent-1"), { kind: "build", id: "child-2" });
  for (const result of [null, {}, { buildRunId: "parent-1" }, { buildRunId: " " }, { buildRunId: 42 }]) {
    assert.equal(model.retryBuildSelection(result, "parent-1"), null);
  }
});


test("retention notice distinguishes explicitly expired details from records with no events", () => {
  const item = { kind: "build", id: "build-1", detailsExpiredAt: 1000, detailsRetentionDays: 14 };
  assert.match(model.historyRetentionNotice(item, "zh"), /14 天.*摘要仍保留/);
  assert.match(model.historyRetentionNotice(item, "en"), /14-day.*summary is retained/);
  assert.equal(model.historyRetentionNotice({ kind: "build", id: "build-1", stepCount: 0 }, "zh"), "");
  assert.equal(model.historyRetentionNotice({ ...item, detailsExpiredAt: 0 }, "en"), "");
  assert.match(model.historyRetentionNotice({ ...item, detailsRetentionDays: undefined }, "en"), /under the retention policy/);
});


test("history success and timeout variants use translated labels and consistent badge states", () => {
  for (const status of ["succeeded", "completed", "complete", "success", " SUCCEEDED "]) {
    assert.equal(model.historyStatus(status, "zh"), "成功");
    assert.equal(model.historyStatusValue(status), "succeeded");
  }
  for (const status of ["timeout", "timed_out"]) {
    assert.equal(model.historyStatus(status, "zh"), "超时");
    assert.equal(model.historyStatus(status, "en"), "Timed out");
    assert.equal(model.historyStatusValue(status), "failed");
  }
  assert.equal(model.historyStatus("running", "zh"), "运行");
  assert.equal(model.historyStatus("provider_specific", "zh"), "provider_specific");
  assert.equal(model.historyStatus(undefined, "zh"), "-");
});
