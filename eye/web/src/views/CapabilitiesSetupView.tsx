import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { Check, Copy, Play, Trash2 } from "@/components/icons";
import { api } from "@/api/client";
import { PageHeader } from "@/components/PageHeader";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useWorkspace } from "@/context/WorkspaceContext";
import { paths } from "@/router/paths";

const CAPABILITY_MATRIX = [
  {
    domain: "用户标识与标准会话",
    auto: "默认开启",
    fields: "actor_id, session_id",
    detail:
      "跨子域一级 Cookie + localStorage 持久化匿名访客标识；采用 30 分钟空闲滑动窗口 + 24h 上限聚合会话，多标签页共享同一会话。业务登录后可调用 identify(userId) 关联实名账户。",
  },
  {
    domain: "全球国家与终端画像",
    auto: "默认开启",
    fields: "country, os, browser, device_type",
    detail:
      "服务端内置离线 DB-IP 库 + CDN 边缘头 + 时区三级解析，毫秒级识别访问国家；原始客户端 IP 仅在内存做加盐哈希后立即丢弃，不落盘原始 IP。",
  },
  {
    domain: "页面浏览、停留与归因",
    auto: "默认开启",
    fields: "pageview, page_path, duration_ms, utm_*",
    detail:
      "自动监听首屏与 SPA 路由切换 (pushState / replaceState / popstate)；基于 visibilitychange 统计真实前台可见毫秒数；自动提取 Referrer 与 UTM 参数并脱敏敏感查询串。",
  },
  {
    domain: "Core Web Vitals 性能",
    auto: "默认开启",
    fields: "LCP, INP, CLS, FCP, TTFB",
    detail:
      "通过浏览器原生 PerformanceObserver 自动采样真实用户体验分位，按 Google 标准分级评估。",
  },
  {
    domain: "1:1 画面回放与隐私脱敏",
    auto: "默认开启",
    fields: "DOM 快照, 增量变更, 鼠标轨迹, data-eye-mask",
    detail:
      "首屏空闲时异步懒加载 5.6 KB 录制子模块（首屏零体积膨胀），1:1 还原网页真实 DOM、样式、滚动与点击波纹；密码、手机号、卡号与 Token 输入框自动打码，非输入框机密区块可加 data-eye-mask / data-eye-block；历史会话自动降级为事件轨迹舞台。",
  },
  {
    domain: "异常崩溃 + 50 步黑匣子",
    auto: "默认开启",
    fields: "ui.click, navigation, http, console, frames",
    detail:
      "客户端内存维护 50 步环形缓冲区 (Ring Buffer)，记录点击目标选择器、路由切换、Fetch 状态码/耗时与控制台输出；平时零额外流量，崩溃瞬间通过 sendBeacon 连同堆栈上报。",
  },
  {
    domain: "自定义事件与转化漏斗",
    auto: "API 调用",
    fields: "track(name, props)",
    detail:
      "调用 1 行 API 记录关键业务节点与结构化属性，自动进入实时事件流、会话时间轴与 windowFunnel 漏斗计算。",
  },
  {
    domain: "常态日志与 SourceMap",
    auto: "按需开启",
    fields: "captureConsoleLogs, sourcemaps",
    detail:
      "开启 captureConsoleLogs 或调用 eye.log() 可将日志持久化至 ClickHouse；构建时通过 CLI 上传 .map 文件即可在黑匣子堆栈中还原 TypeScript 源码上下文。",
  },
];

