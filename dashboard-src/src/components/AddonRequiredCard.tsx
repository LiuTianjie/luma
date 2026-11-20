import { useState } from "react";
import { Activity, ArrowRight, Boxes, Check, Copy, Package } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { spaLink, useRouter } from "../router";
import type { Lang } from "../types";

const ADDON_META: Record<
  "builder" | "observe" | "eye",
  {
    name: string;
    zhTitle: string;
    enTitle: string;
    zhDesc: string;
    enDesc: string;
    zhBullets: string[];
    enBullets: string[];
    cli: string;
  }
> = {
  builder: {
    name: "Luma Build",
    zhTitle: "需要安装 Luma Build 插件",
    enTitle: "Luma Build addon required",
    zhDesc: "从 Git 仓库自动克隆、远程多 Runner 构建镜像并推送部署由 Luma Build 插件提供，启用后自动接入 Control 单点登录。",
    enDesc: "Cloning Git repositories, multi-runner remote image builds, and build queues are provided by the pluggable Luma Build addon.",
    zhBullets: [
      "支持 GitHub / Gitea 私有仓库导入与多架构 BuildKit 缓存",
      "复用 Control 已保存的镜像仓库与 Git 凭据，无需重复填写密码",
      "与 Control 统一 Passkey / CLI Token 鉴权，独立控制台开箱即用",
    ],
    enBullets: [
      "GitHub / Gitea imports with multi-runner BuildKit caching",
      "Reuses saved Control registry & Git credentials with zero duplicate passwords",
      "Unified Control Passkey and CLI token SSO out of the box",
    ],
    cli: "luma addon install builder --domain build.example.com",
  },
  observe: {
    name: "Luma Observe",
    zhTitle: "需要安装 Luma Observe 插件",
    enTitle: "Luma Observe addon required",
    zhDesc: "历史日志检索（VictoriaLogs）、分布式链路追踪（Tempo）、Grafana 应用看板与 HTTP/日志告警由 Luma Observe 插件提供。节点资源指标与实时容器日志仍由 Control 原生提供。",
    enDesc: "Historical log search (VictoriaLogs), distributed traces (Tempo), Grafana dashboards, and HTTP/log alerts are provided by Luma Observe. Node metrics and live container logs remain built into Control.",
    zhBullets: [
      "自动接入 Control forwardAuth 单点登录，无需单独设置 Grafana 账号",
      "解锁应用详情中的「看板」Tab、历史日志检索、Tempo 链路追踪与采集状态",
      "扩展 HTTP 延迟/5xx、日志关键字与后台任务告警规则",
    ],
    enBullets: [
      "Zero-config Control forwardAuth SSO for /grafana",
      "Unlocks application dashboards, historical log search, Tempo traces, and pipeline health",
      "Adds HTTP latency/5xx, log matching, and background-task alert presets",
    ],
    cli: "luma addon install observe",
  },
  eye: {
    name: "Luma Eye",
    zhTitle: "需要安装 Luma Eye 插件",
    enTitle: "Luma Eye addon required",
    zhDesc: "前端访问统计（UV/PV）、Web Vitals、崩溃堆栈 SourceMap 还原与 1:1 会话回放由 Luma Eye 插件提供。",
    enDesc: "Frontend product analytics (UV/PV), Web Vitals, symbolicated crash tracking, and DOM session replay are provided by Luma Eye.",
    zhBullets: [
      "全自动生成内部密钥并接入 Control 统一单点登录，只需填写 1 个域名",
      "在「应用详情」中自动关联对应前端项目，实时查看 UV/PV 与无崩溃率",
      "兼容 Sentry SDK、自带轻量 JS/React/RN SDK 与 `luma eye` CLI",
    ],
    enBullets: [
      "Auto-generates internal secrets and wires Control SSO — only a domain is required",
      "Auto-matches Luma apps in Application Detail with live UV/PV and crash-free rate",
      "Includes Sentry-compatible crash ingestion, lightweight SDKs, and `luma eye` CLI",
    ],
    cli: "luma addon install eye --domain eye.example.com",
  },
};

export function AddonRequiredCard({
  addonId,
  lang,
  featureTitle,
}: {
  addonId: "builder" | "observe" | "eye";
  lang: Lang;
  featureTitle?: string;
}) {
  const zh = lang === "zh";
  const { navigate } = useRouter();
  const [copied, setCopied] = useState(false);
  const meta = ADDON_META[addonId];
  const Icon = addonId === "builder" ? Boxes : addonId === "observe" ? Activity : Package;
  const targetPath = `/addons?install=${encodeURIComponent(addonId)}`;

  const copyCli = async () => {
    try {
      await navigator.clipboard.writeText(meta.cli);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // clipboard unavailable
    }
  };

  return (
    <Card className="mx-auto w-full max-w-2xl">
      <CardHeader className="gap-2">
        <div className="flex items-center gap-2.5">
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted ring-1 ring-border">
            <Icon className="size-4 text-primary" aria-hidden="true" />
          </span>
          <div>
            <CardTitle className="text-base">{featureTitle ? `${featureTitle} · ${meta.name}` : zh ? meta.zhTitle : meta.enTitle}</CardTitle>
            <CardDescription className="mt-0.5 text-xs">{zh ? "可插拔官方插件能力" : "Optional official addon capability"}</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-sm leading-relaxed text-muted-foreground">{zh ? meta.zhDesc : meta.enDesc}</p>
        <ul className="flex flex-col gap-1.5 text-sm text-muted-foreground">
          {(zh ? meta.zhBullets : meta.enBullets).map((item) => (
            <li key={item} className="flex items-start gap-2">
              <span className="mt-2 size-1.5 shrink-0 rounded-full bg-primary/70" aria-hidden="true" />
              <span>{item}</span>
            </li>
          ))}
        </ul>
        <div className="flex items-center justify-between gap-2 rounded-lg border border-dashed border-border bg-muted/40 px-3 py-1.5 font-mono text-xs text-muted-foreground">
          <span className="truncate">{meta.cli}</span>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            onClick={() => void copyCli()}
            className="shrink-0 text-muted-foreground hover:text-foreground"
            aria-label={zh ? "复制命令" : "Copy command"}
          >
            {copied ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" nativeButton={false} render={<a {...spaLink(targetPath, navigate)} />}>
            {zh ? `前往插件中心安装 ${meta.name}` : `Install ${meta.name} in Addons Hub`}
            <ArrowRight className="ml-1 size-3.5" aria-hidden="true" />
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
