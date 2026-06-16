import type { CSSProperties } from "react";
import type { Lang } from "../types";
import type { DeployMode, DeployTemplate } from "./types";
import { deployTemplateDescription, deployTemplateName } from "./templates";

const TEMPLATE_BRANDS: Record<string, { slug?: string; color: string; initials: string; accent: string }> = {
  "service-custom": { color: "#f8fafc", initials: "L", accent: "#7c3aed" },
  "compose-custom": { color: "#f8fafc", initials: "L", accent: "#7c3aed" },
  "service-whoami": { slug: "traefikproxy", color: "#24a1c1", initials: "T", accent: "#24a1c1" },
  "service-nginx": { slug: "nginx", color: "#009639", initials: "N", accent: "#009639" },
  "service-redis-worker": { slug: "redis", color: "#ff4438", initials: "R", accent: "#ff4438" },
  "service-grafana": { slug: "grafana", color: "#f46800", initials: "G", accent: "#f46800" },
  "service-minio": { slug: "minio", color: "#c72e49", initials: "M", accent: "#c72e49" },
  "service-jellyfin": { slug: "jellyfin", color: "#aa5cc3", initials: "J", accent: "#aa5cc3" },
  "service-code-server": { slug: "coder", color: "#ffffff", initials: "C", accent: "#7c3aed" },
  "compose-uptime-kuma": { slug: "uptimekuma", color: "#5cdd8b", initials: "UK", accent: "#5cdd8b" },
  "compose-vaultwarden": { slug: "bitwarden", color: "#175ddc", initials: "VW", accent: "#175ddc" },
  "compose-gitea": { slug: "gitea", color: "#609926", initials: "GT", accent: "#609926" },
  "compose-n8n": { slug: "n8n", color: "#ea4b71", initials: "N8", accent: "#ea4b71" },
  "compose-nextcloud": { slug: "nextcloud", color: "#0082c9", initials: "NC", accent: "#0082c9" },
  "compose-ghost": { slug: "ghost", color: "#ffffff", initials: "Gh", accent: "#8b8f98" },
  "compose-paperless-ngx": { slug: "paperlessngx", color: "#17541f", initials: "P", accent: "#22c55e" },
  "compose-stirling-pdf": { slug: "stirlingpdf", color: "#ffb02e", initials: "PDF", accent: "#ffb02e" },
};

function brandFor(template: DeployTemplate) {
  return TEMPLATE_BRANDS[template.id] || { color: "#f8fafc", initials: template.name.slice(0, 2), accent: "#94a3b8" };
}

function BrandIcon({ template }: { template: DeployTemplate }) {
  const brand = brandFor(template);
  return (
    <span className="template-brand-icon" style={{ "--template-accent": brand.accent } as CSSProperties}>
      {brand.slug ? (
        <img
          alt=""
          src={`https://cdn.jsdelivr.net/npm/simple-icons@latest/icons/${brand.slug}.svg`}
          onLoad={(event) => {
            event.currentTarget.parentElement?.setAttribute("data-loaded", "true");
          }}
          onError={(event) => {
            event.currentTarget.parentElement?.removeAttribute("data-loaded");
            event.currentTarget.style.display = "none";
          }}
        />
      ) : null}
      <b>{brand.initials}</b>
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
            style={{ "--template-accent": brandFor(featuredTemplate).accent } as CSSProperties}
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
              style={{ "--template-accent": brandFor(template).accent } as CSSProperties}
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
