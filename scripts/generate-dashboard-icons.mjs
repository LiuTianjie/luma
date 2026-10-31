#!/usr/bin/env node
// Generate dashboard-src/src/components/icons.ts from Phosphor's raw SVG assets.
//
// The console uses Phosphor icons (MIT), the set Cloudflare's Kumo design system
// ships with. The icons are vendored as plain React components so the dashboard has
// no runtime icon dependency and the Node-based dashboard tests can load them with
// `require`. Edit ICONS below, then run:
//
//   node scripts/generate-dashboard-icons.mjs          # write
//   node scripts/generate-dashboard-icons.mjs --check  # fail when stale
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// The package does not export its asset folder, so read it from node_modules.
const assets = path.join(root, "node_modules/@phosphor-icons/core/assets");
const output = path.join(root, "dashboard-src/src/components/icons.ts");

// Exported name -> Phosphor asset ("weight/name"). Names keep the identifiers the
// console already imports, so call sites read the same; the glyph is Phosphor's.
const ICONS = {
  // Navigation
  LayoutDashboard: "regular/squares-four",
  Boxes: "regular/cube",
  ScrollText: "regular/clock-counter-clockwise",
  Activity: "regular/pulse",
  ServerCog: "regular/hard-drives",
  Server: "regular/hard-drives",
  Settings: "regular/gear-six",
  Settings2: "regular/sliders-horizontal",
  WandSparkles: "regular/magic-wand",
  Network: "regular/tree-structure",
  HardDrive: "regular/hard-drive",
  Database: "regular/database",
  MapPin: "regular/map-pin",
  Wrench: "regular/wrench",
  FingerprintPattern: "regular/fingerprint",
  KeyRound: "regular/key",
  Package: "regular/package",
  PackageCheck: "regular/package",
  Container: "regular/shipping-container",
  Cloud: "regular/cloud",
  Route: "regular/path",
  ShieldCheck: "regular/shield-check",
  GitBranch: "regular/git-branch",
  FileCode2: "regular/file-code",
  LayoutTemplate: "regular/layout",
  Rocket: "regular/rocket-launch",
  Bot: "regular/robot",
  Cpu: "regular/cpu",
  Globe: "regular/globe-simple",
  Bell: "regular/bell-simple",
  BookOpen: "regular/book-open-text",
  Command: "regular/command",
  UserCircle: "regular/user-circle",
  Contrast: "regular/circle-half",
  Languages: "regular/translate",
  CornerDownLeft: "regular/arrow-elbow-down-left",
  Layers: "regular/stack",
  Zap: "regular/lightning",
  // Actions
  Plus: "regular/plus",
  Minus: "regular/minus",
  X: "regular/x",
  XIcon: "regular/x",
  Check: "regular/check",
  CheckIcon: "regular/check",
  Copy: "regular/copy",
  Download: "regular/download-simple",
  Trash2: "regular/trash",
  RefreshCw: "regular/arrows-clockwise",
  RotateCcw: "regular/arrow-counter-clockwise",
  Search: "regular/magnifying-glass",
  SearchX: "regular/magnifying-glass-minus",
  ScanSearch: "regular/file-magnifying-glass",
  SlidersHorizontal: "regular/faders-horizontal",
  Play: "regular/play",
  Pause: "regular/pause",
  Square: "regular/stop",
  Link2: "regular/link-simple",
  ExternalLink: "regular/arrow-square-out",
  LogOut: "regular/sign-out",
  WrapText: "regular/text-align-left",
  ListChecks: "regular/list-checks",
  Pencil: "regular/pencil-simple",
  FileText: "regular/file-text",
  History: "regular/clock-counter-clockwise",
  RotateCw: "regular/arrow-clockwise",
  Box: "regular/cube",
  // Arrows and chevrons
  ArrowLeft: "regular/arrow-left",
  ArrowRight: "regular/arrow-right",
  ArrowDown: "regular/arrow-down",
  ArrowUpRight: "regular/arrow-up-right",
  ChevronDown: "regular/caret-down",
  ChevronDownIcon: "regular/caret-down",
  ChevronUp: "regular/caret-up",
  ChevronUpIcon: "regular/caret-up",
  ChevronLeftIcon: "regular/caret-left",
  ChevronRightIcon: "regular/caret-right",
  ChevronsUpDown: "regular/caret-up-down",
  MoreHorizontal: "bold/dots-three",
  MoreHorizontalIcon: "bold/dots-three",
  PanelLeftIcon: "regular/sidebar-simple",
  // Window
  Maximize: "regular/corners-out",
  Maximize2: "regular/arrows-out-simple",
  Minimize2: "regular/arrows-in-simple",
  Expand: "regular/arrows-out",
  Shrink: "regular/arrows-in",
  // Status
  Info: "regular/info",
  InfoIcon: "regular/info",
  AlertCircle: "regular/warning-circle",
  CircleAlert: "regular/warning-circle",
  AlertTriangle: "regular/warning",
  TriangleAlert: "regular/warning",
  TriangleAlertIcon: "regular/warning",
  CheckCircle2: "regular/check-circle",
  CircleCheckIcon: "regular/check-circle",
  XCircle: "regular/x-circle",
  OctagonXIcon: "regular/x-circle",
  FileQuestion: "regular/question",
  Clock3: "regular/clock",
  CalendarIcon: "regular/calendar-blank",
  Loader2Icon: "bold/circle-notch",
  // Terminal and appearance
  Terminal: "regular/terminal",
  SquareTerminal: "regular/terminal-window",
  TerminalSquare: "regular/terminal-window",
  Sun: "regular/sun",
  Moon: "regular/moon",
  Monitor: "regular/monitor",
};