export function CapabilitiesSetupView() {
  const { projectSlug = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { projects, publicUrl, since, reloadProjects, notify } = useWorkspace();

  const rawTab = searchParams.get("tab");
  const setupTab: "sdk" | "script" | "rn" | "cli" =
    rawTab === "script" || rawTab === "rn" || rawTab === "cli" ? rawTab : "sdk";

  const setSetupTab = (next: string) => {
    const sp = new URLSearchParams(searchParams);
    if (next === "sdk") sp.delete("tab");
    else sp.set("tab", next);
    setSearchParams(sp, { replace: true });
  };

  const [copiedKey, setCopiedKey] = useState("");
  const [sendingDrill, setSendingDrill] = useState<"" | "event" | "crash">("");
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  const project = projects.find((p) => p.slug === projectSlug);
  if (!project) return null;

  const baseUrl = publicUrl || window.location.origin;
  const sentryDsn = (() => {
    const idx = baseUrl.indexOf("://");
    if (idx < 0) return "";
    return `${baseUrl.slice(0, idx)}://${project.public_key}@${baseUrl.slice(idx + 3)}/${project.dsn_id}`;
  })();

  const copyText = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      window.setTimeout(() => setCopiedKey(""), 1800);
    } catch {
      notify("剪贴板不可用，请手动复制。", "error");
    }
  };

  const handleSendDrill = async (kind: "event" | "crash") => {
    setSendingDrill(kind);
    try {
      const { sessionId } = await api.sendDiagnosticSample(project, kind);
      await new Promise((r) => setTimeout(r, 1200));
      if (kind === "crash") {
        notify(`已写入模拟崩溃、1:1 DOM 画面回放与 8 步黑匣子面包屑，正在打开现场回放…`);
        navigate(paths.sessionDetail(project.slug, sessionId, since));
      } else {
        notify(`已写入测试埋点与 1:1 DOM 回放样本（会话 ${sessionId}），正在打开回放…`);
        navigate(paths.sessionDetail(project.slug, sessionId, since));
      }
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setSendingDrill("");
    }
  };

  const handleDeleteProject = async () => {
    setConfirmDeleteOpen(false);
    try {
      await api.deleteProject(project.slug);
      await reloadProjects();
      notify(`已移除项目 ${project.slug}。`);
      navigate(paths.portfolio(since));
    } catch (err) {
      notify(err instanceof Error ? err.message : String(err), "error");
    }
  };

  const sdkCode = `import { initLumaEye, track, identify } from "@nickname4th/luma-eye";

initLumaEye({
  endpoint: "${baseUrl}",
  key: "${project.public_key}",
  project: "${project.slug}",
  env: "production",
  release: "1.0.0",
  captureConsoleLogs: true,
  traceOrigins: [window.location.origin],
});

identify("user_42", { plan: "pro" });
track("share_created", { template: "card" });`;

  const scriptCode = `<script
  defer
  src="${baseUrl}/_e/eye.js"
  data-key="${project.public_key}"
  data-project="${project.slug}"
  data-env="production"
></script>`;

  const rnCode = `import { initLumaEyeRN } from "@nickname4th/luma-eye/react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

export const eye = initLumaEyeRN({
  endpoint: "${baseUrl}",
  key: "${project.public_key}",
  project: "${project.slug}",
  release: "1.0.0",
  storage: AsyncStorage,
});`;

  const cliCode = `# 安装 CLI 与 Agent Skill
npm install -g @nickname4th/luma-eye
luma-eye login --url ${baseUrl} --token <LUMA_EYE_ADMIN_TOKEN>
luma-eye skills install

# 构建发布时上传 SourceMap 还原压缩堆栈
luma-eye sourcemaps upload --project ${project.slug} --release 1.0.0 --dir ./dist`;

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="接入与项目设置"
        description="客户端只写上报凭证、全链路自检演练、采集口径对照与 SDK 接入代码。"
        metrics={[
          { label: "项目标识", value: <code>{project.slug}</code> },
          { label: "限流阈值", value: `${project.rate_limit_rps || 200} RPS` },
        ]}
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={Boolean(sendingDrill)}
              onClick={() => void handleSendDrill("event")}
            >
              {sendingDrill === "event" ? (
                <Spinner aria-hidden="true" data-icon="inline-start" />
              ) : (
                <Play data-icon="inline-start" />
              )}
              发送测试事件
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={Boolean(sendingDrill)}
              onClick={() => void handleSendDrill("crash")}
            >
              {sendingDrill === "crash" ? (
                <Spinner aria-hidden="true" data-icon="inline-start" />
              ) : (
                <Play data-icon="inline-start" />
              )}
              模拟崩溃与黑匣子回放
            </Button>
          </div>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>上报密钥与端点凭证</CardTitle>
          <CardDescription>
            Public Key 仅具备受速率限制与跨域白名单保护的只写上报权限，可安全放置在浏览器或移动端包内。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-1 gap-4 text-xs md:grid-cols-2">
            <div className="flex flex-col gap-1 rounded-md border border-border bg-muted/30 p-3">
              <dt className="text-muted-foreground">客户端上报公钥 (Public Key)</dt>
              <dd className="flex items-center justify-between gap-2 font-mono font-medium">
                <span className="truncate">{project.public_key}</span>
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  onClick={() => void copyText("key", project.public_key)}
                >
                  {copiedKey === "key" ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
                  {copiedKey === "key" ? "已复制" : "复制"}
                </Button>
              </dd>
            </div>

            <div className="flex flex-col gap-1 rounded-md border border-border bg-muted/30 p-3">
              <dt className="text-muted-foreground">统一合批上报端点</dt>
              <dd className="flex items-center justify-between gap-2 font-mono font-medium">
                <span className="truncate">{baseUrl}/_e/b</span>
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  onClick={() => void copyText("endpoint", `${baseUrl}/_e/b`)}
                >
                  {copiedKey === "endpoint" ? (
                    <Check data-icon="inline-start" />
                  ) : (
                    <Copy data-icon="inline-start" />
                  )}
                  {copiedKey === "endpoint" ? "已复制" : "复制"}
                </Button>
              </dd>
            </div>

            <div className="flex flex-col gap-1 rounded-md border border-border bg-muted/30 p-3">
              <dt className="text-muted-foreground">CORS 允许跨域白名单</dt>
              <dd className="font-mono font-medium wrap-anywhere">
                {project.allowed_origins?.length ? project.allowed_origins.join(", ") : "* (允许全部来源)"}
              </dd>
            </div>

            <div className="flex flex-col gap-1 rounded-md border border-border bg-muted/30 p-3">
              <dt className="text-muted-foreground">Sentry SDK 兼容 DSN</dt>
              <dd className="flex items-center justify-between gap-2 font-mono font-medium">
                <span className="truncate">{sentryDsn}</span>
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  onClick={() => void copyText("dsn", sentryDsn)}
                >
                  {copiedKey === "dsn" ? <Check data-icon="inline-start" /> : <Copy data-icon="inline-start" />}
                  {copiedKey === "dsn" ? "已复制" : "复制"}
                </Button>
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>快速接入代码</CardTitle>
          <CardDescription>已自动填入当前项目的真实 Endpoint 与 Public Key。</CardDescription>
        </CardHeader>
        <CardContent>
          <Tabs value={setupTab} onValueChange={setSetupTab}>
            <TabsList>
              <TabsTrigger value="sdk">React / Next.js / Web SDK</TabsTrigger>
              <TabsTrigger value="script">单行 Script 标签</TabsTrigger>
              <TabsTrigger value="rn">React Native / Expo</TabsTrigger>
              <TabsTrigger value="cli">CLI 与 SourceMap</TabsTrigger>
            </TabsList>

            {(
              [
                { key: "sdk", hint: "npm install @nickname4th/luma-eye", code: sdkCode },
                { key: "script", hint: "无需打包构建，直接嵌入 HTML <head>", code: scriptCode },
                { key: "rn", hint: "支持 Hermes 字节码堆栈与离线崩溃发件箱", code: rnCode },
                { key: "cli", hint: "支持终端查询与构建期 SourceMap 上传", code: cliCode },
              ] as const
            ).map((item) => (
              <TabsContent key={item.key} value={item.key} className="mt-3 flex flex-col gap-2">
                <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span className="font-mono">{item.hint}</span>
                  <Button
                    type="button"
                    size="xs"
                    variant="outline"
                    onClick={() => void copyText(item.key, item.code)}
                  >
                    {copiedKey === item.key ? (
                      <Check data-icon="inline-start" />
                    ) : (
                      <Copy data-icon="inline-start" />
                    )}
                    {copiedKey === item.key ? "已复制" : "复制代码"}
                  </Button>
                </div>
                <pre className="overflow-x-auto rounded-lg border border-border bg-muted/60 p-3.5 font-mono text-xs leading-relaxed text-foreground">
                  {item.code}
                </pre>
              </TabsContent>
            ))}
          </Tabs>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>采集能力与字段口径说明</CardTitle>
          <CardDescription>区分默认零配置采集与按需 API 调用的指标范围。</CardDescription>
        </CardHeader>
        <CardContent className="px-0 pb-0">
          <Table
            aria-label="采集能力说明表"
            containerProps={{
              tabIndex: 0,
              role: "region",
              "aria-label": "采集能力说明表（可横向滚动）",
            }}
          >
            <TableHeader>
              <TableRow>
                <TableHead className="w-44">能力模块</TableHead>
                <TableHead className="w-28">触发方式</TableHead>
                <TableHead className="w-52">核心字段</TableHead>
                <TableHead>机制与分析用途</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {CAPABILITY_MATRIX.map((row) => (
                <TableRow key={row.domain}>
                  <TableCell className="font-medium">{row.domain}</TableCell>
                  <TableCell>
                    <Badge variant={row.auto === "默认开启" ? "secondary" : "outline"}>{row.auto}</Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{row.fields}</TableCell>
                  <TableCell className="text-xs text-muted-foreground whitespace-normal">
                    {row.detail}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card className="border-destructive/30">
        <CardHeader>
          <CardTitle className="text-sm font-semibold">危险操作：移除监控项目</CardTitle>
          <CardDescription>
            从项目注册表中移除 <code>{project.slug}</code> 并拒绝该 Public Key 的后续数据上报；已落盘的历史分区按 TTL 自动滚动回收。
          </CardDescription>
          <CardAction>
            <Button variant="destructive" size="sm" onClick={() => setConfirmDeleteOpen(true)}>
              <Trash2 data-icon="inline-start" />
              删除项目
            </Button>
          </CardAction>
        </CardHeader>
      </Card>

      <AlertDialog open={confirmDeleteOpen} onOpenChange={setConfirmDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除监控项目「{project.slug}」？</AlertDialogTitle>
            <AlertDialogDescription>
              删除后，使用该公钥的客户端新上报请求将被立即拒绝。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={() => void handleDeleteProject()}>
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
