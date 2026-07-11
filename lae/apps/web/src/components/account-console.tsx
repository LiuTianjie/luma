"use client";

import {
  ArrowLeft,
  ArrowUpRight,
  Box,
  Check,
  Copy,
  CreditCard,
  ExternalLink,
  Gauge,
  HardDrive,
  KeyRound,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  LogIn,
  LogOut,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import {
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  BillingInterval,
  BillingOrder,
  BillingPlan,
  BillingPlanCode,
  BillingSubscription,
  BillingUsage,
  createCheckoutSession,
  createDeployToken,
  createSourceConnection,
  DeployToken,
  DeployTokenIssue,
  DeployTokenScope,
  getBillingSubscription,
  getBillingUsage,
  getPrincipal,
  LaeApiError,
  LaePrincipal,
  listBillingPlans,
  listDeployTokens,
  listSourceConnections,
  logout,
  newIdempotencyKey,
  revokeDeployToken,
  revokeSourceConnection,
  rotateDeployToken,
  SourceConnection,
} from "../lib/lae-api";

const DEFAULT_SCOPES: DeployTokenScope[] = [
  "apps:read",
  "apps:write",
  "sources:write",
  "analyses:write",
  "deployments:write",
  "logs:read",
];

const SCOPE_OPTIONS: Array<{
  scope: DeployTokenScope;
  label: string;
  detail: string;
}> = [
  { scope: "apps:read", label: "查看应用", detail: "读取应用与状态" },
  { scope: "apps:write", label: "管理应用", detail: "停止、重启与更新" },
  { scope: "sources:write", label: "管理来源", detail: "上传和 Git 连接" },
  { scope: "analyses:write", label: "执行诊断", detail: "生成部署计划" },
  { scope: "deployments:write", label: "执行部署", detail: "构建并发布服务" },
  { scope: "logs:read", label: "读取日志", detail: "查看运行输出" },
  {
    scope: "billing:checkout",
    label: "创建结算",
    detail: "允许 Agent 发起购买",
  },
];

const PLAN_ORDER: BillingPlanCode[] = ["lite", "pro", "ultra"];
const PLAN_COPY: Record<
  BillingPlanCode,
  { title: string; note: string; depth: string }
> = {
  lite: { title: "Lite", note: "轻量服务与个人实验", depth: "浅岸" },
  pro: { title: "Pro", note: "持续运行的产品服务", depth: "中流" },
  ultra: { title: "Ultra", note: "多服务与更高并发", depth: "深水" },
};

const COUNTER_COPY: Record<
  string,
  { label: string; icon: typeof Box; bytes?: boolean }
> = {
  applications: { label: "应用", icon: Box },
  servicesPerApp: { label: "单应用服务", icon: Layers3 },
  publicHttpRoutesPerApp: { label: "公网 HTTP", icon: Gauge },
  persistentVolumeBytes: { label: "持久存储", icon: HardDrive, bytes: true },
  concurrentAnalyses: { label: "并发诊断", icon: Gauge },
  concurrentBuilds: { label: "并发构建", icon: Gauge },
  concurrentDeployments: { label: "并发部署", icon: Gauge },
};

type SessionState = "loading" | "ready" | "guest" | "error";
type ResourceErrors = Partial<
  Record<"tokens" | "plans" | "subscription" | "usage" | "connections", string>
>;
type TokenAction = { kind: "rotate" | "revoke"; token: DeployToken };
type IssuedSecret = DeployTokenIssue & { action: "created" | "rotated" };

export function AccountConsole() {
  const reduceMotion = useReducedMotion();
  const [sessionState, setSessionState] = useState<SessionState>("loading");
  const [principal, setPrincipal] = useState<LaePrincipal | null>(null);
  const [tokens, setTokens] = useState<DeployToken[] | null>(null);
  const [plans, setPlans] = useState<BillingPlan[] | null>(null);
  const [subscription, setSubscription] =
    useState<BillingSubscription | null>(null);
  const [usage, setUsage] = useState<BillingUsage | null>(null);
  const [connections, setConnections] = useState<SourceConnection[] | null>(null);
  const [resourceErrors, setResourceErrors] = useState<ResourceErrors>({});
  const [refreshVersion, setRefreshVersion] = useState(0);

  const [createOpen, setCreateOpen] = useState(false);
  const [tokenName, setTokenName] = useState("");
  const [tokenExpiresAt, setTokenExpiresAt] = useState("");
  const [selectedScopes, setSelectedScopes] = useState<Set<DeployTokenScope>>(
    () => new Set(DEFAULT_SCOPES),
  );
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<TokenAction | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [issuedSecret, setIssuedSecret] = useState<IssuedSecret | null>(null);

  const [interval, setInterval] = useState<BillingInterval>("monthly");
  const [checkoutBusy, setCheckoutBusy] = useState<BillingPlanCode | null>(null);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const [checkoutOrder, setCheckoutOrder] = useState<BillingOrder | null>(null);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [connectionProvider, setConnectionProvider] = useState<SourceConnection["provider"]>("github");
  const [connectionName, setConnectionName] = useState("");
  const [connectionBaseUrl, setConnectionBaseUrl] = useState("https://github.com");
  const [connectionUsername, setConnectionUsername] = useState("");
  const [connectionSecret, setConnectionSecret] = useState("");
  const [connectionBusy, setConnectionBusy] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [pendingConnectionRevoke, setPendingConnectionRevoke] = useState<string | null>(null);
  const [logoutBusy, setLogoutBusy] = useState(false);

  const signOut = async () => {
    if (logoutBusy) return;
    setLogoutBusy(true);
    try {
      await logout();
      window.location.assign("/login");
    } catch (error) {
      setCheckoutError(accountErrorMessage(error));
      setLogoutBusy(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    async function load() {
      if (!principal) setSessionState("loading");
      try {
        const currentPrincipal = await getPrincipal(controller.signal);
        if (!active) return;
        setPrincipal(currentPrincipal);
        setSessionState("ready");

        const [tokenResult, planResult, subscriptionResult, usageResult, connectionResult] =
          await Promise.allSettled([
            listDeployTokens(controller.signal),
            listBillingPlans(controller.signal),
            getBillingSubscription(controller.signal),
            getBillingUsage(controller.signal),
            listSourceConnections(controller.signal),
          ]);
        if (!active) return;

        const nextErrors: ResourceErrors = {};
        if (tokenResult.status === "fulfilled") {
          setTokens(tokenResult.value.tokens);
        } else {
          nextErrors.tokens = accountErrorMessage(tokenResult.reason);
        }
        if (planResult.status === "fulfilled") {
          setPlans(planResult.value.plans);
        } else {
          nextErrors.plans = accountErrorMessage(planResult.reason);
        }
        if (subscriptionResult.status === "fulfilled") {
          setSubscription(subscriptionResult.value.subscription);
        } else {
          nextErrors.subscription = accountErrorMessage(subscriptionResult.reason);
        }
        if (usageResult.status === "fulfilled") {
          setUsage(usageResult.value);
        } else {
          nextErrors.usage = accountErrorMessage(usageResult.reason);
        }
        if (connectionResult.status === "fulfilled") {
          setConnections(connectionResult.value.connections);
        } else {
          nextErrors.connections = accountErrorMessage(connectionResult.reason);
        }
        setResourceErrors(nextErrors);
      } catch (error) {
        if (!active) return;
        setPrincipal(null);
        setSessionState(
          error instanceof LaeApiError && error.status === 401 ? "guest" : "error",
        );
      }
    }

    void load();
    return () => {
      active = false;
      controller.abort();
    };
  }, [refreshVersion]);

  const sortedPlans = useMemo(
    () =>
      [...(plans || [])].sort(
        (left, right) =>
          PLAN_ORDER.indexOf(left.code) - PLAN_ORDER.indexOf(right.code),
      ),
    [plans],
  );

  const refresh = () => setRefreshVersion((value) => value + 1);

  const toggleScope = (scope: DeployTokenScope) => {
    setSelectedScopes((current) => {
      const next = new Set(current);
      if (next.has(scope)) next.delete(scope);
      else next.add(scope);
      return next;
    });
  };

  const submitToken = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCreateError(null);
    if (selectedScopes.size === 0) {
      setCreateError("至少选择一项权限。");
      return;
    }
    let expiresAt: string | null = null;
    if (tokenExpiresAt) {
      const parsed = new Date(tokenExpiresAt);
      if (!Number.isFinite(parsed.getTime()) || parsed.getTime() <= Date.now()) {
        setCreateError("过期时间需要晚于现在。");
        return;
      }
      expiresAt = parsed.toISOString();
    }

    setCreateBusy(true);
    try {
      const issued = await createDeployToken({
        name: tokenName.trim() || "LAE CLI",
        scopes: SCOPE_OPTIONS.map(({ scope }) => scope).filter((scope) =>
          selectedScopes.has(scope),
        ),
        expiresAt,
      });
      setIssuedSecret({ ...issued, action: "created" });
      setTokenName("");
      setTokenExpiresAt("");
      setSelectedScopes(new Set(DEFAULT_SCOPES));
      setCreateOpen(false);
      refresh();
    } catch (error) {
      setCreateError(tokenErrorMessage(error));
    } finally {
      setCreateBusy(false);
    }
  };

  const submitTokenAction = async () => {
    if (!pendingAction) return;
    setActionBusy(true);
    setActionError(null);
    try {
      if (pendingAction.kind === "rotate") {
        const issued = await rotateDeployToken(pendingAction.token.id);
        setIssuedSecret({ ...issued, action: "rotated" });
      } else {
        await revokeDeployToken(pendingAction.token.id);
      }
      setPendingAction(null);
      refresh();
    } catch (error) {
      setActionError(tokenErrorMessage(error));
    } finally {
      setActionBusy(false);
    }
  };

  const beginCheckout = async (plan: BillingPlan) => {
    if (plan.code === "lite") return;
    setCheckoutBusy(plan.code);
    setCheckoutError(null);
    setCheckoutOrder(null);
    try {
      const result = await createCheckoutSession(
        { plan: plan.code, interval },
        newIdempotencyKey("account-checkout"),
      );
      setCheckoutOrder(result.order);
    } catch (error) {
      setCheckoutError(accountErrorMessage(error));
    } finally {
      setCheckoutBusy(null);
    }
  };

  const submitConnection = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (connectionBusy) return;
    setConnectionBusy("create");
    setConnectionError(null);
    try {
      const result = await createSourceConnection(
        {
          provider: connectionProvider,
          displayName: connectionName.trim(),
          baseUrl: connectionBaseUrl.trim(),
          username: connectionUsername.trim() || undefined,
          secret: connectionSecret,
        },
        newIdempotencyKey("source-connection"),
      );
      setConnections((current) => [result.connection, ...(current || [])]);
      setConnectionSecret("");
      setConnectionName("");
      setConnectionOpen(false);
    } catch (error) {
      setConnectionError(accountErrorMessage(error));
    } finally {
      setConnectionBusy(null);
    }
  };

  const removeConnection = async (connectionId: string) => {
    if (connectionBusy) return;
    if (pendingConnectionRevoke !== connectionId) {
      setPendingConnectionRevoke(connectionId);
      return;
    }
    setConnectionBusy(connectionId);
    setConnectionError(null);
    try {
      await revokeSourceConnection(
        connectionId,
        newIdempotencyKey("source-connection-revoke"),
      );
      setConnections((current) =>
        (current || []).map((connection) =>
          connection.id === connectionId
            ? { ...connection, revokedAt: new Date().toISOString() }
            : connection,
        ),
      );
      setPendingConnectionRevoke(null);
    } catch (error) {
      setConnectionError(accountErrorMessage(error));
    } finally {
      setConnectionBusy(null);
    }
  };

  return (
    <main className="account-shell">
      <AccountAmbient reduced={Boolean(reduceMotion)} />
      <AccountHeader principal={principal} logoutBusy={logoutBusy} onLogout={() => void signOut()} />

      {sessionState === "loading" && <AccountLoading />}
      {sessionState === "guest" && <AccountGate mode="guest" onRetry={refresh} />}
      {sessionState === "error" && <AccountGate mode="error" onRetry={refresh} />}

      {sessionState === "ready" && principal && (
        <div className="account-workspace">
          <motion.section
            className="account-intro"
            initial={reduceMotion ? false : { opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.62, ease: [0.22, 1, 0.36, 1] }}
          >
            <div>
              <p><span /> ACCOUNT</p>
              <h1>账户与用量</h1>
            </div>
            <dl>
              <div><dt>账户</dt><dd>{principal.user.email}</dd></div>
              <div><dt>当前套餐</dt><dd>{principal.entitlement.plan.toUpperCase()}</dd></div>
              <div><dt>认证</dt><dd>SESSION</dd></div>
            </dl>
          </motion.section>

          <div className="account-ledger">
            <section className="account-surface account-subscription" aria-labelledby="subscription-title">
              <SectionHeading
                index="01"
                title="订阅与用量"
                note="真实套餐快照"
                icon={CreditCard}
                id="subscription-title"
              />
              {resourceErrors.subscription ? (
                <InlineError message={resourceErrors.subscription} onRetry={refresh} />
              ) : subscription ? (
                <SubscriptionSummary subscription={subscription} />
              ) : (
                <PanelLoading />
              )}

              {resourceErrors.usage ? (
                <InlineError message={resourceErrors.usage} onRetry={refresh} />
              ) : usage ? (
                <UsageSoundings usage={usage} />
              ) : (
                <PanelLoading compact />
              )}
            </section>

            <section className="account-surface account-tokens" aria-labelledby="tokens-title">
              <div className="account-section-top">
                <SectionHeading
                  index="02"
                  title="Deploy tokens"
                  note="Agent 与 CLI 凭据"
                  icon={KeyRound}
                  id="tokens-title"
                />
                <button
                  className="account-primary-small"
                  type="button"
                  aria-expanded={createOpen}
                  aria-controls="token-creator"
                  onClick={() => {
                    setCreateOpen((value) => !value);
                    setCreateError(null);
                  }}
                >
                  {createOpen ? <X size={13} /> : <Plus size={13} />}
                  {createOpen ? "收起" : "新建 token"}
                </button>
              </div>

              {createOpen && (
                <TokenCreator
                  name={tokenName}
                  expiresAt={tokenExpiresAt}
                  scopes={selectedScopes}
                  busy={createBusy}
                  error={createError}
                  onName={setTokenName}
                  onExpiresAt={setTokenExpiresAt}
                  onScope={toggleScope}
                  onSubmit={submitToken}
                />
              )}

              {resourceErrors.tokens ? (
                <InlineError message={resourceErrors.tokens} onRetry={refresh} />
              ) : tokens ? (
                <TokenList
                  tokens={tokens}
                  onAction={(action) => {
                    setActionError(null);
                    setPendingAction(action);
                  }}
                />
              ) : (
                <PanelLoading />
              )}
            </section>
          </div>

          <section className="account-surface account-connections" aria-labelledby="connections-title">
            <div className="account-section-top">
              <SectionHeading
                index="03"
                title="Git 来源连接"
                note="GitHub、Gitea 与私有 Git"
                icon={LockKeyhole}
                id="connections-title"
              />
              <button
                className="account-primary-small"
                type="button"
                aria-expanded={connectionOpen}
                onClick={() => {
                  setConnectionOpen((value) => !value);
                  setConnectionError(null);
                }}
              >
                {connectionOpen ? <X size={13} /> : <Plus size={13} />}
                {connectionOpen ? "收起" : "添加连接"}
              </button>
            </div>

            {connectionOpen && (
              <form className="connection-account-form" onSubmit={submitConnection}>
                <label><span>提供方</span><select value={connectionProvider} onChange={(event) => {
                  const provider = event.target.value as SourceConnection["provider"];
                  setConnectionProvider(provider);
                  if (provider === "github") setConnectionBaseUrl("https://github.com");
                }}><option value="github">GitHub</option><option value="gitea">Gitea</option><option value="generic">Generic Git</option></select></label>
                <label><span>连接名称</span><input required maxLength={96} value={connectionName} onChange={(event) => setConnectionName(event.target.value)} placeholder="Production source" /></label>
                <label><span>Base URL</span><input required type="url" value={connectionBaseUrl} onChange={(event) => setConnectionBaseUrl(event.target.value)} placeholder="https://git.example.com" /></label>
                <label><span>用户名</span><input value={connectionUsername} onChange={(event) => setConnectionUsername(event.target.value)} autoComplete="username" placeholder="可选" /></label>
                <label className="connection-secret"><span>Access token / 密钥</span><input required type="password" value={connectionSecret} onChange={(event) => setConnectionSecret(event.target.value)} autoComplete="new-password" placeholder="只在提交时发送" /></label>
                <div className="connection-form-footer"><span>凭据由 LAE 加密保存，Builder 只获得任务级租约。</span><button type="submit" disabled={connectionBusy === "create"}>{connectionBusy === "create" ? <LoaderCircle className="is-spinning" size={13} /> : <ShieldCheck size={13} />}保存连接</button></div>
              </form>
            )}

            {resourceErrors.connections ? (
              <InlineError message={resourceErrors.connections} onRetry={refresh} />
            ) : connections ? (
              <div className="connection-account-list">
                {connections.length === 0 && <div className="connection-account-empty"><LockKeyhole size={15} />还没有 Git 来源连接</div>}
                {connections.map((connection) => {
                  const inactive = Boolean(connection.revokedAt);
                  return (
                    <article key={connection.id} className={inactive ? "is-inactive" : ""}>
                      <div><strong>{connection.displayName}</strong><span>{connection.provider.toUpperCase()} · {connection.allowedHost}</span></div>
                      <code>{connection.username || "token"} · v{connection.credentialVersion}</code>
                      <span className={`connection-state${inactive ? " is-off" : ""}`}>{inactive ? "已撤销" : "可用"}</span>
                      {!inactive && <button type="button" onClick={() => void removeConnection(connection.id)} disabled={connectionBusy === connection.id}>{connectionBusy === connection.id ? <LoaderCircle className="is-spinning" size={12} /> : <Trash2 size={12} />}{pendingConnectionRevoke === connection.id ? "确认撤销" : "撤销"}</button>}
                    </article>
                  );
                })}
              </div>
            ) : <PanelLoading compact />}
            {connectionError && <div className="account-inline-error" role="alert">{connectionError}</div>}
          </section>

          <section className="account-surface account-plans" aria-labelledby="plans-title">
            <div className="account-section-top plan-heading-row">
              <SectionHeading
                index="04"
                title="套餐与付费周期"
                note="月付或年付"
                icon={Gauge}
                id="plans-title"
              />
              <div className="interval-switch" role="group" aria-label="付费周期">
                <button
                  type="button"
                  aria-pressed={interval === "monthly"}
                  className={interval === "monthly" ? "is-active" : ""}
                  onClick={() => setInterval("monthly")}
                >月付</button>
                <button
                  type="button"
                  aria-pressed={interval === "yearly"}
                  className={interval === "yearly" ? "is-active" : ""}
                  onClick={() => setInterval("yearly")}
                >年付</button>
              </div>
            </div>

            {resourceErrors.plans ? (
              <InlineError message={resourceErrors.plans} onRetry={refresh} />
            ) : plans ? (
              <div className="plan-soundings">
                <div className="plan-waterline" aria-hidden="true" />
                {sortedPlans.map((plan, index) => (
                  <PlanSounding
                    key={`${plan.code}-${plan.version}`}
                    plan={plan}
                    interval={interval}
                    current={subscription?.plan.code === plan.code}
                    busy={checkoutBusy === plan.code}
                    index={index}
                    onCheckout={() => void beginCheckout(plan)}
                  />
                ))}
              </div>
            ) : (
              <PanelLoading />
            )}

            <div className="checkout-message" aria-live="polite">
              {checkoutError && <InlineError message={checkoutError} />}
              {checkoutOrder && <CheckoutHandoff order={checkoutOrder} />}
            </div>
          </section>

          <p className="account-footnote">
            Token 明文不会写入浏览器存储；关闭一次性凭据窗口后，LAE 不会再次展示它。
          </p>
        </div>
      )}

      {pendingAction && (
        <TokenActionDialog
          action={pendingAction}
          busy={actionBusy}
          error={actionError}
          onClose={() => !actionBusy && setPendingAction(null)}
          onConfirm={() => void submitTokenAction()}
        />
      )}
      {issuedSecret && (
        <IssuedSecretDialog
          issued={issuedSecret}
          onClose={() => setIssuedSecret(null)}
        />
      )}
    </main>
  );
}

