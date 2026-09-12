import { Badge } from "@/components/ui/badge";
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { DeployStep } from "./types";
import type { Lang } from "../types";

// Shared component for streamed build/deploy events and saved delivery records.
export function StepLog({ steps, lang, variant = "compact", waitingLabel, keyPrefix = "" }: {
  steps: DeployStep[];
  lang?: Lang;
  variant?: "compact" | "plain";
  waitingLabel?: string;
  keyPrefix?: string;
}) {
  const named = steps.filter((step) => step.name);
  const zh = (lang ?? "zh") === "zh";
  if (!named.length) return waitingLabel !== undefined ? <Empty>
    <EmptyHeader><Spinner aria-hidden="true" /><EmptyTitle>{waitingLabel || (zh ? "等待日志事件" : "Waiting for events")}</EmptyTitle></EmptyHeader>
  </Empty> : null;
  return <ScrollArea className="min-w-0 [&_[data-slot=scroll-area-viewport]]:max-h-96" aria-label={zh ? "步骤日志" : "Step log"}>
    <Table className="table-fixed">
      <TableHeader><TableRow><TableHead className="w-28">{zh ? "状态" : "Status"}</TableHead><TableHead className="w-1/4">{zh ? "步骤" : "Step"}</TableHead><TableHead>{zh ? "详情" : "Details"}</TableHead></TableRow></TableHeader>
      <TableBody>{named.map((step, index) => <TableRow key={`${keyPrefix}${step.name}-${index}`}>
        <TableCell className="align-top"><Badge variant={["error", "failed", "fail"].includes(step.status || "") ? "destructive" : "secondary"} className="max-w-full" title={step.status || "-"}><span className="truncate">{step.status || "-"}</span></Badge></TableCell>
        <TableCell className="whitespace-normal wrap-anywhere align-top">{step.name}</TableCell>
        <TableCell className="whitespace-pre-wrap wrap-anywhere align-top">{step.message ? (variant === "compact" ? `- ${step.message}` : step.message) : "-"}</TableCell>
      </TableRow>)}</TableBody>
    </Table>
  </ScrollArea>;
}
