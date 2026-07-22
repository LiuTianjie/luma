import type { NextConfig } from "next";
import { fileURLToPath } from "node:url";

const workspaceRoot = fileURLToPath(new URL("../..", import.meta.url));

function internalApiOrigin(): string {
  const value = process.env.LAE_API_INTERNAL_URL || "http://127.0.0.1:8080";
  const parsed = new URL(value);
  const allowedHosts = new Set(["127.0.0.1", "localhost", "api"]);
  if (
    parsed.protocol !== "http:" ||
    !allowedHosts.has(parsed.hostname) ||
    parsed.port !== "8080" ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("LAE_API_INTERNAL_URL is not an allowed internal API origin");
  }
  return parsed.origin;
}

const apiOrigin = internalApiOrigin();

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/v1/:path*",
        destination: `${apiOrigin}/v1/:path*`,
      },
      {
        source: "/health/:path*",
        destination: `${apiOrigin}/health/:path*`,
      },
      {
        source: "/version",
        destination: `${apiOrigin}/version`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=()",
          },
        ],
      },
    ];
  },
  output: "standalone",
  outputFileTracingRoot: workspaceRoot,
  poweredByHeader: false,
  reactStrictMode: true,
  turbopack: {
    root: workspaceRoot,
  },
};

export default nextConfig;
