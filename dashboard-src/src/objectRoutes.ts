// Object identities remain encoded as one path segment, including names with '/'.
export function decodeRouteSegment(value: string): string {
  try { return decodeURIComponent(value); } catch { return ""; }
}
export function nodePath(name: string): string { return `/fleet/nodes/${encodeURIComponent(name)}`; }
export function servicePath(name: string): string { return `/services/${encodeURIComponent(name)}`; }
export function updatePath(stack: string): string { return `/apps/${encodeURIComponent(stack)}/edit`; }
export function terminalPath(kind: "node" | "service", name: string, stack?: string): string {
  return `/terminal/${kind}/${encodeURIComponent(name)}${stack ? `?stack=${encodeURIComponent(stack)}` : ""}`;
}
export function parseObjectRoute(path: string): { kind: "node" | "service" | "node-terminal" | "service-terminal" | "update"; name: string } | null {
  const patterns = [
    ["node", /^\/fleet\/nodes\/([^/]+)\/?$/],
    ["service", /^\/services\/([^/]+)\/?$/],
    ["node-terminal", /^\/terminal\/node\/([^/]+)\/?$/],
    ["service-terminal", /^\/terminal\/service\/([^/]+)\/?$/],
    ["update", /^\/apps\/([^/]+)\/edit\/?$/],
  ] as const;
  for (const [kind, pattern] of patterns) {
    const match = pattern.exec(path);
    if (match) return { kind, name: decodeRouteSegment(match[1]) };
  }
  return null;
}
