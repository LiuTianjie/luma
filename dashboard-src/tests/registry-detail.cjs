const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const filename = path.resolve(__dirname, "../src/registryDetail.ts");
const mod = new Module(filename, module);
mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, filename);
const { findRegistryImage } = mod.exports;
const image = { repository: "app", digest: "sha256:abc", tags: ["latest"] };

test("image lookup distinguishes completed missing result from a pending initial scan", async () => {
  const signal = new AbortController().signal;
  assert.deepEqual(await findRegistryImage("app@sha256:abc", async () => ({ entries: [] }), signal), { status: "missing", image: null });
  assert.deepEqual(await findRegistryImage("app@sha256:abc", async () => ({ scanPending: true }), signal), { status: "pending", image: null });
});

test("digest search follows matching pages and uses repository as part of image identity", async () => {
  const offsets = [];
  const result = await findRegistryImage("app@sha256:abc", async (offset) => {
    offsets.push(offset);
    return offset === 0 ? { entries: [{ ...image, repository: "other" }], page: { offset: 0, limit: 1, hasMore: true } } : { entries: [image] };
  }, new AbortController().signal);
  assert.deepEqual(offsets, [0, 1]);
  assert.deepEqual(result, { status: "ready", image });
});

test("a failed lookup propagates its error so it cannot be rendered as loading or missing", async () => {
  await assert.rejects(findRegistryImage("app@sha256:abc", async () => { throw new Error("Unauthorized"); }, new AbortController().signal), /Unauthorized/);
});

test("aborted obsolete lookups cannot publish results when a user switches images", async () => {
  const controller = new AbortController();
  let resolve;
  const pending = findRegistryImage("app@sha256:abc", () => new Promise((done) => { resolve = done; }), controller.signal);
  controller.abort();
  resolve({ entries: [image] });
  await assert.rejects(pending, { name: "AbortError" });
});

test("a subsequent refresh detects that a previously loaded image was deleted", async () => {
  const signal = new AbortController().signal;
  assert.equal((await findRegistryImage("app@sha256:abc", async () => ({ entries: [image] }), signal)).status, "ready");
  assert.equal((await findRegistryImage("app@sha256:abc", async () => ({ entries: [] }), signal)).status, "missing");
});
