import { type FormEvent, useId, useRef, useState } from "react";
import { FingerprintPattern, Info, TriangleAlert } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldSeparator } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { passkeysSupported, type AuthConfig } from "../authApi";
import { t } from "../i18n";
import type { Lang } from "../types";

export function LoginPanel({
  lang,
  config,
  expired,
  onToken,
  onPasskey,
}: {
  lang: Lang;
  config: AuthConfig | null;
  expired: boolean;
  onToken: (token: string) => Promise<void>;
  onPasskey: () => Promise<void>;
}) {
  const [token, setToken] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState<"" | "token" | "passkey">("");
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const fieldId = useId();
  const zh = lang === "zh";
  const invalid = submitted && !token.trim();
  const passkeyReady = Boolean(config?.passkeys.available && config.passkeys.registered) && passkeysSupported();
  const tokenAllowed = !config?.requirePasskey;
  // With a passkey on file the token is the fallback, one click away.
  const [showToken, setShowToken] = useState(false);
  const tokenVisible = tokenAllowed && (!passkeyReady || showToken);
  const canAddPasskeyLater = Boolean(config?.passkeys.available && !config.passkeys.registered);

  const run = async (kind: "token" | "passkey", action: () => Promise<void>) => {
    setBusy(kind);
    setError("");
    try {
      await action();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy("");
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitted(true);
    if (!token.trim()) {
      inputRef.current?.focus();
      return;
    }
    void run("token", () => onToken(token));
  };

  return (
    <Card aria-labelledby={`${fieldId}-title`}>
      <CardHeader>
        <CardTitle id={`${fieldId}-title`} role="heading" aria-level={1} className="text-lg">{zh ? "登录" : "Sign in"}</CardTitle>
        <CardDescription>
          {passkeyReady
            ? (zh ? "使用这台设备或手机上的 Passkey 登录。" : "Use a passkey on this device or your phone.")
            : (zh ? "粘贴 bootstrap 输出的管理 Token。" : "Paste the management token printed by bootstrap.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {expired ? (
          <Alert>
            <Info aria-hidden="true" />
            <AlertTitle>{zh ? "登录已结束" : "Signed out"}</AlertTitle>
            <AlertDescription>{zh ? "会话已过期或在其他地方被撤销，请重新登录。" : "The session expired or was revoked elsewhere. Sign in again."}</AlertDescription>
          </Alert>
        ) : null}
        {error ? (
          <Alert variant="destructive" aria-live="assertive">
            <TriangleAlert aria-hidden="true" />
            <AlertTitle>{zh ? "无法登录" : "Sign-in failed"}</AlertTitle>
            <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
          </Alert>
        ) : null}
        <FieldGroup className="gap-5">
          {passkeyReady ? (
            <Button type="button" size="lg" className="w-full" disabled={Boolean(busy)} onClick={() => void run("passkey", onPasskey)}>
              {busy === "passkey" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <FingerprintPattern data-icon="inline-start" />}
              {zh ? "使用 Passkey 登录" : "Sign in with a passkey"}
            </Button>
          ) : null}
          {passkeyReady && tokenAllowed && !showToken ? (
            <Button type="button" variant="ghost" size="sm" className="self-center text-muted-foreground" onClick={() => { setShowToken(true); window.setTimeout(() => inputRef.current?.focus(), 0); }}>
              {zh ? "改用管理 Token" : "Use the management token instead"}
            </Button>
          ) : null}
          {passkeyReady && tokenVisible ? <FieldSeparator>{zh ? "或" : "or"}</FieldSeparator> : null}
          {tokenVisible ? (
            <form noValidate onSubmit={submit}>
              <FieldGroup className="gap-4">
                <Field data-invalid={invalid}>
                  <FieldLabel htmlFor={fieldId}>{zh ? "管理 Token" : "Management token"}</FieldLabel>
                  <Input
                    ref={inputRef}
                    id={fieldId}
                    className="h-9"
                    aria-describedby={invalid ? `${fieldId}-description ${fieldId}-error` : `${fieldId}-description`}
                    aria-invalid={invalid}
                    autoComplete="current-password"
                    name="management-token"
                    onChange={(event) => setToken(event.target.value)}
                    required
                    spellCheck={false}
                    type="password"
                    value={token}
                  />
                  {invalid ? <FieldError id={`${fieldId}-error`}>{zh ? "请输入管理 Token。" : "Enter a management token."}</FieldError> : null}
                  <FieldDescription id={`${fieldId}-description`}>{t(lang, "loginCopy")}</FieldDescription>
                </Field>
                <Button type="submit" size="lg" className="w-full" variant={passkeyReady ? "outline" : "default"} disabled={Boolean(busy)}>
                  {busy === "token" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                  {t(lang, "openStatus")}
                </Button>
              </FieldGroup>
            </form>
          ) : null}
          {!tokenAllowed ? (
            <p className="text-xs leading-relaxed text-muted-foreground">
              {zh
                ? "此 Control 要求浏览器使用 Passkey 登录。手边没有 Passkey 时，可用管理 Token 在命令行运行 luma passkey enroll 生成添加链接，或运行 luma passkey require off 恢复 Token 登录。"
                : "This Control requires a passkey for browser sign-in. Without one at hand, use the management token from the CLI: luma passkey enroll creates an enrollment link, and luma passkey require off allows token sign-in again."}
            </p>
          ) : null}
          {canAddPasskeyLater ? (
            <p className="text-xs leading-relaxed text-muted-foreground">
              {zh ? "登录后可在 设置 → 登录与 Passkey 添加 Passkey，之后就不需要再粘贴 Token。" : "After signing in, add a passkey in Settings → Sign-in so you no longer need to paste the token."}
            </p>
          ) : null}
        </FieldGroup>
      </CardContent>
    </Card>
  );
}
