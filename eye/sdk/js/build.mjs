import { build } from "esbuild";
import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { execFileSync } from "node:child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const distDir = resolve(__dirname, "dist");
mkdirSync(distDir, { recursive: true });

const rootSkill = resolve(__dirname, "../../skills/luma-eye/SKILL.md");
const localSkill = resolve(__dirname, "SKILL.md");
const goSkillAsset = resolve(__dirname, "../../internal/skillasset/SKILL.md");
if (existsSync(rootSkill)) {
  copyFileSync(rootSkill, localSkill);
  mkdirSync(dirname(goSkillAsset), { recursive: true });
  copyFileSync(rootSkill, goSkillAsset);
}
const skillMarkdown = existsSync(localSkill) ? readFileSync(localSkill, "utf8") : "# Luma Eye Agent Skill\n";
const pkgJson = JSON.parse(readFileSync(resolve(__dirname, "package.json"), "utf8"));

await Promise.all([
  build({
    entryPoints: [resolve(__dirname, "src/index.ts")],
    outfile: resolve(distDir, "index.mjs"),
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/index.ts")],
    outfile: resolve(distDir, "index.cjs"),
    bundle: true,
    format: "cjs",
    platform: "browser",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/react.ts")],
    outfile: resolve(distDir, "react.mjs"),
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/react.ts")],
    outfile: resolve(distDir, "react.cjs"),
    bundle: true,
    format: "cjs",
    platform: "browser",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/react-native.ts")],
    outfile: resolve(distDir, "react-native.mjs"),
    bundle: true,
    external: ["react-native"],
    format: "esm",
    platform: "neutral",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/react-native.ts")],
    outfile: resolve(distDir, "react-native.cjs"),
    bundle: true,
    external: ["react-native"],
    format: "cjs",
    platform: "neutral",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/core.ts")],
    outfile: resolve(distDir, "core.mjs"),
    bundle: true,
    format: "esm",
    platform: "neutral",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/core.ts")],
    outfile: resolve(distDir, "core.cjs"),
    bundle: true,
    format: "cjs",
    platform: "neutral",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/replay.ts")],
    outfile: resolve(distDir, "replay.mjs"),
    bundle: true,
    format: "esm",
    platform: "browser",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/replay.ts")],
    outfile: resolve(distDir, "replay.cjs"),
    bundle: true,
    format: "cjs",
    platform: "browser",
    target: "es2020",
    minify: true,
    sourcemap: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/index.ts")],
    outfile: resolve(distDir, "eye.min.js"),
    bundle: true,
    format: "iife",
    globalName: "LumaEye",
    platform: "browser",
    target: "es2020",
    minify: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "src/replay.ts")],
    outfile: resolve(distDir, "eye-replay.min.js"),
    bundle: true,
    format: "iife",
    globalName: "__LumaEyeReplayBundle",
    platform: "browser",
    target: "es2020",
    minify: true,
  }),
  build({
    entryPoints: [resolve(__dirname, "cli/luma-eye.mjs")],
    outfile: resolve(distDir, "cli.mjs"),
    bundle: true,
    format: "esm",
    platform: "node",
    target: "node18",
    banner: { js: "#!/usr/bin/env node" },
    define: {
      __LUMA_EYE_SKILL_MD__: JSON.stringify(skillMarkdown),
      __LUMA_EYE_VERSION__: JSON.stringify(pkgJson.version),
    },
  }),
]);

chmodSync(resolve(distDir, "cli.mjs"), 0o755);

// Emit TypeScript declarations
execFileSync(resolve(__dirname, "node_modules/.bin/tsc"), ["--project", resolve(__dirname, "tsconfig.json")], {
  stdio: "inherit",
});

// Copy embedded IIFE bundles into Go sdkasset package
const goAsset = resolve(__dirname, "../../internal/sdkasset/eye.min.js");
const goReplayAsset = resolve(__dirname, "../../internal/sdkasset/eye-replay.min.js");
mkdirSync(dirname(goAsset), { recursive: true });
copyFileSync(resolve(distDir, "eye.min.js"), goAsset);
copyFileSync(resolve(distDir, "eye-replay.min.js"), goReplayAsset);

const rawBytes = readFileSync(resolve(distDir, "eye.min.js"));
const gzBytes = gzipSync(rawBytes, { level: 9 });
const replayRawBytes = readFileSync(resolve(distDir, "eye-replay.min.js"));
const replayGzBytes = gzipSync(replayRawBytes, { level: 9 });
console.log(`[sdk/js] eye.min.js: ${rawBytes.length} bytes raw, ${gzBytes.length} bytes gzipped`);
console.log(`[sdk/js] eye-replay.min.js: ${replayRawBytes.length} bytes raw, ${replayGzBytes.length} bytes gzipped`);
if (gzBytes.length > 9216) {
  console.warn(`[sdk/js] WARNING: gzipped bundle (${gzBytes.length} B) exceeded 9216 B target`);
}
