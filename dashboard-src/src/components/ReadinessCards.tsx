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
  const nomad = payload.readiness?.nomad || {};

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
        <span>Nomad</span>
        <ReadyValue lang={lang} value={nomad.available} />
        <small>{nomad.leader ? `leader ${nomad.leader}` : nomad.engine || "-"}</small>
      </article>
      <article>
        <span>{lang === "zh" ? "部署路径" : "Deploy path"}</span>
        <strong className="ok">Nomad</strong>
        <small>{lang === "zh" ? "控制面直接提交 Nomad job" : "Control submits Nomad jobs directly"}</small>
      </article>
    </section>
  );
}
