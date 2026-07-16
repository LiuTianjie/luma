"use client";

import {
  ArrowLeft,
  ArrowRight,
  Check,
  CircleAlert,
  FlaskConical,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  approveMockBillingOrder,
  BillingOrder,
  BillingSubscription,
  getBillingOrder,
  getBillingSubscription,
  getPrincipal,
  LaeApiError,
  newIdempotencyKey,
} from "../lib/lae-api";

type ScreenState = "loading" | "ready" | "guest" | "missing" | "error";

const PLAN_NAMES = { pro: "Pro", ultra: "Ultra" } as const;

export function MockCheckoutConsole({ orderId }: { orderId: string }) {
  const reducedMotion = useReducedMotion();
  const approvalKey = useRef<string | null>(null);
  const [screenState, setScreenState] = useState<ScreenState>("loading");
  const [order, setOrder] = useState<BillingOrder | null>(null);
  const [subscription, setSubscription] = useState<BillingSubscription | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    async function load() {
      setScreenState("loading");
      setError(null);
      try {
        await getPrincipal(controller.signal);
        const result = await getBillingOrder(orderId, controller.signal);
        if (!active) return;
        setOrder(result.order);
        if (result.order.status === "paid") {
          try {
            const current = await getBillingSubscription(controller.signal);
            if (active) setSubscription(current.subscription);
          } catch {
            // The accepted order is still authoritative if this secondary read fails.
          }
        }
        if (active) setScreenState("ready");
      } catch (cause) {
        if (!active) return;
        if (cause instanceof LaeApiError && cause.status === 401) {
          setScreenState("guest");
        } else if (cause instanceof LaeApiError && cause.status === 404) {
          setScreenState("missing");
        } else {
          setScreenState("error");
        }
      }
    }

    void load();
    return () => {
      active = false;
      controller.abort();
    };
  }, [orderId, refreshVersion]);

  const approve = async () => {
    if (!order || order.status !== "pending" || order.provider !== "mock") return;
    approvalKey.current ||= newIdempotencyKey("mock-approve");
    setBusy(true);
    setError(null);
    try {
      const result = await approveMockBillingOrder(order.id, approvalKey.current);
      if (!result.accepted || result.order.status !== "paid") {
        throw new LaeApiError({ code: "LAE_PAYMENT_EVENT_REJECTED", status: 409 });
      }
      setOrder((current) =>
        current
          ? {
              ...current,
              status: result.order.status,
              paidSubscriptionId: result.subscriptionId,
              checkout: null,
            }
          : current,
      );
      const [freshOrder, freshSubscription] = await Promise.all([
        getBillingOrder(order.id),
        getBillingSubscription(),
      ]);
      setOrder(freshOrder.order);
      setSubscription(freshSubscription.subscription);
    } catch (cause) {
      setError(checkoutErrorMessage(cause));
      if (cause instanceof LaeApiError && cause.status === 409) {
        try {
          const fresh = await getBillingOrder(order.id);
          setOrder(fresh.order);
        } catch {
          // Keep the last server-confirmed order visible.
        }
      }
    } finally {
      setBusy(false);
    }
  };

  const retry = () => setRefreshVersion((value) => value + 1);

  return (
    <main className="mock-checkout-shell">
      <CheckoutAmbient reduced={Boolean(reducedMotion)} />
      <header className="mock-checkout-topbar">
        <Link className="account-brand" href="/" aria-label="返回 Luma Application Engine">
          <span className="brand-mark" aria-hidden="true"><span /><span /><span /></span>
          <span><strong>LAE</strong><small>Luma Application Engine</small></span>
        </Link>
        <span className="mock-checkout-environment"><FlaskConical size={12} /> DEVELOPMENT · MOCK</span>
        <Link className="mock-checkout-account" href="/account">账户中心 <ArrowRight size={13} /></Link>
      </header>

      {screenState === "loading" && <CheckoutLoading />}
      {screenState !== "loading" && screenState !== "ready" && (
        <CheckoutGate state={screenState} onRetry={retry} />
      )}
      {screenState === "ready" && order && (
        <motion.div
          className="mock-checkout-stage"
          initial={reducedMotion ? false : { opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.58, ease: [0.22, 1, 0.36, 1] }}
        >
          <section className="mock-checkout-intro" aria-labelledby="checkout-title">
            <p><span /> DEVELOPMENT CHECKOUT</p>
            <h1 id="checkout-title">确认模拟付款</h1>
            <div className="mock-checkout-assurance">
              <ShieldCheck size={14} />
              <span>不会连接微信、支付宝或银行卡，也不会产生真实扣款。</span>
            </div>
          </section>

          <section className="mock-checkout-ledger" aria-label="模拟结算订单">
            <div className="mock-checkout-axis" aria-hidden="true">
              <span>01</span><i /><span>02</span><i /><span>03</span>
            </div>
            <article className="mock-checkout-receipt">
              <div className="mock-receipt-head">
                <div>
                  <p>ORDER / {shortOrderId(order.id)}</p>
                  <span>{formatDate(order.createdAt)}</span>
                </div>
                <OrderStatus status={order.status} />
              </div>

              <div className="mock-receipt-plan">
                <span>{order.interval === "yearly" ? "ANNUAL CURRENT" : "MONTHLY CURRENT"}</span>
                <strong>{PLAN_NAMES[order.plan.code]}</strong>
                <p>{formatPrice(order.price.amountMinor, order.price.currency)}<small> / {order.interval === "yearly" ? "年" : "月"}</small></p>
              </div>

              <dl className="mock-receipt-facts">
                <div><dt>订单编号</dt><dd>{order.id}</dd></div>
                <div><dt>价格快照</dt><dd>{order.price.pricingVersion}</dd></div>
                <div><dt>结算提供方</dt><dd>{order.provider.toUpperCase()}</dd></div>
                <div><dt>确认截止</dt><dd>{formatDate(order.checkout?.expiresAt || null)}</dd></div>
              </dl>

              <div className="mock-receipt-boundary">
                <LockKeyhole size={14} />
                <p><strong>订单事实由服务端锁定</strong><span>此页面只提交“批准”动作；套餐、金额、币种与商户信息不会从浏览器回传。</span></p>
              </div>
            </article>

            <aside className="mock-checkout-action" aria-live="polite">
              <span className="mock-stamp">MOCK<i>NO CHARGE</i></span>
              {order.status === "pending" ? (
                <>
                  <p>APPROVAL REQUIRED</p>
                  <h2>准备激活<br />{PLAN_NAMES[order.plan.code]}</h2>
                  <span className="mock-action-copy">
                    点击后，LAE 仅在开发环境的 mock provider 中记录一条 paid 事件，并以服务端结果更新订阅。
                  </span>
                  <button type="button" disabled={busy} onClick={() => void approve()}>
                    {busy ? <LoaderCircle className="is-spinning" size={15} /> : <Check size={15} />}
                    <span>{busy ? "正在确认服务端状态" : "确认模拟付款"}</span>
                    {!busy && <ArrowRight size={14} />}
                  </button>
                  {error && <div className="mock-action-error"><CircleAlert size={13} /> {error}</div>}
                </>
              ) : order.status === "paid" ? (
                <CheckoutSuccess order={order} subscription={subscription} />
              ) : (
                <CheckoutTerminal order={order} />
              )}
            </aside>
          </section>

          <footer className="mock-checkout-footer">
            <Link href="/account"><ArrowLeft size={12} /> 返回套餐与账户</Link>
            <span>LAE MOCK PAYMENT · SERVER-CONFIRMED STATE ONLY</span>
          </footer>
        </motion.div>
      )}
    </main>
  );
}

