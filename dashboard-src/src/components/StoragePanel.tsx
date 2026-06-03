import { t } from "../i18n";
import type { DashboardVolume, Lang } from "../types";
import { Badge, BadgeGroup, PrimaryCell, StatePill } from "./ui";

export function StoragePanel({
  lang,
  volumes,
  warnings,
}: {
  lang: Lang;
  volumes: DashboardVolume[];
  warnings: string[];
}) {
  return (
    <article className="panel storage-panel" id="section-4">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{t(lang, "storageEyebrow")}</p>
          <h2>{t(lang, "storage")}</h2>
        </div>
        <span>{volumes.length}</span>
      </div>
      {warnings.length ? (
        <div className="storage-warnings">
          {warnings.map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      ) : null}
      <div className="table-wrap">
        <table className="storage-table">
          <thead>
            <tr>
              <th>{t(lang, "volume")}</th>
              <th>{t(lang, "kind")}</th>
              <th>{t(lang, "storageClass")}</th>
              <th>{t(lang, "node")}</th>
              <th>{t(lang, "services")}</th>
            </tr>
          </thead>
          <tbody>
            {volumes.length ? volumes.map((volume) => (
              <tr key={volume.name || "volume"}>
                <td><PrimaryCell title={volume.name || "-"} /></td>
                <td><StatePill label={volume.kind || "unmanaged"} value={volume.kind === "unmanaged" ? "missing" : "ready"} /></td>
                <td><Badge value={volume.storageClass || "-"} /></td>
                <td><Badge value={volume.node || "-"} /></td>
                <td>
                  <BadgeGroup>
                    {(volume.services || []).length ? volume.services?.map((service) => <Badge key={service} value={service} />) : "-"}
                  </BadgeGroup>
                </td>
              </tr>
            )) : (
              <tr>
                <td colSpan={5}>{t(lang, "missing")}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </article>
  );
}
