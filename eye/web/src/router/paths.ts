export type ProjectRouteSegment =
  | "overview"
  | "sessions"
  | "events"
  | "issues"
  | "performance"
  | "funnel"
  | "logs"
  | "settings";

export function buildUrl(
  pathname: string,
  params?: Record<string, string | number | boolean | undefined | null>,
): string {
  if (!params) return pathname;
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    sp.set(k, v === true ? "1" : String(v));
  }
  const qs = sp.toString();
  return qs ? `${pathname}?${qs}` : pathname;
}

export const paths = {
  portfolio: (since?: string) => buildUrl("/portfolio", { since: since === "24h" ? undefined : since }),

  account: () => "/account",

  projectSection: (
    slug: string,
    section: ProjectRouteSegment,
    since?: string,
    extra?: Record<string, string | undefined>,
  ) =>
    buildUrl(`/projects/${encodeURIComponent(slug)}/${section}`, {
      since: since === "24h" ? undefined : since,
      ...extra,
    }),

  sessionDetail: (slug: string, sessionId: string, since?: string) =>
    buildUrl(`/projects/${encodeURIComponent(slug)}/sessions/${encodeURIComponent(sessionId)}`, {
      since: since === "24h" ? undefined : since,
    }),

  userDetail: (slug: string, actorId: string, since?: string) =>
    buildUrl(`/projects/${encodeURIComponent(slug)}/users/${encodeURIComponent(actorId)}`, {
      since: since === "24h" ? undefined : since,
    }),

  issueDetail: (slug: string, fingerprint: string, opts?: { since?: string; sample?: number; tab?: string }) =>
    buildUrl(`/projects/${encodeURIComponent(slug)}/issues/${encodeURIComponent(fingerprint)}`, {
      since: opts?.since === "24h" ? undefined : opts?.since,
      sample: opts?.sample && opts.sample > 0 ? opts.sample : undefined,
      tab: opts?.tab && opts.tab !== "breadcrumbs" ? opts.tab : undefined,
    }),
};
