import type { CSSProperties } from "react";
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
      compact(service.image || "image required"),
      `${service.region} / ${service.exposure}`,
      service.exposure === "none" ? (lang === "zh" ? "内部访问" : "internal only") : compact(`${service.domain}:${service.port}`),
      `${service.replicas} ${lang === "zh" ? "副本" : "replica"}`,
    ];
  }

  const compose = template.compose;
  if (!compose) return [];
  const exposed = compose.services.filter((service) => service.exposure !== "none");
  return [
    `${compose.services.length} ${lang === "zh" ? "服务" : "services"}`,
    `${compose.region} / ${exposed.length ? exposed.map((service) => service.exposure).join(", ") : "none"}`,
    exposed.length ? compact(exposed.map((service) => `${service.domain}:${service.port}`).join(", ")) : (lang === "zh" ? "内部访问" : "internal only"),
    compact(volumeModeSummary(template, lang)),
  ];
}

export function DeployTemplates({
  lang,
  mode,
  templates,
  activeId,
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
  const visibleTemplates = templates.filter((template) => template.mode === mode);
  const featuredTemplate = visibleTemplates.find((template) => template.id === activeId) || visibleTemplates[0];
  const secondaryTemplates = visibleTemplates.filter((template) => template.id !== featuredTemplate?.id);

  const renderFacts = (template: DeployTemplate) => (
    templateFacts(template, lang).map((fact) => <span key={fact}>{fact}</span>)
  );

  return (
    <div className="deploy-gallery-container">
      <div className="deploy-gallery-header-row">
        <div>
          <p className="eyebrow">{lang === "zh" ? "模板库" : "Template library"}</p>
          <div className="deploy-gallery-title-line">
            <h3>{lang === "zh" ? "选择模板后编辑配置" : "Select a template, then edit config"}</h3>
            <div className="deploy-mode-switch-block" aria-label={lang === "zh" ? "模板类型" : "Template type"}>
              <span>{lang === "zh" ? "模板类型" : "Template type"}</span>
              <div className="deploy-mode-switch-pill">
                <button
                  type="button"
                  className={mode === "service" ? "active" : ""}
                  onClick={() => onModeChange("service")}
                >
                  {lang === "zh" ? "单服务" : "Service"}
                </button>
                <button
                  type="button"
                  className={mode === "compose" ? "active" : ""}
                  onClick={() => onModeChange("compose")}
                >
                  Compose
                </button>
              </div>
            </div>
          </div>
          <span>{lang === "zh" ? "卡片只展示会写入部署配置的字段摘要；完整内容以右侧表单和 YAML 为准。" : "Cards show fields that affect the generated deployment config. The form and YAML are the source of truth."}</span>
        </div>
      </div>

      <div className="deploy-gallery-showcase">
        {featuredTemplate ? (
          <button
            type="button"
            className={`template-feature-card ${activeId === featuredTemplate.id ? "active" : ""}`}
            onClick={() => onSelect(featuredTemplate)}
            style={{ "--template-accent": logoFor(featuredTemplate).accent } as CSSProperties}
          >
            <div className="template-feature-visual" aria-hidden="true">
              <BrandIcon template={featuredTemplate} />
              <span>{featuredTemplate.mode === "compose" ? "Compose" : lang === "zh" ? "单服务" : "Service"}</span>
            </div>
            <div className="template-feature-copy">
              <p className="eyebrow">{lang === "zh" ? "当前蓝图" : "Selected blueprint"}</p>
              <h3>{deployTemplateName(featuredTemplate, lang)}</h3>
              <p>{deployTemplateDescription(featuredTemplate, lang)}</p>
            </div>
            <div className="template-feature-facts">
              {renderFacts(featuredTemplate)}
            </div>
            <span className="template-feature-action">{lang === "zh" ? "使用并进入配置" : "Use and configure"} →</span>
          </button>
        ) : null}

        <div className="deploy-gallery-grid">
          {secondaryTemplates.map((template) => (
            <button
              type="button"
              className={`deploy-gallery-card ${activeId === template.id ? "active" : ""}`}
              key={template.id}
              onClick={() => onSelect(template)}
              style={{ "--template-accent": logoFor(template).accent } as CSSProperties}
            >
              <div className="template-card-top">
                <BrandIcon template={template} />
                <span className="template-card-action">{lang === "zh" ? "使用" : "Use"} →</span>
              </div>
              <div className="template-card-info">
                <strong className="template-card-name">{deployTemplateName(template, lang)}</strong>
                <span className="template-card-desc">{deployTemplateDescription(template, lang)}</span>
              </div>
              <div className="template-card-facts">
                {renderFacts(template)}
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