function svgBody(asset) {
  // Non-regular weights carry the weight as a filename suffix: bold/dots-three-bold.svg.
  const [weight, name] = asset.split("/");
  const file = weight === "regular" ? `${name}.svg` : `${name}-${weight}.svg`;
  const svg = readFileSync(path.join(assets, weight, file), "utf8");
  const match = svg.match(/^<svg[^>]*>([\s\S]*)<\/svg>\s*$/);
  if (!match) throw new Error(`Unexpected SVG markup in ${asset}`);
  return match[1].replace(/\s+/g, " ").trim();
}

function pathData(markup) {
  // Phosphor glyphs are single-path; keep the generated module free of JSX so plain
  // Node (type stripping, no JSX transform) can import navigation data that uses it.
  const paths = [...markup.matchAll(/<path d="([^"]+)"\s*\/>/g)].map((m) => m[1]);
  if (!paths.length || markup.replace(/<path d="[^"]+"\s*\/>/g, "").trim()) {
    throw new Error(`Only <path d> glyphs are supported: ${markup.slice(0, 80)}`);
  }
  return paths;
}

const names = Object.keys(ICONS).sort();
const glyphs = new Map();
for (const name of names) {
  const asset = ICONS[name];
  if (!glyphs.has(asset)) glyphs.set(asset, `g${glyphs.size}`);
}

const lines = [
  "// Generated by scripts/generate-dashboard-icons.mjs from @phosphor-icons/core (MIT). Do not edit.",
  'import { createElement, type ReactElement, type SVGProps } from "react";',
  "",
  "export type IconProps = SVGProps<SVGSVGElement> & { size?: number | string };",
  "export type Icon = ((props: IconProps) => ReactElement) & { displayName?: string };",
  "",
  "function icon(paths: string[], name: string): Icon {",
  "  const Component: Icon = ({ size = 16, ...props }: IconProps) => createElement(",
  '    "svg",',
  '    { xmlns: "http://www.w3.org/2000/svg", viewBox: "0 0 256 256", fill: "currentColor", width: size, height: size, "aria-hidden": true, focusable: false, "data-icon": name, ...props },',
  "    ...paths.map((d, key) => createElement(\"path\", { key, d })),",
  "  );",
  "  Component.displayName = name;",
  "  return Component;",
  "}",
  "",
];
for (const [asset, id] of glyphs) {
  lines.push(`const ${id} = ${JSON.stringify(pathData(svgBody(asset)))}; // ${asset}`);
}
lines.push("");
for (const name of names) {
  lines.push(`export const ${name} = icon(${glyphs.get(ICONS[name])}, "${name}");`);
}
lines.push("");
const content = lines.join("\n");

if (process.argv.includes("--check")) {
  let current = "";
  try {
    current = readFileSync(output, "utf8");
  } catch {}
  if (current !== content) {
    console.error(`${path.relative(root, output)} is stale; run node scripts/generate-dashboard-icons.mjs`);
    process.exit(1);
  }
} else {
  writeFileSync(output, content);
  console.log(`wrote ${path.relative(root, output)} (${names.length} names, ${glyphs.size} glyphs)`);
}
