import { useState } from "react";
import type { StackFrame } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { cn } from "@/lib/utils";

interface StackTraceViewerProps {
  frames: StackFrame[];
}

export function StackTraceViewer({ frames }: StackTraceViewerProps) {
  const safeFrames = frames || [];
  const hasInApp = safeFrames.some((f) => f.in_app);
  const [onlyInApp, setOnlyInApp] = useState(false);

  const visibleFrames = onlyInApp && hasInApp ? safeFrames.filter((f) => f.in_app) : safeFrames;

  if (safeFrames.length === 0) {
    return (
      <Empty className="py-8">
        <EmptyHeader>
          <EmptyTitle>未采集到结构化调用堆栈</EmptyTitle>
          <EmptyDescription>
            非 Error 实例或主动上报的快照事件可能不包含堆栈帧。
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span className="tabular-nums">
          共 {safeFrames.length} 个调用帧 · {safeFrames.filter((f) => f.symbolicated).length} 帧已完成 SourceMap 源码还原
        </span>
        {hasInApp ? (
          <Button
            type="button"
            size="xs"
            variant={onlyInApp ? "secondary" : "outline"}
            aria-pressed={onlyInApp}
            onClick={() => setOnlyInApp((v) => !v)}
          >
            {onlyInApp ? "仅业务代码帧 (in_app)" : "隐藏第三方依赖帧"}
          </Button>
        ) : null}
      </div>

      <div className="flex flex-col divide-y divide-hairline overflow-hidden rounded-lg border border-border bg-card">
        {visibleFrames.map((f, idx) => {
          const startLine = f.lineno - (f.pre_context?.length || 0);
          return (
            <div
              key={`${f.filename}-${f.lineno}-${idx}`}
              className={cn("flex flex-col gap-2 px-4 py-3", !f.in_app && "bg-muted/30 opacity-80")}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0 font-mono text-xs wrap-anywhere">
                  <strong className="font-semibold text-foreground">{f.function || "<anonymous>"}</strong>
                  <span className="text-muted-foreground"> at </span>
                  <span className="text-muted-foreground">
                    {f.filename}:{f.lineno}:{f.colno}
                  </span>
                </div>
                <div className="flex items-center gap-1.5">
                  {f.in_app ? <Badge variant="secondary">in_app</Badge> : null}
                  {f.symbolicated ? (
                    <Badge variant="success" appearance="dot">
                      SourceMap
                    </Badge>
                  ) : null}
                </div>
              </div>

              {f.context_line ? (
                <div className="overflow-x-auto rounded-md border border-border bg-muted/60 py-1.5 font-mono text-[0.75rem]">
                  {(f.pre_context || []).map((line, i) => (
                    <div key={`pre-${i}`} className="flex px-3 py-0.5 text-muted-foreground">
                      <span className="w-10 shrink-0 select-none text-right pr-3 opacity-60 tabular-nums">
                        {startLine + i}
                      </span>
                      <span className="whitespace-pre">{line}</span>
                    </div>
                  ))}
                  <div className="flex bg-destructive-soft px-3 py-0.5 font-medium text-destructive-foreground">
                    <span className="w-10 shrink-0 select-none text-right pr-3 tabular-nums">
                      {f.lineno}
                    </span>
                    <span className="whitespace-pre">{f.context_line}</span>
                  </div>
                  {(f.post_context || []).map((line, i) => (
                    <div key={`post-${i}`} className="flex px-3 py-0.5 text-muted-foreground">
                      <span className="w-10 shrink-0 select-none text-right pr-3 opacity-60 tabular-nums">
                        {f.lineno + 1 + i}
                      </span>
                      <span className="whitespace-pre">{line}</span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