function AccountHeader({
  principal,
  logoutBusy,
  onLogout,
}: {
  principal: LaePrincipal | null;
  logoutBusy: boolean;
  onLogout: () => void;
}) {
  return (
    <header className="account-topbar">
      <Link className="account-brand" href="/" aria-label="返回 Luma Application Engine">
        <span className="brand-mark" aria-hidden="true"><span /><span /><span /></span>
        <span><strong>LAE</strong><small>Luma Application Engine</small></span>
      </Link>
      <Link className="account-return" href="/">
        <ArrowLeft size={14} /> 返回部署台
      </Link>
      <div className="account-identity">
        <span>{principal?.entitlement.plan.toUpperCase() || "ACCOUNT"}</span>
        <strong>{principal?.user.email || "账户中心"}</strong>
        {principal && (
          <button type="button" onClick={onLogout} disabled={logoutBusy} aria-label="退出登录">
            {logoutBusy ? <LoaderCircle className="is-spinning" size={13} /> : <LogOut size={13} />}
            退出
          </button>
        )}
      </div>
    </header>
  );
}

function AccountAmbient({ reduced }: { reduced: boolean }) {
  void reduced;
  return null;
}

function AccountLoading() {
  return (
    <div className="account-loading" role="status" aria-live="polite">
      <span><LoaderCircle size={18} /> 正在读取账户信息</span>
      <i /><i /><i />
    </div>
  );
}

