import { useEffect, useId, useRef, useState } from "react";
import { TriangleAlert } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { apiPost } from "../apiClient";
import type { Lang } from "../types";

export function BuilderSignIn({ token, challenge, lang }: { token: string; challenge: string; lang: Lang }) {
  const [error, setError] = useState("");
  const pending = useRef<Promise<{ redirectUrl: string }> | null>(null);
  const titleId = useId();
  const zh = lang === "zh";
  useEffect(() => {
    let active = true;
    // StrictMode may mount effects twice. Reuse the same authorization request
    // so the second handoff cannot invalidate the first one's one-use ticket.
    pending.current ||= apiPost<{ redirectUrl: string }>("/v1/auth/builder/authorize", token, { challenge });
    pending.current.then(result => {
      if (active) window.location.replace(result.redirectUrl);
    }).catch(reason => {
      if (active) setError(reason instanceof Error ? reason.message : String(reason));
    });
    return () => { active = false; };
  }, [token, challenge]);

  return (
    <Card aria-labelledby={titleId} className="shadow-sm [--card-spacing:--spacing(6)]">
      <CardHeader>
        <CardTitle id={titleId} role="heading" aria-level={1} className="text-lg tracking-[-0.01em]">
          {zh ? "登录 Luma Build" : "Sign in to Luma Build"}
        </CardTitle>
        <CardDescription>
          {zh ? "复用当前 Luma 控制台会话完成单点登录。" : "Completing sign-in with your active Luma console session."}
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
