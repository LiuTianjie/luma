const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const { load } = require("js-yaml");

function loadTs(name) {
  const filename = path.resolve(__dirname, `../src/deploy/${name}.ts`);
  const mod = new Module(filename, module);
  mod.filename = filename;
  mod.paths = module.paths;
  mod._compile(ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }, fileName: filename }).outputText, filename);
  return mod.exports;
}
const { DEPLOY_TEMPLATES } = loadTs("templates");
const { serviceDraftToYaml, composeDraftToSidecarYaml } = loadTs("yaml");

test("new deployment templates use local storage without an NFS class or separate storage node", () => {
  for (const template of DEPLOY_TEMPLATES) {
    const yaml = template.service ? serviceDraftToYaml(template.service) : composeDraftToSidecarYaml(template.compose);
    assert.ok(!yaml.includes("storageClass:"), template.id);
    if (template.compose) {
      const parsed = load(yaml);
      for (const volume of Object.values(parsed.volumes || {})) {
        assert.ok(volume.local.path.startsWith(`/srv/luma/data/${template.compose.name}/`), template.id);
        assert.equal(volume.local.node, undefined);
      }
    }
  }
});

test("two applications from the same template get independent data directories and volume names", () => {
  const compose = DEPLOY_TEMPLATES.find(item => item.id === "compose-uptime-kuma").compose;
  const a = load(composeDraftToSidecarYaml({ ...compose, name: "app-a" }));
  const b = load(composeDraftToSidecarYaml({ ...compose, name: "app-b" }));
  assert.notEqual(a.volumes["kuma-data"].local.path, b.volumes["kuma-data"].local.path);
  const native = DEPLOY_TEMPLATES.find(item => item.id === "service-code-server").service;
  assert.notDeepEqual(load(serviceDraftToYaml({ ...native, name: "app-a" })).volumes, load(serviceDraftToYaml({ ...native, name: "app-b" })).volumes);
});

test("explicit legacy named volumes preserve their existing data identity", () => {
  const native = DEPLOY_TEMPLATES.find(item => item.id === "service-code-server").service;
  const draft = { ...native, volumeMounts: [{ ...native.volumeMounts[0], name: "old-data", storageMode: "unmanaged" }] };
  assert.deepEqual(load(serviceDraftToYaml(draft)).volumes, ["old-data:/config"]);
});