function AccountGate({ mode, onRetry }: { mode: "guest" | "error"; onRetry: () => void }) {
  return (
    <section className="account-gate" aria-labelledby="account-gate-title">
      <div className="account-gate-rings" aria-hidden="true"><span /><span /><span /></div>
      <p>{mode === "guest" ? "SESSION REQUIRED" : "CONNECTION INTERRUPTED"}</p>
      <h1 id="account-gate-title">
        {mode === "guest" ? "需要登录" : "暂时无法读取账户"}
      </h1>
      <span>
        {mode === "guest"
          ? "Deploy token、订阅与用量只对已建立 Session 的账户开放。"
          : "LAE API 当前不可用。页面没有使用缓存数据或模拟账户状态。"}
      </span>
      {mode === "guest" ? (
        <Link className="account-gate-action" href="/login">邮件登录 <LogIn size={15} /></Link>
      ) : (
        <button className="account-gate-action" type="button" onClick={onRetry}>重新连接 <RefreshCw size={15} /></button>
      )}
      <Link className="account-gate-back" href="/"><ArrowLeft size={13} /> 返回部署台</Link>
    </section>
  );
}

function SectionHeading({
  index,
  title,
  note,
  icon: Icon,
  id,
}: {
  index: string;
  title: string;
  note: string;
  icon: typeof KeyRound;
  id: string;
}) {
  return (
    <div className="account-section-heading">
      <span>{index}</span>
      <div><h2 id={id}>{title}</h2><p><Icon size={11} /> {note}</p></div>
    </div>
  );
}

