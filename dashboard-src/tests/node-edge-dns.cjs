const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
let route = "/fleet";
const panels = [];
const cache = new Map();
function load(filename) {
  filename = path.resolve(__dirname, "../src", filename);
  if (cache.has(filename)) return cache.get(filename);
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  mod.require = (name) => {
    if (name.endsWith("/router")) return { useRouter: () => ({ path: route, navigate() {} }), toHref: (value) => `/dashboard${value}`, isPlainLeftClick: () => true, spaLink: (to) => ({ href: `/dashboard${to}`, onClick() {} }) };
    const panel = panels.find((entry) => name.endsWith(`/${entry}`));
    // These existing operational panels are not changed by the workspace split.
    if (panel) return { [panel]: () => React.createElement("section", { "data-capability": panel }) };
    if (name.endsWith(".css")) return {};
    if (name === "@xterm/xterm") return { Terminal: class {} };
    if (name === "@xterm/addon-fit") return { FitAddon: class {} };
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
  mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    fileName: filename,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  }).outputText, filename);
  cache.set(filename, mod.exports);
  return mod.exports;
}
const options = load("deploy/options.ts");
const { NodePublicIngress } = load("components/NodePublicIngress.tsx");
const { DnsZonesPanel } = load("components/DnsZonesPanel.tsx");

const ready = { state: "ready", availability: "eligible", region: "cn" };
const nodes = [
  { ...ready, name: "edge-1", publicIngress: { enabled: true, publicIp: "47.100.1.20", status: "ready" } },
  { ...ready, name: "worker-1" },
  { ...ready, name: "edge-off", publicIngress: { enabled: false } },
];

test("node-edge only offers nodes that serve public ingress", () => {
  assert.deepEqual(options.nodeChoicesForExposure(nodes, "cn", "node-edge").map((node) => node.name), ["edge-1"]);
  assert.deepEqual(options.nodeChoicesForExposure(nodes, "cn", "cn-edge").map((node) => node.name), ["edge-1", "worker-1", "edge-off"]);
  assert.equal(options.nodeForExposure(nodes, "worker-1", "cn", "node-edge"), "edge-1");
  assert.equal(options.nodeForExposure(nodes, "edge-1", "cn", "node-edge"), "edge-1");
  assert.equal(options.nodeForExposure(nodes, "worker-1", "cn", "cn-edge"), "worker-1");
});

test("node-edge validation points at the node page", () => {
  assert.match(options.nodeEdgeError(nodes, "node-edge", "", "api", "en"), /needs a node with public ingress/);
  assert.match(options.nodeEdgeError(nodes, "node-edge", "worker-1", "api", "en"), /Infrastructure → Nodes → worker-1/);
  assert.equal(options.nodeEdgeError(nodes, "node-edge", "edge-1", "api", "en"), "");
  assert.equal(options.nodeEdgeError(nodes, "cn-edge", "", "api", "en"), "");
  assert.match(options.exposureOptionLabel([{ ...ready, name: "w" }], "node-edge", "en", "cn"), /no node with public ingress/);
  assert.ok(options.exposureAllowedInRegion("node-edge", "cn"));
  assert.ok(!options.exposureAllowedInRegion("node-edge", "home"));
});

test("node page shows the public ingress card with its state", () => {
  const enabled = renderToStaticMarkup(React.createElement(NodePublicIngress, { lang: "en", token: "t", node: { ...nodes[0], agentStatus: "ready", publicIngress: { enabled: true, publicIp: "47.100.1.20", status: "unreachable", detail: "does not answer on port(s) 80" } } }));
  assert.match(enabled, /Public ingress/);
  assert.match(enabled, /Ports unreachable/);
  assert.match(enabled, /value="47.100.1.20"/);
  assert.match(enabled, /Re-check \/ update address/);
  assert.match(enabled, />Disable</);
  const manager = renderToStaticMarkup(React.createElement(NodePublicIngress, { lang: "en", token: "t", node: { ...ready, name: "m", role: "server", leader: true } }));
  assert.match(manager, /already the cluster edge/);
  assert.doesNotMatch(manager, /Enable public ingress/);
});

test("domains page renders the add form for every provider", () => {
  const html = renderToStaticMarkup(React.createElement(DnsZonesPanel, { lang: "zh", token: "t" }));
  assert.match(html, /添加域名/);
  for (const label of ["Cloudflare", "阿里云 DNS", "腾讯云 DNSPod"]) assert.match(html, new RegExp(label));
  assert.match(html, /AccessKey Secret/);
  assert.match(html, /验证并添加/);
});
