import { t } from "../i18n";
import type { DashboardStorageClass, DashboardVolume, Lang } from "../types";
import { Badge, BadgeGroup, PrimaryCell, StatePill } from "./ui";

export function StoragePanel({
  lang,
  volumes,
  storageClasses,
  warnings,
}: {
  lang: Lang;
  volumes: DashboardVolume[];
  storageClasses?: DashboardStorageClass[];
  warnings: string[];
}) {
  return (
    <article className="panel storage-panel" id="section-6">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{t(lang, "storageEyebrow")}</p>
          <h2>{t(lang, "storage")}</h2>
        </div>
        <span>{volumes.length + (storageClasses?.length || 0)}</span>
      </div>
      {warnings.length ? (
        <div className="alert alert-warning">
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
              <th>endpoint</th>
              <th>path</th>
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
                <td><code>{volume.endpoint || "-"}</code></td>
                <td><Badge value={volume.networkPath || "-"} /></td>
                <td>
                  <BadgeGroup>
                    {(volume.services || []).length ? volume.services?.map((service) => <Badge key={service} value={service} />) : "-"}
                  </BadgeGroup>
                </td>
              </tr>
            )) : (
              <tr>
                <td colSpan={7}>{t(lang, "missing")}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {storageClasses?.length ? (
        <div className="table-wrap storage-class-wrap">
          <table className="storage-table">
            <thead>
              <tr>
                <th>{t(lang, "storageClass")}</th>
                <th>provider</th>
                <th>mode</th>
                <th>{t(lang, "node")}</th>
                <th>path / endpoint</th>
                <th>regions</th>
              </tr>
            </thead>
            <tbody>
              {storageClasses.map((item) => (
                <tr key={item.name || "storage-class"}>
                  <td><PrimaryCell title={item.name || "-"} /></td>
                  <td><Badge value={item.provider || "-"} /></td>
                  <td><StatePill label={item.mode || "-"} value={item.mode === "external" ? "pending" : "ready"} /></td>
                  <td><Badge value={item.node || "-"} /></td>
                  <td><code>{item.path || item.endpoint || "-"}</code></td>
                  <td>
                    <BadgeGroup>
                      {(item.regions || []).length ? item.regions?.map((region) => <Badge key={region} value={region} />) : "-"}
                    </BadgeGroup>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </article>
  );
}
