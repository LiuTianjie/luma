import { useEffect, useId, useRef, useState } from "react";
import { TriangleAlert } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { apiGet, apiPost } from "../apiClient";
import type { Lang } from "../types";

const KNOWN_ADDON_NAMES: Record<string, string> = {
  builder: "Luma Build",
  eye: "Luma Eye",
  observe: "Luma Observe",
};

export function AddonSignIn({
  addonId,
  token,
  challenge,
  lang,
}: {
  addonId: string;
  token: string;
  challenge: string;
  lang: Lang;
}) {
  const cleanId = (addonId || "builder").trim().toLowerCase();
  const [addonName, setAddonName] = useState(KNOWN_ADDON_NAMES[cleanId] || cleanId);
  const [error, setError] = useState("");
  const pending = useRef<Promise<{ redirectUrl: string; name?: string }> | null>(null);
  const titleId = useId();
  const zh = lang === "zh";

  useEffect(() => {
    let active = true;
    apiGet<{ name?: string }>(`/v1/auth/addon/${encodeURIComponent(cleanId)}/info`, token)
      .then((info) => {
        if (active && info.name) setAddonName(info.name);
      })
      .catch(() => {});
    return () => { active = false; };
  }, [cleanId, token]);

  useEffect(() => {
    let active = true;
    // StrictMode may mount effects twice. Reuse the same authorization request
    // so the second handoff cannot invalidate the first one's one-use ticket.
    pending.current ||= apiPost<{ redirectUrl: string; name?: string }>(
      `/v1/auth/addon/${encodeURIComponent(cleanId)}/authorize`,
      token,
      { challenge },
    );
    pending.current
      .then((result) => {
        if (!active) return;
        if (result.name) setAddonName(result.name);
        window.location.replace(result.redirectUrl);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { active = false; };
  }, [cleanId, token, challenge]);

  return (
    <Card aria-labelledby={titleId} className="shadow-sm [--card-spacing:--spacing(6)]">
      <CardHeader>
        <CardTitle id={titleId} role="heading" aria-level={1} className="text-lg tracking-[-0.01em]">
          {zh ? `登录 ${addonName}` : `Sign in to ${addonName}`}
        </CardTitle>
        <CardDescription>
          {zh
            ? "复用当前 Luma 控制台会话完成单点登录。"
            : "Completing single sign-on with your active Luma console session."}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error ? (
          <>
            <Alert variant="destructive" aria-live="assertive">
              <TriangleAlert aria-hidden="true" />
              <AlertTitle>{zh ? "无法登录" : "Sign-in failed"}</AlertTitle>
              <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
            </Alert>
            <Button type="button" size="lg" className="w-full" onClick={() => window.location.reload()}>
              {zh ? "重试" : "Retry"}
            </Button>
          </>
        ) : (
          <div role="status" className="flex items-center gap-2.5 rounded-lg border border-border bg-muted/50 px-3.5 py-3 text-sm text-muted-foreground">
            <Spinner aria-hidden="true" />
            <span>{zh ? "正在使用当前 Luma 会话登录…" : "Signing in with your Luma session…"}</span>
          </div>
        )}
        <Button variant="outline" size="lg" className="w-full" nativeButton={false} render={<a href="/dashboard/" />}>
          {zh ? "返回 Luma 控制台" : "Back to Luma console"}
        </Button>
      </CardContent>
    </Card>
  );
}
