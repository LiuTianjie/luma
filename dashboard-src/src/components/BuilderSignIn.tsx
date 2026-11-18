import { useEffect, useRef, useState } from "react";
import { apiPost } from "../apiClient";
import type { Lang } from "../types";
import { Button } from "./ui/button";
import { Spinner } from "./ui/spinner";

export function BuilderSignIn({ token, challenge, lang }: { token: string; challenge: string; lang: Lang }) {
  const [error, setError] = useState("");
  const pending = useRef<Promise<{ redirectUrl: string }> | null>(null);
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

  return <div className="space-y-4">
    <h1 className="text-lg font-semibold">{zh ? "登录 Luma Build" : "Sign in to Luma Build"}</h1>
    {error ? <>
      <p role="alert" className="text-sm text-destructive">{error}</p>
      <Button onClick={() => window.location.reload()}>{zh ? "重试" : "Retry"}</Button>
    </> : <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner />{zh ? "正在使用当前 Luma 会话登录…" : "Signing in with your Luma session…"}</p>}
    <a className="block text-sm text-muted-foreground underline" href="/dashboard/">{zh ? "返回 Luma 控制台" : "Back to Luma console"}</a>
  </div>;
}
