import {
  chmodSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, relative, resolve } from "node:path";

const VERSION = typeof __LUMA_EYE_VERSION__ === "string" ? __LUMA_EYE_VERSION__ : "1.1.0";
const SKILL_MD =
  typeof __LUMA_EYE_SKILL_MD__ === "string"
    ? __LUMA_EYE_SKILL_MD__
    : "# Luma Eye Agent Skill\n";

function printUsage() {
  process.stdout.write(`luma-eye v${VERSION} — Multi-service telemetry, error tracking & product analytics for Luma

Usage:
  luma-eye login --url https://eye.example.com --token <admin-token> [--skip-verify] [--json]
  luma-eye logout [--json]
  luma-eye status [--json]
  luma-eye skills install [--agent all|claude|antigravity|codex] [--path <dir>] [--json]
  luma-eye skills status [--path <dir>] [--json]
  luma-eye projects list [--json]
  luma-eye projects ensure <slug> [--name ...] [--origins https://app.example.com] [--rps 200] [--json]
  luma-eye projects delete <slug> [--json]
  luma-eye portfolio [--since 24h] [--json]
  luma-eye stats <project> [--since 24h] [--json]
  luma-eye pages <project> [--since 24h] [--limit 50] [--json]
  luma-eye sessions <project> [--since 7d] [--search "..."] [--country CN] [--errors-only] [--limit 80] [--json]
  luma-eye timeline <project> [--session <sid>] [--actor <uid>] [--since 7d] [--json]
  luma-eye events <project> [--type all|pageview|custom] [--name <ev>] [--search "..."] [--since 24h] [--limit 100] [--json]
  luma-eye funnel <project> --steps "pageview,signup,checkout" [--window 3600] [--since 7d] [--json]
  luma-eye retention <project> [--days 7] [--json]
  luma-eye issues <project> [--since 7d] [--status open|resolved|ignored|all] [--detail <fp>] [--resolve <fp>] [--ignore <fp>] [--reopen <fp>] [--json]
  luma-eye logs <project> [--level error] [--search "..."] [--trace <id>] [--since 1h] [--limit 100] [--json]
  luma-eye sourcemaps upload --project <slug> --release <version> [--prefix /assets] <dir-or-file>
  luma-eye grafana render --project <slug> [--datasource victoria|clickhouse] [--output <path>]

Credentials resolution order:
  1. --url / --token flags
  2. LUMA_EYE_URL / LUMA_EYE_ADMIN_TOKEN environment variables
  3. Saved CLI config (~/.config/luma-eye/config.json or $LUMA_EYE_CONFIG)
`);
}

function cliConfigPath() {
  const custom = (process.env.LUMA_EYE_CONFIG || "").trim();
  if (custom) return custom;
  const home = homedir();
  return home ? join(home, ".config", "luma-eye", "config.json") : "";
}