function SubscriptionSummary({ subscription }: { subscription: BillingSubscription }) {
  return (
    <div className="subscription-current">
      <div className="subscription-orbit" aria-hidden="true"><span /><span /></div>
      <div>
        <p>CURRENT PLAN</p>
        <strong>{PLAN_COPY[subscription.plan.code].title}</strong>
        <span>{humanStatus(subscription.status)} · {subscription.interval === "yearly" ? "年付" : "月付"}</span>
      </div>
      <dl>
        <div><dt>提供方</dt><dd>{subscription.provider}</dd></div>
        <div><dt>周期结束</dt><dd>{formatDate(subscription.currentPeriodEnd)}</dd></div>
        <div><dt>版本</dt><dd>v{subscription.plan.version}</dd></div>
      </dl>
    </div>
  );
}

function UsageSoundings({ usage }: { usage: BillingUsage }) {
  const entries = Object.entries(usage.counters);
  const usageNotice = usage.ledger.connected
    ? usage.notice
    : "用量账本尚未接通；以下数值为占位快照，不参与计费或额度扣减。";
  return (
    <div className="usage-soundings">
      <div className="usage-note">
        <ShieldCheck size={13} />
        <span>{usageNotice}</span>
      </div>
      <div className="usage-grid">
        {entries.map(([key, counter]) => {
          const meta = COUNTER_COPY[key] || { label: key, icon: Gauge };
          const Icon = meta.icon;
          const ratio = counter.limit && counter.limit > 0
            ? Math.min(100, Math.max(0, (counter.used / counter.limit) * 100))
            : 0;
          return (
            <article key={key}>
              <div><Icon size={12} /><span>{meta.label}</span></div>
              <strong>{formatCounter(counter.used, Boolean(meta.bytes))}<small> / {formatCounter(counter.limit, Boolean(meta.bytes))}</small></strong>
              <span className="usage-track" aria-hidden="true"><i style={{ width: `${ratio}%` }} /></span>
            </article>
          );
        })}
      </div>
      <small className="usage-as-of">快照 {formatDate(usage.asOf, true)}</small>
    </div>
  );
}

