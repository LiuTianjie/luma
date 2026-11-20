import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const __dirname = dirname(fileURLToPath(import.meta.url));
const cliPath = resolve(__dirname, "../dist/cli.mjs");
const skillSrcPath = resolve(__dirname, "../SKILL.md");

async function runCLI(args, env = {}) {
  const { stdout } = await execFileAsync(process.execPath, [cliPath, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
  return stdout;
}

test("npm bundled luma-eye CLI login, API queries, sourcemaps, and skills install", async () => {
  const workDir = mkdtempSync(join(tmpdir(), "luma-eye-node-cli-"));
  const cfgPath = join(workDir, "config", "config.json");
  const skillsDir = join(workDir, "skills");
  const mapDir = join(workDir, "dist", "assets");
  mkdirSync(mapDir, { recursive: true });
  writeFileSync(join(mapDir, "index-abc.js.map"), '{"version":3,"sources":[],"names":[],"mappings":""}');

  const projects = new Map();
  let uploadedMultipartBody = "";
  const token = "cli-node-secret-token";

  const srv = http.createServer((req, res) => {
    const u = new URL(req.url, "http://127.0.0.1");
    const auth = req.headers.authorization || "";
    const authorized = auth === `Bearer ${token}`;

    if (u.pathname === "/api/v1/auth/status" && req.method === "GET") {
      res.setHeader("Content-Type", "application/json");
      res.end(
        JSON.stringify({
          authenticated: authorized,
          mode: "all",
          retention: {
            retention_days: 7,
            max_storage_bytes: 16106127360,
            total_bytes: 1048576,
          },
        })
      );
      return;
    }

    if (!authorized) {
      res.statusCode = 401;
      res.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }

    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      res.setHeader("Content-Type", "application/json");

      if (u.pathname === "/api/v1/projects" && req.method === "GET") {
        res.end(JSON.stringify({ projects: Array.from(projects.values()) }));
        return;
      }
      if (u.pathname === "/api/v1/projects" && req.method === "POST") {
        const body = JSON.parse(raw);
        const created = !projects.has(body.slug);
        const proj = {
          dsn_id: projects.size + 1,
          slug: body.slug,
          name: body.name || body.slug,
          public_key: `pk_${body.slug}_1234`,
          allowed_origins: body.allowed_origins || [],
        };
        projects.set(body.slug, proj);
        res.statusCode = created ? 201 : 200;
        res.end(
          JSON.stringify({
            created,
            project: proj,
            ingest_url: `http://127.0.0.1:${srv.address().port}/_e/b`,
            sentry_dsn: `http://${proj.public_key}@127.0.0.1:${srv.address().port}/${proj.dsn_id}`,
          })
        );
        return;
      }
      if (u.pathname.startsWith("/api/v1/projects/") && req.method === "DELETE") {
        const slug = decodeURIComponent(u.pathname.split("/").pop());
        projects.delete(slug);
        res.end(JSON.stringify({ deleted: slug }));
        return;
      }
      if (u.pathname === "/api/v1/query/overview" && req.method === "GET") {
        res.end(
          JSON.stringify({
            project: u.searchParams.get("project"),
            window: u.searchParams.get("since") || "24h",
            active_users_5m: 5,
            unique_visitors: 42,
            pageviews: 120,
            custom_events: 18,
            sessions: 50,
            error_count: 1,
            crash_free_rate: 98.0,
            vitals_p75: [{ metric_name: "LCP", p75: 1250.5, samples: 12 }],
          })
        );
        return;
      }
      if (u.pathname === "/api/v1/sourcemaps" && req.method === "POST") {
        uploadedMultipartBody = raw;
        res.end(JSON.stringify({ ok: true }));
        return;
      }
      res.statusCode = 404;
      res.end(JSON.stringify({ error: "not found" }));
    });
  });

  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  const baseURL = `http://127.0.0.1:${srv.address().port}`;
  const cleanEnv = {
    LUMA_EYE_CONFIG: cfgPath,
    LUMA_EYE_URL: "",
    LUMA_EYE_ADMIN_TOKEN: "",
  };

  try {
    // 1. Invalid token fails verification
    await assert.rejects(
      () => runCLI(["login", "--url", baseURL, "--token", "bad-token"], cleanEnv),
      (err) => /authentication failed/.test(String(err?.stderr || err?.message || err))
    );

    // 2. Valid token verifies & writes 0600 config
    const loginOut = JSON.parse(
      await runCLI(["login", "--url", baseURL, "--token", token, "--json"], cleanEnv)
    );
    assert.equal(loginOut.ok, true);
    assert.equal(loginOut.url, baseURL);
    assert.equal(statSync(cfgPath).mode & 0o777, 0o600);

    // 3. Subsequent commands use saved config without --url / --token flags
    const st = JSON.parse(await runCLI(["status", "--json"], cleanEnv));
    assert.equal(st.authenticated, true);
    assert.equal(st.retention.retention_days, 7);

    const ensured = JSON.parse(
      await runCLI(["projects", "ensure", "vibecheck", "--origins", "https://vibecheck.itool.tech", "--json"], cleanEnv)
    );
    assert.equal(ensured.created, true);
    assert.equal(ensured.project.slug, "vibecheck");

    const listOut = JSON.parse(await runCLI(["projects", "list", "--json"], cleanEnv));
    assert.equal(listOut.projects.length, 1);

    const statsText = await runCLI(["stats", "vibecheck", "--since", "24h"], cleanEnv);
    assert.match(statsText, /UV: 42/);
    assert.match(statsText, /LCP=1250\.50/);

    const smOut = JSON.parse(
      await runCLI(
        ["sourcemaps", "upload", "--project", "vibecheck", "--release", "v1", "--prefix", "/assets", "--json", mapDir],
        cleanEnv
      )
    );
    assert.equal(smOut.ok, true);
    assert.equal(smOut.uploaded[0].asset, "/assets/index-abc.js");
    assert.match(uploadedMultipartBody, /"version":3/);

    const delOut = JSON.parse(await runCLI(["projects", "delete", "vibecheck", "--json"], cleanEnv));
    assert.equal(delOut.deleted, "vibecheck");

    // 4. Skills install writes SKILL.md cleanly
    const skillInstall = JSON.parse(
      await runCLI(["skills", "install", "--path", skillsDir, "--json"], cleanEnv)
    );
    assert.equal(skillInstall.installed.length, 1);
    const writtenSkill = readFileSync(join(skillsDir, "luma-eye", "SKILL.md"), "utf8");
    assert.equal(writtenSkill, readFileSync(skillSrcPath, "utf8"));

    const skillStatus = JSON.parse(await runCLI(["skills", "status", "--path", skillsDir, "--json"], cleanEnv));
    assert.equal(skillStatus.skills[0].installed, true);

    // 5. Logout removes config file
    const outLogout = JSON.parse(await runCLI(["logout", "--json"], cleanEnv));
    assert.equal(outLogout.ok, true);
    assert.equal(existsSync(cfgPath), false);
  } finally {
    srv.close();
    rmSync(workDir, { recursive: true, force: true });
  }
});
