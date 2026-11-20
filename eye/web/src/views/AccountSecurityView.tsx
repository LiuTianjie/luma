import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  AlertCircle,
  Contrast,
  FingerprintPattern,
  KeyRound,
  LogOut,
  Monitor,
  Moon,
  Pencil,
  RefreshCw,
  ShieldCheck,
  Sun,
  Trash2,
  UserCircle,
} from "@/components/icons";
import { ArrowUpRight, Eye, EyeOff } from "lucide-react";
import { api } from "@/api/client";
import {
  detectPasskeyCapabilities,
  registerPasskeyCredential,
  syncAllAcceptedCredentials,
  syncCurrentUserDetails,
} from "@/api/passkeys";
import type { BrowserSession, PasskeyCredential } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { PrimaryCell } from "@/components/primitives";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useWorkspace } from "@/context/WorkspaceContext";
import { cn } from "@/lib/utils";
import { formatLocalTime } from "@/utils/format";

function describeUserAgent(ua: string): string {
  if (!ua) return "—";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /Firefox\//.test(ua)
      ? "Firefox"
      : /Chrome\//.test(ua)
        ? "Chrome"
        : /Safari\//.test(ua)
          ? "Safari"
          : "";
  const os = /iPhone|iPad/.test(ua)
    ? "iOS"
    : /Android/.test(ua)
      ? "Android"
      : /Mac OS X|Macintosh/.test(ua)
        ? "macOS"
        : /Windows/.test(ua)
          ? "Windows"
          : /Linux/.test(ua)
            ? "Linux"
            : "";
  return [browser, os].filter(Boolean).join(" · ") || ua.slice(0, 48);
}

function formatEpochMs(ms?: number): string {
  if (!ms || ms <= 0) return "从未";
  return formatLocalTime(new Date(ms).toISOString());
}

