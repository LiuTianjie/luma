import { useCallback, useEffect, useState } from "react";
import { AlertCircle, Copy, FingerprintPattern, Link2, LogOut, RefreshCw, ShieldCheck } from "@/components/icons";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldContent, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  createEnrollmentLink,
  fetchAuthSettings,
  listBrowserSessions,
  listPasskeys,
  passkeysSupported,
  registerPasskey,
  removePasskey,
  revokeBrowserSessions,
  updateAuthSettings,
  type AuthSettings,
  type BrowserSession,
  type Passkey,
} from "../authApi";
import { isSessionCredential } from "../apiClient";
import { useConfirm } from "../components/ConfirmDialog";
import { PrimaryCell } from "../components/primitives";
import { formatDateTime } from "../format";
import type { Lang } from "../types";
import { PageHeader } from "./PageHeader";

type SecurityState = {
  passkeys: Passkey[];
  sessions: BrowserSession[];
  settings: AuthSettings | null;
  loading: boolean;
  error: string;
};

// Short label such as "Safari · macOS" for the session list.
export function describeUserAgent(userAgent: string): string {
  if (!userAgent) return "-";
  const browser = /Edg\//.test(userAgent) ? "Edge"
    : /Firefox\//.test(userAgent) ? "Firefox"
      : /Chrome\//.test(userAgent) ? "Chrome"
        : /Safari\//.test(userAgent) ? "Safari"
          : "";
  const os = /iPhone|iPad/.test(userAgent) ? "iOS"
    : /Android/.test(userAgent) ? "Android"
      : /Mac OS X/.test(userAgent) ? "macOS"
        : /Windows/.test(userAgent) ? "Windows"
          : /Linux/.test(userAgent) ? "Linux"
            : "";
  return [browser, os].filter(Boolean).join(" · ") || userAgent.slice(0, 48);
}

const isUnauthorized = (message: string) => /unauthorized|bearer token/i.test(message);

