import { access, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const laeRoot = resolve(here, "../../..");
const schema = resolve(
  laeRoot,
  "packages/contracts/src/lae_contracts/specs/schemas/deployment-plan.v1.schema.json",
);
const consoleComponent = resolve(laeRoot, "apps/web/src/components/lae-console.tsx");
const stylesheet = resolve(laeRoot, "apps/web/src/app/globals.css");

await access(schema);
const parsed = JSON.parse(await readFile(schema, "utf8"));
if (parsed.$id !== "https://schemas.itool.tech/lae/deployment-plan.v1.schema.json") {
  throw new Error("LAE Web workspace cannot resolve the canonical deployment-plan contract");
}

const [consoleSource, css] = await Promise.all([
  readFile(consoleComponent, "utf8"),
  readFile(stylesheet, "utf8"),
]);

for (const section of ["deployment", "applications", "activity", "cli"]) {
  if (!consoleSource.includes(`href="#${section}"`) || !consoleSource.includes(`id="${section}"`)) {
    throw new Error(`Console navigation target #${section} must have both a link and a real panel`);
  }
}
if (!consoleSource.includes("aria-current={activeSection")) {
  throw new Error("Console navigation must expose the current location to assistive technology");
}
if (!consoleSource.includes('setCatalogStatus(unauthenticated ? "connected" : "unavailable")')) {
  throw new Error("An unauthenticated 401 must not be presented as an unavailable LAE API");
}
if (!consoleSource.includes('{app.status} · {app.services} service')) {
  throw new Error("Application state must be visible as text instead of color alone");
}
if (consoleSource.includes('`*:${name}`')) {
  throw new Error("Compose environment values must never be widened to wildcard scope");
}
if (!consoleSource.includes("patchPlanEnvironment")) {
  throw new Error("Deployment configuration must use the plan-bound environment endpoint");
}
if (
  !consoleSource.includes("operationFailureMessage") ||
  !consoleSource.includes("recovered.operation.error") ||
  !consoleSource.includes("operation?.error")
) {
  throw new Error("Terminal deployment failures must expose the public operation error");
}
if (!consoleSource.includes('"fastapi-minimal": "轻量 Python API')) {
  throw new Error("FastAPI template description must use the console's Chinese locale");
}
if (!css.includes(".auth-panel { order: 1;") || !css.includes("min-height: 100svh")) {
  throw new Error("The mobile authentication action must remain in the first viewport");
}
if (!css.includes("@media (prefers-reduced-motion: reduce)")) {
  throw new Error("Console motion must retain a reduced-motion path");
}

process.stdout.write("LAE Web contracts and browser QA invariants are wired.\n");
