import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { FingerprintPattern, KeyRound, ShieldCheck, TriangleAlert } from "@/components/icons";
import { Eye, EyeOff } from "lucide-react";
import lumaLogoMark from "@/assets/luma-logo-mark.png";
import {
  detectPasskeyCapabilities,
  runConditionalPasskeyAutofill,
  triggerExplicitPasskeyLogin,
} from "@/api/passkeys";
import type { AuthStatus } from "@/api/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldSeparator,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";

interface LoginViewProps {
  authHint: AuthStatus | null;
  redirectTarget?: string;
  ssoFailed?: boolean;
  signedOut?: boolean;
  manualLogin?: boolean;
  onPasswordOrTokenLogin: (username: string, secret: string) => Promise<void>;
  onPasskeyAuthenticated: () => Promise<void>;
}

function buildSSOLoginHref(basePath: string, redirectTarget: string): string {
  const sp = new URLSearchParams();
  if (redirectTarget && redirectTarget.startsWith("/") && !redirectTarget.startsWith("//")) {
    sp.set("redirect", redirectTarget);
  }
  const theme = document.documentElement.classList.contains("dark") ? "dark" : "light";
  sp.set("theme", theme);
  const qs = sp.toString();
  return qs ? `${basePath}?${qs}` : basePath;
}