export function SecurityPage({ lang, token, onUnauthorized }: { lang: Lang; token: string; onUnauthorized: () => void }) {
  const zh = lang === "zh";
  const { confirm, element: confirmElement } = useConfirm(lang);
  const [state, setState] = useState<SecurityState>({ passkeys: [], sessions: [], settings: null, loading: true, error: "" });
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState("");
  const [passkeyName, setPasskeyName] = useState("");
  const [link, setLink] = useState<{ url: string; expiresAt: number } | null>(null);
  const supported = passkeysSupported();
  const sessionMode = isSessionCredential(token);

  const refresh = useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: "" }));
    try {
      const [passkeys, sessions, settings] = await Promise.all([listPasskeys(token), listBrowserSessions(token), fetchAuthSettings(token)]);
      setState({ passkeys: passkeys.passkeys, sessions: sessions.sessions, settings, loading: false, error: "" });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (isUnauthorized(message)) {
        onUnauthorized();
        return;
      }
      setState((current) => ({ ...current, loading: false, error: message }));
    }
  }, [token, onUnauthorized]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const onRefresh = () => void refresh();
    window.addEventListener("luma:refresh", onRefresh);
    return () => window.removeEventListener("luma:refresh", onRefresh);
  }, [refresh]);

  const act = async (key: string, action: () => Promise<string>) => {
    setBusy(key);
    setNotice("");
    setActionError("");
    try {
      setNotice(await action());
      await refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (isUnauthorized(message)) onUnauthorized();
      else setActionError(message);
    } finally {
      setBusy("");
    }
  };

  const addPasskey = () => act("add", async () => {
    const result = await registerPasskey({ token, name: passkeyName });
    setPasskeyName("");
    return zh ? `已添加 Passkey：${result.passkey.name}` : `Passkey added: ${result.passkey.name}`;
  });

  const createLink = () => act("link", async () => {
    const created = await createEnrollmentLink(token, passkeyName);
    setLink(created);
    return zh ? "已生成一次性添加链接。" : "One-time enrollment link created.";
  });

  const deletePasskey = async (passkey: Passkey) => {
    const last = state.passkeys.length === 1;
    const ok = await confirm({
      title: zh ? `删除 Passkey ${passkey.name}？` : `Remove passkey ${passkey.name}?`,
      body: <p>{zh
        ? "用它登录的浏览器会话会立即结束。认证器里保存的凭据不会被删除，可在设备的密码管理中自行清理。"
        : "Browser sessions signed in with it end immediately. The credential stays on the authenticator; remove it there from the device's password manager if you like."}</p>,
      warning: last && state.settings?.requirePasskey
        ? (zh ? "这是最后一个 Passkey，“要求 Passkey 登录”会随之关闭，浏览器可再次使用管理 Token 登录。" : "This is the last passkey, so “Require a passkey” turns off and browsers may sign in with the management token again.")
        : undefined,
      confirmLabel: zh ? "删除" : "Remove",
    });
    if (!ok) return;
    await act(`remove-${passkey.id}`, async () => {
      const result = await removePasskey(token, passkey.id);
      return zh ? `已删除 Passkey，结束 ${result.signedOutSessions} 个会话。` : `Passkey removed; ended ${result.signedOutSessions} sessions.`;
    });
  };

  const toggleRequire = async (enabled: boolean) => {
    if (enabled) {
      const ok = await confirm({
        title: zh ? "要求浏览器使用 Passkey 登录？" : "Require a passkey for browser sign-in?",
        body: <p>{zh
          ? "开启后浏览器不能再用管理 Token 登录，现有的 Token 登录会话会立即结束。CLI 和 CI 继续使用管理 Token，不受影响；需要恢复时运行 luma passkey require off。"
          : "Browsers can no longer sign in with the management token, and existing token sessions end now. The CLI and CI keep using the management token; to recover, run luma passkey require off."}</p>,
        confirmLabel: zh ? "开启" : "Require passkeys",
        tone: "neutral",
      });
      if (!ok) return;
    }
    await act("require", async () => {
      const result = await updateAuthSettings(token, enabled);
      return result.requirePasskey
        ? (zh ? `已要求 Passkey 登录，结束 ${result.signedOutSessions || 0} 个 Token 会话。` : `Passkeys are now required; ended ${result.signedOutSessions || 0} token sessions.`)
        : (zh ? "浏览器可再次使用管理 Token 登录。" : "Browsers may sign in with the management token again.");
    });
  };

  const revokeSession = async (session: BrowserSession) => {
    if (session.current) {
      const ok = await confirm({
        title: zh ? "结束当前会话？" : "End this session?",
        body: <p>{zh ? "这个浏览器会退出登录。" : "This browser will be signed out."}</p>,
        confirmLabel: zh ? "退出" : "Sign out",
      });
      if (!ok) return;
    }
    await act(`session-${session.id}`, async () => {
      await revokeBrowserSessions(token, { id: session.id });
      return zh ? "会话已结束。" : "Session ended.";
    });
  };

  const revokeAll = async () => {
    const ok = await confirm({
      title: zh ? "退出所有浏览器？" : "Sign out every browser?",
      body: <p>{zh ? "所有浏览器会话立即结束，包括当前这个。CLI 不受影响。" : "Every browser session ends now, including this one. The CLI is unaffected."}</p>,
      confirmLabel: zh ? "全部退出" : "Sign out all",
    });
    if (!ok) return;
    await act("revoke-all", async () => {
      const result = await revokeBrowserSessions(token, { all: true });
      return zh ? `已结束 ${result.revoked} 个会话。` : `Ended ${result.revoked} sessions.`;
    });
  };

  const copyLink = async () => {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link.url);
      setNotice(zh ? "链接已复制。" : "Link copied.");
    } catch {
      setActionError(zh ? "无法访问剪贴板，请手动复制。" : "Clipboard unavailable; copy the link manually.");
    }
  };

  const settings = state.settings;
  const initialLoading = state.loading && !settings;

  return (
    <div className="settings-workspace flex min-w-0 flex-col gap-6">
      {confirmElement}
      <PageHeader meta={{
        metrics: [],
        eyebrow: zh ? "设置" : "Settings",
        title: zh ? "登录与 Passkey" : "Sign-in and passkeys",
        description: zh
          ? "浏览器用 Passkey 或一次性输入的管理 Token 登录，之后由服务端会话维持；管理 Token 始终是 CLI 和 CI 的凭据。"
          : "Browsers sign in with a passkey or a one-time management token entry and then hold a server-side session. The management token stays the CLI and CI credential.",
        action: <Button variant="outline" size="sm" disabled={state.loading} onClick={() => void refresh()}><RefreshCw data-icon="inline-start" />{zh ? "刷新" : "Refresh"}</Button>,
      }} />

      {notice ? <Alert><ShieldCheck /><AlertTitle>{zh ? "操作完成" : "Completed"}</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert> : null}
      {actionError ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "操作失败" : "Operation failed"}</AlertTitle><AlertDescription className="wrap-anywhere">{actionError}</AlertDescription></Alert> : null}
      {state.error ? <Alert variant="destructive"><AlertCircle /><AlertTitle>{zh ? "读取失败" : "Load failed"}</AlertTitle><AlertDescription className="wrap-anywhere">{state.error}</AlertDescription></Alert> : null}

      <Card>
        <CardHeader>
          <CardTitle role="heading" aria-level={2}>Passkeys</CardTitle>
          <CardDescription>{zh ? `RP ID 是 Control 域名 ${window.location.hostname}；Passkey 只能在这个域名上使用。` : `The RP ID is the Control domain ${window.location.hostname}; passkeys only work on this domain.`}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
          {initialLoading ? (
            <div className="flex flex-col gap-3" role="status" aria-label={zh ? "加载中…" : "Loading…"}>
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-3/4" />
            </div>
          ) : !state.passkeys.length ? (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon"><FingerprintPattern /></EmptyMedia>
                <EmptyTitle>{zh ? "尚未添加 Passkey" : "No passkeys yet"}</EmptyTitle>
                <EmptyDescription>{zh ? "在下面为这台设备添加，或生成链接在另一台设备上添加。" : "Add one for this device below, or create a link to add one on another device."}</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <Table aria-label="Passkeys" containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "Passkey 列表，可横向滚动" : "Passkeys, horizontally scrollable" }}>
              <TableHeader><TableRow>
                <TableHead>{zh ? "名称" : "Name"}</TableHead>
                <TableHead>{zh ? "添加于" : "Added"}</TableHead>
                <TableHead>{zh ? "最近使用" : "Last used"}</TableHead>
                <TableHead>{zh ? "同步" : "Sync"}</TableHead>
                <TableHead className="text-right">{zh ? "操作" : "Actions"}</TableHead>
              </TableRow></TableHeader>
              <TableBody>{state.passkeys.map((passkey) => (
                <TableRow key={passkey.id}>
                  <TableCell><PrimaryCell title={passkey.name} meta={passkey.id.slice(0, 12)} /></TableCell>
                  <TableCell>{formatDateTime(passkey.createdAt, lang)}</TableCell>
                  <TableCell>{passkey.lastUsedAt ? formatDateTime(passkey.lastUsedAt, lang) : (zh ? "从未" : "Never")}</TableCell>
                  <TableCell><Badge variant="outline">{passkey.backedUp ? (zh ? "已同步" : "Synced") : (zh ? "仅此设备" : "Device-bound")}</Badge></TableCell>
                  <TableCell>
                    <div className="flex justify-end">
                      <Button variant="destructive" size="sm" disabled={Boolean(busy)} aria-label={zh ? `删除 ${passkey.name}` : `Remove ${passkey.name}`} onClick={() => void deletePasskey(passkey)}>
                        {busy === `remove-${passkey.id}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                        {zh ? "删除" : "Remove"}
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}</TableBody>
            </Table>
          )}
          <form className="w-full max-w-xl" onSubmit={(event) => { event.preventDefault(); if (supported) void addPasskey(); }}>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="passkey-name">{zh ? "新 Passkey 名称" : "New passkey name"}</FieldLabel>
                <Input id="passkey-name" value={passkeyName} maxLength={64} placeholder={zh ? "例如 MacBook Touch ID" : "For example MacBook Touch ID"} onChange={(event) => setPasskeyName(event.target.value)} />
                <FieldDescription>{supported
                  ? (zh ? "“在此设备添加”会弹出系统的 Passkey 提示。另一台设备可用一次性链接添加，链接 15 分钟内有效。" : "“Add on this device” opens the system passkey prompt. For another device, create a one-time link that is valid for 15 minutes.")
                  : (zh ? "这个浏览器不支持 Passkey；可以生成链接在其他设备上添加。" : "This browser does not support passkeys; create a link to add one on another device.")}</FieldDescription>
              </Field>
              <div className="flex flex-wrap gap-2">
                <Button type="submit" disabled={Boolean(busy) || !supported}>
                  {busy === "add" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <FingerprintPattern data-icon="inline-start" />}
                  {zh ? "在此设备添加" : "Add on this device"}
                </Button>
                <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => void createLink()}>
                  {busy === "link" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <Link2 data-icon="inline-start" />}
                  {zh ? "生成添加链接" : "Create enrollment link"}
                </Button>
              </div>
            </FieldGroup>
          </form>
          {link ? (
            <Alert>
              <Link2 />
              <AlertTitle>{zh ? `一次性链接，${formatDateTime(link.expiresAt, lang)} 前有效` : `One-time link, valid until ${formatDateTime(link.expiresAt, lang)}`}</AlertTitle>
              <AlertDescription className="flex flex-col gap-2">
                <code className="wrap-anywhere">{link.url}</code>
                <p>{zh ? "像密码一样对待它：任何拿到链接的人都能添加 Passkey。" : "Treat it like a password: anyone holding it can add a passkey."}</p>
                <Button variant="outline" size="sm" className="w-fit" onClick={() => void copyLink()}><Copy data-icon="inline-start" />{zh ? "复制链接" : "Copy link"}</Button>
              </AlertDescription>
            </Alert>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle role="heading" aria-level={2}>{zh ? "登录策略" : "Sign-in policy"}</CardTitle>
          <CardDescription>{zh ? "无论如何设置，CLI 和 CI 都继续使用管理 Token。" : "Whatever you choose, the CLI and CI keep using the management token."}</CardDescription>
        </CardHeader>
        <CardContent>
          <Field orientation="horizontal">
            <FieldContent>
              <FieldLabel htmlFor="require-passkey">{zh ? "要求 Passkey 登录" : "Require a passkey"}</FieldLabel>
              <FieldDescription>{settings && !settings.passkeyCount
                ? (zh ? "先添加一个 Passkey 才能开启。" : "Add a passkey before turning this on.")
                : (zh ? "开启后浏览器不能再用管理 Token 登录。恢复：luma passkey require off。" : "Browsers can no longer sign in with the management token. Recover with luma passkey require off.")}</FieldDescription>
            </FieldContent>
            <Switch
              id="require-passkey"
              checked={Boolean(settings?.requirePasskey)}
              disabled={!settings || Boolean(busy) || (!settings.passkeyCount && !settings.requirePasskey)}
              onCheckedChange={(checked) => void toggleRequire(checked)}
            />
          </Field>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle role="heading" aria-level={2}>{zh ? "浏览器会话" : "Browser sessions"}</CardTitle>
          <CardDescription>{settings
            ? (zh ? `会话最长 ${Math.round(settings.sessionAbsoluteSeconds / 3600)} 小时，闲置 ${Math.round(settings.sessionIdleSeconds / 3600)} 小时后结束。` : `Sessions last at most ${Math.round(settings.sessionAbsoluteSeconds / 3600)} hours and end after ${Math.round(settings.sessionIdleSeconds / 3600)} idle hours.`)
            : null}</CardDescription>
          <CardAction>
            <Button variant="outline" size="sm" disabled={Boolean(busy) || !state.sessions.length} onClick={() => void revokeAll()}>
              {busy === "revoke-all" ? <Spinner aria-hidden="true" data-icon="inline-start" /> : <LogOut data-icon="inline-start" />}
              {zh ? "全部退出" : "Sign out all"}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          {initialLoading ? <Skeleton className="h-10 w-full" /> : !state.sessions.length ? (
            <p className="text-muted-foreground">{sessionMode ? (zh ? "没有活动会话。" : "No active sessions.") : (zh ? "没有浏览器会话；当前页面使用开发令牌。" : "No browser sessions; this page uses a development token.")}</p>
          ) : (
            <Table aria-label={zh ? "浏览器会话" : "Browser sessions"} containerProps={{ tabIndex: 0, role: "region", "aria-label": zh ? "会话列表，可横向滚动" : "Sessions, horizontally scrollable" }}>
              <TableHeader><TableRow>
                <TableHead>{zh ? "浏览器" : "Browser"}</TableHead>
                <TableHead>{zh ? "登录方式" : "Signed in with"}</TableHead>
                <TableHead>{zh ? "最近活动" : "Last active"}</TableHead>
                <TableHead>{zh ? "过期" : "Expires"}</TableHead>
                <TableHead className="text-right">{zh ? "操作" : "Actions"}</TableHead>
              </TableRow></TableHeader>
              <TableBody>{state.sessions.map((session) => (
                <TableRow key={session.id}>
                  <TableCell>
                    <span className="flex items-center gap-2">
                      <span title={session.userAgent}>{describeUserAgent(session.userAgent)}</span>
                      {session.current ? <Badge variant="secondary">{zh ? "当前" : "This browser"}</Badge> : null}
                    </span>
                  </TableCell>
                  <TableCell>{session.method === "passkey" ? (session.passkeyName || "Passkey") : (zh ? "管理 Token" : "Management token")}</TableCell>
                  <TableCell>{formatDateTime(session.lastSeenAt, lang)}</TableCell>
                  <TableCell>{formatDateTime(session.expiresAt, lang)}</TableCell>
                  <TableCell>
                    <div className="flex justify-end">
                      <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => void revokeSession(session)}>
                        {busy === `session-${session.id}` ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
                        {session.current ? (zh ? "退出" : "Sign out") : (zh ? "结束" : "End")}
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}</TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
