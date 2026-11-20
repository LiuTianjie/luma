import { AlertTriangle, HardDrive } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { t } from "../i18n";
import type { DashboardVolume, Lang } from "../types";
import { CodeCell, PrimaryCell } from "./primitives";

export function StoragePanel({
  lang,
  volumes,
  warnings,
}: {
  lang: Lang;
  volumes: DashboardVolume[];
  warnings: string[];
}) {
  const zh = lang === "zh";
  return (
    <div className="flex min-w-0 flex-col gap-6" id="section-6">
      {warnings.length ? <Alert><AlertTriangle /><AlertTitle>{zh ? "存储提示" : "Storage notice"}</AlertTitle><AlertDescription>{warnings.map((warning) => <p key={warning}>{warning}</p>)}</AlertDescription></Alert> : null}
      <Card>
        <CardHeader><CardTitle>{t(lang, "volume")}</CardTitle><CardDescription>{zh ? "查看卷来源、节点绑定与消费服务。" : "Review volume sources, node bindings, and consuming services."}</CardDescription><CardAction><Badge variant="secondary">{volumes.length}</Badge></CardAction></CardHeader>
        <CardContent>
          {volumes.length ? <Table containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "存储卷，可横向滚动" : "Storage volumes, horizontally scrollable" }} className="min-w-[800px]">
              <TableHeader><TableRow><TableHead>{t(lang, "volume")}</TableHead><TableHead>{t(lang, "kind")}</TableHead><TableHead>{t(lang, "node")}</TableHead><TableHead>{zh ? "端点" : "Endpoint"}</TableHead><TableHead>{zh ? "路径" : "Path"}</TableHead><TableHead>{t(lang, "services")}</TableHead></TableRow></TableHeader>
              <TableBody>{volumes.map((volume, index) => <TableRow key={`${volume.name || "volume"}-${index}`}>
                <TableCell><PrimaryCell title={volume.name || "—"} /></TableCell>
                <TableCell><Badge variant={volume.kind === "unmanaged" ? "outline" : "secondary"}>{volume.kind || "unmanaged"}</Badge></TableCell>
                <TableCell><Badge variant="outline">{volume.node || "—"}</Badge></TableCell>
                <TableCell><CodeCell value={volume.endpoint || "—"} /></TableCell>
                <TableCell><CodeCell value={volume.networkPath || "—"} /></TableCell>
                <TableCell><span className="flex max-w-64 flex-wrap gap-1">{volume.services?.length ? volume.services.map((service) => <Badge key={service} variant="secondary" title={service}><span className="max-w-60 truncate">{service}</span></Badge>) : "—"}</span></TableCell>
              </TableRow>)}</TableBody>
            </Table> : <Empty><EmptyHeader><EmptyMedia variant="icon"><HardDrive /></EmptyMedia><EmptyTitle>{zh ? "暂无存储卷" : "No storage volumes"}</EmptyTitle><EmptyDescription>{zh ? "应用声明存储卷后会显示在这里。" : "Volumes declared by applications appear here."}</EmptyDescription></EmptyHeader></Empty>}
        </CardContent>
      </Card>
    </div>
  );
}