function loadSavedCLIConfig() {
  const p = cliConfigPath();
  if (!p) return {};
  try {
    const parsed = JSON.parse(readFileSync(p, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveCLIConfig(cfg) {
  const p = cliConfigPath();
  if (!p) throw new Error("cannot determine home directory for luma-eye config");
  mkdirSync(dirname(p), { recursive: true, mode: 0o700 });
  const tmp = `${p}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(cfg, null, 2)}\n`, { mode: 0o600 });
  chmodSync(tmp, 0o600);
  renameSync(tmp, p);
  chmodSync(p, 0o600);
  return p;
}

function resolveClient(urlFlag, tokenFlag, timeoutMs = 30000) {
  const saved = loadSavedCLIConfig();
  const baseURL = (
    (urlFlag || "").trim() ||
    (process.env.LUMA_EYE_URL || "").trim() ||
    (saved.url || "").trim() ||
    "http://127.0.0.1:8090"
  ).replace(/\/+$/, "");
  const token =
    (tokenFlag || "").trim() ||
    (process.env.LUMA_EYE_ADMIN_TOKEN || "").trim() ||
    (saved.token || "").trim();
  return { baseURL, token, timeoutMs };
}

async function doJSON(client, method, reqPath, body) {
  const headers = {};
  let payload;
  if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  if (client.token) {
    headers.Authorization = `Bearer ${client.token}`;
  }
  const resp = await fetch(`${client.baseURL}${reqPath}`, {
    method,
    headers,
    body: payload,
    signal: AbortSignal.timeout(client.timeoutMs || 30000),
  });
  const raw = await resp.text();
  if (resp.status >= 300) {
    throw new Error(`HTTP ${resp.status}: ${raw.trim()}`);
  }
  return { raw, data: raw ? JSON.parse(raw) : null };
}

function parseFlags(args, valuedNames = [], boolNames = ["json", "help", "h"]) {
  const valued = new Set(valuedNames);
  const bools = new Set(boolNames);
  const flags = {};
  const pos = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--") {
      pos.push(...args.slice(i + 1));
      break;
    }
    if (!a.startsWith("-") || a === "-") {
      pos.push(a);
      continue;
    }
    const stripped = a.replace(/^-+/, "");
    const eq = stripped.indexOf("=");
    if (eq >= 0) {
      const k = stripped.slice(0, eq);
      const v = stripped.slice(eq + 1);
      flags[k] = bools.has(k) ? v !== "false" && v !== "0" : v;
      continue;
    }
    if (bools.has(stripped)) {
      flags[stripped] = true;
      continue;
    }
    if (valued.has(stripped)) {
      if (i + 1 >= args.length) {
        throw new Error(`flag --${stripped} requires a value`);
      }
      flags[stripped] = args[++i];
      continue;
    }
    throw new Error(`unknown flag: ${a}`);
  }
  return { flags, pos };
}

async function runLogin(args) {
  const { flags } = parseFlags(args, ["url", "token"], ["json", "skip-verify"]);
  const cleanURL = (
    flags.url ||
    process.env.LUMA_EYE_URL ||
    ""
  ).trim().replace(/\/+$/, "");
  const cleanToken = (
    flags.token ||
    process.env.LUMA_EYE_ADMIN_TOKEN ||
    ""
  ).trim();
  if (!cleanURL || !cleanToken) {
    throw new Error("usage: luma-eye login --url https://eye.example.com --token <admin-token>");
  }

  let mode = "";
  if (!flags["skip-verify"]) {
    const cli = { baseURL: cleanURL, token: cleanToken, timeoutMs: 15000 };
    let authRes;
    try {
      const res = await doJSON(cli, "GET", "/api/v1/auth/status");
      authRes = res.data || {};
    } catch (err) {
      throw new Error(`verify server ${cleanURL}: ${err.message}`);
    }
    if (!authRes.authenticated) {
      throw new Error(`authentication failed: invalid admin token for ${cleanURL}`);
    }
    mode = authRes.mode || "";
  }

  const cfgPath = saveCLIConfig({ url: cleanURL, token: cleanToken });
  if (flags.json) {
    console.log(
      JSON.stringify(
        {
          ok: true,
          url: cleanURL,
          mode,
          config_path: cfgPath,
          verified: !flags["skip-verify"],
        },
        null,
        2
      )
    );
    return;
  }
  console.log(`Logged in to ${cleanURL} (mode=${mode}). Saved credentials to ${cfgPath}`);
}

async function runLogout(args) {
  const { flags } = parseFlags(args, [], ["json"]);
  const p = cliConfigPath();
  if (p) {
    rmSync(p, { force: true });
  }
  if (flags.json) {
    console.log(JSON.stringify({ ok: true, removed: p }));
    return;
  }
  console.log(`Cleared saved luma-eye credentials (${p})`);
}

async function runStatus(args) {
  const { flags } = parseFlags(args, ["url", "token"], ["json"]);
  const cli = resolveClient(flags.url, flags.token);
  const cfgPath = cliConfigPath();
  const hasSaved = Boolean(cfgPath && existsSync(cfgPath));

  const { data: authRes } = await doJSON(cli, "GET", "/api/v1/auth/status");
  let projectCount = 0;
  if (authRes?.authenticated) {
    try {
      const { data: projRes } = await doJSON(cli, "GET", "/api/v1/projects");
      if (Array.isArray(projRes?.projects)) {
        projectCount = projRes.projects.length;
      }
    } catch {
      // ignore optional project count lookup errors
    }
  }

  if (flags.json) {
    const out = {
      url: cli.baseURL,
      authenticated: Boolean(authRes?.authenticated),
      mode: authRes?.mode || "",
      project_count: projectCount,
      has_saved_config: hasSaved,
      config_path: cfgPath,
    };
    if (authRes?.retention) {
      out.retention = authRes.retention;
    }
    console.log(JSON.stringify(out, null, 2));
    return;
  }

  let line = `URL: ${cli.baseURL} | Mode: ${authRes?.mode || ""} | Authenticated: ${Boolean(authRes?.authenticated)} | Projects: ${projectCount}`;
  if (authRes?.retention) {
    const r = authRes.retention;
    const usedMiB = ((r.total_bytes || 0) / (1024 * 1024)).toFixed(1);
    if ((r.max_storage_bytes || 0) > 0) {
      const capGiB = (r.max_storage_bytes / 1024 ** 3).toFixed(1);
      line += ` | Retention: ${r.retention_days}d (${usedMiB} MiB / ${capGiB} GiB)`;
    } else {
      line += ` | Retention: ${r.retention_days}d (${usedMiB} MiB / unlimited)`;
    }
  }
  console.log(line);
}

function resolveSkillDirs(agent, customPath) {
  if (customPath) return [customPath];
  const home = homedir();
  if (!home) throw new Error("cannot determine home directory");
  const clean = (agent || "all").trim().toLowerCase();
  switch (clean) {
    case "":
    case "all": {
      const dirs = [
        join(home, ".agents", "skills"),
        join(home, ".claude", "skills"),
        join(home, ".gemini", "config", "skills"),
      ];
      if (existsSync(join(home, ".codex"))) {
        dirs.push(join(home, ".codex", "skills"));
      }
      return dirs;
    }
    case "claude":
      return [join(home, ".claude", "skills")];
    case "antigravity":
    case "gemini":
      return [join(home, ".gemini", "config", "skills"), join(home, ".agents", "skills")];
    case "codex":
      return [join(home, ".codex", "skills"), join(home, ".agents", "skills")];
    default:
      throw new Error(`unknown --agent ${JSON.stringify(agent)} (must be all, claude, antigravity, or codex)`);
  }
}

async function runSkills(args) {
  if (args.length === 0) throw new Error("usage: luma-eye skills <install|status> ...");
  const action = args[0];
  const rest = args.slice(1);
  if (action === "install") {
    const { flags } = parseFlags(rest, ["agent", "path"], ["json"]);
    const dirs = resolveSkillDirs(flags.agent || "all", flags.path || "");
    const installed = [];
    for (const base of dirs) {
      const dst = join(base, "luma-eye", "SKILL.md");
      mkdirSync(dirname(dst), { recursive: true, mode: 0o755 });
      writeFileSync(dst, SKILL_MD, { mode: 0o644 });
      installed.push(dst);
    }
    if (flags.json) {
      console.log(JSON.stringify({ installed }, null, 2));
      return;
    }
    for (const p of installed) console.log(`installed ${p}`);
    return;
  }
  if (action === "status") {
    const { flags } = parseFlags(rest, ["path"], ["json"]);
    const dirs = resolveSkillDirs("all", flags.path || "");
    const statuses = dirs.map((base) => {
      const p = join(base, "luma-eye", "SKILL.md");
      return { path: p, installed: existsSync(p) };
    });
    if (flags.json) {
      console.log(JSON.stringify({ skills: statuses }, null, 2));
      return;
    }
    for (const st of statuses) {
      console.log(`installed=${String(st.installed).padEnd(5)}  ${st.path}`);
    }
    return;
  }
  throw new Error(`unknown skills subcommand ${JSON.stringify(action)} (expected install or status)`);
}

async function runProjects(args) {
  if (args.length === 0) throw new Error("usage: luma-eye projects <list|ensure|delete> ...");
  const action = args[0];
  const { flags, pos } = parseFlags(args.slice(1), ["url", "token", "name", "origins", "rps"], ["json"]);
  const cli = resolveClient(flags.url, flags.token);

  if (action === "list") {
    const { raw, data } = await doJSON(cli, "GET", "/api/v1/projects");
    if (flags.json) {
      console.log(raw);
      return;
    }
    for (const p of data?.projects || []) {
      const origins = Array.isArray(p.allowed_origins) ? p.allowed_origins.join(",") : "";
      console.log(`#${p.dsn_id}  ${String(p.slug || "").padEnd(20)}  key=${p.public_key}  origins=${origins}`);
    }
    return;
  }
  if (action === "ensure") {
    if (pos.length < 1) throw new Error("usage: luma-eye projects ensure <slug>");
    const slug = pos[0];
    const origins = (flags.origins || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const req = {
      slug,
      name: flags.name || "",
      allowed_origins: origins,
      rate_limit_rps: flags.rps ? Number(flags.rps) : 200,
    };
    const { raw, data } = await doJSON(cli, "POST", "/api/v1/projects", req);
    if (flags.json) {
      console.log(raw);
      return;
    }
    console.log(
      `project=${data.project?.slug} (created=${data.created}, dsn_id=${data.project?.dsn_id})\npublic_key=${data.project?.public_key}\ningest_url=${data.ingest_url}\nsentry_dsn=${data.sentry_dsn}`
    );
    return;
  }
  if (action === "delete") {
    if (pos.length < 1) throw new Error("usage: luma-eye projects delete <slug>");
    const slug = pos[0];
    const { raw } = await doJSON(cli, "DELETE", `/api/v1/projects/${encodeURIComponent(slug)}`);
    if (flags.json) {
      console.log(raw);
      return;
    }
    console.log(`deleted project: ${slug}`);
    return;
  }
  throw new Error(`unknown projects subcommand ${JSON.stringify(action)}`);
}

async function runStats(args) {
  const { flags, pos } = parseFlags(args, ["url", "token", "since"], ["json"]);
  if (pos.length < 1) throw new Error("usage: luma-eye stats <project>");
  const slug = pos[0];
  const since = flags.since || "24h";
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({ project: slug, since });
  const { raw, data: ov } = await doJSON(cli, "GET", `/api/v1/query/overview?${q}`);
  if (flags.json) {
    console.log(raw);
    return;
  }
  console.log(`Project: ${ov.project} (${ov.window})`);
  console.log(
    `Active(5m): ${ov.active_users_5m} | UV: ${ov.unique_visitors} | PV: ${ov.pageviews} | Custom Events: ${ov.custom_events} | Sessions: ${ov.sessions} | Errors: ${ov.error_count} | Crash-Free: ${Number(ov.crash_free_rate || 0).toFixed(2)}%`
  );
  if (Array.isArray(ov.vitals_p75) && ov.vitals_p75.length > 0) {
    const parts = ov.vitals_p75.map((v) => `${v.metric_name}=${Number(v.p75 || 0).toFixed(2)} (${v.samples})`);
    console.log(`Web Vitals P75: ${parts.join(" | ")}`);
  }
}

async function runFunnel(args) {
  const { flags, pos } = parseFlags(args, ["url", "token", "steps", "window", "since"], ["json"]);
  if (pos.length < 1 || !flags.steps) {
    throw new Error('usage: luma-eye funnel <project> --steps "step1,step2"');
  }
  const slug = pos[0];
  const steps = String(flags.steps)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const cli = resolveClient(flags.url, flags.token);
  const { raw, data: res } = await doJSON(cli, "POST", "/api/v1/query/funnel", {
    project: slug,
    steps,
    window_seconds: flags.window ? Number(flags.window) : 3600,
    since: flags.since || "7d",
  });
  if (flags.json) {
    console.log(raw);
    return;
  }
  console.log(
    `Funnel for ${res.project} (window=${res.window_seconds}s, overall=${Number(res.overall_conversion || 0).toFixed(2)}%)`
  );
  for (const st of res.steps || []) {
    console.log(
      `  ${st.step_index}. ${String(st.step || "").padEnd(24)} users=${st.users}  overall=${Number(st.conversion_rate || 0).toFixed(2)}%  step=${Number(st.step_rate || 0).toFixed(2)}%`
    );
  }
}

async function runRetention(args) {
  const { flags, pos } = parseFlags(args, ["url", "token", "days"], ["json"]);
  if (pos.length < 1) throw new Error("usage: luma-eye retention <project>");
  const slug = pos[0];
  const days = flags.days ? Number(flags.days) : 7;
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({ project: slug, days: String(days) });
  const { raw, data: res } = await doJSON(cli, "GET", `/api/v1/query/retention?${q}`);
  if (flags.json) {
    console.log(raw);
    return;
  }
  console.log(`Retention (${res.days} days) for ${res.project}:`);
  for (const row of res.rows || []) {
    console.log(`  ${row.cohort_date} (n=${row.cohort_users}): ${JSON.stringify(row.day_counts || [])}`);
  }
}

async function runIssues(args) {
  const { flags, pos } = parseFlags(
    args,
    ["url", "token", "since", "status", "detail", "resolve", "ignore", "reopen"],
    ["json"]
  );
  if (pos.length < 1) throw new Error("usage: luma-eye issues <project>");
  const slug = pos[0];
  const cli = resolveClient(flags.url, flags.token);

  if (flags.resolve || flags.ignore || flags.reopen) {
    const fp = flags.resolve || flags.ignore || flags.reopen;
    const newSt = flags.resolve ? "resolved" : flags.ignore ? "ignored" : "open";
    const { raw } = await doJSON(cli, "POST", `/api/v1/issues/${encodeURIComponent(fp)}/status`, {
      project: slug,
      status: newSt,
    });
    console.log(raw);
    return;
  }

  if (flags.detail) {
    const q = new URLSearchParams({ project: slug });
    const { raw, data: d } = await doJSON(
      cli,
      "GET",
      `/api/v1/issues/${encodeURIComponent(flags.detail)}?${q}`
    );
    if (flags.json) {
      console.log(raw);
      return;
    }
    const sm = d.summary || {};
    console.log(
      `[${sm.status}] ${sm.error_type}: ${sm.error_message} (count=${sm.count} users=${sm.users} release=${d.release || ""})\nCulprit: ${sm.culprit}\nTraceID: ${d.trace_id || ""}`
    );
    console.log("Frames:");
    for (const f of d.frames || []) {
      console.log(`  at ${f.function} (${f.filename}:${f.lineno}:${f.colno}) [symbolicated=${Boolean(f.symbolicated)}]`);
      if (f.context_line) console.log(`    > ${f.context_line}`);
    }
    const bcs = d.breadcrumbs || [];
    console.log(`Breadcrumbs (${bcs.length}):`);
    for (const bc of bcs) {
      console.log(`  [${bc.ts || ""}] ${bc.category} (${bc.level}): ${bc.message}`);
    }
    return;
  }

  const q = new URLSearchParams({
    project: slug,
    since: flags.since || "7d",
    status: flags.status || "open",
  });
  const { raw, data } = await doJSON(cli, "GET", `/api/v1/issues?${q}`);
  if (flags.json) {
    console.log(raw);
    return;
  }
  for (const is of data?.issues || []) {
    console.log(
      `[${is.status}] ${String(is.fingerprint || "").padEnd(16)} count=${String(is.count).padEnd(5)} users=${String(is.users).padEnd(4)} ${is.error_type}: ${is.error_message} (${is.culprit})`
    );
  }
}

async function runLogs(args) {
  const { flags, pos } = parseFlags(
    args,
    ["url", "token", "level", "search", "trace", "since", "limit"],
    ["json"]
  );
  if (pos.length < 1) throw new Error("usage: luma-eye logs <project>");
  const slug = pos[0];
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({
    project: slug,
    level: flags.level || "all",
    q: flags.search || "",
    trace_id: flags.trace || "",
    since: flags.since || "1h",
    limit: String(flags.limit || 100),
  });
  const { raw, data } = await doJSON(cli, "GET", `/api/v1/logs?${q}`);
  if (flags.json) {
    console.log(raw);
    return;
  }
  for (const lg of data?.logs || []) {
    console.log(`${lg.ts} [${String(lg.level || "").padEnd(5)}] ${lg.message} (trace=${lg.trace_id || ""})`);
  }
}

function collectMapFiles(dirPath) {
  const out = [];
  for (const entry of readdirSync(dirPath, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const full = join(dirPath, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectMapFiles(full));
    } else if (entry.isFile() && entry.name.endsWith(".map")) {
      out.push(full);
    }
  }
  return out;
}

async function runSourceMaps(args) {
  if (args.length === 0 || args[0] !== "upload") {
    throw new Error("usage: luma-eye sourcemaps upload --project <slug> --release <ver> [--prefix assets/] <dir-or-file>");
  }
  const { flags, pos } = parseFlags(args.slice(1), ["url", "token", "project", "release", "prefix"], ["json"]);
  if (!flags.project || pos.length < 1) {
    throw new Error("usage: luma-eye sourcemaps upload --project <slug> --release <ver> <dir-or-file>");
  }
  const release = flags.release || "default";
  const prefix = flags.prefix || "";
  const targetPath = resolve(pos[0]);
  const st = statSync(targetPath);
  const isDir = st.isDirectory();
  const files = isDir ? collectMapFiles(targetPath) : [targetPath];
  if (files.length === 0) {
    throw new Error(`no .map files found in ${pos[0]}`);
  }

  const cli = resolveClient(flags.url, flags.token, 60000);
  const uploaded = [];
  for (const f of files) {
    let rel = isDir ? relative(targetPath, f).replace(/\\/g, "/") : basename(f);
    let assetPath = rel.endsWith(".map") ? rel.slice(0, -4) : rel;
    if (prefix) {
      assetPath = `${prefix.replace(/\/+$/, "")}/${assetPath.replace(/^\/+/, "")}`;
    }
    const form = new FormData();
    form.append("project", flags.project);
    form.append("release", release);
    form.append("asset", assetPath);
    form.append("file", new Blob([readFileSync(f)]), basename(f));

    const headers = {};
    if (cli.token) headers.Authorization = `Bearer ${cli.token}`;
    const resp = await fetch(`${cli.baseURL}/api/v1/sourcemaps`, {
      method: "POST",
      headers,
      body: form,
      signal: AbortSignal.timeout(cli.timeoutMs),
    });
    const raw = await resp.text();
    if (resp.status >= 300) {
      throw new Error(`upload ${f} failed (HTTP ${resp.status}): ${raw.trim()}`);
    }
    uploaded.push({ file: f, asset: assetPath, release });
    if (!flags.json) {
      console.log(`uploaded ${f} -> asset=${assetPath} (release=${release})`);
    }
  }
  if (flags.json) {
    console.log(JSON.stringify({ ok: true, uploaded }, null, 2));
  }
}

async function runPortfolio(args) {
  const { flags } = parseFlags(args, ["url", "token", "since"], ["json"]);
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({ since: flags.since || "24h" });
  const { raw, data } = await doJSON(cli, "GET", `/api/v1/query/portfolio?${q}`);
  if (flags.json) {
    console.log(raw.trim());
    return;
  }
  console.log(
    `Portfolio (${data.window}): ${data.active_projects}/${data.total_projects} active | 5m=${data.active_users_5m} | UV=${data.unique_visitors} | PV=${data.pageviews} | OpenIssues=${data.open_issues}`
  );
  for (const p of data.projects || []) {
    console.log(
      `  - ${p.project.slug}: UV=${p.unique_visitors} PV=${p.pageviews} Events=${p.custom_events} OpenIssues=${p.open_issues} CrashFree=${Number(p.crash_free_rate || 0).toFixed(2)}%`
    );
  }
}

async function runPages(args) {
  const { flags, pos } = parseFlags(args, ["url", "token", "project", "since", "limit"], ["json"]);
  const proj = flags.project || pos[0];
  if (!proj) throw new Error("project slug is required");
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({
    project: proj,
    since: flags.since || "24h",
    limit: String(flags.limit || 50),
  });
  const { raw, data } = await doJSON(cli, "GET", `/api/v1/query/pages?${q}`);
  if (flags.json) {
    console.log(raw.trim());
    return;
  }
  for (const r of data.pages || []) {
    console.log(
      `${r.page_path}\tPV=${r.pageviews}\tUV=${r.users}\tAvgStay=${Math.round(r.avg_duration_ms || 0)}ms\tLCP_P75=${Math.round(r.lcp_p75 || 0)}ms\tErrors=${r.error_count}`
    );
  }
}

async function runSessions(args) {
  const { flags, pos } = parseFlags(
    args,
    ["url", "token", "project", "since", "search", "country", "actor", "limit"],
    ["json", "errors-only"]
  );
  const proj = flags.project || pos[0];
  if (!proj) throw new Error("project slug is required");
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({
    project: proj,
    since: flags.since || "7d",
    limit: String(flags.limit || 80),
  });
  if (flags.search) q.set("q", flags.search);
  if (flags.country) q.set("country", flags.country);
  if (flags.actor) q.set("actor_id", flags.actor);
  if (flags["errors-only"]) q.set("has_error", "true");
  const { raw, data } = await doJSON(cli, "GET", `/api/v1/sessions?${q}`);
  if (flags.json) {
    console.log(raw.trim());
    return;
  }
  for (const s of data.sessions || []) {
    console.log(
      `${s.last_ts}\tactor=${s.actor_id}\tsession=${s.session_id}\tcountry=${s.country || "-"}\tPV=${s.pageviews}\terrors=${s.error_count}\t${s.entry_page} -> ${s.exit_page}`
    );
  }
}

async function runTimeline(args) {
  const { flags, pos } = parseFlags(args, ["url", "token", "project", "session", "actor", "since", "limit"], ["json"]);
  const proj = flags.project || pos[0];
  if (!proj) throw new Error("project slug is required");
  if (!flags.session && !flags.actor) throw new Error("--session <sid> or --actor <uid> is required");
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({
    project: proj,
    since: flags.since || "7d",
    limit: String(flags.limit || 300),
  });
  if (flags.session) q.set("session_id", flags.session);
  if (flags.actor) q.set("actor_id", flags.actor);
  const { raw, data } = await doJSON(cli, "GET", `/api/v1/timeline?${q}`);
  if (flags.json) {
    console.log(raw.trim());
    return;
  }
  console.log(
    `Timeline project=${data.project} actor=${data.actor_id} session=${data.session_id} country=${data.country || "-"} steps=${data.total_items} errors=${data.error_count}`
  );
  for (const it of data.items || []) {
    console.log(`  [+${it.offset_ms}ms] (${it.kind}) ${it.title}${it.subtitle ? ` | ${it.subtitle}` : ""}`);
    if (it.kind === "exception" && Array.isArray(it.breadcrumbs)) {
      for (const bc of it.breadcrumbs) {
        console.log(`      ↳ [${bc.category}] ${bc.message}`);
      }
    }
  }
}

async function runEvents(args) {
  const { flags, pos } = parseFlags(
    args,
    ["url", "token", "project", "type", "name", "search", "actor", "session", "country", "since", "limit"],
    ["json"]
  );
  const proj = flags.project || pos[0];
  if (!proj) throw new Error("project slug is required");
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({
    project: proj,
    since: flags.since || "24h",
    limit: String(flags.limit || 100),
  });
  if (flags.type) q.set("event_type", flags.type);
  if (flags.name) q.set("event_name", flags.name);
  if (flags.search) q.set("q", flags.search);
  if (flags.actor) q.set("actor_id", flags.actor);
  if (flags.session) q.set("session_id", flags.session);
  if (flags.country) q.set("country", flags.country);
  const { raw, data } = await doJSON(cli, "GET", `/api/v1/events?${q}`);
  if (flags.json) {
    console.log(raw.trim());
    return;
  }
  for (const ev of data.events || []) {
    console.log(
      `${ev.ts}\t[${ev.event_type}]\t${ev.event_name}\tpath=${ev.page_path}\tcountry=${ev.country || "-"}\tactor=${ev.actor_id}`
    );
  }
}

async function runGrafana(args) {
  if (args.length === 0 || args[0] !== "render") {
    throw new Error("usage: luma-eye grafana render --project <slug> [--datasource victoria|clickhouse] [--output <path>]");
  }
  const { flags } = parseFlags(args.slice(1), ["url", "token", "project", "datasource", "output"], ["json"]);
  if (!flags.project) throw new Error("--project is required");
  const cli = resolveClient(flags.url, flags.token);
  const q = new URLSearchParams({
    project: flags.project,
    datasource: flags.datasource || "victoria",
  });
  const { raw } = await doJSON(cli, "GET", `/api/v1/grafana-dashboard?${q}`);
  if (!flags.output) {
    console.log(raw);
    return;
  }
  mkdirSync(dirname(flags.output), { recursive: true, mode: 0o755 });
  writeFileSync(flags.output, `${raw.trimEnd()}\n`, { mode: 0o644 });
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv.length === 0 || argv[0] === "-h" || argv[0] === "--help" || argv[0] === "help") {
    printUsage();
    return;
  }
  if (argv[0] === "-v" || argv[0] === "--version" || argv[0] === "version") {
    console.log(`luma-eye v${VERSION}`);
    return;
  }

  const [sub, ...rest] = argv;
  switch (sub) {
    case "login":
      await runLogin(rest);
      break;
    case "logout":
      await runLogout(rest);
      break;
    case "status":
      await runStatus(rest);
      break;
    case "skills":
      await runSkills(rest);
      break;
    case "projects":
      await runProjects(rest);
      break;
    case "portfolio":
      await runPortfolio(rest);
      break;
    case "stats":
      await runStats(rest);
      break;
    case "pages":
      await runPages(rest);
      break;
    case "sessions":
      await runSessions(rest);
      break;
    case "timeline":
      await runTimeline(rest);
      break;
    case "events":
      await runEvents(rest);
      break;
    case "funnel":
      await runFunnel(rest);
      break;
    case "retention":
      await runRetention(rest);
      break;
    case "issues":
      await runIssues(rest);
      break;
    case "logs":
      await runLogs(rest);
      break;
    case "sourcemaps":
      await runSourceMaps(rest);
      break;
    case "grafana":
      await runGrafana(rest);
      break;
    default:
      process.stderr.write(`unknown subcommand: ${sub}\n\n`);
      printUsage();
      process.exitCode = 1;
  }
}

main().catch((err) => {
  process.stderr.write(`error: ${err?.message || err}\n`);
  process.exit(1);
});