function CheckoutAmbient({ reduced }: { reduced: boolean }) {
  void reduced;
  return null;
}

function CheckoutLoading() {
  return (
    <div className="mock-checkout-loading" role="status">
      <LoaderCircle className="is-spinning" size={18} />
      <span>正在核对服务端订单</span>
    </div>
  );
}

function CheckoutGate({ state, onRetry }: { state: Exclude<ScreenState, "loading" | "ready">; onRetry: () => void }) {
  const guest = state === "guest";
  const missing = state === "missing";
  return (
    <section className="mock-checkout-gate">
      <p>{guest ? "SESSION REQUIRED" : missing ? "ORDER NOT FOUND" : "CONNECTION INTERRUPTED"}</p>
      <h1>{guest ? "需要先登录" : missing ? "找不到这笔订单" : "暂时无法读取订单"}</h1>
      <span>{guest ? "模拟结算只能由当前账户的浏览器 Session 确认，Deploy token 无法批准付款。" : missing ? "订单不存在，或它不属于当前租户。LAE 不会泄露其他账户的订单状态。" : "页面没有使用缓存或伪造数据，请重新连接服务端。"}</span>
      {guest ? (
        <Link href="/login">前往邮件登录 <ArrowRight size={14} /></Link>
      ) : (
        <button type="button" onClick={onRetry}>重新读取 <RefreshCw size={14} /></button>
      )}
      <Link className="mock-gate-back" href="/account"><ArrowLeft size={12} /> 返回账户中心</Link>
    </section>
  );
}

