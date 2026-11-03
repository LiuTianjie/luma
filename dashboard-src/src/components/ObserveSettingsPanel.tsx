import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost } from "../apiClient";
import type { Lang } from "../types";
import { AlertCircle, ExternalLink, RefreshCw } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field";
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
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    setSettings(null);
    setError("");
    void apiGet<Settings>(SETTINGS_PATH, token, AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]))
      .then((value) => { if (!controller.signal.aborted) setSettings(value); })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { controller.abort(); request.current?.abort(); };
  }, [token, revision]);

  const changeSharing = async (enabled: boolean) => {
    if (!settings || busy) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const value = await apiPost<Settings>(SETTINGS_PATH, token, { grafanaPublicSharingEnabled: enabled },
        AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]));
      if (!controller.signal.aborted) {
        setSettings(value);
        setNotice(zh ? "设置已保存，数秒内生效。" : "Saved. The setting takes effect within a few seconds.");
      }
    } catch (reason) {
      if (!controller.signal.aborted) {
        // A timed-out response does not establish whether the server saved it.
        setSettings(null);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{zh ? "看板对外分享" : "External dashboard sharing"}</CardTitle>
        <CardDescription>{zh
          ? "允许他人通过 Grafana 公开链接查看指定看板。默认关闭。"
          : "Let others view selected dashboards through Grafana public links. Off by default."}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <Field orientation="horizontal">
          <FieldContent>
            <FieldLabel htmlFor="grafana-public-sharing">{zh ? "允许公开看板链接" : "Allow public dashboard links"}</FieldLabel>
            <FieldDescription>{zh
              ? "开启后，在 Grafana 中主动分享的看板可免登录查看；其他看板、日志、链路和管理接口仍需登录。关闭后，公开链接也需要登录 Luma。"
              : "When enabled, dashboards explicitly shared in Grafana can be viewed without signing in. Other dashboards, logs, traces and management APIs still require sign-in. Turning this off requires Luma sign-in for public links too."}</FieldDescription>
          </FieldContent>
          <Switch id="grafana-public-sharing" checked={settings?.grafanaPublicSharingEnabled ?? false}
            disabled={!settings || busy} onCheckedChange={(checked) => void changeSharing(checked)} />
        </Field>
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
        {notice && <p role="status" className="text-sm text-muted-foreground">{notice}</p>}
        <p className="text-sm text-muted-foreground">{zh
          ? "开启后，在 Grafana 看板中选择“分享 → 外部分享（Share externally）”。可随时在 Grafana 停用单个链接。"
          : "After enabling, choose Share → Share externally in Grafana. You can disable an individual link in Grafana at any time."}</p>
        <div>
          <a className={buttonVariants({ variant: "outline", size: "sm" })} href="/grafana/" target="_blank" rel="noopener noreferrer">
            {zh ? "打开 Grafana" : "Open Grafana"}<ExternalLink data-icon="inline-end" />
          </a>
        </div>
      </CardContent>
    </Card>
  );
}
