import { access, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const laeRoot = resolve(here, "../../..");
const schema = resolve(
  laeRoot,
  "packages/contracts/src/lae_contracts/specs/schemas/deployment-plan.v1.schema.json",
);

await access(schema);
const parsed = JSON.parse(await readFile(schema, "utf8"));
if (parsed.$id !== "https://schemas.itool.tech/lae/deployment-plan.v1.schema.json") {
  throw new Error("LAE Web workspace cannot resolve the canonical deployment-plan contract");
}

process.stdout.write("LAE Web workspace scaffold is wired to the canonical contracts.\n");
