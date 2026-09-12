import { useId, useState } from "react";
import { Search, ArrowRight } from "lucide-react";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Table, TableBody, TableCell, TableHead, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Button } from "@/components/ui/button";

import type { Lang } from "../types";
import type { DeployMode, DeployTemplate } from "./types";
import { deployTemplateDescription, deployTemplateName } from "./templates";
import lumaLogoMark from "../assets/luma-logo-mark.png";

const TEMPLATE_LOGOS: Record<string, { src: string; initials: string; label: string }> = {
  "service-custom": { src: lumaLogoMark, initials: "L", label: "Luma" },
  "compose-custom": { src: lumaLogoMark, initials: "L", label: "Luma" },
  "service-whoami": { src: "https://cdn.simpleicons.org/traefikproxy", initials: "T", label: "Traefik" },
  "service-nginx": { src: "https://cdn.simpleicons.org/nginx", initials: "N", label: "nginx" },
  "service-redis-worker": { src: "https://cdn.simpleicons.org/redis", initials: "R", label: "Redis" },
  "service-grafana": { src: "https://cdn.simpleicons.org/grafana", initials: "G", label: "Grafana" },
  "service-minio": { src: "https://cdn.simpleicons.org/minio", initials: "M", label: "MinIO" },
  "service-jellyfin": { src: "https://cdn.simpleicons.org/jellyfin", initials: "J", label: "Jellyfin" },
  "service-code-server": { src: "https://cdn.simpleicons.org/coder", initials: "C", label: "Coder" },
  "compose-uptime-kuma": { src: "https://cdn.simpleicons.org/uptimekuma", initials: "UK", label: "Uptime Kuma" },
  "compose-vaultwarden": { src: "https://cdn.simpleicons.org/vaultwarden", initials: "VW", label: "Vaultwarden" },
  "compose-gitea": { src: "https://cdn.simpleicons.org/gitea", initials: "GT", label: "Gitea" },
  "compose-n8n": { src: "https://cdn.simpleicons.org/n8n", initials: "N8", label: "n8n" },
  "compose-nextcloud": { src: "https://cdn.simpleicons.org/nextcloud", initials: "NC", label: "Nextcloud" },
  "compose-ghost": { src: "https://cdn.simpleicons.org/ghost", initials: "Gh", label: "Ghost" },
  "compose-paperless-ngx": { src: "https://cdn.simpleicons.org/paperlessngx", initials: "P", label: "Paperless-ngx" },
  "compose-stirling-pdf": { src: "https://raw.githubusercontent.com/Stirling-Tools/Stirling-PDF/204bae3bc1a693de09c68cbe23e2bf2376b2f10c/docs/stirling.svg", initials: "PDF", label: "Stirling PDF" },
};

function logoFor(template: DeployTemplate) {
  return TEMPLATE_LOGOS[template.id] || { src: lumaLogoMark, initials: template.name.slice(0, 2), label: template.name };
}

function BrandIcon({ template }: { template: DeployTemplate }) {
  const logo = logoFor(template);
  return <Avatar>
    <AvatarImage src={logo.src} alt={`${logo.label} logo`} loading="lazy" referrerPolicy="no-referrer" />
    <AvatarFallback>{logo.initials}</AvatarFallback>
  </Avatar>;
}

function volumeModeSummary(template: DeployTemplate, lang: Lang) {
  const volumes = template.compose?.volumes || [];
  if (!volumes.length) return lang === "zh" ? "无命名卷" : "no named volumes";
  const counts = volumes.reduce<Record<string, number>>((current, volume) => {
    const key = volume.storageMode || "unmanaged";
    current[key] = (current[key] || 0) + 1;
    return current;
  }, {});
  return Object.entries(counts).map(([key, count]) => `${count} ${key}`).join(" / ");
}

function templateFacts(template: DeployTemplate, lang: Lang) {
  if (template.mode === "service" && template.service) {
    const service = template.service;
    return [
      { label: "Image", value: service.image || "image required" },
      { label: lang === "zh" ? "位置" : "Target", value: `${service.region} / ${service.exposure}` },
      { label: lang === "zh" ? "入口" : "Route", value: service.exposure === "none" ? (lang === "zh" ? "内部访问" : "internal only") : `${service.domain}:${service.port}` },
      { label: lang === "zh" ? "副本" : "Scale", value: `${service.replicas} ${lang === "zh" ? "副本" : "replica"}` },
    ];
  }

  const compose = template.compose;
  if (!compose) return [];
  const exposed = compose.services.filter((service) => service.exposure !== "none");
  return [
    { label: lang === "zh" ? "服务" : "Services", value: `${compose.services.length} ${lang === "zh" ? "服务" : "services"}` },
    { label: lang === "zh" ? "位置" : "Target", value: `${compose.region} / ${exposed.length ? exposed.map((service) => service.exposure).join(", ") : "none"}` },
    { label: lang === "zh" ? "入口" : "Route", value: exposed.length ? exposed.map((service) => `${service.domain}:${service.port}`).join(", ") : (lang === "zh" ? "内部访问" : "internal only") },
    { label: lang === "zh" ? "存储" : "Storage", value: volumeModeSummary(template, lang) },
  ];
}

