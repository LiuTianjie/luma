import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "../apiClient";
import type { Lang } from "../types";
import { AlertCircle, CheckCircle2, ExternalLink, KeyRound, RefreshCw } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldContent, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";

type Settings = { grafanaPublicSharingEnabled: boolean };
const SETTINGS_PATH = "/v1/dashboard/observe/settings";

export function ObserveSettingsPanel({ lang, token }: { lang: Lang; token: string }) {
  const zh = lang === "zh";
  const [settings, setSettings] = useState<Settings | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [passwordError, setPasswordError] = useState("");
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    setSettings(null);
    setError("");
    setPasswordOpen(false);
    setPassword("");
    void apiGet<Settings>(SETTINGS_PATH, token, AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]))
      .then((value) => { if (!controller.signal.aborted) setSettings(value); })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { controller.abort(); request.current?.abort(); };
  }, [token, revision]);

  const changeSharing = async (enabled: boolean) => {
    if (!settings || busy) return;
    if (enabled && (password.length < 8 || password.length > 128 || !password.trim())) {
      setPasswordError(zh ? "请输入 8～128 个字符的密码，不能全部为空格。" : "Enter 8 to 128 characters; the password cannot be blank.");
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await apiPost<Settings>(SETTINGS_PATH, token, {
        grafanaPublicSharingEnabled: enabled,
        ...(enabled ? { grafanaPublicSharingPassword: password } : {}),
      },
        AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]));
      if (!controller.signal.aborted) {
        setSettings(value);
        setPasswordOpen(false);
        setPassword("");
        setNotice(zh ? "设置已保存，数秒内生效。" : "Saved. The setting takes effect within a few seconds.");
      }
    } catch (reason) {
      if (!controller.signal.aborted) {
        // A timed-out response does not establish whether the server saved it.
        setSettings(null);
        setPasswordOpen(false);
        setPassword("");
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const openPassword = () => {
    setPassword("");
    setPasswordError("");
    setNotice("");
    setPasswordOpen(true);
  };

  return (
    <Dialog open={passwordOpen} onOpenChange={(open) => {
      if (busy) return;
      setPasswordOpen(open);
      if (!open) { setPassword(""); setPasswordError(""); }
    }}>
      <Card>
        <CardHeader className="border-b border-hairline">
          <CardTitle className="flex items-center gap-2">
            {zh ? "看板对外分享" : "External dashboard sharing"}
            {settings ? <Badge appearance="dot" variant={settings.grafanaPublicSharingEnabled ? "warning" : "outline"}>{settings.grafanaPublicSharingEnabled ? (zh ? "已开启" : "On") : (zh ? "已关闭" : "Off")}</Badge> : null}
          </CardTitle>
          <CardDescription>{zh
            ? "通过链接和分享密码，让他人查看指定看板。默认关闭。"
            : "Let others view selected dashboards with a link and sharing password. Off by default."}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <Field orientation="horizontal">
            <FieldContent>
              <FieldLabel htmlFor="grafana-public-sharing">{zh ? "允许公开看板链接" : "Allow public dashboard links"}</FieldLabel>
              <FieldDescription>{zh
                ? "开启时必须设置密码。访客输入密码后，可查看在 Grafana 中主动分享的看板；其他看板、日志、链路和管理接口仍需登录 Luma。关闭后，所有公开链接恢复要求 Luma 登录。"
                : "Set a password when enabling. Visitors enter it to view dashboards explicitly shared in Grafana. Other dashboards, logs, traces and management APIs require Luma sign-in. Turning this off requires Luma sign-in for public links too."}</FieldDescription>
            </FieldContent>
            <Switch id="grafana-public-sharing" checked={settings?.grafanaPublicSharingEnabled ?? false}
              disabled={!settings || busy} onCheckedChange={(checked) => checked ? openPassword() : void changeSharing(false)} />
          </Field>
          {settings?.grafanaPublicSharingEnabled ? (
            <div className="flex items-center justify-between gap-3 rounded-lg bg-muted/50 px-4 py-3 ring-1 ring-hairline">
              <p className="flex items-center gap-2 text-[0.8125rem]"><KeyRound className="size-4 text-muted-foreground" aria-hidden="true" />{zh ? "已启用密码保护" : "Password protection is enabled"}</p>
              <Button variant="outline" size="sm" disabled={busy} onClick={openPassword}>{zh ? "修改密码" : "Change password"}</Button>
            </div>
          ) : null}
          {(!settings && !error) || busy ? (
            <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
              <Spinner aria-hidden="true" />{busy ? (zh ? "正在保存…" : "Saving…") : (zh ? "正在加载设置…" : "Loading settings…")}
            </p>
          ) : null}
          {error && (
            <Alert variant="warning">
              <AlertCircle />
              <AlertTitle>{zh ? "暂时无法确认设置" : "Could not confirm the setting"}</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
              <Button variant="outline" size="sm" onClick={() => { setBusy(false); setRevision((value) => value + 1); }}>
                <RefreshCw data-icon="inline-start" />{zh ? "重新加载" : "Reload"}
              </Button>
            </Alert>
          )}
          {notice && <p role="status" className="flex items-center gap-2 text-[0.8125rem] text-success-foreground"><CheckCircle2 className="size-4" aria-hidden="true" />{notice}</p>}
        </CardContent>
        <CardFooter className="flex-wrap gap-3">
          <p className="min-w-0 flex-1 basis-80 text-xs/5 text-muted-foreground">{zh
            ? "开启后，在 Grafana 看板中选择“分享 → 外部分享（Share externally）”，将链接和密码提供给访客。可随时在 Grafana 停用单个链接。"
            : "After enabling, choose Share → Share externally in Grafana and give visitors the link and password. You can disable an individual link in Grafana at any time."}</p>
          <a className={buttonVariants({ variant: "outline", size: "sm" })} href="/grafana/" target="_blank" rel="noopener noreferrer">
            {zh ? "打开 Grafana" : "Open Grafana"}<ExternalLink data-icon="inline-end" />
          </a>
        </CardFooter>
      </Card>
      <DialogContent showCloseButton={!busy}>
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); void changeSharing(true); }}>
          <DialogHeader>
            <DialogTitle>{settings?.grafanaPublicSharingEnabled ? (zh ? "修改分享密码" : "Change sharing password") : (zh ? "设置分享密码" : "Set a sharing password")}</DialogTitle>
            <DialogDescription>{zh ? "所有对外分享的看板使用此密码。访客打开链接后，需要先输入密码。" : "All externally shared dashboards use this password. Visitors must enter it after opening a link."}</DialogDescription>
          </DialogHeader>
          <Field data-invalid={Boolean(passwordError)}>
            <FieldLabel htmlFor="grafana-sharing-password">{zh ? "分享密码" : "Sharing password"}</FieldLabel>
            <Input id="grafana-sharing-password" name="sharing-password" type="password" autoComplete="new-password"
              value={password} onChange={(event) => { setPassword(event.target.value); setPasswordError(""); }}
              minLength={8} maxLength={128} required disabled={busy} spellCheck={false}
              aria-invalid={Boolean(passwordError)} aria-describedby={`grafana-sharing-password-help${passwordError ? " grafana-sharing-password-error" : ""}`} />
            <FieldDescription id="grafana-sharing-password-help">{zh ? "8～128 个字符。修改密码后，访客需要重新输入密码。" : "8 to 128 characters. Changing it requires visitors to enter the new password."}</FieldDescription>
            {passwordError ? <FieldError id="grafana-sharing-password-error">{passwordError}</FieldError> : null}
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={() => { setPasswordOpen(false); setPassword(""); }}>{zh ? "取消" : "Cancel"}</Button>
            <Button type="submit" disabled={busy || !password}>
              {busy ? <Spinner data-icon="inline-start" /> : null}
              {settings?.grafanaPublicSharingEnabled ? (zh ? "保存密码" : "Save password") : (zh ? "设置密码并开启" : "Set password and enable")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