export function AccountSecurityView() {
  const { authStatus, refreshAuthStatus, themeMode, setThemeMode, logout } = useWorkspace();

  const [loading, setLoading] = useState(true);
  const [rpId, setRpId] = useState("");
  const [passkeys, setPasskeys] = useState<PasskeyCredential[]>([]);
  const [sessions, setSessions] = useState<BrowserSession[]>([]);
  const [sessionTtlHours, setSessionTtlHours] = useState(720);
  const [canRegisterPasskey, setCanRegisterPasskey] = useState<boolean | null>(null);

  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  // Passkey add / rename / delete states
  const [newPasskeyName, setNewPasskeyName] = useState("");
  const [renamingPasskey, setRenamingPasskey] = useState<PasskeyCredential | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [deletingPasskey, setDeletingPasskey] = useState<PasskeyCredential | null>(null);
  const [confirmRevokeOthers, setConfirmRevokeOthers] = useState(false);

  // Password form states
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [showNewPassword, setShowNewPassword] = useState(false);

  // Profile form states
  const [profileName, setProfileName] = useState(authStatus?.username || "admin");
  const [profileDisplayName, setProfileDisplayName] = useState(authStatus?.display_name || "Administrator");

  useEffect(() => {
    if (authStatus?.username) setProfileName(authStatus.username);
    if (authStatus?.display_name) setProfileDisplayName(authStatus.display_name);
  }, [authStatus?.username, authStatus?.display_name]);

  useEffect(() => {
    let active = true;
    detectPasskeyCapabilities().then((caps) => {
      if (active) {
        setCanRegisterPasskey(caps.supported && caps.platformAuthenticator);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const loadSecurityData = useCallback(async (syncSignals = false) => {
    setLoading(true);
    setError("");
    try {
      const [pkRes, sessRes] = await Promise.all([api.listPasskeys(), api.listAuthSessions()]);
      setRpId(pkRes.rpId || "");
      setPasskeys(pkRes.credentials || []);
      setSessions(sessRes.sessions || []);
      if (sessRes.sessionTtlHours) setSessionTtlHours(sessRes.sessionTtlHours);
      if (syncSignals) {
        await syncAllAcceptedCredentials(pkRes.rpId, pkRes.userId, pkRes.credentials || []);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const isControlSSO = authStatus?.auth_mode === "control-sso";

  useEffect(() => {
    if (isControlSSO) {
      setLoading(false);
      return;
    }
    void loadSecurityData(true);
  }, [isControlSSO, loadSecurityData]);

  const runAction = async (key: string, fn: () => Promise<string>) => {
    setBusy(key);
    setNotice("");
    setError("");
    try {
      const msg = await fn();
      setNotice(msg);
      await Promise.all([loadSecurityData(false), refreshAuthStatus()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const handleRegisterPasskey = (ev: FormEvent) => {
    ev.preventDefault();
    void runAction("add-passkey", async () => {
      const res = await registerPasskeyCredential({ name: newPasskeyName, promotion: false });
      if (res.cancelled || !res.passkey) {
        throw new Error("已取消 Passkey 注册");
      }
      setNewPasskeyName("");
      return `已成功添加 Passkey「${res.passkey.name}」，下次可直接用生物识别一键登录。`;
    });
  };

  const handleConfirmRename = (ev: FormEvent) => {
    ev.preventDefault();
    if (!renamingPasskey || !renameDraft.trim()) return;
    const target = renamingPasskey;
    const nextName = renameDraft.trim();
    setRenamingPasskey(null);
    void runAction(`rename-${target.id}`, async () => {
      await api.renamePasskey(target.id, nextName);
      return `已将 Passkey 重命名为「${nextName}」。`;
    });
  };

  const handleConfirmDeletePasskey = () => {
    if (!deletingPasskey) return;
    const target = deletingPasskey;
    setDeletingPasskey(null);
    void runAction(`delete-${target.id}`, async () => {
      const res = await api.deletePasskey(target.id);
      await syncAllAcceptedCredentials(res.rpId, res.userId, res.credentials || []);
      return `已删除 Passkey「${target.name}」${res.signedOutSessions > 0 ? `（同时结束 ${res.signedOutSessions} 个关联会话）` : ""}。`;
    });
  };

  const handlePasswordSubmit = (ev: FormEvent) => {
    ev.preventDefault();
    void runAction("save-password", async () => {
      await api.updatePassword({
        current_password: currentPassword || undefined,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      return "登录密码已保存生效；管理 Token 仍保留给 CLI 与应急恢复使用。";
    });
  };

  const handleProfileSubmit = (ev: FormEvent) => {
    ev.preventDefault();
    void runAction("save-profile", async () => {
      const res = await api.updateProfile({
        name: profileName.trim(),
        displayName: profileDisplayName.trim(),
      });
      await syncCurrentUserDetails(res.rpId, res.userId, res.name, res.displayName);
      return `账户信息已更新为「${res.displayName} (${res.name})」，并已同步至密码管理器。`;
    });
  };

  const handleRevokeSingleSession = (s: BrowserSession) => {
    void runAction(`revoke-${s.id}`, async () => {
      await api.revokeAuthSessions({ id: s.id });
      return "已结束该浏览器会话。";
    });
  };

  const handleRevokeOtherSessions = () => {
    setConfirmRevokeOthers(false);
    void runAction("revoke-others", async () => {
      const res = await api.revokeAuthSessions({ all: false });
      return `已吊销其他 ${res.revoked} 个浏览器登录会话，当前浏览器保持登录。`;
    });
  };

  const hasPassword = Boolean(authStatus?.has_password);

  if (isControlSSO) {
    return (
      <div className="flex min-w-0 flex-col gap-6">
        <PageHeader
          title="身份与安全"
          description="当前 Luma Eye 已接入 Luma Control 统一身份认证，Passkey、登录策略与会话吊销由控制面集中管理。"
        />

        <Card>
          <CardHeader>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2">
              <ShieldCheck className="size-4 text-emerald-600 dark:text-emerald-400" />
              由 Luma Control 统一管理登录与安全
            </CardTitle>
            <CardDescription>
              无需在 Luma Eye 中重复配置密码或注册 Passkey；在 Luma Control 退出登录或吊销 Passkey 后，本控制台会话会立即失效。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="grid gap-3 rounded-lg border border-border bg-muted/40 p-4 text-sm sm:grid-cols-2">
              <div>
                <div className="text-xs text-muted-foreground">身份提供方 (IdP)</div>
                <div className="mt-1 font-mono text-xs text-foreground">{authStatus?.control_url || "Luma Control"}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">当前登录方式</div>
                <div className="mt-1 flex items-center gap-2">
                  <Badge variant="secondary">{authStatus?.auth_method || "control-sso"}</Badge>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {authStatus?.control_security_url ? (
                <Button
                  nativeButton={false}
                  render={<a href={authStatus.control_security_url} target="_blank" rel="noreferrer" />}
                >
                  前往 Luma Control 管理 Passkey 与会话
                  <ArrowUpRight className="ml-1 size-4" />
                </Button>
              ) : null}
              <Button variant="outline" onClick={() => void logout()}>
                <LogOut data-icon="inline-start" />
                退出当前 Luma Eye 会话
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle role="heading" aria-level={2}>
              界面外观
            </CardTitle>
            <CardDescription>仅影响当前浏览器的配色方案偏好。</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid max-w-md grid-cols-3 gap-2" role="radiogroup" aria-label="界面配色方案">
              {(
                [
                  { mode: "system", label: "跟随系统", icon: Monitor },
                  { mode: "light", label: "浅色模式", icon: Sun },
                  { mode: "dark", label: "深色模式", icon: Moon },
                ] as const
              ).map(({ mode, label, icon: Icon }) => (
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={themeMode === mode}
                  onClick={() => setThemeMode(mode)}
                  className={cn(
                    "flex items-center justify-center gap-2 rounded-md px-3 py-2 text-xs font-medium ring-1 ring-border transition-colors outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring",
                    themeMode === mode
                      ? "bg-accent text-foreground ring-foreground/30"
                      : "text-muted-foreground",
                  )}
                >
                  <Icon className="size-3.5" />
                  <span>{label}</span>
                </button>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <PageHeader
        title="登录与 Passkey"
        description="配置 Passkey（Touch ID / Face ID / 1Password / 安全密钥）与专属登录密码。会话密钥与状态持久化存储在数据卷中，容器重部署或进程重启均保持 30 天免重新登录。"
        action={
          <Button
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={() => void loadSecurityData(false)}
          >
            <RefreshCw data-icon="inline-start" />
            刷新
          </Button>
        }
      />

      {notice ? (
        <Alert>
          <ShieldCheck />
          <AlertTitle>操作成功</AlertTitle>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      ) : null}

      {error ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>操作未完成</AlertTitle>
          <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
        </Alert>
      ) : null}

      {/* 1. Passkeys Management Card */}
      <Card>
        <CardHeader>
          <CardTitle role="heading" aria-level={2}>
            通行密钥 (Passkeys)
          </CardTitle>
          <CardDescription>
            {rpId
              ? `绑定的信赖方域名 (RP ID) 为 ${rpId}；使用设备生物识别或硬件安全密钥即可防钓鱼免密登录。`
              : "绑定设备生物识别或密码管理器后，无需再查找或粘贴长串 Token。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
          {loading && passkeys.length === 0 ? (
            <div className="flex flex-col gap-2.5" role="status" aria-label="正在加载 Passkey 列表">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-3/4" />
            </div>
          ) : passkeys.length === 0 ? (
            <Empty className="py-8">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <FingerprintPattern />
                </EmptyMedia>
                <EmptyTitle>尚未添加 Passkey</EmptyTitle>
                <EmptyDescription>
                  在下方输入备注名并点击「在此设备添加 Passkey」，即可绑定 MacBook Touch ID、iCloud 钥匙串、1Password 或 YubiKey。
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <Table
              aria-label="已注册的 Passkey 列表"
              containerProps={{
                tabIndex: 0,
                role: "region",
                "aria-label": "Passkey 列表（可横向滚动）",
              }}
            >
              <TableHeader>
                <TableRow>
                  <TableHead>名称与提供商</TableHead>
                  <TableHead>注册时间</TableHead>
                  <TableHead>最近使用</TableHead>
                  <TableHead>同步类型</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {passkeys.map((pk) => (
                  <TableRow key={pk.id}>
                    <TableCell>
                      <div className="flex items-center gap-2.5">
                        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                          <FingerprintPattern className="size-4" />
                        </span>
                        <PrimaryCell
                          title={pk.name}
                          meta={
                            pk.providerName && pk.providerName !== pk.name
                              ? `${pk.providerName} · ${pk.id.slice(0, 10)}…`
                              : `ID ${pk.id.slice(0, 12)}…`
                          }
                        />
                      </div>
                    </TableCell>
                    <TableCell className="tabular-nums">{formatEpochMs(pk.registeredAt)}</TableCell>
                    <TableCell className="tabular-nums">{formatEpochMs(pk.lastUsedAt)}</TableCell>
                    <TableCell>
                      <Badge variant="outline">
                        {pk.credentialBackedUp ? "云端已同步" : "设备绑定"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1.5">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={Boolean(busy)}
                          aria-label={`重命名 ${pk.name}`}
                          onClick={() => {
                            setRenamingPasskey(pk);
                            setRenameDraft(pk.name);
                          }}
                        >
                          <Pencil data-icon="inline-start" />
                          重命名
                        </Button>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          disabled={Boolean(busy)}
                          aria-label={`删除 ${pk.name}`}
                          onClick={() => setDeletingPasskey(pk)}
                        >
                          {busy === `delete-${pk.id}` ? (
                            <Spinner aria-hidden="true" data-icon="inline-start" />
                          ) : (
                            <Trash2 data-icon="inline-start" />
                          )}
                          删除
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          {canRegisterPasskey !== false ? (
            <form onSubmit={handleRegisterPasskey} className="w-full max-w-xl">
              <FieldGroup className="gap-4">
                <Field>
                  <FieldLabel htmlFor="new-passkey-name">新 Passkey 备注名称（可选）</FieldLabel>
                  <Input
                    id="new-passkey-name"
                    value={newPasskeyName}
                    maxLength={64}
                    placeholder="例如 MacBook Pro Touch ID / 1Password"
                    onChange={(ev) => setNewPasskeyName(ev.target.value)}
                  />
                  <FieldDescription>
                    同时支持平台内置认证器（Touch ID / Face ID / Windows Hello）与外部 USB/NFC 安全密钥。
                  </FieldDescription>
                </Field>
                <div>
                  <Button type="submit" disabled={Boolean(busy) || canRegisterPasskey === null}>
                    {busy === "add-passkey" ? (
                      <Spinner aria-hidden="true" data-icon="inline-start" />
                    ) : (
                      <FingerprintPattern data-icon="inline-start" />
                    )}
                    在此设备添加 Passkey
                  </Button>
                </div>
              </FieldGroup>
            </form>
          ) : (
            <p className="text-xs text-muted-foreground">
              当前浏览器或系统未检测到可用的平台 Passkey 认证器；您可在下方设置专属登录密码，或在支持 Touch ID / Face ID / Windows Hello 的现代浏览器中添加 Passkey。
            </p>
          )}
        </CardContent>
      </Card>

      {/* 2. Password & Account Profile Grid */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2">
              <KeyRound className="size-4 text-muted-foreground" />
              {hasPassword ? "修改登录密码" : "设置专属登录密码"}
            </CardTitle>
            <CardDescription>
              设置好记的密码替代长串随机 Token；无论是否设置密码，CLI 与 Agent 始终可继续使用{" "}
              <code>LUMA_EYE_ADMIN_TOKEN</code>。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handlePasswordSubmit}>
              <input
                type="text"
                name="username"
                value={authStatus?.username || "admin"}
                autoComplete="username"
                readOnly
                tabIndex={-1}
                aria-hidden="true"
                className="sr-only"
              />
              <FieldGroup className="gap-4">
                {hasPassword ? (
                  <Field>
                    <FieldLabel htmlFor="current-password">当前密码（若当前通过 Passkey/Token 登录可留空）</FieldLabel>
                    <Input
                      id="current-password"
                      name="current-password"
                      type="password"
                      value={currentPassword}
                      onChange={(ev) => setCurrentPassword(ev.target.value)}
                      autoComplete="current-password"
                      placeholder="输入旧密码或管理 Token"
                    />
                  </Field>
                ) : null}

                <Field>
                  <FieldLabel htmlFor="new-password">新登录密码（至少 8 位）</FieldLabel>
                  <div className="relative flex items-center">
                    <Input
                      id="new-password"
                      name="new-password"
                      type={showNewPassword ? "text" : "password"}
                      className="pr-9"
                      minLength={8}
                      required
                      value={newPassword}
                      onChange={(ev) => setNewPassword(ev.target.value)}
                      autoComplete="new-password"
                      enterKeyHint="done"
                      placeholder="建议使用浏览器密码管理器生成强密码"
                    />
                    <button
                      type="button"
                      onClick={() => setShowNewPassword((v) => !v)}
                      aria-label={showNewPassword ? "隐藏新密码" : "显示新密码"}
                      aria-pressed={showNewPassword}
                      className="absolute right-2 inline-flex size-6 items-center justify-center rounded text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                    >
                      {showNewPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                  <FieldDescription>
                    密码在服务端通过 Argon2id 加盐哈希存储，永不明文落盘。
                  </FieldDescription>
                </Field>

                <div>
                  <Button type="submit" disabled={Boolean(busy) || newPassword.length < 8}>
                    {busy === "save-password" ? (
                      <Spinner aria-hidden="true" data-icon="inline-start" />
                    ) : null}
                    {hasPassword ? "更新登录密码" : "保存登录密码"}
                  </Button>
                </div>
              </FieldGroup>
            </form>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle role="heading" aria-level={2} className="flex items-center gap-2">
              <UserCircle className="size-4 text-muted-foreground" />
              管理员账号与外观偏好
            </CardTitle>
            <CardDescription>
              更新用户名或显示名称后，将通过 WebAuthn Signal API 自动同步至本机密码管理器中的 Passkey 条目。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
            <form onSubmit={handleProfileSubmit}>
              <FieldGroup className="gap-4">
                <Field>
                  <FieldLabel htmlFor="profile-username">登录用户名</FieldLabel>
                  <Input
                    id="profile-username"
                    name="username"
                    type="text"
                    required
                    maxLength={64}
                    spellCheck={false}
                    autoCapitalize="none"
                    autoCorrect="off"
                    value={profileName}
                    onChange={(ev) => setProfileName(ev.target.value)}
                  />
                </Field>

                <Field>
                  <FieldLabel htmlFor="profile-display-name">显示名称</FieldLabel>
                  <Input
                    id="profile-display-name"
                    name="fullname"
                    type="text"
                    required
                    maxLength={80}
                    autoComplete="name"
                    value={profileDisplayName}
                    onChange={(ev) => setProfileDisplayName(ev.target.value)}
                  />
                </Field>

                <div>
                  <Button type="submit" variant="outline" disabled={Boolean(busy)}>
                    {busy === "save-profile" ? (
                      <Spinner aria-hidden="true" data-icon="inline-start" />
                    ) : null}
                    保存账户资料
                  </Button>
                </div>
              </FieldGroup>
            </form>

            <div className="border-t border-hairline pt-4">
              <div className="mb-2 flex items-center gap-2 text-xs font-medium text-foreground">
                <Contrast className="size-3.5 text-muted-foreground" />
                <span>界面配色方案</span>
              </div>
              <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="界面配色方案">
                {(
                  [
                    { mode: "system", label: "跟随系统", icon: Monitor },
                    { mode: "light", label: "浅色模式", icon: Sun },
                    { mode: "dark", label: "深色模式", icon: Moon },
                  ] as const
                ).map(({ mode, label, icon: Icon }) => (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={themeMode === mode}
                    onClick={() => setThemeMode(mode)}
                    className={cn(
                      "flex items-center justify-center gap-2 rounded-md px-3 py-2 text-xs font-medium ring-1 ring-border transition-colors outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring",
                      themeMode === mode
                        ? "bg-accent text-foreground ring-foreground/30"
                        : "text-muted-foreground",
                    )}
                  >
                    <Icon className="size-3.5" />
                    <span>{label}</span>
                  </button>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* 3. Persistent Browser Sessions Card */}
      <Card>
        <CardHeader>
          <CardTitle role="heading" aria-level={2}>
            活动浏览器会话
          </CardTitle>
          <CardDescription>
            每个会话最长保持 {Math.round(sessionTtlHours / 24)} 天并在活跃访问时自动滑动续期；重部署容器不会清空会话。
          </CardDescription>
          {sessions.length > 1 ? (
            <CardAction>
              <Button
                variant="outline"
                size="sm"
                disabled={Boolean(busy)}
                onClick={() => setConfirmRevokeOthers(true)}
              >
                <LogOut data-icon="inline-start" />
                吊销其他设备会话
              </Button>
            </CardAction>
          ) : null}
        </CardHeader>
        <CardContent>
          {loading && sessions.length === 0 ? (
            <Skeleton className="h-10 w-full" />
          ) : sessions.length === 0 ? (
            <p className="text-sm text-muted-foreground">当前没有持久化的浏览器 Cookie 会话。</p>
          ) : (
            <Table
              aria-label="活动浏览器会话"
              containerProps={{
                tabIndex: 0,
                role: "region",
                "aria-label": "会话列表（可横向滚动）",
              }}
            >
              <TableHeader>
                <TableRow>
                  <TableHead>浏览器与系统</TableHead>
                  <TableHead>认证方式</TableHead>
                  <TableHead>最近活跃</TableHead>
                  <TableHead>到期时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sessions.map((s) => (
                  <TableRow key={s.id}>
                    <TableCell>
                      <span className="flex items-center gap-2">
                        <span title={s.userAgent}>{describeUserAgent(s.userAgent)}</span>
                        {s.current ? <Badge variant="secondary">当前浏览器</Badge> : null}
                      </span>
                    </TableCell>
                    <TableCell>
                      {s.method === "passkey"
                        ? `Passkey (${s.passkeyName || "生物识别"})`
                        : s.method === "password"
                          ? "专属密码"
                          : "管理 Token"}
                    </TableCell>
                    <TableCell className="tabular-nums">{formatEpochMs(s.lastSeenAt)}</TableCell>
                    <TableCell className="tabular-nums">{formatEpochMs(s.expiresAt)}</TableCell>
                    <TableCell>
                      <div className="flex justify-end">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={Boolean(busy)}
                          onClick={() => handleRevokeSingleSession(s)}
                        >
                          {s.current ? "退出此浏览器" : "结束会话"}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Rename Passkey Dialog */}
      <Dialog
        open={Boolean(renamingPasskey)}
        onOpenChange={(open) => {
          if (!open) setRenamingPasskey(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重命名 Passkey</DialogTitle>
            <DialogDescription>修改此 Passkey 在控制台列表中的备注名称。</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleConfirmRename} className="flex flex-col gap-4">
            <Field>
              <FieldLabel htmlFor="rename-passkey-input">名称</FieldLabel>
              <Input
                id="rename-passkey-input"
                value={renameDraft}
                maxLength={64}
                required
                onChange={(ev) => setRenameDraft(ev.target.value)}
              />
            </Field>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setRenamingPasskey(null)}>
                取消
              </Button>
              <Button type="submit">保存</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete Passkey Confirmation */}
      <AlertDialog
        open={Boolean(deletingPasskey)}
        onOpenChange={(open) => {
          if (!open) setDeletingPasskey(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除 Passkey「{deletingPasskey?.name}」？</AlertDialogTitle>
            <AlertDialogDescription>
              删除后无法再使用该凭据登录，通过它建立的浏览器会话也会立即失效，并向浏览器密码管理器发送同步清理通知。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={handleConfirmDeletePasskey}>
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Revoke Other Sessions Confirmation */}
      <AlertDialog open={confirmRevokeOthers} onOpenChange={setConfirmRevokeOthers}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>吊销所有其他设备的登录态？</AlertDialogTitle>
            <AlertDialogDescription>
              除当前浏览器以外的所有已登录设备都将立即失效，CLI 与 API Token 不受影响。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={handleRevokeOtherSessions}>确认吊销</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
