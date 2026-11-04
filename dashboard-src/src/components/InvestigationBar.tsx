import { useId, useState } from "react";
import { useRouter, toHref } from "../router";
import { observationQuery, investigationPath } from "../observeContext";
import { DateTimePicker } from "./DateTimePicker";
import { localDateInput } from "../historyModel";
import { SelectControl } from "./primitives";
import { Input } from "./ui/input";
import { Button } from "./ui/button";
import { Card, CardContent } from "./ui/card";
import type { Lang } from "../types";

type Filters = "all" | "service" | "none";

export function InvestigationBar({ app, lang, filters = "all" }: { app: string; lang: Lang; filters?: Filters }) {
  const { search } = useRouter();
  const [notice, setNotice] = useState("");
  return (
    <InvestigationForm
      key={`${search}:${filters}`}
      app={app}
      filters={filters}
      lang={lang}
      notice={notice}
      setNotice={setNotice}
    />
  );
}
function InvestigationForm({
  app,
  filters,
  lang,
  notice,
  setNotice,
}: {
  app: string;
  filters: Filters;
  lang: Lang;
  notice: string;
  setNotice: (notice: string) => void;
}) {
  const zh = lang === "zh",
    id = useId();
  const { path, search, navigate } = useRouter(),
    params = new URLSearchParams(search);
  const filterNames = filters === "none" ? [] : filters === "service" ? ["environment", "service", "instance"] : ["environment", "service", "instance", "keyword"];
  const [values, setValues] = useState(
    Object.fromEntries(
      filterNames.map((k) => [
        k,
        params.get(`observe_${k}`) || "",
      ]),
    ),
  );
  const [start, setStart] = useState(
    localDateInput(
      params.get("observe_since")
        ? new Date(Number(observationQuery(search).since) * 1000).toISOString()
        : "",
    ),
  );
  const [end, setEnd] = useState(
    localDateInput(
      params.get("observe_until")
        ? new Date(Number(observationQuery(search).until) * 1000).toISOString()
        : "",
    ),
  );
  const apply = () => {
    const from = start ? new Date(start).getTime() / 1000 : 0,
      until = end
        ? new Date(end).getTime() / 1000
        : Math.floor(Date.now() / 1000);
    if (
      start &&
      (!Number.isFinite(from) || from >= until || until - from > 259200)
    ) {
      setNotice(
        zh
          ? "请选择不超过 72 小时、结束晚于开始的范围。"
          : "Choose an ordered range of at most 72 hours.",
      );
      return;
    }
    const changes = Object.fromEntries(
      Object.entries(values).map(([k, v]) => [`observe_${k}`, v.trim()]),
    );
    navigate(
      investigationPath(path, search, {
        ...changes,
        observe_since: from ? String(from) : "",
        observe_until: end ? String(until) : "",
      }),
    );
  };
  const q = observationQuery(search);
  const deploymentParams = new URLSearchParams({
    app,
    since: new Date(Number(q.since) * 1000).toISOString(),
    until: new Date(Number(q.until) * 1000).toISOString(),
  });
  return (
    <Card className="gap-0 py-0">
      <CardContent className="py-3">
        <form
          className="flex min-w-0 flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            apply();
          }}
          aria-label={zh ? "排查范围" : "Investigation scope"}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="mr-auto text-sm font-medium">
              {zh ? "排查范围" : "Investigation scope"}
            </span>
            <SelectControl
              className="w-full sm:w-44"
              ariaLabel={zh ? "时间范围" : "Time range"}
              value={
                start || end
                  ? "custom"
                  : String(params.get("observeHours") || "1")
              }
              options={[
                ...[1, 6, 24, 72].map((n) => ({
                  value: String(n),
                  label: zh ? `最近 ${n} 小时` : `Last ${n}h`,
                })),
                { value: "custom", label: zh ? "固定时间" : "Fixed range" },
              ]}
              onChange={(v) => {
                if (v === "custom")
                  setEnd(localDateInput(new Date().toISOString()));
                else
                  navigate(
                    investigationPath(path, search, {
                      observeHours: v,
                      observe_since: "",
                      observe_until: "",
                    }),
                  );
              }}
            />
            <Button
              variant="outline"
              size="sm"
              type="button"
              onClick={async () => {
                const fixed = investigationPath(path, search, {
                  observe_since: String(q.since),
                  observe_until: String(q.until),
                });
                try {
                  navigate(fixed);
                  await navigator.clipboard.writeText(
                    new URL(toHref(fixed), location.origin).href,
                  );
                  setNotice(
                    zh
                      ? "已复制固定时间范围的链接"
                      : "Copied a link with a fixed time range",
                  );
                } catch {
                  setNotice(
                    zh
                      ? "无法复制，请使用浏览器地址栏分享链接。"
                      : "Copy unavailable; share the browser address.",
                  );
                }
              }}
            >
              {zh ? "复制排查链接" : "Copy investigation link"}
            </Button>
            <a
              className="text-sm text-link underline-offset-4 hover:underline"
              href={toHref(
                investigationPath(`/deployments?${deploymentParams}`, search),
              )}
            >
              {zh ? "此时段部署" : "Deployments in range"}
            </a>
          </div>
          <details
            open={Boolean(start || end || Object.values(values).some(Boolean))}
          >
            <summary className="w-fit cursor-pointer rounded text-sm text-muted-foreground focus-visible:outline-2 focus-visible:outline-ring">
              {zh
                ? (filters === "none" ? "自定义时间" : "更多筛选")
                : (filters === "none" ? "Custom time range" : "More filters")}
            </summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {[
                ["environment", "环境", "Environment"],
                ["service", "服务名", "Service name"],
                ["instance", "实例 ID", "Instance ID"],
                [
                  "keyword",
                  "日志 / Trace 名称关键词",
                  "Log / trace name keyword",
                ],
              ].filter(([key]) => filterNames.includes(key)).map(([k, cn, en]) => (
                <label
                  key={k}
                  htmlFor={`${id}-${k}`}
                  className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground"
                >
                  {zh ? cn : en}
                  <Input
                    id={`${id}-${k}`}
                    name={k}
                    autoComplete="off"
                    spellCheck={false}
                    maxLength={k === "instance" ? 200 : 160}
                    value={values[k]}
                    onChange={(e) =>
                      setValues((v) => ({ ...v, [k]: e.target.value }))
                    }
                  />
                </label>
              ))}
              <div className="flex flex-col gap-1.5 text-xs text-muted-foreground">
                <label id={`${id}-start`}>{zh ? "开始时间" : "From"}</label>
                <DateTimePicker
                  lang={lang}
                  aria-labelledby={`${id}-start`}
                  value={start}
                  onChange={setStart}
                  placeholder={zh ? "跟随相对范围" : "Relative range"}
                />
              </div>
              <div className="flex flex-col gap-1.5 text-xs text-muted-foreground">
                <label id={`${id}-end`}>{zh ? "结束时间" : "To"}</label>
                <DateTimePicker
                  lang={lang}
                  aria-labelledby={`${id}-end`}
                  value={end}
                  onChange={setEnd}
                  placeholder={zh ? "现在" : "Now"}
                />
              </div>
              <div className="flex items-end gap-2">
                <Button type="submit" size="sm">
                  {zh ? "应用筛选" : "Apply"}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  type="button"
                  onClick={() =>
                    navigate(
                      investigationPath(
                        path,
                        search,
                        Object.fromEntries(
                          [
                            "environment",
                            "service",
                            "instance",
                            "keyword",
                            "since",
                            "until",
                          ].map((k) => [`observe_${k}`, ""]),
                        ),
                      ),
                    )
                  }
                >
                  {zh ? "重置筛选" : "Reset"}
                </Button>
              </div>
            </div>
          </details>
          {notice && (
            <p role="status" className="text-sm text-muted-foreground">
              {notice}
            </p>
          )}
        </form>
      </CardContent>
    </Card>
  );
}
