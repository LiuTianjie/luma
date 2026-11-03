import { useEffect, useRef, useState, type FormEvent } from "react";
import type { Lang } from "../types";
import { useRouter } from "../router";
import { useTheme } from "../useTheme";
import { AuthLayout } from "./AuthLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";

export function SharedDashboardPage({ accessToken }: { accessToken: string }) {
  const { search } = useRouter();
  const [lang, setLang] = useState<Lang>(() => {
    const saved = localStorage.getItem("luma.dashboard.lang");
    return saved === "en" || saved === "zh" ? saved : navigator.language.startsWith("zh") ? "zh" : "en";
  });
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const zh = lang === "zh";
  useTheme();

  useEffect(() => { document.documentElement.lang = zh ? "zh-CN" : "en"; }, [zh]);
  useEffect(() => () => request.current?.abort(), []);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || !password) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/v1/auth/grafana-public/session", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]),
      });
      if (!response.ok) {
        const message = response.status === 401 ? (zh ? "密码不正确，请重新输入。" : "Incorrect password. Try again.")
          : response.status === 404 ? (zh ? "对外分享已关闭，请联系分享者。" : "External sharing is disabled. Contact the person who shared the link.")
          : response.status === 429 ? (zh ? "尝试次数过多，请稍等一分钟再试。" : "Too many attempts. Wait a minute and try again.")
          : (zh ? "暂时无法访问，请稍后重试。" : "Unable to open the dashboard. Try again shortly.");
        throw new Error(message);
      }
      const result = await response.json();
      if (result.authenticated !== true) throw new Error(zh ? "密码验证未完成，请重试。" : "Password verification did not complete. Try again.");
      if (!controller.signal.aborted) {
        setPassword("");
        // The destination is built from the matched share token, never an arbitrary return URL.
        window.location.replace(`/grafana/public-dashboards/${accessToken}${search}`);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return (
    <AuthLayout lang={lang} onLangChange={setLang} title={zh ? "共享看板" : "Shared dashboard"}>
      <Card className="shadow-sm [--card-spacing:--spacing(6)]">
        <CardHeader>
          <CardTitle role="heading" aria-level={1} className="text-lg">{zh ? "输入分享密码" : "Enter the sharing password"}</CardTitle>
          <CardDescription>{zh ? "此看板已设置密码保护。" : "This dashboard is protected by a password."}</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="flex flex-col gap-5" onSubmit={(event) => void submit(event)}>
            <Field data-invalid={Boolean(error)}>
              <FieldLabel htmlFor="sharing-password">{zh ? "分享密码" : "Sharing password"}</FieldLabel>
              <Input id="sharing-password" name="sharing-password" type="password" autoComplete="current-password"
                value={password} required maxLength={128} disabled={busy} spellCheck={false}
                onChange={(event) => { setPassword(event.target.value); setError(""); }}
                aria-invalid={Boolean(error)} aria-describedby={`sharing-password-help${error ? " sharing-password-error" : ""}`} />
              <FieldDescription id="sharing-password-help">{zh ? "请向分享者获取密码。" : "Ask the person who shared the link for the password."}</FieldDescription>
              {error ? <FieldError id="sharing-password-error" role="alert">{error}</FieldError> : null}
            </Field>
            <Button type="submit" size="lg" className="w-full" disabled={busy || !password}>
              {busy ? <Spinner data-icon="inline-start" /> : null}
              {busy ? (zh ? "正在验证…" : "Verifying…") : (zh ? "查看看板" : "View dashboard")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </AuthLayout>
  );
}
