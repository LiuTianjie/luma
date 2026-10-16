import { useState } from "react";
import { CheckCircle2, Clock, Layers, Rocket, Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { CodeCell } from "../components/primitives";
import type { BuildStepTiming, Lang } from "../types";

export function BuildWaterfallView({
  lang,
  artifactId,
  application,
  digest,
  steps,
  onDeployArtifact,
}: {
  lang: Lang;
  artifactId?: string;
  application?: string;
  digest?: string;
  steps?: BuildStepTiming[];
  onDeployArtifact?: (artifactId: string, environment: string) => void;
}) {
  const zh = lang === "zh";
  const [selectedEnv, setSelectedEnv] = useState<"staging" | "prod">("prod");
  const [promotedNotice, setPromotedNotice] = useState("");

  const resolvedSteps: BuildStepTiming[] =
    steps && steps.length
      ? steps
      : [
          {
            stepId: "stage-0",
            name: "FROM docker.io/library/node:20-alpine",
            durationMs: 120,
            cached: true,
          },
          {
            stepId: "stage-1",
            name: "RUN sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' && apk add --no-cache libc6-compat",
            durationMs: 240,
            cached: true,
          },
          {
            stepId: "stage-2",
            name: "COPY package.json pnpm-lock.yaml ./ && RUN --mount=type=cache pnpm install --frozen-lockfile",
            durationMs: 410,
            cached: true,
          },
          {
            stepId: "stage-3",
            name: "COPY . . && RUN pnpm run build",
            durationMs: 4820,
            cached: false,
          },
          {
            stepId: "stage-4",
            name: "EXPORT OCI image + Zot registry cache manifest (type=registry,mode=max)",
            durationMs: 910,
            cached: false,
          },
        ];

  const cachedCount = resolvedSteps.filter((s) => s.cached).length;
  const cacheHitPercent = Math.round(
    (cachedCount / Math.max(resolvedSteps.length, 1)) * 100,
  );
  const totalDurationMs = resolvedSteps.reduce(
    (sum, s) => sum + Math.max(s.durationMs, 0),
    0,
  );
  const maxStepMs = Math.max(...resolvedSteps.map((s) => s.durationMs), 1);
  const resolvedArtifactId =
    artifactId || `art-${application || "app"}-${(digest || "sha256").slice(-8)}`;

  const triggerZeroRebuildDeploy = () => {
    onDeployArtifact?.(resolvedArtifactId, selectedEnv);
    setPromotedNotice(
      zh
        ? `已触发零重构发布：直接将不可变制品 ${resolvedArtifactId} 部署至 ${selectedEnv} 环境（跳过镜像构建）。`
        : `Zero-rebuild release triggered: deploying immutable artifact ${resolvedArtifactId} directly to ${selectedEnv} without rebuilding.`,
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          <Layers className="size-4" />
          <span>
            {zh
              ? "BuildKit 阶段耗时瀑布图与 OCI 制品卡片"
              : "BuildKit Stage Waterfall & Immutable OCI Artifact"}
          </span>
        </CardTitle>
        <CardDescription>
          {zh
            ? "Tekton + 远程 BuildKit 池分阶段构建分析，支持 Zot Registry 远程缓存命中分析与零重构环境晋升。"
            : "Tekton + Remote BuildKit stage-by-stage timing with Zot Registry cache hit analysis and zero-rebuild environment promotion."}
        </CardDescription>
        <CardAction className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">
            <Zap data-icon="inline-start" />
            {zh ? `缓存命中率 ${cacheHitPercent}%` : `Cache hit ${cacheHitPercent}%`}
          </Badge>
          <Badge variant="outline">
            <Clock data-icon="inline-start" />
            {(totalDurationMs / 1000).toFixed(1)}s
          </Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid grid-cols-1 gap-3 rounded-lg border bg-muted/30 p-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">
              {zh ? "不可变制品 ID (Artifact ID)" : "Immutable Artifact ID"}
            </span>
            <CodeCell value={resolvedArtifactId} />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">
              {zh ? "OCI 镜像摘要 (Zot Pin 保护)" : "OCI Digest (Zot Pin Protected)"}
            </span>
            <CodeCell
              value={
                digest ||
                "sha256:8f4b2e9c1a7d3e5f6a0b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f"
              }
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-xs text-muted-foreground">
              {zh ? "APT / APK 镜像加速" : "APT / APK Mirror Acceleration"}
            </span>
            <span className="text-xs font-medium">
              {zh
                ? "已启用 HTTPS 镜像源重写与 BuildKit 远程层缓存"
                : "HTTPS mirror rewrite & BuildKit registry cache active"}
            </span>
          </div>
        </div>

        <div className="flex flex-col gap-2.5" role="list" aria-label={zh ? "BuildKit 构建阶段" : "BuildKit stages"}>
          {resolvedSteps.map((step) => {
            const widthPct = Math.max(
              6,
              Math.round((step.durationMs / maxStepMs) * 100),
            );
            return (
              <div
                key={step.stepId}
                role="listitem"
                className="flex flex-col gap-1.5 rounded-md border p-2.5"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <Badge variant={step.cached ? "secondary" : "outline"}>
                      {step.cached
                        ? zh
                          ? "CACHED 命中"
                          : "CACHED"
                        : zh
                          ? "REBUILT 重构"
                          : "REBUILT"}
                    </Badge>
                    <code className="truncate font-mono text-xs" title={step.name}>
                      {step.name}
                    </code>
                  </div>
                  <span className="font-mono text-xs text-muted-foreground tabular-nums">
                    {step.durationMs >= 1000
                      ? `${(step.durationMs / 1000).toFixed(2)}s`
                      : `${step.durationMs}ms`}
                  </span>
                </div>
                <Progress
                  value={widthPct}
                  aria-label={`${step.name} (${step.durationMs}ms)`}
                />
              </div>
            );
          })}
        </div>

        {promotedNotice ? (
          <Alert role="status">
            <CheckCircle2 />
            <AlertTitle>
              {zh ? "零重构发布已提交" : "Zero-rebuild deployment submitted"}
            </AlertTitle>
            <AlertDescription>{promotedNotice}</AlertDescription>
          </Alert>
        ) : null}
      </CardContent>
      <CardFooter className="flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>
            {zh
              ? "构建与部署已彻底解耦：可直接将此 Artifact 晋升至目标环境，无需重新执行构建。"
              : "Build and Deploy are decoupled: promote this Artifact directly without rebuilding."}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant={selectedEnv === "staging" ? "secondary" : "outline"}
            size="sm"
            onClick={() => setSelectedEnv("staging")}
          >
            Staging
          </Button>
          <Button
            type="button"
            variant={selectedEnv === "prod" ? "secondary" : "outline"}
            size="sm"
            onClick={() => setSelectedEnv("prod")}
          >
            Production
          </Button>
          <Button type="button" size="sm" onClick={triggerZeroRebuildDeploy}>
            <Rocket data-icon="inline-start" />
            {zh
              ? `直接部署至 ${selectedEnv}（零重构）`
              : `Deploy Artifact to ${selectedEnv} (Zero Rebuild)`}
          </Button>
        </div>
      </CardFooter>
    </Card>
  );
}