function TokenCreator({
  name,
  expiresAt,
  scopes,
  busy,
  error,
  onName,
  onExpiresAt,
  onScope,
  onSubmit,
}: {
  name: string;
  expiresAt: string;
  scopes: Set<DeployTokenScope>;
  busy: boolean;
  error: string | null;
  onName: (value: string) => void;
  onExpiresAt: (value: string) => void;
  onScope: (scope: DeployTokenScope) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  return (
    <form id="token-creator" className="token-creator" onSubmit={onSubmit}>
      <div className="token-form-row">
        <label>
          <span>名称（可选）</span>
          <input
            value={name}
            onChange={(event) => onName(event.target.value)}
            maxLength={120}
            autoComplete="off"
            placeholder="默认：LAE CLI"
          />
        </label>
        <label>
          <span>过期时间（可选）</span>
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(event) => onExpiresAt(event.target.value)}
          />
        </label>
      </div>
      <fieldset>
        <legend>权限范围</legend>
        <div className="scope-grid">
          {SCOPE_OPTIONS.map((option) => (
            <label key={option.scope} className={scopes.has(option.scope) ? "is-checked" : ""}>
              <input
                type="checkbox"
                checked={scopes.has(option.scope)}
                onChange={() => onScope(option.scope)}
              />
              <span aria-hidden="true">{scopes.has(option.scope) && <Check size={11} />}</span>
              <div><strong>{option.label}</strong><small>{option.detail}</small></div>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="token-create-footer">
        <p><LockKeyhole size={12} /> 明文只在创建完成后出现一次</p>
        <span className="form-error" role="alert">{error}</span>
        <button type="submit" disabled={busy || scopes.size === 0}>
          {busy ? <LoaderCircle className="is-spinning" size={13} /> : <KeyRound size={13} />}
          {busy ? "正在签发" : "签发 token"}
        </button>
      </div>
    </form>
  );
}

function TokenList({ tokens, onAction }: { tokens: DeployToken[]; onAction: (action: TokenAction) => void }) {
  if (tokens.length === 0) {
    return <div className="token-empty"><KeyRound size={17} /><span>还没有 deploy token</span></div>;
  }
  return (
    <div className="token-list">
      {tokens.map((token) => {
        const inactive = Boolean(token.revokedAt) || isExpired(token.expiresAt);
        return (
          <article key={token.id} className={inactive ? "is-inactive" : ""}>
            <div className="token-mark"><KeyRound size={15} /></div>
            <div className="token-main">
              <div>
                <strong>{token.name}</strong>
                {token.isDefault && <span className="token-default">DEFAULT</span>}
                <span className={`token-state${inactive ? " is-off" : ""}`}>{token.revokedAt ? "已撤销" : isExpired(token.expiresAt) ? "已过期" : "有效"}</span>
              </div>
              <code>{token.prefix}••••••••</code>
              <p>{token.scopes.join(" · ")}</p>
            </div>
            <dl>
              <div><dt>最近使用</dt><dd>{formatDate(token.lastUsedAt, true)}</dd></div>
              <div><dt>过期</dt><dd>{token.expiresAt ? formatDate(token.expiresAt) : "永不过期"}</dd></div>
            </dl>
            <div className="token-actions">
              <button
                type="button"
                disabled={inactive}
                onClick={() => onAction({ kind: "rotate", token })}
                aria-label={`轮换 ${token.name}`}
              ><RefreshCw size={12} /> 轮换</button>
              <button
                type="button"
                disabled={inactive || token.isDefault}
                title={token.isDefault ? "默认 deploy token 不可撤销，请使用轮换" : "撤销 token"}
                onClick={() => onAction({ kind: "revoke", token })}
                aria-label={token.isDefault ? `${token.name} 是默认 token，只能轮换` : `撤销 ${token.name}`}
              ><Trash2 size={12} /> {token.isDefault ? "默认项受保护" : "撤销"}</button>
            </div>
          </article>
        );
      })}
    </div>
  );
}

function PlanSounding({
  plan,
  interval,
  current,
  busy,
  index,
  onCheckout,
}: {
  plan: BillingPlan;
  interval: BillingInterval;
  current: boolean;
  busy: boolean;
  index: number;
  onCheckout: () => void;
}) {
  const reduceMotion = useReducedMotion();
  const copy = PLAN_COPY[plan.code];
  const price = plan.pricing[interval];
  const purchasable = plan.code !== "lite" && price !== null && !current;
  return (
    <motion.article
      className={`plan-sounding plan-${plan.code}${current ? " is-current" : ""}`}
      initial={reduceMotion ? false : { opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.44, delay: index * 0.06, ease: [0.4, 0, 0.2, 1] }}
    >
      <div className="depth-marker" aria-hidden="true"><i /><span>{copy.depth}</span></div>
      <p>{plan.pricing.mode === "mock-development-only" ? "MOCK PLAN" : "PLAN"} · V{plan.version}</p>
      <h3>{copy.title}</h3>
      <span className="plan-note">{copy.note}</span>
      <strong className="plan-price">{price ? formatPrice(price.amountMinor, price.currency) : plan.code === "lite" ? "免费" : "未配置"}<small>{price ? ` / ${interval === "monthly" ? "月" : "年"}` : ""}</small></strong>
      <dl>
        {Object.entries(plan.limits).slice(0, 4).map(([key, value]) => (
          <div key={key}><dt>{COUNTER_COPY[key]?.label || key}</dt><dd>{formatCounter(value, Boolean(COUNTER_COPY[key]?.bytes))}</dd></div>
        ))}
      </dl>
      {current ? (
        <span className="plan-current-label"><Check size={12} /> 当前套餐</span>
      ) : plan.code === "lite" ? (
        <span className="plan-current-label is-muted">免费基础层</span>
      ) : (
        <button type="button" disabled={!purchasable || busy} onClick={onCheckout}>
          {busy ? <LoaderCircle className="is-spinning" size={13} /> : <ArrowUpRight size={13} />}
          {busy ? "正在创建" : plan.pricing.commerciallyApproved ? "前往结算" : "打开模拟结算"}
        </button>
      )}
    </motion.article>
  );
}

function CheckoutHandoff({ order }: { order: BillingOrder }) {
  const safeUrl = safeCheckoutUrl(order.checkout?.url || null);
  return (
    <div className="checkout-handoff">
      <div className="checkout-handoff-mark"><CreditCard size={17} /></div>
      <div>
        <p>{order.provider === "mock" ? "MOCK CHECKOUT READY" : "CHECKOUT READY"}</p>
        <strong>{PLAN_COPY[order.plan.code].title} · {formatPrice(order.price.amountMinor, order.price.currency)}</strong>
        <span>订单 {order.id} · {order.interval === "yearly" ? "年付" : "月付"} · {order.status}</span>
        {safeUrl && <code>{safeUrl}</code>}
      </div>
      {safeUrl ? (
        <a href={safeUrl} target="_blank" rel="noopener noreferrer">
          打开{order.provider === "mock" ? "模拟" : ""}结算 <ExternalLink size={13} />
        </a>
      ) : (
        <span className="checkout-unavailable">结算地址未通过安全校验</span>
      )}
    </div>
  );
}

function TokenActionDialog({
  action,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  action: TokenAction;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  const rotate = action.kind === "rotate";
  return (
    <dialog
      ref={ref}
      className="account-dialog"
      aria-labelledby="token-action-title"
      aria-describedby="token-action-description"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <button className="dialog-close" type="button" disabled={busy} onClick={onClose} aria-label="关闭"><X size={15} /></button>
      <span className={`dialog-mark${rotate ? "" : " is-danger"}`}>{rotate ? <RefreshCw size={19} /> : <Trash2 size={19} />}</span>
      <p>{rotate ? "ROTATE CREDENTIAL" : "REVOKE CREDENTIAL"}</p>
      <h2 id="token-action-title">{rotate ? "轮换这个 Deploy token？" : "撤销这个 Deploy token？"}</h2>
      <div id="token-action-description" className="dialog-description">
        <strong>{action.token.name}</strong><code>{action.token.prefix}••••••••</code>
        <span>{rotate ? "旧 token 会立即失效，新明文仅展示一次。正在使用它的 Agent 需要同步更新。" : "撤销后无法恢复，使用它的 CLI 与 Agent 将立即失去访问权限。"}</span>
      </div>
      <div className="dialog-error" role="alert">{error}</div>
      <div className="dialog-actions">
        <button type="button" disabled={busy} onClick={onClose}>取消</button>
        <button type="button" disabled={busy} onClick={onConfirm} autoFocus className={rotate ? "" : "is-danger"}>
          {busy && <LoaderCircle className="is-spinning" size={13} />}
          {busy ? "正在处理" : rotate ? "确认轮换" : "确认撤销"}
        </button>
      </div>
    </dialog>
  );
}

function IssuedSecretDialog({ issued, onClose }: { issued: IssuedSecret; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(issued.plaintext);
      setCopied(true);
      setCopyError(null);
    } catch {
      setCopyError("浏览器未允许复制，请在关闭前手动保存。 ");
    }
  };
  return (
    <dialog
      ref={ref}
      className="account-dialog issued-dialog"
      aria-labelledby="issued-title"
      aria-describedby="issued-description"
      onCancel={(event) => { event.preventDefault(); onClose(); }}
    >
      <span className="dialog-mark"><KeyRound size={19} /></span>
      <p>PLAINTEXT · ONCE ONLY</p>
      <h2 id="issued-title">{issued.action === "rotated" ? "新 token 已轮换" : "新 token 已签发"}</h2>
      <p id="issued-description" className="issued-warning">离开这个窗口后，LAE 不会再次展示明文。请现在交给密码管理器或目标 Agent。</p>
      <div className="issued-token">
        <span>{issued.token.name}</span>
        <code>{issued.plaintext}</code>
      </div>
      <div className="dialog-error" role="alert">{copyError}</div>
      <div className="dialog-actions issued-actions">
        <button type="button" onClick={onClose}>我已安全保存</button>
        <button type="button" onClick={() => void copy()} autoFocus>
          {copied ? <Check size={13} /> : <Copy size={13} />}{copied ? "已复制" : "复制 token"}
        </button>
      </div>
    </dialog>
  );
}

function InlineError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="account-inline-error" role="alert">
      <span>{message}</span>
      {onRetry && <button type="button" onClick={onRetry}><RefreshCw size={11} /> 重试</button>}
    </div>
  );
}

function PanelLoading({ compact = false }: { compact?: boolean }) {
  return <div className={`panel-loading${compact ? " is-compact" : ""}`} aria-hidden="true"><i /><i /><i /></div>;
}

function accountErrorMessage(error: unknown) {
  if (error instanceof LaeApiError) {
    if (error.status === 401) return "Session 已失效，请重新登录。";
    if (error.code === "LAE_BILLING_UNAVAILABLE") return "订阅服务暂时不可用，请稍后重试。";
    return error.message;
  }
  return "账户数据暂时无法读取，请稍后重试。";
}

function tokenErrorMessage(error: unknown) {
  if (error instanceof LaeApiError) {
    if (error.code === "LAE_DEFAULT_DEPLOY_TOKEN_PROTECTED") {
      return "默认 deploy token 不可撤销，请改用轮换。";
    }
    if (error.code === "LAE_DEPLOY_TOKEN_LIMIT") {
      return "有效 deploy token 已达到上限，请先撤销不再使用的 token。";
    }
    if (error.code === "LAE_DEPLOY_TOKEN_INACTIVE") {
      return "这枚 token 已失效，不能再次轮换。";
    }
    return error.message;
  }
  return "Token 操作未能完成，请稍后重试。";
}

function humanStatus(status: string) {
  const labels: Record<string, string> = {
    active: "生效中",
    trialing: "试用中",
    past_due: "待处理",
    canceled: "已取消",
  };
  return labels[status] || status;
}

function formatDate(value: string | null, withTime = false) {
  if (!value) return "尚无记录";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function formatPrice(amountMinor: number, currency: string) {
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(amountMinor / 100);
  } catch {
    return `${currency} ${(amountMinor / 100).toFixed(2)}`;
  }
}

function formatCounter(value: number | null, bytes: boolean) {
  if (value === null) return "—";
  if (!bytes) return new Intl.NumberFormat("zh-CN").format(value);
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let current = value;
  let unit = -1;
  while (current >= 1024 && unit < units.length - 1) {
    current /= 1024;
    unit += 1;
  }
  return `${current >= 10 ? current.toFixed(0) : current.toFixed(1)} ${units[unit]}`;
}

function isExpired(value: string | null) {
  if (!value) return false;
  const time = new Date(value).getTime();
  return Number.isFinite(time) && time <= Date.now();
}

function safeCheckoutUrl(value: string | null) {
  if (!value) return null;
  try {
    const parsed = new URL(value, window.location.origin);
    const localHttp = parsed.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(parsed.hostname);
    if ((parsed.protocol !== "https:" && !localHttp) || parsed.username || parsed.password) return null;
    return parsed.toString();
  } catch {
    return null;
  }
}
