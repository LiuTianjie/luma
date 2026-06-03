import type { Lang } from "../types";
import type { DeployMode, DeployTemplate } from "./types";

function getTemplateIcon(id: string) {
  const iconProps = { className: "template-icon-svg", width: 18, height: 18, strokeWidth: 2, fill: "none", stroke: "currentColor" };
  switch (id) {
    case "service-whoami":
      return {
        bg: "rgba(99, 102, 241, 0.15)",
        color: "#6366f1",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <path d="M4 17l6-6-6-6M12 19h8" />
          </svg>
        ),
      };
    case "service-nginx":
      return {
        bg: "rgba(16, 185, 129, 0.15)",
        color: "#10b981",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <rect x="2" y="3" width="20" height="8" rx="2" ry="2" />
            <rect x="2" y="13" width="20" height="8" rx="2" ry="2" />
            <line x1="6" y1="7" x2="6.01" y2="7" />
            <line x1="6" y1="17" x2="6.01" y2="17" />
            <line x1="20" y1="7" x2="16" y2="7" />
            <line x1="20" y1="17" x2="16" y2="17" />
          </svg>
        ),
      };
    case "service-redis-worker":
      return {
        bg: "rgba(244, 63, 94, 0.15)",
        color: "#f43f5e",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <ellipse cx="12" cy="5" rx="9" ry="3" />
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
            <path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3" />
          </svg>
        ),
      };
    case "compose-uptime-kuma":
      return {
        bg: "rgba(20, 184, 166, 0.15)",
        color: "#14b8a6",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
          </svg>
        ),
      };
    case "compose-vaultwarden":
      return {
        bg: "rgba(245, 158, 11, 0.15)",
        color: "#f59e0b",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
        ),
      };
    case "compose-gitea":
      return {
        bg: "rgba(59, 130, 246, 0.15)",
        color: "#3b82f6",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <line x1="6" y1="3" x2="6" y2="15" />
            <circle cx="18" cy="6" r="3" />
            <circle cx="6" cy="18" r="3" />
            <path d="M18 9a9 9 0 0 1-9 9" />
          </svg>
        ),
      };
    case "compose-n8n":
      return {
        bg: "rgba(236, 72, 153, 0.15)",
        color: "#ec4899",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <circle cx="18" cy="5" r="3" />
            <circle cx="6" cy="12" r="3" />
            <circle cx="18" cy="19" r="3" />
            <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" />
            <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" />
          </svg>
        ),
      };
    case "service-custom":
    case "compose-custom":
      return {
        bg: "rgba(99, 102, 241, 0.15)",
        color: "#6366f1",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <path d="M12 5v14M5 12h14" />
          </svg>
        ),
      };
    default:
      return {
        bg: "rgba(148, 163, 184, 0.15)",
        color: "#94a3b8",
        svg: (
          <svg viewBox="0 0 24 24" {...iconProps}>
            <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
            <path d="M9 3v18M15 3v18" />
          </svg>
        ),
      };
  }
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
  return (
    <div className="deploy-gallery-container">
      <div className="deploy-gallery-sidebar">
        <p className="eyebrow">{lang === "zh" ? "部署类型" : "Deploy Type"}</p>
        <div className="deploy-mode-switch-pill">
          <button
            type="button"
            className={mode === "service" ? "active" : ""}
            onClick={() => onModeChange("service")}
          >
            单服务
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

      <div className="deploy-gallery-divider" />

      <div className="deploy-gallery-main">
        <div className="deploy-gallery-header-row">
          <span className="deploy-gallery-title">常用模板</span>
          <span className="deploy-gallery-tip">
            从模板开始后可任意修改 Luma 配置，手动编辑 YAML 时以 YAML 为准。
          </span>
        </div>
        <div className="deploy-gallery-scroll">
          {templates
            .filter((template) => template.mode === mode)
            .map((template) => {
              const iconData = getTemplateIcon(template.id);
              return (
                <button
                  type="button"
                  className={`deploy-gallery-card ${activeId === template.id ? "active" : ""}`}
                  key={template.id}
                  onClick={() => onSelect(template)}
                >
                  <div
                    className="template-icon-wrapper"
                    style={{ backgroundColor: iconData.bg, color: iconData.color }}
                  >
                    {iconData.svg}
                  </div>
                  <div className="template-card-info">
                    <strong className="template-card-name">{template.name}</strong>
                    <span className="template-card-desc">{template.description}</span>
                  </div>
                </button>
              );
            })}
        </div>
      </div>
    </div>
  );
}

