import type {
  AuthStatus,
  BrowserSession,
  EventView,
  FunnelResult,
  IssueDetail,
  IssueSummary,
  LogView,
  OverviewStats,
  PageMetricRow,
  PasskeyCredential,
  PortfolioOverview,
  Project,
  RetentionResult,
  SessionReplayResponse,
  SessionSummary,
  TimelineResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function req<T>(
  path: string,
  init?: RequestInit & { suppressUnauthorizedEvent?: boolean },
): Promise<T> {
  const { suppressUnauthorizedEvent, ...fetchInit } = init || {};
  const resp = await fetch(path, {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(fetchInit.headers || {}),
    },
    ...fetchInit,
  });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body?.error) msg = body.error;
    } catch {
      // ignore non-json error bodies
    }
    if (resp.status === 401 && !suppressUnauthorizedEvent && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("luma-eye:unauthorized"));
    }
    throw new ApiError(resp.status, msg);
  }
  return resp.json() as Promise<T>;
}

function buildQuery(params: Record<string, string | number | boolean | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === "") continue;
    sp.set(k, String(v));
  }
  return sp.toString();
}

export const api = {
  authStatus: (signal?: AbortSignal) =>
    req<AuthStatus>("/api/v1/auth/status", { signal, suppressUnauthorizedEvent: true }),

  login: (payload: { username?: string; password?: string; token?: string }) =>
    req<{
      authenticated: boolean;
      method: string;
      username: string;
      display_name: string;
      has_password: boolean;
      passkey_count: number;
    }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
      suppressUnauthorizedEvent: true,
    }),

  logout: () =>
    req<{ authenticated: boolean }>("/api/v1/auth/logout", {
      method: "POST",
      suppressUnauthorizedEvent: true,
    }),

  updatePassword: (payload: { current_password?: string; new_password: string }) =>
    req<{ ok: boolean; username: string; has_password: boolean }>("/api/v1/auth/password", {
      method: "POST",
      body: JSON.stringify(payload),
      suppressUnauthorizedEvent: true,
    }),

  updateProfile: (payload: { name: string; displayName: string }) =>
    req<{ name: string; displayName: string; rpId: string; userId: string }>("/api/v1/auth/profile", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  listAuthSessions: (signal?: AbortSignal) =>
    req<{ sessions: BrowserSession[]; sessionTtlHours: number }>("/api/v1/auth/sessions", { signal }),

  revokeAuthSessions: (payload: { id?: string; all?: boolean }) =>
    req<{ revoked: number }>("/api/v1/auth/sessions/revoke", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listPasskeys: (signal?: AbortSignal) =>
    req<{ rpId: string; available: boolean; userId: string; credentials: PasskeyCredential[] }>(
      "/api/v1/auth/passkeys",
      { signal },
    ),

  renamePasskey: (id: string, name: string) =>
    req<PasskeyCredential>(`/api/v1/auth/passkeys/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ name }),
    }),

  deletePasskey: (id: string) =>
    req<{
      deleted: string;
      signedOutSessions: number;
      rpId: string;
      userId: string;
      credentials: PasskeyCredential[];
    }>(`/api/v1/auth/passkeys/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  listProjects: (signal?: AbortSignal) =>
    req<{ projects: Project[]; public_url: string }>("/api/v1/projects", { signal }),

  ensureProject: (slug: string, name: string, allowedOrigins: string[]) =>
    req<{ created: boolean; project: Project; sentry_dsn: string; ingest_url: string }>("/api/v1/projects", {
      method: "POST",
      body: JSON.stringify({ slug, name, allowed_origins: allowedOrigins }),
    }),

  deleteProject: (slug: string) =>
    req<{ deleted: string }>(`/api/v1/projects/${encodeURIComponent(slug)}`, { method: "DELETE" }),

  portfolio: (since: string, signal?: AbortSignal) =>
    req<PortfolioOverview>(`/api/v1/query/portfolio?${buildQuery({ since })}`, { signal }),

  overview: (project: string, since: string, signal?: AbortSignal) =>
    req<OverviewStats>(`/api/v1/query/overview?${buildQuery({ project, since })}`, { signal }),

  pages: (project: string, since: string, limit = 60, signal?: AbortSignal) =>
    req<{ pages: PageMetricRow[] }>(`/api/v1/query/pages?${buildQuery({ project, since, limit })}`, {
      signal,
    }),

  listEvents: (
    project: string,
    opts: {
      since: string;
      eventType?: string;
      eventName?: string;
      actorId?: string;
      sessionId?: string;
      country?: string;
      q?: string;
      limit?: number;
      signal?: AbortSignal;
    },
  ) =>
    req<{ events: EventView[] }>(
      `/api/v1/events?${buildQuery({
        project,
        since: opts.since,
        event_type: opts.eventType,
        event_name: opts.eventName,
        actor_id: opts.actorId,
        session_id: opts.sessionId,
        country: opts.country,
        q: opts.q,
        limit: opts.limit ?? 120,
      })}`,
      { signal: opts.signal },
    ),

  listSessions: (
    project: string,
    opts: {
      since: string;
      actorId?: string;
      country?: string;
      q?: string;
      hasError?: boolean;
      limit?: number;
      signal?: AbortSignal;
    },
  ) =>
    req<{ sessions: SessionSummary[] }>(
      `/api/v1/sessions?${buildQuery({
        project,
        since: opts.since,
        actor_id: opts.actorId,
        country: opts.country,
        q: opts.q,
        has_error: opts.hasError ? "true" : undefined,
        limit: opts.limit ?? 100,
      })}`,
      { signal: opts.signal },
    ),

  timeline: (
    project: string,
    opts: { sessionId?: string; actorId?: string; since?: string; limit?: number; signal?: AbortSignal },
  ) =>
    req<TimelineResponse>(
      `/api/v1/timeline?${buildQuery({
        project,
        session_id: opts.sessionId,
        actor_id: opts.actorId,
        since: opts.since ?? "7d",
        limit: opts.limit ?? 300,
      })}`,
      { signal: opts.signal },
    ),

  replay: (project: string, sessionId: string, signal?: AbortSignal) =>
    req<SessionReplayResponse>(
      `/api/v1/replays/${encodeURIComponent(sessionId)}?${buildQuery({ project })}`,
      { signal },
    ),

  funnel: (project: string, steps: string[], windowSeconds: number, since: string, signal?: AbortSignal) =>
    req<FunnelResult>("/api/v1/query/funnel", {
      method: "POST",
      body: JSON.stringify({ project, steps, window_seconds: windowSeconds, since }),
      signal,
    }),

  retention: (project: string, days: number, signal?: AbortSignal) =>
    req<RetentionResult>(`/api/v1/query/retention?${buildQuery({ project, days })}`, { signal }),

  listIssues: (project: string, since: string, status: string, signal?: AbortSignal) =>
    req<{ issues: IssueSummary[] }>(`/api/v1/issues?${buildQuery({ project, since, status })}`, { signal }),

  issueDetail: (project: string, fingerprint: string, signal?: AbortSignal) =>
    req<IssueDetail>(`/api/v1/issues/${encodeURIComponent(fingerprint)}?${buildQuery({ project })}`, {
      signal,
    }),

  setIssueStatus: (project: string, fingerprint: string, status: "open" | "resolved" | "ignored") =>
    req<unknown>(`/api/v1/issues/${encodeURIComponent(fingerprint)}/status`, {
      method: "POST",
      body: JSON.stringify({ project, status }),
    }),

  searchLogs: (
    project: string,
    opts: {
      level: string;
      q?: string;
      traceId?: string;
      actorId?: string;
      sessionId?: string;
      since: string;
      signal?: AbortSignal;
    },
  ) =>
    req<{ logs: LogView[] }>(
      `/api/v1/logs?${buildQuery({
        project,
        level: opts.level,
        q: opts.q,
        trace_id: opts.traceId,
        actor_id: opts.actorId,
        session_id: opts.sessionId,
        since: opts.since,
      })}`,
      { signal: opts.signal },
    ),

  sendDiagnosticSample: async (project: Project, kind: "event" | "crash") => {
    const now = Date.now();
    const sessionId = `s_drill_${Math.random().toString(36).slice(2, 9)}`;
    const actorId = "u_drill_inspector";
    const traceId = `4bf92f3577b34da6a3ce929d${Math.random().toString(16).slice(2, 10).padEnd(8, "0")}`;

    const payload: Record<string, unknown> = {
      key: project.public_key,
      project: project.slug,
      track: kind === "crash" ? "urgent" : "normal",
      env: "drill",
      release: "drill-1.0.0",
      actor_id: actorId,
      session_id: sessionId,
      page_url: `${window.location.origin}/drill-checkout`,
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      lang: navigator.language,
      events: [
        {
          ts: now - 6200,
          type: "pageview",
          name: "pageview",
          page_path: "/drill-pricing",
          page_url: `${window.location.origin}/drill-pricing`,
          duration_ms: 3400,
        },
        {
          ts: now - 2800,
          type: "custom",
          name: "drill_plan_selected",
          page_path: "/drill-checkout",
          props: { plan: "pro", billing: "annual", source: "console_drill" },
        },
      ],
      vitals: [
        {
          ts: now - 5400,
          name: "LCP",
          value: 1180,
          rating: "good",
          page_path: "/drill-pricing",
        },
      ],
    };

    if (kind === "crash") {
      payload.logs = [
        {
          ts: now - 950,
          level: "warn",
          message: "Payment gateway response exceeded 800ms threshold",
          page_url: `${window.location.origin}/drill-checkout`,
          trace_id: traceId,
          attrs: { gateway: "stripe", retry: 1 },
        },
      ];
      payload.errors = [
        {
          ts: now,
          level: "error",
          type: "TypeError",
          message: "Cannot read properties of undefined (reading 'couponCode') [演练样本]",
          stack: `TypeError: Cannot read properties of undefined (reading 'couponCode')\n    at submitCheckoutOrder (https://app.example.com/src/features/checkout/CheckoutForm.tsx:84:29)\n    at onClickPayButton (https://app.example.com/src/features/checkout/PayButton.tsx:31:14)`,
          trace_id: traceId,
          page_url: `${window.location.origin}/drill-checkout`,
          tags: { drill: "true", module: "checkout", payment_method: "card" },
          extra: { cart_items: 2, currency: "USD", subtotal: 99 },
          breadcrumbs: [
            {
              ts: now - 6200,
              category: "navigation",
              level: "info",
              message: "/ -> /drill-pricing",
              data: { from: "/", to: "/drill-pricing" },
            },
            {
              ts: now - 3900,
              category: "ui.click",
              level: "info",
              message: 'button#select-pro-plan "立即升级专业版"',
            },
            {
              ts: now - 2900,
              category: "navigation",
              level: "info",
              message: "/drill-pricing -> /drill-checkout",
              data: { from: "/drill-pricing", to: "/drill-checkout" },
            },
            {
              ts: now - 1600,
              category: "ui.click",
              level: "info",
              message: 'button#apply-coupon "使用优惠码"',
            },
            {
              ts: now - 980,
              category: "http",
              level: "error",
              message: "POST /api/v1/checkout/coupon -> 502 (842ms)",
              data: {
                method: "POST",
                url: "/api/v1/checkout/coupon",
                status: 502,
                duration_ms: 842,
                trace_id: traceId,
              },
            },
            {
              ts: now - 420,
              category: "console",
              level: "warn",
              message: "[Checkout] coupon validation returned empty payload, falling back",
            },
            {
              ts: now - 90,
              category: "ui.click",
              level: "info",
              message: 'button#submit-order.btn-primary "确认支付 $99"',
            },
            {
              ts: now,
              category: "exception",
              level: "error",
              message: "TypeError: Cannot read properties of undefined (reading 'couponCode') [演练样本]",
            },
          ],
        },
      ];
    }

    const resp = await fetch(
      `/_e/b?k=${encodeURIComponent(project.public_key)}&p=${encodeURIComponent(project.slug)}&t=${kind === "crash" ? "urgent" : "normal"}`,
      {
        method: "POST",
        headers: { "Content-Type": "text/plain;charset=UTF-8" },
        body: JSON.stringify(payload),
      },
    );
    if (!resp.ok) {
      const txt = await resp.text().catch(() => "");
      throw new Error(txt || `HTTP ${resp.status}`);
    }

    // Also upload a 1:1 DOM Replay segment so the user can test exact visual replay immediately
    const t0 = now - 6200;
    const drillCss = `
      * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
      body { margin: 0; background: #f8fafc; color: #0f172a; padding: 36px 48px; }
      .shell { max-width: 820px; margin: 0 auto; background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 28px 32px; box-shadow: 0 2px 8px rgba(15,23,42,0.04); }
      .top { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #f1f5f9; padding-bottom: 16px; margin-bottom: 24px; }
      .brand { font-weight: 700; font-size: 16px; letter-spacing: -0.01em; }
      .step { font-size: 12px; color: #475569; background: #f1f5f9; padding: 4px 10px; border-radius: 999px; }
      .plans { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
      .plan { border: 1px solid #cbd5e1; border-radius: 10px; padding: 16px; transition: all 0.15s ease; }
      .plan.selected { border-color: #2563eb; background: #eff6ff; box-shadow: 0 0 0 2px rgba(37,99,235,0.15); }
      .price { font-size: 24px; font-weight: 700; margin: 6px 0; }
      .row { display: flex; gap: 10px; margin-bottom: 18px; }
      input { flex: 1; height: 38px; border: 1px solid #cbd5e1; border-radius: 8px; padding: 0 12px; font-size: 14px; }
      .btn { height: 38px; padding: 0 18px; border-radius: 8px; border: 1px solid #cbd5e1; background: #fff; font-weight: 600; font-size: 13px; }
      .btn-primary { width: 100%; height: 42px; background: #2563eb; color: #fff; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; }
      .err-banner { margin-bottom: 16px; padding: 12px 14px; border-radius: 8px; background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; font-size: 13px; }
    `;
    const replayPayload = {
      project: project.slug,
      env: "drill",
      release: "drill-1.0.0",
      session_id: sessionId,
      actor_id: actorId,
      seq: 0,
      start_ms: t0,
      end_ms: now,
      has_full_snapshot: 1,
      page_url: `${window.location.origin}/drill-pricing`,
      viewport_w: 1024,
      viewport_h: 640,
      events: [
        { t: 1, ts: t0, href: `${window.location.origin}/drill-pricing`, w: 1024, h: 640, title: "套餐升级结账" },
        {
          t: 2,
          ts: t0 + 10,
          href: `${window.location.origin}/drill-pricing`,
          w: 1024,
          h: 640,
          root: {
            id: 1,
            t: 0,
            ch: [
              { id: 2, t: 1, tag: "html" },
              {
                id: 3,
                t: 2,
                tag: "html",
                ch: [
                  {
                    id: 4,
                    t: 2,
                    tag: "head",
                    ch: [{ id: 5, t: 2, tag: "style", css: drillCss }],
                  },
                  {
                    id: 6,
                    t: 2,
                    tag: "body",
                    ch: [
                      {
                        id: 7,
                        t: 2,
                        tag: "div",
                        a: { class: "shell" },
                        ch: [
                          {
                            id: 8,
                            t: 2,
                            tag: "div",
                            a: { class: "top" },
                            ch: [
                              { id: 9, t: 2, tag: "div", a: { class: "brand" }, ch: [{ id: 10, t: 3, tx: `${project.name || project.slug} · 结算中心` }] },
                              { id: 11, t: 2, tag: "span", a: { class: "step" }, ch: [{ id: 12, t: 3, tx: "步骤 1 / 2 · 选择方案" }] },
                            ],
                          },
                          {
                            id: 13,
                            t: 2,
                            tag: "div",
                            a: { class: "plans" },
                            ch: [
                              {
                                id: 14,
                                t: 2,
                                tag: "div",
                                a: { class: "plan" },
                                ch: [
                                  { id: 15, t: 2, tag: "div", ch: [{ id: 16, t: 3, tx: "基础版 Starter" }] },
                                  { id: 17, t: 2, tag: "div", a: { class: "price" }, ch: [{ id: 18, t: 3, tx: "$19 / 月" }] },
                                ],
                              },
                              {
                                id: 19,
                                t: 2,
                                tag: "div",
                                a: { class: "plan", id: "select-pro-plan" },
                                ch: [
                                  { id: 20, t: 2, tag: "div", ch: [{ id: 21, t: 3, tx: "专业版 Pro（年付推荐）" }] },
                                  { id: 22, t: 2, tag: "div", a: { class: "price" }, ch: [{ id: 23, t: 3, tx: "$99 / 年" }] },
                                ],
                              },
                            ],
                          },
                          {
                            id: 24,
                            t: 2,
                            tag: "div",
                            a: { class: "row" },
                            ch: [
                              { id: 25, t: 2, tag: "input", a: { type: "text", placeholder: "输入优惠码或专属凭证 (敏感输入已开启脱敏)" } },
                              { id: 26, t: 2, tag: "button", a: { class: "btn", id: "apply-coupon" }, ch: [{ id: 27, t: 3, tx: "使用优惠码" }] },
                            ],
                          },
                          { id: 28, t: 2, tag: "div", a: { id: "alert-slot" } },
                          {
                            id: 29,
                            t: 2,
                            tag: "button",
                            a: { class: "btn-primary", id: "submit-order" },
                            ch: [{ id: 30, t: 3, tx: "确认支付 $99" }],
                          },
                        ],
                      },
                    ],
                  },
                ],
              },
            ],
          },
        },
        {
          t: 4,
          ts: t0 + 900,
          pts: [
            [180, 140, 0],
            [340, 175, 420],
            [560, 195, 920],
            [640, 198, 1300],
          ],
        },
        { t: 5, ts: now - 3900, x: 640, y: 198, id: 19, tag: "div", label: "专业版 Pro $99 / 年" },
        {
          t: 3,
          ts: now - 3850,
          at: [{ id: 19, k: "class", v: "plan selected" }],
          tx: [{ id: 12, v: "步骤 2 / 2 · 核对订单与支付" }],
        },
        { t: 1, ts: now - 2900, href: `${window.location.origin}/drill-checkout`, w: 1024, h: 640, title: "确认订单" },
        {
          t: 4,
          ts: now - 2600,
          pts: [
            [640, 198, 0],
            [450, 292, 380],
            [360, 304, 650],
          ],
        },
        { t: 5, ts: now - 1900, x: 360, y: 304, id: 25, tag: "input[text]" },
        { t: 7, ts: now - 1750, id: 25, v: "••••••••" },
        {
          t: 4,
          ts: now - 1700,
          pts: [
            [360, 304, 0],
            [795, 304, 100],
          ],
        },
        { t: 5, ts: now - 1600, x: 795, y: 304, id: 26, tag: "button", label: "使用优惠码" },
        ...(kind === "crash"
          ? [
              {
                t: 3,
                ts: now - 950,
                ad: [
                  {
                    p: 28,
                    nx: null,
                    n: {
                      id: 31,
                      t: 2,
                      tag: "div",
                      a: { class: "err-banner" },
                      ch: [{ id: 32, t: 3, tx: "优惠码网关响应超时 (HTTP 502 Bad Gateway · 842ms)，已回退原价计算。" }],
                    },
                  },
                ],
              },
              {
                t: 4,
                ts: now - 600,
                pts: [
                  [795, 304, 0],
                  [580, 395, 280],
                  [512, 420, 480],
                ],
              },
              { t: 5, ts: now - 90, x: 512, y: 420, id: 29, tag: "button", label: "确认支付 $99" },
            ]
          : []),
      ],
    };

    await fetch(
      `/_e/r?k=${encodeURIComponent(project.public_key)}&p=${encodeURIComponent(project.slug)}&s=${encodeURIComponent(sessionId)}&seq=0`,
      {
        method: "POST",
        headers: { "Content-Type": "text/plain;charset=UTF-8" },
        body: JSON.stringify(replayPayload),
      },
    ).catch(() => {});

    return { sessionId, actorId, traceId };
  },
};