export function LoginView({
  authHint,
  redirectTarget = "",
  ssoFailed = false,
  signedOut = false,
  manualLogin = false,
  onPasswordOrTokenLogin,
  onPasskeyAuthenticated,
}: LoginViewProps) {
  const isControlSSO = authHint?.auth_mode === "control-sso";
  const ssoLoginPath = authHint?.sso_login_url || "/auth/login";
  const defaultUser = authHint?.username || "admin";
  const hasPassword = Boolean(authHint?.has_password);
  const hasRegisteredPasskeys = (authHint?.passkey_count || 0) > 0;
  const serverPasskeysAvailable = !isControlSSO && authHint?.passkeys_available !== false;

  const [username, setUsername] = useState(defaultUser);
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showCredentialForm, setShowCredentialForm] = useState(
    isControlSSO ? manualLogin : !hasRegisteredPasskeys,
  );
  const [redirectingSSO, setRedirectingSSO] = useState(
    Boolean(isControlSSO && !ssoFailed && !signedOut && !manualLogin),
  );
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState<"" | "password" | "passkey">("");
  const [error, setError] = useState(
    ssoFailed ? "Luma Control 单点登录未能完成，请重试或检查控制面会话状态。" : "",
  );
  const [clientPasskeyReady, setClientPasskeyReady] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const passwordInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    document.title = "登录控制台 · Luma Eye";
  }, []);

  useEffect(() => {
    if (!isControlSSO) return;
    if (ssoFailed || signedOut || manualLogin) {
      setRedirectingSSO(false);
      return;
    }
    setRedirectingSSO(true);
    window.location.replace(buildSSOLoginHref(ssoLoginPath, redirectTarget));
  }, [isControlSSO, ssoFailed, signedOut, manualLogin, ssoLoginPath, redirectTarget]);

  useEffect(() => {
    if (authHint?.username) {
      setUsername(authHint.username);
    }
    if (!isControlSSO && (authHint?.passkey_count || 0) === 0) {
      setShowCredentialForm(true);
    }
  }, [authHint?.username, authHint?.passkey_count, isControlSSO]);

  const armConditionalAutofill = useCallback(() => {
    if (!serverPasskeysAvailable) return;
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    void runConditionalPasskeyAutofill(
      ctrl.signal,
      async () => {
        await onPasskeyAuthenticated();
      },
      (msg) => setError(msg),
    );
  }, [serverPasskeysAvailable, onPasskeyAuthenticated]);

  useEffect(() => {
    let mounted = true;
    detectPasskeyCapabilities().then((caps) => {
      if (!mounted) return;
      setClientPasskeyReady(caps.supported && serverPasskeysAvailable);
      if (caps.conditionalGet && serverPasskeysAvailable) {
        armConditionalAutofill();
      }
    });
    return () => {
      mounted = false;
      abortRef.current?.abort();
    };
  }, [serverPasskeysAvailable, armConditionalAutofill]);

  const passkeyPrimary = !isControlSSO && clientPasskeyReady && hasRegisteredPasskeys;
  const credentialFormVisible = isControlSSO ? showCredentialForm : (!passkeyPrimary || showCredentialForm);

  const handlePasskeyButton = async () => {
    setBusy("passkey");
    setError("");
    abortRef.current?.abort();
    const btnCtrl = new AbortController();
    abortRef.current = btnCtrl;
    try {
      const res = await triggerExplicitPasskeyLogin(btnCtrl.signal);
      if (res.cancelled) {
        armConditionalAutofill();
        return;
      }
      if (res.authenticated) {
        await onPasskeyAuthenticated();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      armConditionalAutofill();
    } finally {
      setBusy("");
    }
  };

  const handleFormSubmit = async (ev: FormEvent<HTMLFormElement>) => {
    ev.preventDefault();
    setSubmitted(true);
    if (!password.trim()) {
      passwordInputRef.current?.focus();
      return;
    }
    setBusy("password");
    setError("");
    try {
      await onPasswordOrTokenLogin(username.trim() || "admin", password);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const passwordInvalid = submitted && !password.trim();

  return (
    <main
      id="main"
      className="relative isolate flex min-h-dvh flex-col items-center justify-center gap-8 overflow-hidden bg-canvas px-4 py-12"
    >
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(var(--border)_1px,transparent_1px)] [background-size:20px_20px] [mask-image:radial-gradient(ellipse_at_center,black_20%,transparent_70%)]"
      />

      <div className="flex flex-col items-center gap-3">
        <span className="flex size-12 items-center justify-center rounded-xl bg-background shadow-sm ring-1 ring-border">
          <img src={lumaLogoMark} alt="" className="size-7" width={28} height={28} />
        </span>
        <div className="flex items-baseline gap-2">
          <span className="text-xl font-semibold tracking-[-0.02em]">Luma Eye</span>
          <span className="text-sm text-muted-foreground">观测与分析控制台</span>
        </div>
      </div>

      <div className="w-full max-w-sm">
        <Card aria-labelledby="signin-heading" className="shadow-sm [--card-spacing:--spacing(6)]">
          <CardHeader>
            <CardTitle id="signin-heading" role="heading" aria-level={1} className="text-lg tracking-[-0.01em]">
              登录控制台
            </CardTitle>
            <CardDescription>
              {isControlSSO
                ? "已接入 Luma Control 统一身份认证，无需重复设置密码或 Passkey。"
                : passkeyPrimary
                  ? "使用此设备或手机上的 Passkey 一键验证登录。"
                  : hasPassword
                    ? "输入管理员密码或应急管理 Token 登录（会话持久化保存 30 天）。"
                    : "粘贴管理 Token 登录；进入后可设置登录密码或添加 Passkey。"}
            </CardDescription>
          </CardHeader>

          <CardContent className="flex flex-col gap-4">
            {signedOut && !error ? (
              <Alert>
                <ShieldCheck aria-hidden="true" />
                <AlertTitle>已退出 Luma Eye 会话</AlertTitle>
                <AlertDescription>你可以随时使用 Luma Control 再次单点登录。</AlertDescription>
              </Alert>
            ) : null}

            {error ? (
              <Alert variant="destructive" aria-live="assertive">
                <TriangleAlert aria-hidden="true" />
                <AlertTitle>无法登录</AlertTitle>
                <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
              </Alert>
            ) : null}

            <FieldGroup className="gap-4">
              {isControlSSO ? (
                redirectingSSO ? (
                  <div role="status" className="flex items-center gap-2.5 rounded-lg border border-border bg-muted/50 px-3.5 py-3 text-sm text-muted-foreground">
                    <Spinner aria-hidden="true" />
                    <span>正在跳转 Luma Control 完成单点登录…</span>
                  </div>
                ) : (
                  <>
                    <Button
                      size="lg"
                      className="w-full"
                      nativeButton={false}
                      render={<a href={buildSSOLoginHref(ssoLoginPath, redirectTarget)} />}
                    >
                      <ShieldCheck data-icon="inline-start" />
                      使用 Luma Control 单点登录
                    </Button>
                    {!showCredentialForm ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="self-center text-muted-foreground"
                        onClick={() => {
                          setShowCredentialForm(true);
                          window.setTimeout(() => passwordInputRef.current?.focus(), 0);
                        }}
                      >
                        <KeyRound data-icon="inline-start" />
                        改用管理 Token 登录
                      </Button>
                    ) : null}
                  </>
                )
              ) : null}

              {!isControlSSO && clientPasskeyReady ? (
                <Button
                  type="button"
                  size="lg"
                  variant={passkeyPrimary ? "default" : "outline"}
                  className="w-full"
                  disabled={Boolean(busy)}
                  onClick={() => void handlePasskeyButton()}
                >
                  {busy === "passkey" ? (
                    <Spinner aria-hidden="true" data-icon="inline-start" />
                  ) : (
                    <FingerprintPattern data-icon="inline-start" />
                  )}
                  使用 Passkey 登录
                </Button>
              ) : null}

              {passkeyPrimary && !showCredentialForm ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="self-center text-muted-foreground"
                  onClick={() => {
                    setShowCredentialForm(true);
                    window.setTimeout(() => passwordInputRef.current?.focus(), 0);
                  }}
                >
                  <KeyRound data-icon="inline-start" />
                  改用密码或管理 Token 登录
                </Button>
              ) : null}

              {((!isControlSSO && clientPasskeyReady) || isControlSSO) && credentialFormVisible ? <FieldSeparator>或</FieldSeparator> : null}

              {credentialFormVisible ? (
                <form onSubmit={(ev) => void handleFormSubmit(ev)}>
                  <FieldGroup className="gap-4">
                    {!isControlSSO ? (
                      <Field>
                        <FieldLabel htmlFor="login-username">管理员账号</FieldLabel>
                        <Input
                          id="login-username"
                          name="username"
                          type="text"
                          className="h-9"
                          value={username}
                          onChange={(ev) => setUsername(ev.target.value)}
                          autoComplete="username webauthn"
                          spellCheck={false}
                          autoCapitalize="none"
                          autoCorrect="off"
                          enterKeyHint="next"
                          aria-describedby="signin-heading"
                          autoFocus={!passkeyPrimary}
                          required
                        />
                      </Field>
                    ) : null}

                    <Field data-invalid={passwordInvalid}>
                      <FieldLabel htmlFor="login-password">
                        {isControlSSO
                          ? "管理 Token（Luma Control / 备用 Token）"
                          : hasPassword
                            ? "登录密码 / 管理 Token"
                            : "管理 Token (LUMA_EYE_ADMIN_TOKEN)"}
                      </FieldLabel>
                      <div className="relative flex items-center">
                        <Input
                          ref={passwordInputRef}
                          id="login-password"
                          name="password"
                          type={showPassword ? "text" : "password"}
                          className="h-9 pr-9"
                          value={password}
                          onChange={(ev) => setPassword(ev.target.value)}
                          autoComplete="current-password"
                          enterKeyHint="done"
                          spellCheck={false}
                          aria-invalid={passwordInvalid}
                          aria-describedby={passwordInvalid ? "login-password-err login-password-desc" : "login-password-desc"}
                          required
                        />
                        <button
                          type="button"
                          onClick={() => setShowPassword((v) => !v)}
                          aria-label={showPassword ? "隐藏密码" : "显示密码"}
                          aria-pressed={showPassword}
                          className="absolute right-2 inline-flex size-6 items-center justify-center rounded text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                        >
                          {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                        </button>
                      </div>
                      {passwordInvalid ? (
                        <FieldError id="login-password-err">请输入密码或管理 Token。</FieldError>
                      ) : null}
                      <FieldDescription id="login-password-desc">
                        登录态持久化保存在数据卷，服务更新或容器重启均保持 30 天免重新输入。
                      </FieldDescription>
                    </Field>

                    <Button
                      type="submit"
                      size="lg"
                      variant={passkeyPrimary ? "outline" : "default"}
                      className="w-full"
                      disabled={Boolean(busy)}
                    >
                      {busy === "password" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                      登录控制台
                    </Button>
                  </FieldGroup>
                </form>
              ) : null}
            </FieldGroup>
          </CardContent>
        </Card>
      </div>

      <footer className="flex items-center gap-2 text-xs text-muted-foreground">
        <span translate="no">{window.location.host}</span>
        <span aria-hidden="true">·</span>
        <span>Luma Observability Engine</span>
      </footer>
    </main>
  );
}
