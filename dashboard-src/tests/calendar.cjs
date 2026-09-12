const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { zhCN } = require("react-day-picker/locale");

const cache = new Map();
function load(filename) {
  if (cache.has(filename)) return cache.get(filename);
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  mod.require = (name) => {
    if (name.startsWith("@/")) {
      const base = path.resolve(__dirname, "../src", name.slice(2));
      const resolved = [base, `${base}.ts`, `${base}.tsx`].find(file => fs.existsSync(file) && fs.statSync(file).isFile());
      if (resolved) return load(resolved);
    }
    return require(name);
  };
  mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020 },
    fileName: filename,
  }).outputText, filename);
  cache.set(filename, mod.exports);
  return mod.exports;
}

const { Calendar } = load(path.resolve(__dirname, "../src/components/ui/calendar.tsx"));
const today = new Date(2026, 8, 12);

test("calendar preserves the localized DayPicker grid, column headers and selected-today semantics", () => {
  const html = renderToStaticMarkup(React.createElement(Calendar, {
    mode: "single", defaultMonth: today, today, selected: today, locale: zhCN,
  }));
  assert.match(html, /<table\b[^>]*role="grid"[^>]*aria-label="2026年9月"/);
  assert.equal((html.match(/<th\b[^>]*scope="col"/g) || []).length, 7);
  assert.match(html, /<td\b[^>]*aria-selected="true"[^>]*data-day="2026-09-12"[^>]*data-today="true"/);
  assert.match(html, /<button\b[^>]*type="button"[^>]*aria-label="今天，2026年9月12日 星期六，已选择"/);
  assert.doesNotMatch(html, /<div\b[^>]*role="grid"/);
});

test("calendar keeps disabled dates as native disabled buttons inside their grid cells", () => {
  const html = renderToStaticMarkup(React.createElement(Calendar, {
    mode: "single", defaultMonth: today, today, selected: today,
    disabled: new Date(2026, 8, 13),
  }));
  const cell = html.match(/<td\b[^>]*data-day="2026-09-13"[^>]*>([\s\S]*?)<\/td>/)?.[1];
  assert.ok(cell);
  assert.match(cell, /<button\b[^>]*disabled=""/);
  assert.match(cell, /aria-label="Sunday, September 13th, 2026"/);
  assert.match(cell, /type="button"/);
});
