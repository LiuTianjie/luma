import type { AlertIncidentDetail, AlertSource, AlertSourceSilence } from "../alertingApi";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const sourceLabel = (kind: AlertSource["kind"], zh: boolean) =>
  ({ task: zh ? "任务" : "Task", service: zh ? "服务" : "Service", target: zh ? "对象" : "Target" })[kind];

export function AlertSourceControls({ detail, zh, busy, stamp, onSilence, onRemove }: {
  detail: AlertIncidentDetail; zh: boolean; busy: boolean; stamp: (at: number, zh: boolean) => string;
  onSilence: (sourceKey: string, seconds: number) => void;
  onRemove: (sourceKey: string) => void;
}) {
  const active = ["pending", "firing"].includes(detail.incident.status);
  if (!detail.sources?.length) return null;
  return <section className="flex flex-col gap-3" aria-labelledby="alert-source-heading">
    <div>
      <h3 id="alert-source-heading" className="text-sm font-medium">{zh ? "按来源限时静默" : "Silence a source"}</h3>
      <p className="text-sm text-muted-foreground">{zh
        ? "范围限定为当前规则和对象下的所选来源。其他来源照常通知，到期自动恢复提醒。"
        : "Applies to this source within this rule and target. Other sources still notify; reminders resume at expiry."}</p>
    </div>
    {detail.sources.map((item) => {
      const until = detail.sourceSilences?.[item.key] || 0;
      return <div key={item.key} className="flex flex-wrap items-center gap-3 rounded-lg border border-hairline p-3">
        <div className="min-w-0 flex-1">
          <p className="text-xs text-muted-foreground">{sourceLabel(item.kind, zh)}</p>
          <p className="font-mono text-sm wrap-anywhere" translate="no">{item.name}</p>
          {until > Date.now() / 1000 && <p className="text-sm text-muted-foreground">{zh ? "静默至" : "Silenced until"} {stamp(until, zh)}</p>}
        </div>
        {until > Date.now() / 1000 ? <Button size="sm" variant="outline" disabled={busy} onClick={() => onRemove(item.key)}>
          {zh ? "提前解除" : "Resume now"}
        </Button> : active && <div className="flex flex-wrap gap-2">
          {[1, 6, 24].map((hours) => <Button key={hours} size="sm" variant="outline" disabled={busy}
            onClick={() => onSilence(item.key, hours * 3600)}>
            {zh ? `静默 ${hours} 小时` : `Silence ${hours}h`}
          </Button>)}
        </div>}
      </div>;
    })}
  </section>;
}

export function ActiveSourceSilences({ items, zh, busy, stamp, onRemove }: {
  items: AlertSourceSilence[]; zh: boolean; busy: boolean; stamp: (at: number, zh: boolean) => string;
  onRemove: (item: AlertSourceSilence) => void;
}) {
  const active = items.filter((item) => item.until > Date.now() / 1000);
  if (!active.length) return null;
  return <Card>
    <CardHeader><CardTitle>{zh ? "已静默的来源" : "Silenced sources"}</CardTitle>
      <CardDescription>{zh ? "告警继续记录，静默到期后自动恢复通知。" : "Incidents remain recorded. Notifications resume automatically at expiry."}</CardDescription>
    </CardHeader>
    <CardContent className="flex flex-col gap-3">
      {active.map((item) => <div key={`${item.ruleId}:${item.target}:${item.sourceKey}`} className="flex flex-wrap items-center gap-3 rounded-lg border border-hairline p-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm">{item.ruleName} · {item.target}</p>
          <p className="font-mono text-sm wrap-anywhere" translate="no">{item.name}</p>
          <p className="text-xs text-muted-foreground">{sourceLabel(item.kind, zh)} · {zh ? "静默至" : "Until"} {stamp(item.until, zh)}</p>
        </div>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onRemove(item)}>{zh ? "提前解除" : "Resume now"}</Button>
      </div>)}
    </CardContent>
  </Card>;
}
