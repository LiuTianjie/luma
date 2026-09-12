import type { DetailState } from "./detailRecords";
import { ArrowLeft, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import type { Lang } from "./types";
import { detailLabel, t } from "./i18n";
import { PageHeader } from "./pages/PageHeader";

// Units follow nodeDetail's explicit metric mapping. Other fields retain their
// original representation; load1 is not a percentage and CPU capacity is not usage.
function displayValue(kind: "node" | "service", key: string, value: string | number | boolean | undefined): string {
  if (kind === "node" && typeof value === "number") {
    if (["cpu", "memory"].includes(key)) return Number.isFinite(value) ? `${value}%` : "-";
    if (["memoryTotal", "memoryCapacity"].includes(key)) {
      if (!Number.isFinite(value) || value < 0) return "-";
      if (value === 0) return "0 B";
      const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
      const unit = Math.min(Math.max(0, Math.floor(Math.log(value) / Math.log(1024))), units.length - 1);
      return `${Number((value / 1024 ** unit).toFixed(2))} ${units[unit]}`;
    }
  }
  return String(value ?? "-");
}

export function DetailDrawer({ lang, detail, onClose, inline = false, showBack = true }: { lang: Lang; detail: DetailState; onClose: () => void; inline?: boolean; showBack?: boolean }) {
  if (!detail) return null;
  const values = (
    <dl className={cn("grid min-w-0 grid-cols-1 gap-4", inline && "sm:grid-cols-2 lg:grid-cols-3")}>
      {Object.entries(detail.items).map(([key, value]) => (
        <div className="flex min-w-0 flex-col gap-1" key={key}>
          <dt className="text-sm text-muted-foreground">{detailLabel(lang, key)}</dt>
          <dd className="m-0 min-w-0 text-sm wrap-break-word">{displayValue(detail.kind, key, value)}</dd>
        </div>
      ))}
    </dl>
  );
  if (inline) return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader meta={{
        eyebrow: t(lang, "details"),
        title: detail.title,
        description: "",
        metrics: [],
        action: showBack ? <Button variant="outline" type="button" onClick={onClose}><ArrowLeft data-icon="inline-start" />{lang === "zh" ? "返回列表" : "Back to list"}</Button> : undefined,
      }} />
      <Card>
        <CardHeader><CardTitle>{t(lang, "details")}</CardTitle></CardHeader>
        <CardContent>{values}</CardContent>
      </Card>
    </div>
  );
  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent showCloseButton={false} className="data-[side=right]:w-full data-[side=right]:sm:max-w-lg">
        <SheetHeader className="flex-row items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-1">
            <SheetTitle className="line-clamp-3 wrap-break-word" title={detail.title}>{detail.title}</SheetTitle>
            <SheetDescription>{t(lang, "details")}</SheetDescription>
          </div>
          <SheetClose render={<Button variant="ghost" size="icon" aria-label={t(lang, "close")} />}>
            <X data-icon="inline-start" />
          </SheetClose>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1">
          <div className="px-4 pb-4">{values}</div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
