import { t } from "../i18n";
import type { DashboardPayload, Lang } from "../types";

function flag(lang: Lang, value?: boolean) {
  return value ? t(lang, "configured") : t(lang, "missing");
}

function ReadyValue({ lang, value }: { lang: Lang; value?: boolean }) {
  return <strong className={value ? "ok" : "bad"}>{value ? t(lang, "ready") : t(lang, "missing")}</strong>;
}

export function ReadinessCards({ lang, payload }: { lang: Lang; payload: DashboardPayload }) {
  const cluster = payload.cluster || {};
  const dns = payload.readiness?.dns || {};
  const portainer = payload.readiness?.portainer || {};
  const swarm = payload.readiness?.swarm || {};

  return (
    <section className="summary-grid">
      <article>
        <span>{t(lang, "cluster")}</span>
        <strong>{cluster.id || "-"}</strong>
        <small>{cluster.version ? `version ${cluster.version}` : "-"}</small>
      </article>
      <article>
        <span>DNS</span>
        <ReadyValue lang={lang} value={dns.ready} />
        <small>{[dns.provider, dns.zone, dns.target].filter(Boolean).join(" / ") || "-"}</small>
      </article>
      <article>
        <span>Portainer</span>
        <ReadyValue lang={lang} value={portainer.ready} />
        <small>api {flag(lang, portainer.apiConfigured)}, endpoint {flag(lang, portainer.endpointConfigured)}</small>
      </article>
      <article>
        <span>Swarm</span>
        <ReadyValue lang={lang} value={swarm.available} />
        <small>{swarm.available ? t(lang, "dockerReachable") : t(lang, "dockerUnavailable")}</small>
      </article>
    </section>
  );
}
