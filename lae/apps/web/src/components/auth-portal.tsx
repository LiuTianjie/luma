"use client";

import {
  ArrowLeft,
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
      <div className="auth-ambient" aria-hidden="true">
        <motion.span
          animate={reduceMotion ? undefined : { scale: [1, 1.06, 1], opacity: [0.3, 0.48, 0.3] }}
          transition={{ duration: 9, repeat: Infinity, ease: "easeInOut" }}
        />
        <i /><i /><i />
      </div>

      <section className="auth-story">
        <Link href="/" className="auth-back"><ArrowLeft size={14} /> 返回 LAE</Link>
        <div className="auth-monogram" aria-hidden="true"><span /><span /><span /></div>
        <div className="auth-story-copy">
          <p>BUILT ON LUMA</p>
          <h1>你的应用，<em>不必学会云。</em></h1>
          <span>LAE 读懂源码、生成部署计划，并把每一个 HTTP 服务安放到可持续运行的位置。</span>
        </div>
        <div className="auth-assurances">
          <span><ShieldCheck size={14} /> Rootless build isolation</span>
          <span><KeyRound size={14} /> Task-bound credentials</span>
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
              {step === "request" && (
                <form onSubmit={requestChallenge}>
                  <p className="auth-step">01 · EMAIL</p>
                  <h2 id="auth-title">{mode === "register" ? "建立你的部署空间" : "回到你的应用"}</h2>
                  <p className="auth-description">我们会发送一个短时验证码和登录链接。没有密码，也不把凭据放进浏览器存储。</p>
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
                  <button className="auth-text-action" type="button" onClick={() => setStep("request")}>更换邮箱</button>
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
