const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const filename = path.resolve(__dirname, "../src/components/ApplicationProperties.tsx");
const mod = new Module(filename, module);
mod.filename = filename;
mod.paths = module.paths;
mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 }, fileName: filename }).outputText, filename);
const { ApplicationProperties, ApplicationVersionEntry } = mod.exports;

test("version details preserve the complete digest and rollback control as separate content", () => {
  const image = 'registry.example.com/team/application@sha256:' + 'a'.repeat(64);
  const html = renderToStaticMarkup(React.createElement(ApplicationVersionEntry, {
    version: 'v300', current: false, image, imageLabel: '镜像', submitted: '2026-09-05 12:34', submittedLabel: '提交时间',
    action: React.createElement('button', { disabled: true }, '回滚中'),
  }));
  assert.ok(html.includes(image));
  assert.ok(html.includes('<dt>镜像</dt>'));
  assert.ok(html.includes('<dt>提交时间</dt><dd>2026-09-05 12:34</dd>'));
  assert.match(html, /<button disabled="">回滚中<\/button>/);
});

test("volume paths and their type retain independent labels, including special characters", () => {
  const html = renderToStaticMarkup(React.createElement(ApplicationProperties, { items: [
    { label: '卷 / 路径', value: '/srv/agent-pool/postgres-data<&>' },
    { label: '类型', value: 'bind' },
    { label: '存储类 / 节点', value: '-' },
  ] }));
  assert.ok(html.includes('<dd>/srv/agent-pool/postgres-data&lt;&amp;&gt;</dd>'));
  assert.ok(html.includes('<dt>类型</dt><dd>bind</dd>'));
  assert.equal((html.match(/<dt>/g) || []).length, 3);
});
