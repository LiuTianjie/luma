const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
let route = "/fleet";
const panels = ["NodeFleetMap", "RegionPanel", "NodeTopology", "SystemUpdatePanel", "TrafficPaths", "StoragePanel", "StorageGovernancePanel"];
const cache = new Map();
function load(filename) {
  filename = path.resolve(__dirname, "../src", filename);
  if (cache.has(filename)) return cache.get(filename);
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  mod.require = (name) => {
    if (name.endsWith("/router")) return { useRouter: () => ({ path: route, navigate() {} }), toHref: (value) => `/dashboard${value}` };
    const panel = panels.find((entry) => name.endsWith(`/${entry}`));
    // These existing operational panels are not changed by the workspace split.
    if (panel) return { [panel]: () => React.createElement("section", { "data-capability": panel }) };
    if (name.endsWith(".css")) return {};
    if (name === "@xterm/xterm") return { Terminal: class {} };
    if (name === "@xterm/addon-fit") return { FitAddon: class {} };
    if (name.startsWith(".")) {
      const base = path.resolve(path.dirname(filename), name);
      const resolved = [base, `${base}.ts`, `${base}.tsx`].find((file) => fs.existsSync(file) && fs.statSync(file).isFile());
      if (resolved) return load(resolved);
    }
    return require(name);
  };
  mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    fileName: filename,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, filename);
  cache.set(filename, mod.exports);
  return mod.exports;
}
const { NodesPage } = load("pages/NodesPage.tsx");
const { StoragePage } = load("pages/StoragePage.tsx");
const { DetailDrawer } = load("DetailDrawer.tsx");
const { TerminalDrawer } = load("components/TerminalDrawer.tsx");
const props = { lang: "en", token: "test", theme: "light", controlVersion: "1", vm: { nodes: [], services: [], regions: [], trafficPaths: [], storageClasses: [], storageVolumes: [], storageWarnings: [] }, onSelectNode() {}, onTerminal() {}, onRefresh() {} };

test("fleet opens at inventory and preserves independent region, network, and maintenance destinations", () => {
  for (const [url, expected] of [["/fleet", ["NodeFleetMap"]], ["/fleet/regions", ["RegionPanel"]], ["/fleet/network", ["TrafficPaths", "NodeTopology"]], ["/fleet/maintenance", ["SystemUpdatePanel"]], ["/fleet/join", []]]) {
    route = url;
    const html = renderToStaticMarkup(React.createElement(NodesPage, props));
    const rendered = [...html.matchAll(/data-capability="([^"]+)"/g)].map((match) => match[1]);
    assert.deepEqual(rendered, expected, url);
    if (url === "/fleet/join") assert.match(html, /luma node join/);
    assert.match(html, /href="\/dashboard\/fleet\/maintenance"/);
  }
});

test("storage governance has a dedicated destination without hiding volume inventory", () => {
  route = "/storage";
  assert.match(renderToStaticMarkup(React.createElement(StoragePage, props)), /data-capability="StoragePanel"/);
  route = "/storage/governance";
  const html = renderToStaticMarkup(React.createElement(StoragePage, props));
  assert.match(html, /data-capability="StorageGovernancePanel"/);
  assert.doesNotMatch(html, /data-capability="StoragePanel"/);
});

test("inline object details retain zero and false values and do not declare a modal", () => {
  const html = renderToStaticMarkup(React.createElement(DetailDrawer, { lang: "en", inline: true, detail: { kind: "node", title: "manager", items: { cpu: 0, leader: false } }, onClose() {} }));
  assert.match(html, /<dd>0<\/dd>/);
  assert.match(html, /<dd>false<\/dd>/);
  assert.doesNotMatch(html, /role="dialog"|aria-modal/);
});

test("inline shell renders its target and surface without requiring a modal portal", () => {
  const html = renderToStaticMarkup(React.createElement(TerminalDrawer, { lang: "en", inline: true, token: "test", target: { kind: "node", node: { name: "manager", region: "cn" } }, onClose() {} }));
  assert.match(html, /manager/);
  assert.match(html, /terminal-surface/);
  assert.match(html, /End session and return/);
  assert.doesNotMatch(html, /role="dialog"|aria-modal/);
});