function OrderStatus({ status }: { status: BillingOrder["status"] }) {
  const label = {
    pending: "待确认",
    paid: "已生效",
    failed: "失败",
    expired: "已过期",
    canceled: "已取消",
  }[status];
  return <span className={`mock-order-status is-${status}`}><i /> {label}</span>;
}

function CheckoutSuccess({ order, subscription }: { order: BillingOrder; subscription: BillingSubscription | null }) {
  return (
    <div className="mock-checkout-success">
      <span><Check size={22} /></span>
      <p>SUBSCRIPTION ACTIVE</p>
      <h2>模拟付款<br />已确认</h2>
      <dl>
        <div><dt>当前套餐</dt><dd>{subscription ? subscription.plan.code.toUpperCase() : PLAN_NAMES[order.plan.code]}</dd></div>
        <div><dt>订阅状态</dt><dd>{subscription?.status || "active"}</dd></div>
        <div><dt>订阅编号</dt><dd>{subscription?.id || order.paidSubscriptionId || "已生成"}</dd></div>
      </dl>
      <Link href="/account">返回账户中心 <ArrowRight size={14} /></Link>
    </div>
  );
}

function CheckoutTerminal({ order }: { order: BillingOrder }) {
  const copy = order.status === "expired" ? "确认窗口已经结束，请返回账户中心重新创建订单。" : "这笔订单已结束，无法再次批准。";
  return (
    <div className="mock-checkout-terminal">
      <CircleAlert size={18} />
      <p>ORDER {order.status.toUpperCase()}</p>
      <h2>无法继续<br />模拟结算</h2>
      <span>{copy}</span>
      <Link href="/account">重新选择套餐 <ArrowRight size={13} /></Link>
    </div>
  );
}

function shortOrderId(value: string) {
  return value.length > 17 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function formatPrice(amountMinor: number, currency: string) {
  try {
    return new Intl.NumberFormat("zh-CN", { style: "currency", currency }).format(amountMinor / 100);
  } catch {
    return `${currency} ${(amountMinor / 100).toFixed(2)}`;
  }
}

function formatDate(value: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function checkoutErrorMessage(error: unknown) {
  if (!(error instanceof LaeApiError)) return "确认失败，请保留页面后重试。";
  if (error.code === "LAE_CSRF_FAILED") return "登录校验已失效，请重新登录后再确认。";
  if (error.code === "LAE_MOCK_CHECKOUT_ORDER_TERMINAL") return "订单状态已经变化，正在重新读取。";
  if (error.status === 404) return "订单不存在，或不属于当前账户。";
  if (error.retryable) return "服务暂时不可用；重试会沿用同一个幂等请求，不会重复生效。";
  return `确认未完成（${error.code}）。`;
}
