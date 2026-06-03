import { localizeState, t } from "../i18n";
import type { DashboardService, Lang } from "../types";
import { Badge, BadgeGroup, CodeCell, PrimaryCell, StatePill } from "./ui";

export function ServicesTable({
  lang,
  services,
  onSelect,
}: {
  lang: Lang;
  services: DashboardService[];
  onSelect: (service: DashboardService) => void;
}) {
  return (
    <article className="panel services-panel" id="section-2">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{t(lang, "servicesEyebrow")}</p>
          <h2>{t(lang, "services")}</h2>
        </div>
        <span>{services.length}</span>
      </div>
      <div className="table-wrap">
        <table className="services-table">
          <colgroup>
            <col className="service-name-col" />
            <col className="service-region-col" />
            <col className="service-exposure-col" />
            <col className="service-image-col" />
            <col className="service-replicas-col" />
            <col className="service-health-col" />
            <col className="service-nodes-col" />
          </colgroup>
          <thead>
            <tr>
              <th>{t(lang, "service")}</th>
              <th>{t(lang, "region")}</th>
              <th>{t(lang, "exposure")}</th>
              <th>{t(lang, "image")}</th>
              <th>{t(lang, "replicas")}</th>
              <th>{t(lang, "health")}</th>
              <th>{t(lang, "nodes")}</th>
            </tr>
          </thead>
          <tbody>
            {services.map((service, index) => {
              const title = service.stack ? `${service.stack}/${service.name || "-"}` : service.name || "-";
              return (
                <tr key={`${title}-${index}`} onClick={() => onSelect(service)}>
                  <td><PrimaryCell meta={service.fullName} title={title} /></td>
                  <td><Badge value={service.region || "-"} /></td>
                  <td><Badge value={service.exposure || "none"} /></td>
                  <td><CodeCell value={service.image || "-"} /></td>
                  <td>
                    <BadgeGroup>
                      <Badge value={`${service.running ?? 0}/${service.desired ?? 0} ${t(lang, "running")}`} />
                      <Badge value={`${service.pending ?? 0} ${t(lang, "pending")}`} />
                      <Badge value={`${service.failed ?? 0} ${t(lang, "failed")}`} />
                    </BadgeGroup>
                  </td>
                  <td><StatePill label={localizeState(lang, service.health)} value={service.health} /></td>
                  <td>
                    <BadgeGroup>
                      {(service.nodes || []).length ? service.nodes?.map((node) => <Badge key={node} value={node} />) : "-"}
                    </BadgeGroup>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </article>
  );
}
