const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const cache = new Map();
function load(filename) {
  if (cache.has(filename)) return cache.get(filename);
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  mod.require = (name) => {
    if (name.startsWith("@/") || name.startsWith(".")) {
      const base = name.startsWith("@/")
        ? path.resolve(__dirname, "../src", name.slice(2))
        : path.resolve(path.dirname(filename), name);
      const resolved = [base, `${base}.ts`, `${base}.tsx`].find(file => fs.existsSync(file) && fs.statSync(file).isFile());
      if (resolved) return load(resolved);
    }
    return require(name);
  };
  mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 }, fileName: filename }).outputText, filename);
  cache.set(filename, mod.exports);
  return mod.exports;
}
const { ApplicationProperties, ApplicationVersionEntry } = load(path.resolve(__dirname, "../src/components/ApplicationProperties.tsx"));

test("version details preserve the complete digest and rollback control as separate content", () => {
  const image = 'registry.example.com/team/application@sha256:' + 'a'.repeat(64);
  const html = renderToStaticMarkup(React.createElement(ApplicationVersionEntry, {
    version: 'v300', current: false, image, imageLabel: '镜像', submitted: '2026-09-05 12:34', submittedLabel: '提交时间',
    action: React.createElement('button', { disabled: true }, '回滚中'),
  }));
  assert.ok(html.includes(image));
  assert.match(html, /<th\b[^>]*scope="row"[^>]*>镜像<\/th><td\b[^>]*><code[^>]*>registry\.example\.com\/team\/application@sha256:a{64}<\/code><\/td>/);
  assert.match(html, /<th\b[^>]*scope="row"[^>]*>提交时间<\/th><td\b[^>]*>2026-09-05 12:34<\/td>/);
  assert.match(html, /<button disabled="">回滚中<\/button>/);
});

test("volume paths and their type retain independent labels, including special characters", () => {
  const html = renderToStaticMarkup(React.createElement(ApplicationProperties, { items: [
    { label: '卷 / 路径', value: '/srv/agent-pool/postgres-data<&>' },
    { label: '类型', value: 'bind' },
    { label: '存储类 / 节点', value: '-' },
  ] }));
  assert.match(html, /<th\b[^>]*scope="row"[^>]*>卷 \/ 路径<\/th><td\b[^>]*>\/srv\/agent-pool\/postgres-data&lt;&amp;&gt;<\/td>/);
  assert.match(html, /<th\b[^>]*scope="row"[^>]*>类型<\/th><td\b[^>]*>bind<\/td>/);
  assert.equal((html.match(/<th\b[^>]*scope="row"/g) || []).length, 3);
});
