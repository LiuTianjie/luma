import { useState } from "react";
import { ArrowUpRight, Check, Copy, RefreshCw, Terminal, TriangleAlert } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import type { Lang } from "../types";

export function BuilderRunnerOnboardingCard({
  lang,
  builderUrl,
  compact = false,
  rechecking = false,
  onRecheck,
}: {
  lang: Lang;
  builderUrl?: string;
  compact?: boolean;
  rechecking?: boolean;
  onRecheck?: () => void;
}) {
  const zh = lang === "zh";
  const [copiedKey, setCopiedKey] = useState("");
  const cleanUrl = (builderUrl || "").replace(/\/+$/, "");
  const displayOrigin = cleanUrl || "https://build.example.com";
  const tokenCmd = "luma builder token create my-runner";
  const joinCmd = `luma runner join --url ${displayOrigin} --token <RUNNER_TOKEN>`;

  const copy = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      window.setTimeout(() => setCopiedKey(""), 1800);
    } catch {
      // clipboard unavailable
    }
  };

  return (
    <Alert className="border-amber-500/40 bg-amber-500/5 text-foreground [&>svg]:text-amber-600 dark:[&>svg]:text-amber-400">
      <TriangleAlert className="size-4" />
      <AlertTitle className="flex flex-wrap items-center justify-between gap-2">
        <span>{zh ? "尚未接入在线构建 Runner（0 台在线）" : "No online build runners enrolled (0 online)"}</span>
      </AlertTitle>
      <AlertDescription className="mt-1.5 flex flex-col gap-2.5 text-xs">
        <p className="text-muted-foreground">
          {zh
            ? "Luma Build 控制队列已就绪，但当前没有在线构建机。提交的 Git 构建任务会保持排队，请先接入至少一台 Linux 或 macOS Runner："
            : "Luma Build server is running, but 0 runners are online. Queued Git builds will wait until at least one Linux or macOS runner joins:"}
        </p>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-1.5 font-medium text-foreground">
            <Terminal className="size-3.5 text-amber-600 dark:text-amber-400" />
            <span>{zh ? "1. 生成 Runner 接入令牌（在 Builder 控制台或终端运行）" : "1. Create a runner token (in Builder console or CLI)"}</span>
          </div>
          <div className="flex items-center justify-between gap-2 rounded-lg border border-border bg-background/90 px-2.5 py-1 font-mono text-[0.7rem] text-foreground">
            <span className="truncate select-all">{tokenCmd}</span>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={() => void copy("token", tokenCmd)}
              className="shrink-0 text-muted-foreground hover:text-foreground"
              aria-label={zh ? "复制生成令牌命令" : "Copy token creation command"}
            >
              {copiedKey === "token" ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
            </Button>
          </div>

          <div className="mt-1 flex items-center gap-1.5 font-medium text-foreground">
            <Terminal className="size-3.5 text-amber-600 dark:text-amber-400" />
            <span>{zh ? "2. 在构建机（Linux / macOS）上运行接入命令" : "2. Enroll a build machine (Linux / macOS)"}</span>
          </div>
          <div className="flex items-center justify-between gap-2 rounded-lg border border-border bg-background/90 px-2.5 py-1 font-mono text-[0.7rem] text-foreground">
            <span className={compact ? "truncate select-all" : "break-all select-all"}>{joinCmd}</span>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={() => void copy("join", joinCmd)}
              className="shrink-0 text-muted-foreground hover:text-foreground"
              aria-label={zh ? "复制接入命令" : "Copy runner join command"}
            >
              {copiedKey === "join" ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 pt-0.5">
          {cleanUrl ? (
            <Button
              size="sm"
              variant="outline"
              nativeButton={false}
              render={<a href={`${cleanUrl}/`} target="_blank" rel="noreferrer" />}
            >
              {zh ? "打开 Builder 控制台生成 Token" : "Open Builder console for token"}
              <ArrowUpRight className="ml-1 size-3.5" />
            </Button>
          ) : null}
          {onRecheck ? (
            <Button size="sm" variant="ghost" disabled={rechecking} onClick={onRecheck}>
              <RefreshCw className={rechecking ? "mr-1 size-3.5 animate-spin" : "mr-1 size-3.5"} />
              {zh ? "重新检测 Runner" : "Recheck runners"}
            </Button>
          ) : null}
        </div>
      </AlertDescription>
    </Alert>
  );
}
