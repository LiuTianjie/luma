import { useEffect, useState } from "react";
import type { AlertRule } from "../alertingApi";
import { postAlerting } from "../alertingApi";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel, FieldDescription } from "@/components/ui/field";

type Preview = { status: string; value?: number | null; total?: number; sampledAt?: number; sample?: string };
export function LogRuleFields({ rule, onChange, token, zh, disabled }: {
  rule: AlertRule; onChange: (rule: AlertRule) => void; token: string; zh: boolean; disabled: boolean;
}) {
  const query = { windowSeconds: 300, task: "", contains: "", exclude: "", field: "", equals: "", ...rule.logQuery };
  const [result, setResult] = useState<Preview>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const signature = JSON.stringify([rule.target, query]);
  useEffect(() => { setResult(undefined); setError(""); }, [signature]);
  useEffect(() => {
    if (!loading) return;
    const controller = new AbortController();
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await postAlerting<Preview>("log-preview", token, { target: rule.target, logQuery: query }, controller.signal);
        if (controller.signal.aborted) return;
        setResult(next);
        if (next.status === "pending" && ++attempts < 25) timer = setTimeout(poll, 2000);
        else setLoading(false);
      } catch (e) {
        if (!controller.signal.aborted) { setError(String(e)); setLoading(false); }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  // signature captures the exact request and cancels stale previews on edits.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, signature, token]);
  const update = (key: string, value: string | number) => onChange({ ...rule, logQuery: { ...query, [key]: value } });
  return <div className="sm:col-span-2 grid gap-4 sm:grid-cols-2">
    <p className="sm:col-span-2 text-sm text-muted-foreground">{zh
      ? "按应用和服务统计匹配日志条数。文本条件区分大小写，所有条件同时满足；JSON 字段支持点分路径。优先匹配错误摘要，避免重复计算堆栈。"
      : "Count matching log lines per application and service. Text is case-sensitive; all conditions must match. JSON fields support dotted paths. Match summary lines to avoid counting stack traces repeatedly."}</p>
    {([
      ["task", zh ? "服务名（留空包含该应用全部服务）" : "Task name (blank for all tasks in this app)"],
      ["contains", zh ? "日志包含文本" : "Log contains"],
      ["exclude", zh ? "排除包含文本的日志" : "Exclude text"],
      ["field", zh ? "JSON 字段（可选）" : "JSON field (optional)"],
      ["equals", zh ? "JSON 字段等于" : "JSON field equals"],
    ] as const).map(([key, label]) => <Field key={key}>
      <FieldLabel htmlFor={`log-${key}`}>{label}</FieldLabel>
      <Input id={`log-${key}`} disabled={disabled} maxLength={key === "field" ? 128 : key === "task" ? 300 : 256}
        value={query[key] || ""} onChange={e => update(key, e.target.value)} />
    </Field>)}
    <Field>
      <FieldLabel htmlFor="log-window">{zh ? "统计窗口（秒）" : "Window (seconds)"}</FieldLabel>
      <Input id="log-window" type="number" min={60} max={900} step={1} required disabled={disabled}
        value={Number.isFinite(query.windowSeconds) ? query.windowSeconds : ""} onChange={e => update("windowSeconds", e.target.value === "" ? NaN : Number(e.target.value))} />
      <FieldDescription>{zh ? "60–900 秒，评估间隔至少 60 秒。无日志或采集异常不视为恢复。阈值 0 表示出现一条即触发。" : "60–900 seconds; evaluated no more than once per minute. Missing logs or collection failures do not mean recovery. Threshold 0 fires on one match."}</FieldDescription>
    </Field>
    <div className="sm:col-span-2 flex flex-col items-start gap-2" aria-live="polite">
      <Button type="button" variant="outline" disabled={disabled || loading} onClick={() => { setError(""); setResult(undefined); setLoading(true); }}>
        {loading ? (zh ? "正在预览…" : "Previewing…") : (zh ? "预览最近日志统计" : "Preview recent log counts")}
      </Button>
      {result && <p className="text-sm">{result.status === "ok"
        ? (zh ? `匹配 ${result.value} / ${result.total} 条；${Number(result.value) > rule.threshold ? "超过" : "未超过"}阈值（持续时间另行判断）。` : `${result.value} / ${result.total} lines match; ${Number(result.value) > rule.threshold ? "above" : "below or at"} threshold (duration evaluated separately).`)
        : result.status === "pending" ? (zh ? "统计已排队，请稍后重新预览。" : "Preview queued; retry shortly.")
        : result.status === "busy" ? (zh ? "预览繁忙，请稍后重试。" : "Preview busy; retry shortly.")
        : result.status === "invalid_format" ? (zh ? "源日志中未找到指定 JSON 字段，请检查格式和字段名。" : "JSON field not found in source logs; check the field and format.")
        : result.status === "no_source_logs" ? (zh ? "窗口内没有源日志，无法判断业务是否恢复。" : "No source logs in this window; recovery is unknown.")
        : (zh ? "日志统计不可用：请确认 observe 已升级、日志采集正常；查询超时也会显示此状态。" : "Log statistics unavailable: check observe version and collection health; query timeouts also cause this state.")}</p>}
      {result?.sample && <pre className="max-w-full whitespace-pre-wrap break-all rounded border p-3 text-xs">{result.sample}</pre>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </div>
  </div>;
}