export function DeployTemplates({
  lang,
  mode,
  templates,
  onModeChange,
  onSelect,
}: {
  lang: Lang;
  mode: DeployMode;
  templates: DeployTemplate[];
  activeId: string;
  onModeChange: (mode: DeployMode) => void;
  onSelect: (template: DeployTemplate) => void;
}) {
  const [query, setQuery] = useState("");
  const zh = lang === "zh";
  const modeTemplates = templates.filter((template) => template.mode === mode);
  const visibleTemplates = modeTemplates.filter((template) => `${deployTemplateName(template, lang)} ${deployTemplateDescription(template, lang)} ${template.service?.image || ""}`.toLowerCase().includes(query.trim().toLowerCase()));
  const searchId = useId();
  return <section className="flex min-w-0 flex-col gap-6" aria-label={zh ? "模板库" : "Template library"}>
    <Card>
      <CardHeader>
        <CardTitle>{zh ? "选择模板" : "Choose a template"}</CardTitle>
        <CardDescription>{zh ? "选择后可调整配置，再进行部署。" : "Customize the configuration before deploying."}</CardDescription>
      </CardHeader>
      <CardContent className="@container">
        <FieldGroup className="grid grid-cols-1 gap-4 @md:grid-cols-2">
          <Field>
            <FieldLabel id={`${searchId}-type`}>{zh ? "模板类型" : "Template type"}</FieldLabel>
            <ToggleGroup aria-labelledby={`${searchId}-type`} variant="outline" value={[mode]} onValueChange={(values) => { if (values[0] === "service" || values[0] === "compose") onModeChange(values[0]); }}>
              <ToggleGroupItem value="service">{zh ? "单服务" : "Service"}<Badge variant="secondary">{templates.filter((template) => template.mode === "service").length}</Badge></ToggleGroupItem>
              <ToggleGroupItem value="compose">Compose<Badge variant="secondary">{templates.filter((template) => template.mode === "compose").length}</Badge></ToggleGroupItem>
            </ToggleGroup>
          </Field>
          <Field>
            <FieldLabel htmlFor={searchId}>{zh ? "搜索模板" : "Search templates"}</FieldLabel>
            <InputGroup>
              <InputGroupAddon><Search aria-hidden="true" /></InputGroupAddon>
              <InputGroupInput id={searchId} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={zh ? "搜索名称、镜像或用途…" : "Search name, image or use…"} />
            </InputGroup>
          </Field>
        </FieldGroup>
      </CardContent>
    </Card>
    <div className="grid min-w-0 grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {visibleTemplates.map((template) => <Card key={template.id} className="min-w-0">
        <CardHeader>
          <div className="mb-2 flex items-center gap-3"><BrandIcon template={template} /><CardTitle>{deployTemplateName(template, lang)}</CardTitle></div>
          <CardDescription>{deployTemplateDescription(template, lang)}</CardDescription>
        </CardHeader>
        <CardContent className="mt-auto">
          <Table aria-label={`${deployTemplateName(template, lang)} ${zh ? "模板配置" : "configuration"}`}>
            <TableBody>{templateFacts(template, lang).map((fact) => <TableRow key={fact.label}><TableHead scope="row" className="w-20">{fact.label}</TableHead><TableCell className="max-w-0 truncate" title={fact.value}>{fact.value}</TableCell></TableRow>)}</TableBody>
          </Table>
        </CardContent>
        <CardFooter className="justify-end"><Button variant="outline" size="sm" onClick={() => onSelect(template)} aria-label={`${zh ? "使用模板" : "Use template"} ${deployTemplateName(template, lang)}`}>{zh ? "使用模板" : "Use template"}<ArrowRight data-icon="inline-end" /></Button></CardFooter>
      </Card>)}
    </div>
    {visibleTemplates.length === 0 ? <Empty>
      <EmptyHeader><EmptyMedia variant="icon"><Search aria-hidden="true" /></EmptyMedia><EmptyTitle>{zh ? "没有匹配的模板" : "No matching templates"}</EmptyTitle><EmptyDescription>{zh ? "试试其他关键词，或切换模板类型。" : "Try another keyword or template type."}</EmptyDescription></EmptyHeader>
      <EmptyContent><Button variant="outline" size="sm" onClick={() => setQuery("")}>{zh ? "清空搜索" : "Clear search"}</Button></EmptyContent>
    </Empty> : null}
  </section>;
}
