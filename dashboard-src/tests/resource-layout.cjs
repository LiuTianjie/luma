const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
let route = "/registry";
const longName = "very-long-resource-".repeat(12);
const digest = `sha256:${"a".repeat(64)}`;
let registryUsage = {};
const cache = new Map();
function load(filename) {
  filename = path.resolve(__dirname, "../src", filename);
  if (cache.has(filename)) return cache.get(filename);
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  mod.require = (name) => {
    if (name === "react") return { ...React, useState(initial) {
      if (initial === null) return [{ usage: registryUsage, entries: [{ repository: longName, digest, tags: [longName], protectionReasons: [{ source: longName }] }], protectionComplete: true }, () => {}];
      if (initial && typeof initial === "object" && "secrets" in initial) return [{ ...initial, secrets: [longName], loading: false, gitProviders: [{ id: longName, account: longName, configured: true }] }, () => {}];
      if (initial instanceof Set) return [new Set(["global:other"]), () => {}];
      return [initial, () => {}];
    } };
    if (name.endsWith("/router")) return { useRouter: () => ({ path: route, search: "", navigate() {} }), toHref: (value) => `/dashboard${value}` };
    if (name.endsWith("/ConfirmDialog")) return { useConfirm: () => ({ confirm() {}, element: null }) };
    if (name.endsWith(".css")) return {};
    if (name.startsWith("@/")) {
      const base = path.resolve(__dirname, "../src", name.slice(2));
      const resolved = [base, `${base}.ts`, `${base}.tsx`].find((file) => fs.existsSync(file) && fs.statSync(file).isFile());
      if (resolved) return load(resolved);
    }
    if (name.startsWith(".")) {
      const base = path.resolve(path.dirname(filename), name);
      const resolved = [base, `${base}.ts`, `${base}.tsx`].find((file) => fs.existsSync(file) && fs.statSync(file).isFile());
      if (resolved) return load(resolved);
    }
    return require(name);
  };
  mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX } }).outputText, filename);
  cache.set(filename, mod.exports);
  return mod.exports;
}
const { RegistryPage } = load("pages/RegistryPage.tsx");
const { CredentialsPage } = load("pages/CredentialsPage.tsx");
test("registry retains exact long identifiers and exposes a keyboard-scrollable table", () => {
  route = "/registry";
  const html = renderToStaticMarkup(React.createElement(RegistryPage, { lang: "en", token: "test" }));
  assert.ok(html.includes(digest));
  assert.ok(html.includes(longName));
  assert.match(html, /tabindex="0" role="region" aria-label="Image inventory, horizontally scrollable"/);
  assert.match(html, /registry-workspace/);
  assert.ok(!html.includes("\u200b"));
});
test("credentials retain full long names, scope and rotation actions inside their scroll region", () => {
  route = "/settings/secrets";
  const html = renderToStaticMarkup(React.createElement(CredentialsPage, { lang: "en", token: "test", vm: { storageClasses: [] } }));
  assert.ok(html.includes(longName));
  assert.match(html, /tabindex="0" role="region" aria-label="Secrets, horizontally scrollable"/);
  assert.match(html, /Rotate/);
  assert.match(html, /write-only/);
  assert.match(html, /settings-workspace/);
});

test("registry inventory exposes real storage usage and does not turn failed collection into zero", () => {
  route = "/registry";
  registryUsage = { volumeBytes: 12500000000, filesystemUsePercent: 0, filesystemAvailableBytes: 50000000000, monthlyBlobs: [{ month: "2026-08", bytes: 2000000000 }, { month: "2026-09", bytes: 0 }] };
  let html = renderToStaticMarkup(React.createElement(RegistryPage, { lang: "en", token: "test" }));
  assert.match(html, /Registry storage usage/);
  assert.match(html, /12.5 GB/);
  assert.match(html, />0%/);
  assert.match(html, /Registry storage by month/);
  assert.match(html, /2026-08/);
  assert.match(html, /2\.0 GB/);
  assert.match(html, /height:0%/);
  registryUsage = { error: "node agent restarted before task completion" };
  html = renderToStaticMarkup(React.createElement(RegistryPage, { lang: "zh", token: "test" }));
  assert.match(html, /镜像仓库容量/);
  assert.match(html, /暂不可用/);
  assert.match(html, /重新采集/);
  assert.match(html, /暂无法显示月度分布/);
  const capacity = html.match(/<section class="registry-usage-grid"[\s\S]*?<\/section>/)[0];
  assert.ok(!capacity.includes("0 B"));
  assert.ok(!html.includes("node agent restarted before task completion"));
  registryUsage = {};
});
