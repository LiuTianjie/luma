"use client";

import {
  ArrowRight,
  Check,
  Copy,
  KeyRound,
  Mail,
  ShieldCheck,
} from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";

type Mode = "register" | "login";
type Step = "request" | "verify" | "magic" | "complete";
type DeliveryMode = "loading" | "email" | "unavailable";
type DeliveryCapabilities = {
  mode: DeliveryMode;
};

const API_ROOT = (process.env.NEXT_PUBLIC_LAE_API_URL || "/v1").replace(/\/$/, "");

export function AuthPortal() {
  const reduceMotion = useReducedMotion();
  const [mode, setMode] = useState<Mode>("register");
  const [step, setStep] = useState<Step>("request");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deployToken, setDeployToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [deliveryMode, setDeliveryMode] = useState<DeliveryMode>("loading");

  useEffect(() => {
    let active = true;
    void readAuthConfig()
      .then((capabilities) => {
        if (active) {
          setDeliveryMode(capabilities.mode);
        }
      })
      .catch(() => {
        // A rolling deployment may briefly pair the new Web with the previous
        // API. Preserve the established email flow until config is readable.
        if (active) setDeliveryMode("email");
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const rawFragment = window.location.hash.slice(1);
    if (!rawFragment) return;
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${window.location.search}`,
    );
    const params = new URLSearchParams(rawFragment);
    const fragmentEmail = params.get("email") || "";
    const magicToken = params.get("magicToken") || "";
    const purpose = params.get("purpose");
    if (
      !fragmentEmail ||
      fragmentEmail.length > 320 ||
      !magicToken.startsWith("lae_em_") ||
      magicToken.length > 128 ||
      (purpose !== "register" && purpose !== "login")
    ) {
      setError("一次性链接无效或已经过期，请重新获取。");
      return;
    }

    let active = true;
    const nextMode: Mode = purpose;
    setMode(nextMode);
    setEmail(fragmentEmail);
    setStep("magic");
    setLoading(true);
    void request(
      nextMode === "register" ? "/auth/email/verify" : "/auth/login/verify",
      { email: fragmentEmail, magicToken },
    )
      .then((response) => {
        if (!active) return;
        setDeployToken(
          typeof response.defaultDeployToken === "string"
            ? response.defaultDeployToken
            : null,
        );
        setStep("complete");
      })
      .catch(() => {
        if (!active) return;
        setStep("request");
        setError("一次性链接无效或已经过期，请重新获取。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const switchMode = (next: Mode) => {
    setMode(next);
    setStep("request");
    setCode("");
    setError(null);
    setDeployToken(null);
  };

  const requestChallenge = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await request(mode === "register" ? "/auth/register" : "/auth/login/request", {
        email,
      });
      setStep("verify");
    } catch {
      setError("邮件服务暂时不可用，请稍后重试。");
    } finally {
      setLoading(false);
    }
  };

  const verifyChallenge = async (event: FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const response = await request(
        mode === "register" ? "/auth/email/verify" : "/auth/login/verify",
        { email, code },
      );
      setDeployToken(
        typeof response.defaultDeployToken === "string"
          ? response.defaultDeployToken
          : null,
      );
      setStep("complete");
      setCode("");
    } catch {
      setError("验证码无效或已经过期，请重新获取。");
    } finally {
      setLoading(false);
    }
  };

  const resendChallenge = async () => {
    if (loading) return;
    setLoading(true);
    setError(null);
    try {
      await request(mode === "register" ? "/auth/register" : "/auth/login/request", {
        email,
      });
      setCode("");
    } catch {
      setError("验证码暂时无法重新发送；请稍后再试或更换邮箱。");
    } finally {
      setLoading(false);
    }
  };

  const copyToken = async () => {
    if (!deployToken) return;
    try {
      await navigator.clipboard.writeText(deployToken);
      setCopied(true);
    } catch {
      setError("浏览器未允许复制，请在本页关闭前手动保存 token。");
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-story">
        <Link href="/" className="auth-back">
          <span className="brand-mark" aria-hidden="true"><span /><span /><span /></span>
          <span><strong>LAE</strong><small>Luma Application Engine</small></span>
        </Link>
        <div className="auth-story-copy">
          <p>BUILT ON LUMA</p>
          <h1>部署服务</h1>
          <span>连接源码，交给 LAE Agent 诊断，然后确认 Luma 部署计划。Git、静态产物和多服务 Compose 使用同一条流程。</span>
        </div>
        <div className="auth-assurances">
          <span><ShieldCheck size={14} /> Rootless builder</span>
          <span><KeyRound size={14} /> Scoped deploy token</span>
        </div>
      </section>

      <section className="auth-panel" aria-labelledby="auth-title">
        <div className="auth-panel-inner">
          <div className="auth-switch" aria-label="认证方式">
            <button type="button" disabled={loading} className={mode === "register" ? "is-active" : ""} onClick={() => switchMode("register")}>注册</button>
            <button type="button" disabled={loading} className={mode === "login" ? "is-active" : ""} onClick={() => switchMode("login")}>登录</button>
          </div>

          <AnimatePresence mode="wait">
            <motion.div
              key={`${mode}-${step}`}
              initial={reduceMotion ? false : { opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.38, ease: [0.4, 0, 0.2, 1] }}
            >
              {step === "request" && deliveryMode === "loading" && (
                <div className="auth-magic-progress" aria-live="polite">
                  <p className="auth-step">01 · IDENTITY</p>
                  <span aria-hidden="true" />
                  <h2 id="auth-title">正在确认邮件通道</h2>
                  <p className="auth-description">LAE 正在读取此环境可用的登录方式。</p>
                </div>
              )}

              {step === "request" && deliveryMode === "unavailable" && (
                <div>
                  <p className="auth-step">01 · IDENTITY</p>
                  <h2 id="auth-title">身份服务正在准备</h2>
                  <p className="auth-description">当前无法签发登录凭据。稍后刷新即可，不需要重复提交邮箱。</p>
                </div>
              )}

              {step === "request" && deliveryMode === "email" && (
                <form onSubmit={requestChallenge}>
                  <p className="auth-step">01 · EMAIL</p>
                  <h2 id="auth-title">{mode === "register" ? "建立你的部署空间" : "回到你的应用"}</h2>
                  <p className="auth-description">验证码和一次性链接会送到你输入的真实邮箱。没有密码，也不把凭据放进浏览器存储。</p>
                  <label className="auth-field">
                    <span>邮箱地址</span>
                    <div><Mail size={16} /><input type="email" required autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" /></div>
                  </label>
                  <button className="auth-submit" type="submit" disabled={loading}>
                    {loading ? "正在发送…" : "继续"}<ArrowRight size={16} />
                  </button>
                </form>
              )}

              {step === "verify" && (
                <form onSubmit={verifyChallenge}>
                  <p className="auth-step">02 · VERIFY</p>
                  <h2 id="auth-title">检查你的邮箱</h2>
                  <p className="auth-description">验证码已发送到 <strong>{email}</strong>。LAE 对不存在或受限的账户也返回相同结果。</p>
                  <label className="auth-field auth-code-field">
                    <span>六位验证码</span>
                    <input inputMode="numeric" pattern="[0-9]{6}" maxLength={6} required autoComplete="one-time-code" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} placeholder="000000" />
                  </label>
                  <button className="auth-submit" type="submit" disabled={loading || code.length !== 6}>
                    {loading ? "正在验证…" : mode === "register" ? "完成注册" : "登录"}<ArrowRight size={16} />
                  </button>
                  <div className="auth-secondary-actions">
                    <button className="auth-text-action" type="button" disabled={loading} onClick={() => void resendChallenge()}>重新发送验证码</button>
                    <button className="auth-text-action" type="button" disabled={loading} onClick={() => setStep("request")}>更换邮箱</button>
                  </div>
                </form>
              )}

              {step === "magic" && (
                <div className="auth-magic-progress" aria-live="polite">
                  <p className="auth-step">02 · VERIFY</p>
                  <span aria-hidden="true" />
                  <h2 id="auth-title">正在验证一次性链接</h2>
                  <p className="auth-description">凭据已从地址栏移除。验证完成后，这个链接将不能再次使用。</p>
                </div>
              )}

              {step === "complete" && (
                <div>
                  <p className="auth-step">03 · READY</p>
                  <div className="auth-success-mark"><Check size={24} /></div>
                  <h2 id="auth-title">{mode === "register" ? "部署空间已就绪" : "欢迎回来"}</h2>
                  <p className="auth-description">Session 已建立，可以继续进入 Luma Application Engine。</p>
                  {deployToken && (
                    <div className="token-once">
                      <div><KeyRound size={15} /><strong>默认 deploy token</strong><span>仅显示这一次</span></div>
                      <code>{deployToken}</code>
                      <button type="button" onClick={copyToken}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "已复制" : "复制到本机"}</button>
                    </div>
                  )}
                  <Link href="/" className="auth-submit">进入 LAE <ArrowRight size={16} /></Link>
                </div>
              )}
            </motion.div>
          </AnimatePresence>

          <div className="auth-error" aria-live="polite">{error}</div>
          <p className="auth-legal">继续即表示你同意服务条款与隐私政策。公开运营前将替换为正式备案文本。</p>
        </div>
      </section>
    </main>
  );
}

async function request(path: string, body: Record<string, string>) {
  const response = await fetch(`${API_ROOT}${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error("identity request failed");
  const value: unknown = await response.json();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("identity response invalid");
  }
  return value as Record<string, unknown>;
}

async function readAuthConfig(): Promise<DeliveryCapabilities> {
  const response = await fetch(`${API_ROOT}/auth/config`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) throw new Error("identity config unavailable");
  const value: unknown = await response.json();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("identity config invalid");
  }
  const delivery = (value as Record<string, unknown>).emailDelivery;
  if (typeof delivery !== "object" || delivery === null || Array.isArray(delivery)) {
    throw new Error("identity delivery config invalid");
  }
  const record = delivery as Record<string, unknown>;
  const mode = record.mode;
  const normalizedMode = mode === "email" ? mode : "unavailable";
  return {
    mode: normalizedMode,
  };
}
