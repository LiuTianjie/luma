import { useState, type CSSProperties } from "react";
import { Search, ArrowRight } from "lucide-react";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

import type { Lang } from "../types";
import type { DeployMode, DeployTemplate } from "./types";
import { deployTemplateDescription, deployTemplateName } from "./templates";
import lumaLogoMark from "../assets/luma-logo-mark.png";

const TEMPLATE_LOGOS: Record<string, { src: string; initials: string; accent: string; label: string }> = {
  "service-custom": { src: lumaLogoMark, initials: "L", accent: "#1ed760", label: "Luma" },
  "compose-custom": { src: lumaLogoMark, initials: "L", accent: "#1ed760", label: "Luma" },
  "service-whoami": { src: "https://cdn.simpleicons.org/traefikproxy", initials: "T", accent: "#24a1c1", label: "Traefik" },
  "service-nginx": { src: "https://cdn.simpleicons.org/nginx", initials: "N", accent: "#009639", label: "nginx" },
  "service-redis-worker": { src: "https://cdn.simpleicons.org/redis", initials: "R", accent: "#ff4438", label: "Redis" },
  "service-grafana": { src: "https://cdn.simpleicons.org/grafana", initials: "G", accent: "#f46800", label: "Grafana" },
  "service-minio": { src: "https://cdn.simpleicons.org/minio", initials: "M", accent: "#c72e49", label: "MinIO" },
  "service-jellyfin": { src: "https://cdn.simpleicons.org/jellyfin", initials: "J", accent: "#aa5cc3", label: "Jellyfin" },
  "service-code-server": { src: "https://cdn.simpleicons.org/coder", initials: "C", accent: "#ffffff", label: "Coder" },
  "compose-uptime-kuma": { src: "https://cdn.simpleicons.org/uptimekuma", initials: "UK", accent: "#5cdd8b", label: "Uptime Kuma" },
  "compose-vaultwarden": { src: "https://cdn.simpleicons.org/vaultwarden", initials: "VW", accent: "#175ddc", label: "Vaultwarden" },
  "compose-gitea": { src: "https://cdn.simpleicons.org/gitea", initials: "GT", accent: "#609926", label: "Gitea" },
  "compose-n8n": { src: "https://cdn.simpleicons.org/n8n", initials: "N8", accent: "#ea4b71", label: "n8n" },
  "compose-nextcloud": { src: "https://cdn.simpleicons.org/nextcloud", initials: "NC", accent: "#0082c9", label: "Nextcloud" },
  "compose-ghost": { src: "https://cdn.simpleicons.org/ghost", initials: "Gh", accent: "#ffffff", label: "Ghost" },
  "compose-paperless-ngx": { src: "https://cdn.simpleicons.org/paperlessngx", initials: "P", accent: "#22c55e", label: "Paperless-ngx" },
  "compose-stirling-pdf": { src: "https://raw.githubusercontent.com/Stirling-Tools/Stirling-PDF/204bae3bc1a693de09c68cbe23e2bf2376b2f10c/docs/stirling.svg", initials: "PDF", accent: "#ffb02e", label: "Stirling PDF" },
};

function logoFor(template: DeployTemplate) {
  return TEMPLATE_LOGOS[template.id] || { src: lumaLogoMark, initials: template.name.slice(0, 2), accent: "#1ed760", label: template.name };
}

function BrandIcon({ template }: { template: DeployTemplate }) {
  const logo = logoFor(template);
  return (
    <span className="template-brand-icon" style={{ "--template-accent": logo.accent } as CSSProperties}>
      <img
        src={logo.src}
        alt={`${logo.label} logo`}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={(event) => {
          event.currentTarget.hidden = true;
          event.currentTarget.parentElement?.classList.add("logo-missing");
        }}
      />
      <b>{logo.initials}</b>
    </span>
  );
}

function compact(value: string, max = 34) {
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
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
      { label: "Image", value: compact(service.image || "image required") },
      { label: lang === "zh" ? "位置" : "Target", value: `${service.region} / ${service.exposure}` },
      { label: lang === "zh" ? "入口" : "Route", value: service.exposure === "none" ? (lang === "zh" ? "内部访问" : "internal only") : compact(`${service.domain}:${service.port}`) },
      { label: lang === "zh" ? "副本" : "Scale", value: `${service.replicas} ${lang === "zh" ? "副本" : "replica"}` },
    ];
  }

  const compose = template.compose;
  if (!compose) return [];
  const exposed = compose.services.filter((service) => service.exposure !== "none");
  return [
    { label: lang === "zh" ? "服务" : "Services", value: `${compose.services.length} ${lang === "zh" ? "服务" : "services"}` },
    { label: lang === "zh" ? "位置" : "Target", value: `${compose.region} / ${exposed.length ? exposed.map((service) => service.exposure).join(", ") : "none"}` },
    { label: lang === "zh" ? "入口" : "Route", value: exposed.length ? compact(exposed.map((service) => `${service.domain}:${service.port}`).join(", ")) : (lang === "zh" ? "内部访问" : "internal only") },
    { label: lang === "zh" ? "存储" : "Storage", value: compact(volumeModeSummary(template, lang)) },
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
  return <section className="template-catalog" aria-label={zh ? "模板库" : "Template library"}>
    <div className="template-catalog-heading"><h2>{zh ? "选择模板" : "Choose a template"}</h2><span>{zh ? "选择后可调整配置，再进行部署。" : "Customize the configuration before deploying."}</span></div>
    <div className="template-catalog-toolbar">
      <Tabs value={mode} onValueChange={(value) => onModeChange(value as DeployMode)}>
        <TabsList aria-label={zh ? "模板类型" : "Template type"}>
          <TabsTrigger value="service">{zh ? "单服务" : "Service"}<span className="template-count">{templates.filter(t => t.mode === "service").length}</span></TabsTrigger>
          <TabsTrigger value="compose">Compose<span className="template-count">{templates.filter(t => t.mode === "compose").length}</span></TabsTrigger>
        </TabsList>
      </Tabs>
      <div className="template-search"><Search aria-hidden="true" /><Input value={query} onChange={event => setQuery(event.target.value)} aria-label={zh ? "搜索模板" : "Search templates"} placeholder={zh ? "搜索名称、镜像或用途…" : "Search name, image or use…"} /></div>
    </div>
    <div className="template-catalog-grid">
      {visibleTemplates.map(template => <Card key={template.id} className="template-catalog-card">
        <div className="template-catalog-identity"><BrandIcon template={template} /><h3>{deployTemplateName(template, lang)}</h3></div>
        <p className="template-catalog-description">{deployTemplateDescription(template, lang)}</p>
        <dl className="template-catalog-facts">{templateFacts(template, lang).map(fact => <div key={fact.label}><dt>{fact.label}</dt><dd title={fact.value}>{fact.value}</dd></div>)}</dl>
        <div className="template-catalog-action"><Button variant="outline" size="sm" onClick={() => onSelect(template)} aria-label={`${zh ? "使用模板" : "Use template"} ${deployTemplateName(template, lang)}`}>{zh ? "使用模板" : "Use template"}<ArrowRight /></Button></div>
      </Card>)}
    </div>
    {visibleTemplates.length === 0 && <div className="template-catalog-empty"><Search aria-hidden="true" /><strong>{zh ? "没有匹配的模板" : "No matching templates"}</strong><p>{zh ? "试试其他关键词，或切换模板类型。" : "Try another keyword or template type."}</p><Button variant="outline" size="sm" onClick={() => setQuery("")}>{zh ? "清空搜索" : "Clear search"}</Button></div>}
  </section>;
}
