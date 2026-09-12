export type ApplicationSecret = { name: string; source: "application" | "global" | "missing"; referenced: boolean };
export function applicationSecretScope(name: string) {
  return name.trim().toLowerCase().replace(/[^a-zA-Z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
}
export function applicationSecretRows(names: string[], scope: string, texts: string[]): ApplicationSecret[] {
  const references = new Set<string>();
  for (const text of texts) {
    for (const match of text.matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) references.add(match[1]);
  }
  const scoped = new Set(names.filter(name => name.startsWith(`${scope}/`)).map(name => name.slice(scope.length + 1)));
  const global = new Set(names.filter(name => !name.includes("/")));
  return [...new Set([...scoped, ...references])].sort().map(name => ({
    name, source: scoped.has(name) ? "application" : global.has(name) ? "global" : "missing", referenced: references.has(name),
  }));
}
